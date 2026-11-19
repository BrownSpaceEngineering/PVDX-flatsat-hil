"""IEEE 802.15.4g MR-FSK, as the flatsat's AT86RF215 sends and receives it, in software for the HackRF ground station.

The PHY settings come from the satellite's UHF config (src/tasks/uhf_radio/uhf_radio_task.c, AT86_MR_FSK) and agree
with BrownSpaceEngineering/gr-ccsds (USLP/GNURadioFiles/Files/Two_GFSK.py): 2-FSK, 50 ksym/s, modulation index 0.5
(+-12.5 kHz), Gaussian BT 0.5, 8 preamble octets, 16-bit FCS appended by the chip. Frame layout (AT86RF215 datasheet,
6.10.3.1 and Figure 6-19):

    preamble (01010101 x 8) | SFD (16 bits) | PHR (16 bits) | PSDU (length octets, the last 2 the FCS)

Uncoded frames only. The satellite's current config sends coded frames (rate-1/2 RSC FEC, SFD1 0x72F6), which this
modem can't decode until the RSC encoder from IEEE 802.15.4g is added (frames with SFD1 are counted, not decoded).

Details the datasheet leaves to IEEE 802.15.4g, taken from the 802.15.4 conventions and to be confirmed against the
real chip (each is a constant below, so a correction is one line):
- PSDU octets go least significant bit first.
- The SFD register value goes least significant bit first (0x7209 is then the 802.15.4g uncoded SFD bit string).
- The PHR goes MS, R1, R0, FCS type, DW, then the length L10 down to L0.
- The 16-bit FCS is the 802.15.4 ITU-T CRC: polynomial x^16 + x^12 + x^5 + 1, register preset to 0, bits processed
  least significant first, appended low octet first.
The ground also checks the USLP frame's own CRC, so a wrong guess here shows up as frames with a bad FCS but a good
USLP CRC rather than as silence.
"""

import numpy as np

SYMBOL_RATE = 50_000
MODULATION_INDEX = 0.5
BT = 0.5
PREAMBLE_OCTETS = 8
SFD_UNCODED = 0x7209  # BBCn_FSKSFD0 (FSKPHRTX.SFD = 0)
SFD_CODED = 0x72F6  # BBCn_FSKSFD1, sent with FEC
SAMPLE_RATE = 2_000_000
SPS = SAMPLE_RATE // SYMBOL_RATE  # 40 samples per symbol
RX_SPS = 4  # The receiver decimates to this many samples per symbol
GAUSS_SPAN = 4  # Symbols the Gaussian pulse spans
SYNC_THRESHOLD = 0.7
CHANNEL_CUTOFF_HZ = 60_000  # Carson bandwidth (+-37.5 kHz) plus room for the satellite's untrimmed crystal  # Normalised correlation with the preamble end and SFD that counts as a frame start
MAX_PSDU = 2047

PSDU_LSB_FIRST = True
SFD_LSB_FIRST = True


def crc16(data):
    """802.15.4 FCS-16 (ITU-T x^16 + x^12 + x^5 + 1, preset 0, reflected)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def octet_bits(data, lsb_first=PSDU_LSB_FIRST):
    bits = np.unpackbits(np.frombuffer(bytes(data), dtype=np.uint8)[:, None], axis=1)  # MSB first per row
    return (bits[:, ::-1] if lsb_first else bits).reshape(-1)


def bits_octets(bits, lsb_first=PSDU_LSB_FIRST):
    rows = np.asarray(bits, dtype=np.uint8).reshape(-1, 8)
    return np.packbits(rows[:, ::-1] if lsb_first else rows, axis=1).reshape(-1).tobytes()


def sfd_bits(value=SFD_UNCODED):
    order = range(16) if SFD_LSB_FIRST else range(15, -1, -1)
    return np.array([(value >> i) & 1 for i in order], dtype=np.uint8)


def phr_bits(length, fcs16=True, whitened=False):
    """MS, R1, R0, FCS type, DW, L10..L0."""
    if not 0 < length <= MAX_PSDU:
        raise ValueError(f"PSDU length {length}")
    head = [0, 0, 0, 1 if fcs16 else 0, 1 if whitened else 0]
    return np.array(head + [(length >> i) & 1 for i in range(10, -1, -1)], dtype=np.uint8)


def parse_phr(bits):
    """(length, fcs16, whitened) from the 16 PHR bits."""
    bits = [int(b) for b in bits]
    length = 0
    for b in bits[5:16]:
        length = (length << 1) | b
    return length, bool(bits[3]), bool(bits[4])


def frame_bits(payload):
    """Every bit of the PPDU for a payload (the PSDU without its FCS, which is appended here as the chip does)."""
    fcs = crc16(payload)
    psdu = bytes(payload) + bytes([fcs & 0xFF, fcs >> 8])
    preamble = np.tile(np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.uint8), PREAMBLE_OCTETS)
    return np.concatenate([preamble, sfd_bits(), phr_bits(len(psdu)), octet_bits(psdu)])


def _lowpass(cutoff_hz, rate, taps):
    """Windowed-sinc low-pass FIR."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.complex64)


def _gaussian_taps(sps):
    t = np.arange(-GAUSS_SPAN * sps / 2, GAUSS_SPAN * sps / 2 + 1) / sps
    alpha = np.sqrt(np.log(2) / 2) / BT
    taps = np.sqrt(np.pi) / alpha * np.exp(-((np.pi * t / alpha) ** 2))
    return taps / taps.sum()


def modulate(bits, sps=SPS, amplitude=0.9):
    """Complex baseband GFSK for the bits (1 = +deviation), with a symbol of silence-free lead-in and tail."""
    symbols = np.repeat(2.0 * np.asarray(bits, dtype=float) - 1.0, sps)
    freq = np.convolve(symbols, _gaussian_taps(sps), mode="full")
    phase = np.cumsum(freq) * np.pi * MODULATION_INDEX / sps
    return amplitude * np.exp(1j * phase)


def to_hackrf(iq):
    """Interleaved signed 8-bit I/Q, as hackrf_transfer -t expects."""
    out = np.empty(2 * len(iq), dtype=np.int8)
    out[0::2] = np.clip(np.round(iq.real * 127), -127, 127)
    out[1::2] = np.clip(np.round(iq.imag * 127), -127, 127)
    return out.tobytes()


def from_hackrf(raw):
    data = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int8).astype(np.float32)
    return (data[0::2] + 1j * data[1::2]) / 128.0


class Received:
    """One frame the demodulator found."""

    def __init__(self, psdu, fcs_ok, coded, sample, power_db, freq_offset_hz):
        self.psdu = psdu  # Including the 2 FCS octets
        self.payload = psdu[:-2]
        self.fcs_ok = fcs_ok
        self.coded = coded  # SFD1: FEC-coded, not decoded (payload is empty)
        self.sample = sample  # Position of the SFD's first sample in the input stream
        self.power_db = power_db
        self.freq_offset_hz = freq_offset_hz

    def __repr__(self):
        return f"Received({len(self.payload)} bytes, fcs_ok={self.fcs_ok}, coded={self.coded}, {self.power_db:.1f} dB)"


class Demodulator:
    """Finds frames in a stream of complex samples at SAMPLE_RATE. feed() takes any amount and returns the frames that
    ended in it; samples are kept across calls, so a frame may straddle them."""

    def __init__(self):
        self.decim = SPS // RX_SPS
        self.prev = np.complex64(1)
        self.lpf = _lowpass(CHANNEL_CUTOFF_HZ, SAMPLE_RATE, 129)
        self.iq_tail = np.zeros(len(self.lpf) - 1, dtype=np.complex64)
        self.decim_phase = 0
        self.disc_tail = np.zeros(0, dtype=np.float32)  # Discriminator output not yet averaged
        self.power_tail = np.zeros(0, dtype=np.float32)
        self.soft = np.zeros(0, dtype=np.float32)  # Discriminator output at RX_SPS, not yet searched
        self.soft_start = 0  # Stream position (in RX_SPS samples) of soft[0]
        sync = np.concatenate([np.tile([0, 1], 8), sfd_bits(SFD_UNCODED)])
        coded = np.concatenate([np.tile([0, 1], 8), sfd_bits(SFD_CODED)])
        self.refs = {False: self._reference(sync), True: self._reference(coded)}
        self.sync_symbols = len(sync)
        self.raw_power = []

    @staticmethod
    def _reference(bits):
        ref = np.repeat(2.0 * np.asarray(bits, dtype=float) - 1.0, RX_SPS)
        return (ref - ref.mean()) / np.linalg.norm(ref - ref.mean())

    def _discriminate(self, iq):
        """Channel filter and decimate to RX_SPS samples per symbol, then the instantaneous frequency averaged over one
        symbol (a matched filter for the symbols, which the Gaussian pulse only rounds off). Filtering before the
        discriminator keeps the out-of-band noise of the HackRF's 2 MHz out of it. Returns that and each sample's power."""
        x = np.concatenate([self.iq_tail, iq])
        y = np.convolve(x, self.lpf, mode="valid")  # y[i] is filtered up to iq[i]
        self.iq_tail = x[len(x) - (len(self.lpf) - 1):]
        idx = np.arange(self.decim_phase, len(y), self.decim)
        self.decim_phase = int(idx[-1] + self.decim - len(y)) if len(idx) else self.decim_phase - len(y)
        z = y[idx]
        if not len(z):
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        joined = np.concatenate([[self.prev], z])
        self.prev = z[-1]
        disc = np.concatenate([self.disc_tail, np.angle(joined[1:] * np.conj(joined[:-1]))])
        power = np.concatenate([self.power_tail, np.abs(z) ** 2])
        sums = np.concatenate([[0.0], np.cumsum(disc, dtype=np.float64)])
        averaged = ((sums[RX_SPS:] - sums[:-RX_SPS]) / RX_SPS).astype(np.float32)  # averaged[i] covers disc[i : i + RX_SPS]
        self.disc_tail = disc[len(averaged):]
        self.power_tail = power[len(averaged):]
        return averaged, power[:len(averaged)].astype(np.float32)

    def feed(self, iq):
        iq = np.asarray(iq, dtype=np.complex64)
        if not len(iq):
            return []
        soft, power = self._discriminate(iq)
        self.soft = np.concatenate([self.soft, soft])
        self.raw_power.append(power)
        power_all = np.concatenate(self.raw_power)
        self.raw_power = [power_all]
        frames = []
        n_ref = len(self.refs[False])
        pos = 0
        while len(self.soft) - pos >= n_ref + 32 * RX_SPS:
            found = self._search(pos)
            if found is None:
                pos = len(self.soft) - n_ref - 32 * RX_SPS  # Keep a tail that a sync could still start in
                break
            start, coded = found
            result = self._decode(start + n_ref, coded, power_all)
            if result is None:  # The rest of the frame hasn't arrived yet
                pos = start
                break
            frame, end = result
            if frame is not None:
                frames.append(frame)
            pos = end
        pos = max(0, min(pos, len(self.soft)))
        self.soft = self.soft[pos:]
        self.raw_power = [power_all[pos:]] if len(power_all) > pos else []
        self.soft_start += pos
        return frames

    def _search(self, pos):
        """Index (in soft) where the sync reference starts, at the first strong correlation after pos."""
        n_ref = len(self.refs[False])
        window = self.soft[pos:]
        if len(window) < n_ref:
            return None
        frames = np.lib.stride_tricks.sliding_window_view(window, n_ref)
        centred = frames - frames.mean(axis=1, keepdims=True)
        energy = np.linalg.norm(centred, axis=1) + 1e-9
        best = None
        for coded, ref in self.refs.items():
            score = centred @ ref / energy
            hits = np.nonzero(score > SYNC_THRESHOLD)[0]
            if len(hits):
                first = hits[0]
                peak = first + int(np.argmax(score[first:first + RX_SPS * 2]))
                if best is None or peak < best[0]:
                    best = (peak, coded)
        if best is None:
            return None
        return pos + best[0], best[1]

    def _bits(self, start, count, dc, phase):
        """count hard bits from soft[start:], one per symbol, at the sample phase where symbols are strongest."""
        idx = start + phase + RX_SPS * np.arange(count)
        if idx[-1] >= len(self.soft):
            return None
        return (self.soft[idx] - dc > 0).astype(np.uint8)

    def _decode(self, start, coded, power):
        """(frame or None, end index) for a frame whose PHR starts at soft[start], or None if more input is needed."""
        n_ref = len(self.refs[False])
        sync = self.soft[start - n_ref:start]
        preamble = sync[: 16 * RX_SPS]
        dc = float(preamble.mean())  # The preamble is balanced (0101...): its mean is the frequency offset
        # Each soft sample averages one symbol's worth of discriminator output starting at it, so the sample phase whose
        # window lines up with the symbols has the largest magnitude
        phase = int(np.argmax([np.abs(sync[r::RX_SPS] - dc).mean() for r in range(RX_SPS)]))
        offset_hz = dc * (SAMPLE_RATE / self.decim) / (2 * np.pi)
        sfd_at = (self.soft_start + start - 16 * RX_SPS) * self.decim  # Approximate: ignores the filter delays
        level = float(10 * np.log10(power[start - n_ref:start].mean() + 1e-12))
        if coded:
            # The PHR is coded too, so not even the length is readable: skip past the sync and carry on searching
            return Received(b"", False, True, sfd_at, level, offset_hz), start
        phr = self._bits(start, 16, dc, phase)
        if phr is None:
            return None
        length, _, _ = parse_phr(phr)
        if not 3 <= length <= MAX_PSDU:
            return (None, start)  # A false sync
        psdu_bits = self._bits(start + 16 * RX_SPS, 8 * length, dc, phase)
        if psdu_bits is None:
            return None
        psdu = bits_octets(psdu_bits)
        fcs_ok = crc16(psdu[:-2]) == psdu[-2] | (psdu[-1] << 8)
        # A bad frame may have been a false sync: carry on searching just after its sync, so a real frame inside it
        # isn't skipped
        end = start + (16 + 8 * length) * RX_SPS if fcs_ok else start
        return Received(psdu, fcs_ok, False, sfd_at, level, offset_hz), end
