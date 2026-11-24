"""IEEE 802.15.4g MR-FSK, as the flatsat's AT86RF215 sends and receives it, in software for the HackRF ground station.

The PHY settings come from the satellite's UHF config (src/tasks/uhf_radio/uhf_radio_task.c, AT86_MR_FSK) and agree
with BrownSpaceEngineering/gr-ccsds (USLP/GNURadioFiles/Files/Two_GFSK.py): 2-FSK, 50 ksym/s, modulation index 0.5
(+-12.5 kHz), Gaussian BT 0.5, 8 preamble octets, 16-bit FCS appended by the chip. Frame layout (AT86RF215 datasheet,
6.10.3.1 and Figure 6-19):

    preamble (01010101 x 8) | SFD (16 bits) | PHR (16 bits) | PSDU (length octets, the last 2 the FCS)

Frames are uncoded (SFD0) or FEC-coded (SFD1), as the satellite's FSKC4.CSFD0/CSFD1 say; the satellite sends coded
frames and receives either (dual SFD sense). Coding follows IEEE 802.15.4g-2012 18.1.2.4-18.1.2.5, as implemented in
dudmuck/gr-ieee802154g: the PHR, PSDU and FCS bits, then 3 tail bits and pad bits, go through a rate-1/2, constraint
length 4 convolutional code (generators 1 + D^2 + D^3 and 1 + D + D^2 + D^3), optionally followed by the 32-bit block
interleaver; PN9 whitening (PHR bit DW) covers the PSDU only and comes before coding. The receiver decodes with a soft
Viterbi decoder.

The satellite uses the recursive systematic code (RSC, FSKC3.FECS = 1), which that source doesn't implement: which
generator is the feedback and which output goes first is a guess (RSC below, confirmed=False) until fit_rsc.py has
matched it against beacons captured from the satellite. Until then a coded frame is read from its systematic bits
(every other coded bit is the data itself), checked by its FCS, and reported as fec="systematic".

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

from dataclasses import dataclass

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


# ---------- FEC (IEEE 802.15.4g-2012 18.1.2.4-18.1.2.5) ----------

SYS = -1  # An output that is the input bit itself


def _parity(x):
    return bin(x).count("1") & 1


@dataclass(frozen=True)
class Code:
    """A rate-1/2, constraint length 4 convolutional code. Polynomials are in D, bit k the coefficient of D^k. The
    shift register holds a_k: the input bit for a feedforward code (feedback 0), the input plus the feedback parity of
    the register for a recursive one. Each input bit gives the two outputs in transmit order: SYS (the input bit) or
    the parity of a polynomial over a_k, a_k-1, a_k-2, a_k-3."""

    name: str
    feedback: int
    outputs: tuple
    confirmed: bool = True  # Matched against the real chip

    def step(self, state, u):
        """(next state, (out0, out1)). state holds a_k-1 (bit 0), a_k-2, a_k-3."""
        a = u ^ _parity((self.feedback >> 1) & state) if self.feedback else u
        w = a | (state << 1)
        out = tuple(u if g == SYS else _parity(g & w) for g in self.outputs)
        return ((state << 1) | a) & 7, out

    def terminating_input(self, state):
        """The input bit that shifts a 0 into the register, so 3 of them in a row flush it to state 0."""
        return _parity((self.feedback >> 1) & state) if self.feedback else 0

    def encode(self, bits, state=0):
        out = np.empty(2 * len(bits), dtype=np.uint8)
        for i, u in enumerate(bits):
            state, (out[2 * i], out[2 * i + 1]) = self.step(state, int(u))
        return out

    def trellis(self):
        """For each next state, its two (previous state, input) and the outputs as +-1, for the Viterbi decoder."""
        cached = _TRELLIS.get(self)
        if cached is None:
            prev = [[] for _ in range(8)]
            for s in range(8):
                for u in (0, 1):
                    ns, out = self.step(s, u)
                    prev[ns].append((s, u, [2 * o - 1 for o in out]))
            if any(len(p) != 2 for p in prev):
                raise ValueError(f"{self.name} isn't a valid rate-1/2 trellis")
            ps = np.array([[p[0] for p in row] for row in prev])
            pu = np.array([[p[1] for p in row] for row in prev], dtype=np.uint8)
            signs = np.array([[p[2] for p in row] for row in prev], dtype=np.float64)
            cached = _TRELLIS[self] = (ps, pu, signs)
        return cached


_TRELLIS = {}

# Non-recursive non-systematic code (FSKC3.FECS = 0): Figure 124 of 802.15.4g-2012, as gr-ieee802154g encodes it
NRNSC = Code("NRNSC", 0, (0b1101, 0b1111))
# Recursive systematic code (FSKC3.FECS = 1), which the satellite uses: the same generators with the first as the
# feedback, systematic bit first. NOT CONFIRMED: soak/tools/fit_rsc.py finds the real one from captured beacons
RSC = Code("RSC", 0b1101, (SYS, 0b1111), confirmed=False)
CODES = {"NRNSC": NRNSC, "RSC": RSC}

TAIL_BITS = 3
PAD_ODD = (0, 1, 0, 1, 1)  # When the PHR and PSDU are an odd number of octets
PAD_EVEN = (0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 1, 1)
# The interleaver permutes the 16 coded bit pairs of each 32 coded bits: pair k comes from pair INTERLEAVE[k]. It is its
# own inverse. From gr-ieee802154g; the satellite doesn't interleave (FSKC2.FECIE = 0), so this is unconfirmed
INTERLEAVE = (15, 11, 7, 3, 14, 10, 6, 2, 13, 9, 5, 1, 12, 8, 4, 0)


def pad_bits(psdu_octets):
    return PAD_ODD if (psdu_octets + 2) % 2 else PAD_EVEN


def coded_info_bits(psdu_octets):
    """Input bits to the encoder for a PSDU of this many octets (including the FCS): PHR, PSDU, tail and pad."""
    return 16 + 8 * psdu_octets + TAIL_BITS + len(pad_bits(psdu_octets))


def interleave(bits):
    """Applies (or undoes: it's an involution) the interleaver to whole 32-bit blocks; a partial last block is left."""
    bits = np.asarray(bits)
    out = bits.copy()
    whole = len(bits) // 32 * 32
    blocks = bits[:whole].reshape(-1, 16, 2)
    out[:whole] = blocks[:, list(INTERLEAVE), :].reshape(-1)
    return out


def pn9(n):
    """The first n bits of the PN9 whitening sequence (x^9 + x^5 + 1, seeded with all ones)."""
    lfsr = 0x1FF
    out = np.empty(n, dtype=np.uint8)
    for i in range(n):
        out[i] = lfsr & 1
        feedback = (lfsr & 1) ^ ((lfsr >> 5) & 1)
        lfsr = (lfsr >> 1) | (feedback << 8)
    return out


def encode_frame(phr, psdu_bits, code):
    """The coded bits of a frame: PHR and PSDU bits, then the tail that returns the encoder to state 0, then the pad."""
    state = 0
    out = []
    for u in list(phr) + list(psdu_bits):
        state, pair = code.step(state, int(u))
        out += pair
    for _ in range(TAIL_BITS):
        u = code.terminating_input(state)
        state, pair = code.step(state, u)
        out += pair
    for u in pad_bits(len(psdu_bits) // 8):
        state, pair = code.step(state, u)
        out += pair
    return np.array(out, dtype=np.uint8)


def viterbi(code, soft, start_state=0):
    """Most likely input bits for soft coded bits (positive means 1), starting from start_state and ending anywhere."""
    ps, pu, signs = code.trellis()
    n = len(soft) // 2
    pairs = np.asarray(soft[:2 * n], dtype=np.float64).reshape(n, 2)
    metric = np.full(8, -1e30)
    metric[start_state] = 0.0
    decisions = np.empty((n, 8), dtype=np.uint8)
    rows = np.arange(8)
    branch = signs @ pairs.T  # (8, 2, n): each next state's two branch metrics, for every step
    for k in range(n):
        cand = metric[ps] + branch[:, :, k]
        choice = (cand[:, 1] > cand[:, 0]).astype(np.uint8)
        decisions[k] = choice
        metric = cand[rows, choice]
        metric -= metric.max()
    state = int(np.argmax(metric))
    bits = np.empty(n, dtype=np.uint8)
    for k in range(n - 1, -1, -1):
        c = decisions[k, state]
        bits[k] = pu[state, c]
        state = int(ps[state, c])
    return bits


# What the satellite's baseband settings (ccsds.phy_config()) must be for this modem to talk to it. Each is the value
# as at86_pvdx.c writes it ("0" for one it leaves out)
EXPECTED_PHY = {
    "pt": "AT86RF215_BB_MRFSK",
    "fcst": "AT86RF215_FCS_16",
    "fsk.mord": "AT86RF215_2FSK",
    "fsk.midx": "AT86RF215_MIDX_1",  # Modulation index 0.5
    "fsk.bt": "AT86RF215_FSK_BT_05",
    "fsk.srate": "AT86RF215_FSK_SRATE_50",
    "fsk.preamble_length": str(PREAMBLE_OCTETS),
    "fsk.sfd32": "0",  # Dual SFD sense: the satellite receives both uncoded and coded frames
    "fsk.csfd0": "AT86RF215_SFD_UNCODED_IEEE",
    "fsk.csfd1": "AT86RF215_SFD_CODED_IEEE",
    "fsk.dw": "0",  # The satellite doesn't whiten (pn9() is unconfirmed)
}


def check_phy(settings):
    """The (code, interleaved, tx_coded) the satellite's settings imply, after checking that they're what this modem
    speaks. Raises ValueError naming every difference."""
    get = lambda key: settings.get(key, "0").strip()
    problems = [f"{key} is {get(key)}, expected {want}" for key, want in EXPECTED_PHY.items() if get(key) != want]
    for key, want in (("fsk.sfd0", SFD_UNCODED), ("fsk.sfd1", SFD_CODED)):
        if int(get(key), 0) != want:
            problems.append(f"{key} is {get(key)}, expected 0x{want:04X}")
    fecs = get("fsk.fecs")
    code = {"AT86RF215_FSK_FEC_RSC": RSC, "AT86RF215_FSK_FEC_NRNSC": NRNSC, "0": NRNSC}.get(fecs)
    if code is None:
        problems.append(f"fsk.fecs is {fecs}, which this modem doesn't know")
    if problems:
        raise ValueError(f"the satellite's PHY ({settings.get('config')}) isn't what mrfsk.py speaks: " + "; ".join(problems))
    # FSKPHRTX.SFD picks the SFD the satellite sends with, and CSFD1 makes SFD1 coded
    return code, get("fsk.fecie") == "1", get("fsk.sfd") == "1"


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


def frame_bits(payload, code=None, interleaved=False, whitened=False):
    """Every bit of the PPDU for a payload (the PSDU without its FCS, which is appended here as the chip does):
    uncoded on SFD0, or coded with `code` on SFD1."""
    fcs = crc16(payload)
    psdu = bytes(payload) + bytes([fcs & 0xFF, fcs >> 8])
    preamble = np.tile(np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.uint8), PREAMBLE_OCTETS)
    phr = phr_bits(len(psdu), whitened=whitened)
    data = octet_bits(psdu)
    if whitened:
        data = data ^ pn9(len(data))
    if code is None:
        return np.concatenate([preamble, sfd_bits(SFD_UNCODED), phr, data])
    coded = encode_frame(phr, data, code)
    if interleaved:
        coded = interleave(coded)
    return np.concatenate([preamble, sfd_bits(SFD_CODED), coded])


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

    def __init__(self, psdu, fcs_ok, coded, sample, power_db, freq_offset_hz, fec="", corrected_bits=None,
                 coded_bits=None):
        self.psdu = psdu  # Including the 2 FCS octets
        self.payload = psdu[:-2]
        self.fcs_ok = fcs_ok
        self.coded = coded  # Sent with SFD1 (FEC-coded)
        self.sample = sample  # Position of the SFD's first sample in the input stream
        self.power_db = power_db
        self.freq_offset_hz = freq_offset_hz
        # How a coded frame was decoded: "viterbi", "systematic:<0|1>" (the data read from the systematic bits at that
        # position of each pair, the code being unconfirmed) or "failed"; "" for an uncoded frame
        self.fec = fec
        self.corrected_bits = corrected_bits  # Coded bits the Viterbi decoder corrected (None if not decoded by it)
        self.coded_bits = coded_bits  # Hard decisions on every coded bit (deinterleaved), for fit_rsc.py

    def __repr__(self):
        return (f"Received({len(self.payload)} bytes, fcs_ok={self.fcs_ok}, coded={self.coded}, fec={self.fec!r}, "
                f"{self.power_db:.1f} dB)")


class Demodulator:
    """Finds frames in a stream of complex samples at SAMPLE_RATE. feed() takes any amount and returns the frames that
    ended in it; samples are kept across calls, so a frame may straddle them."""

    def __init__(self, code=RSC, interleaved=False):
        self.code = code  # The code coded frames use
        self.interleaved = interleaved
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

    def _soft(self, start, count, dc, phase):
        """count soft symbols from soft[start:] (positive means 1), or None if they haven't all arrived."""
        idx = start + phase + RX_SPS * np.arange(count)
        if idx[-1] >= len(self.soft):
            return None
        return (self.soft[idx] - dc).astype(np.float64)

    def _decoders(self):
        """(name, function from soft coded bits to input bits) to try on a coded frame, in order."""
        decoders = [("viterbi", lambda soft: viterbi(self.code, soft))]
        if not self.code.confirmed and SYS in self.code.outputs:
            # The data is every other coded bit, whatever the parity is; which of each pair isn't known either
            decoders += [(f"systematic:{p}", lambda soft, p=p: (soft[p::2] > 0).astype(np.uint8)) for p in (0, 1)]
        return decoders

    def _decode_coded(self, start, dc, phase):
        """(frame or None, end) for a coded frame whose PHR starts at soft[start], or None if more input is needed."""
        head = self._soft(start, 96, dc, phase)  # 48 input bits: the PHR and some lookahead for the decoder
        if head is None:
            return None
        if self.interleaved:
            head = interleave(head)
        best = None
        waiting = False
        for name, decode in self._decoders():
            length, _, whitened = parse_phr(decode(head)[:16])
            if not 3 <= length <= MAX_PSDU:
                continue
            n_coded = 2 * coded_info_bits(length)
            soft = self._soft(start, n_coded, dc, phase)
            if soft is None:
                waiting = True
                continue
            if self.interleaved:
                soft = interleave(soft)
            info = decode(soft)
            data = info[16:16 + 8 * length]
            if whitened:
                data = data ^ pn9(len(data))
            psdu = bits_octets(data)
            fcs_ok = crc16(psdu[:-2]) == psdu[-2] | (psdu[-1] << 8)
            hard = (soft > 0).astype(np.uint8)
            corrected = None
            if name == "viterbi":
                corrected = int(np.count_nonzero(self.code.encode(info) != hard))
            result = (psdu, fcs_ok, name, corrected, hard, start + n_coded * RX_SPS)
            if fcs_ok:
                return result
            best = best or result
        if waiting:
            return None
        return best or "false"

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
            result = self._decode_coded(start, dc, phase)
            if result is None:
                return None
            if result == "false":
                return (None, start)  # No decoder read a plausible length: a false sync
            psdu, fcs_ok, fec, corrected, hard, end = result
            frame = Received(psdu, fcs_ok, True, sfd_at, level, offset_hz, fec=fec if fcs_ok else "failed",
                             corrected_bits=corrected, coded_bits=hard)
            # As for an uncoded frame, a bad one may have been a false sync
            return frame, end if fcs_ok else start
        phr = self._bits(start, 16, dc, phase)
        if phr is None:
            return None
        length, _, whitened = parse_phr(phr)
        if not 3 <= length <= MAX_PSDU:
            return (None, start)  # A false sync
        psdu_bits = self._bits(start + 16 * RX_SPS, 8 * length, dc, phase)
        if psdu_bits is None:
            return None
        if whitened:
            psdu_bits = psdu_bits ^ pn9(len(psdu_bits))
        psdu = bits_octets(psdu_bits)
        fcs_ok = crc16(psdu[:-2]) == psdu[-2] | (psdu[-1] << 8)
        # A bad frame may have been a false sync: carry on searching just after its sync, so a real frame inside it
        # isn't skipped
        end = start + (16 + 8 * length) * RX_SPS if fcs_ok else start
        return Received(psdu, fcs_ok, False, sfd_at, level, offset_hz), end
