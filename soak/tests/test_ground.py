"""The ground station without hardware: framing, the coded modem, telemetry decoding, the CFDP uplink, PUSP, and the
soak's radio, uplink and telemetry monitors. Needs PVDXos's radio code: PVDX_ROOT (default: the PVDX checkout this repo
sits in) must have the UHF radio task.

The link tests run the ground against FakeSatellite, a transcription of the satellite's PUSP handlers
(src/tasks/uhf_radio/uhf_process.c) and of its CFDP receive side (src/tasks/cfdp/cfdp_task.c), whose PDUs are built
here in the satellite's format and read by the ground with PVDXos's own parsers. They check the ground's half against
that reading of the satellite code; the real check is on the bench.
"""

import ctypes
import os
import random
import struct
import sys
import threading
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "hil", "lib"))

import paths  # noqa: E402

if not os.path.exists(os.path.join(paths.PVDX, "src", "ccsds", "radio_routing.h")):
    pytest.skip("PVDX_ROOT has no UHF radio code (src/ccsds/radio_routing.h)", allow_module_level=True)

import ccsds  # noqa: E402
import cfdp  # noqa: E402
import ground  # noqa: E402
import hackrf  # noqa: E402
import mrfsk  # noqa: E402
import telemetry  # noqa: E402
import uplink  # noqa: E402

C = ccsds.constants()
VC = ccsds.routes()


# ---------- Framing ----------


def test_framing_round_trip_and_rejection():
    data = bytes([C["SYN"], 0])
    frame = ccsds.wrap(VC["RADIO_STREAM_PUSP"], C["CCSDS_APID_PUSP"], data, 5, timestamp=1_800_000_000)
    parsed = ccsds.unwrap(frame)
    assert (parsed.apid, parsed.sequence_count, parsed.data) == (C["CCSDS_APID_PUSP"], 5, data)
    assert parsed.callsign == ccsds.ground_callsign()
    assert parsed.timestamp == 1_800_000_000  # The satellite sets its clock from this
    with pytest.raises(ccsds.FramingError):
        ccsds.unwrap(frame[:-1] + bytes([frame[-1] ^ 1]))


def test_vc_frame_counts_advance_per_vc():
    vc = VC["RADIO_STREAM_UPLINK_CMD"]
    counts = [ccsds.unwrap(ccsds.wrap(vc, C["CCSDS_APID_CFDP"], b"x", 0)).vc_frame_count for _ in range(3)]
    assert (counts[1] - counts[0]) % 256 == 1 and (counts[2] - counts[1]) % 256 == 1


def test_constants_come_from_the_headers():
    pusp = ccsds.enum("src/tasks/uhf_radio/pusp.h", "pusp_directive")
    assert list(pusp)[:6] == list(ground.DIRECTIVES)
    commands = ccsds.enum("src/tasks/cfdp/uplink_parse.h", "uplink_cmd_type_e")
    assert {"SET_UNIX_TIME", "SET_ADCS_OP_MODE", "ENABLE_DISABLE_DEVICE"} <= set(commands)
    assert C["CAMERA_ID"] == ccsds.enum("src/globals.h", "device_id_t")["CAMERA_ID"]


# ---------- Modem ----------


def test_crc16_matches_the_802154_check_value():
    assert mrfsk.crc16(b"123456789") == 0x2189  # CRC-16/KERMIT, the 802.15.4 FCS


def _channel(iq, rng, snr_db, offset_hz, lead):
    n = np.arange(len(iq))
    iq = iq * np.exp(2j * np.pi * offset_hz * n / mrfsk.SAMPLE_RATE + 0.4j)
    sigma = 10 ** (-snr_db / 20) / np.sqrt(2)
    noise = lambda k: (rng.normal(size=k) + 1j * rng.normal(size=k)) * sigma  # noqa: E731
    return np.concatenate([noise(lead), iq + noise(len(iq)), noise(lead)])


def _demodulate(iq, demod, chunk=33333):
    out = []
    for i in range(0, len(iq), chunk):  # Frames straddle the reads
        out += demod.feed(iq[i:i + chunk])
    return out


@pytest.mark.parametrize("snr_db, offset_hz", [(20, 0), (6, 8000), (3, -15000)])
def test_modem_recovers_uncoded_frames(snr_db, offset_hz):
    rng = np.random.default_rng(snr_db)
    payloads = [rng.integers(0, 256, 300).astype(np.uint8).tobytes(), b"short"]
    iq = np.concatenate([_channel(mrfsk.modulate(mrfsk.frame_bits(p)), rng, snr_db, offset_hz, 7000) for p in payloads])
    out = _demodulate(iq, mrfsk.Demodulator())
    assert [f.payload for f in out if f.fcs_ok] == payloads
    assert all(abs(f.freq_offset_hz - offset_hz) < 2000 for f in out)


@pytest.mark.parametrize("code, interleaved", [(mrfsk.RSC, False), (mrfsk.NRNSC, False), (mrfsk.NRNSC, True)])
@pytest.mark.parametrize("snr_db, offset_hz", [(20, 0), (3, -12000)])
def test_modem_decodes_coded_frames(code, interleaved, snr_db, offset_hz):
    rng = np.random.default_rng(snr_db + 100)
    payloads = [rng.integers(0, 256, 540).astype(np.uint8).tobytes(), b"abc", b"four"]  # Odd and even pads
    iq = np.concatenate([_channel(mrfsk.modulate(mrfsk.frame_bits(p, code, interleaved)), rng, snr_db, offset_hz, 7000)
                         for p in payloads])
    out = _demodulate(iq, mrfsk.Demodulator(code=code, interleaved=interleaved))
    assert [f.payload for f in out if f.fcs_ok] == payloads
    assert all(f.coded and f.fec == "viterbi" for f in out)


def test_the_viterbi_decoder_corrects_what_uncoded_frames_lose():
    """At -2.5 dB (per sample, before the channel filter) uncoded frames fail; coded ones are corrected."""
    rng = np.random.default_rng(5)
    payload = rng.integers(0, 256, 200).astype(np.uint8).tobytes()
    coded_ok, uncoded_ok, corrected = 0, 0, []
    for _ in range(4):
        for code in (None, mrfsk.RSC):
            iq = _channel(mrfsk.modulate(mrfsk.frame_bits(payload, code)), rng, -2.5, 0, 7000)
            frames = [f for f in mrfsk.Demodulator(code=mrfsk.RSC).feed(iq) if f.fcs_ok and f.payload == payload]
            if code is None:
                uncoded_ok += bool(frames)
            else:
                coded_ok += bool(frames)
                corrected += [f.corrected_bits for f in frames]
    assert coded_ok == 4 and uncoded_ok <= 1
    assert min(corrected) > 0


def test_an_unconfirmed_code_falls_back_to_the_systematic_bits():
    """The satellite's RSC differs from mrfsk.RSC's guess (other feedback, systematic bit second): the frame is still
    read, from its systematic bits, and its coded bits are kept for fit_rsc.py."""
    real = mrfsk.Code("real", 0b1111, (0b1101, mrfsk.SYS))
    payload = bytes(range(200))
    iq = np.concatenate([np.zeros(4000), mrfsk.modulate(mrfsk.frame_bits(payload, real)), np.zeros(4000)])
    out = mrfsk.Demodulator(code=mrfsk.RSC).feed(iq)
    assert [f.payload for f in out] == [payload]
    assert out[0].fec == "systematic:1" and out[0].corrected_bits is None
    assert len(out[0].coded_bits) == 2 * mrfsk.coded_info_bits(len(payload) + 2)


def test_the_tail_returns_the_encoder_to_zero():
    for code in (mrfsk.RSC, mrfsk.NRNSC):
        state = 0
        for u in np.random.default_rng(1).integers(0, 2, 100):
            state, _ = code.step(state, int(u))
        for _ in range(mrfsk.TAIL_BITS):
            state, _ = code.step(state, code.terminating_input(state))
        assert state == 0


def test_whitening_round_trips():
    bits = mrfsk.octet_bits(b"whitened payload")
    assert not np.array_equal(bits ^ mrfsk.pn9(len(bits)), bits)
    payload = b"whitened payload"
    iq = np.concatenate([np.zeros(4000), mrfsk.modulate(mrfsk.frame_bits(payload, mrfsk.NRNSC, whitened=True)),
                         np.zeros(4000)])
    assert [f.payload for f in mrfsk.Demodulator(code=mrfsk.NRNSC).feed(iq)] == [payload]


def test_modem_finds_nothing_in_noise():
    rng = np.random.default_rng(7)
    noise = rng.normal(size=400_000) + 1j * rng.normal(size=400_000)
    assert mrfsk.Demodulator().feed(noise) == []


def test_the_satellites_phy_is_what_the_modem_speaks():
    settings = ccsds.phy_config()
    code, interleaved, sat_sends_coded = mrfsk.check_phy(settings)
    assert code in (mrfsk.RSC, mrfsk.NRNSC)
    with pytest.raises(ValueError, match="fsk.srate"):
        mrfsk.check_phy(dict(settings, **{"fsk.srate": "AT86RF215_FSK_SRATE_100"}))
    with pytest.raises(ValueError, match="fsk.sfd1"):
        mrfsk.check_phy(dict(settings, **{"fsk.sfd1": "0x1234"}))


# ---------- Telemetry ----------


def _sample():
    buf = ctypes.create_string_buffer(C["TELEMETRY_DOWNLINK_SIZE"])
    assert ccsds.library().ground_telemetry_sample(buf, len(buf)) == 0
    return buf.raw


def test_telemetry_decodes_what_the_satellites_serializer_wrote():
    t = telemetry.decode(_sample())
    assert t["timestamp"] == 1_800_000_000 and t["boot_timestamp"] == 1_799_990_000
    assert t["magnetometer.raw_readings"] == [0, -123456, 0]
    assert t["magnetometer.gain_adjusted_readings"][2] == -45.5
    assert t["photodiode.raw_readings"][-1] == 4095
    assert telemetry.device_status_name(t["gyro.health"]) == "TEL_DEVICE_STATUS_BROKEN"
    assert telemetry.device_status_name(t["camera.health"]) == "TEL_DEVICE_STATUS_DISABLED"
    assert t["camera.last_capture_succeeded"] is True
    assert (t["eps.charge_percent"][0], t["eps.voltage"][3], t["eps.current"][1]) == (87.5, 7.25, -0.5)
    assert t["state_machine.state_history"][0] == 3 and t["state_machine.transition_times"][0] == 1_799_999_999
    assert telemetry.opmode_name(t["adcs.opmode"]) == "TEL_ADCS_OPMODE_DETUMBLE_ONLY"
    assert telemetry.adcs_flags(t["adcs.flags"]) == ["TEL_ADCS_FLAG_IN_SUNLIGHT", "TEL_ADCS_FLAG_UTC_VALID"]
    assert t["adcs.filter_resets"] == 513 and t["command_timestamps"][0] == 1_799_999_000
    assert t["os_errors.error_timestamps"][7] == 0xA1B2C3D4
    assert telemetry.encode(t) == _sample()


def test_the_telemetry_layout_covers_every_byte_the_serializer_fills():
    layout = telemetry.layout()
    assert layout[0][2] == 0 and telemetry.size() <= C["TELEMETRY_DOWNLINK_SIZE"]
    assert len({(name, index) for name, index, *_ in layout}) == len(layout)
    with pytest.raises(ValueError):
        telemetry.decode(b"\0" * (telemetry.size() - 1))


# ---------- CFDP ----------


def test_modular_checksum():
    assert cfdp.modular_checksum(b"\x01\x02\x03\x04\x05") == 0x01020304 + 0x05000000
    assert cfdp.modular_checksum(b"\xff" * 8) == 0xFFFFFFFE


def test_every_ground_pdu_parses_with_the_satellites_parsers():
    data = bytes(range(256)) * 3
    u = cfdp.Uplink(data, seq=4242, segment=300)
    pdus = [ccsds.parse_cfdp(p) for p in u.turn_pdus()]
    assert all(p["error"] == 0 and p["transaction_seq"] == 4242 and p["transmission_mode"] == 0 for p in pdus)
    assert all(p["source_entity_id"] == C["ENTITY_ID_GROUND"] and p["dest_entity_id"] == C["ENTITY_ID_SPACECRAFT"]
               for p in pdus)
    meta, *chunks, eof = pdus
    assert meta["directive"] == C["CFDP_DIR_METADATA"] and meta["file_length"] == len(data)
    assert meta["checksum_type"] == 0 and meta["closure_req"] == 0
    assert [c["offset"] for c in chunks] == [0, 300, 600] and b"".join(c["data"] for c in chunks) == data
    assert eof["directive"] == C["CFDP_DIR_EOF"] and (eof["filesize"], eof["checksum"]) == (len(data), u.checksum)
    ack = ccsds.parse_cfdp(cfdp.ack(C["CFDP_DIR_FINISHED"], 0, 4242, u.source, u.dest))
    assert (ack["error"], ack["directive"], ack["ack_directive"], ack["ack_subtype"]) == (0, C["CFDP_DIR_ACK"],
                                                                                          C["CFDP_DIR_FINISHED"], 1)


# ---------- The satellite, transcribed ----------


def sat_pdu(directive, body, seq):
    """A CFDP directive from the satellite, as cfdp_prepare_pdu_header() builds it for a receive transaction."""
    first = (0b001 << 5) | (1 << 3)  # Directive, toward the sender, acknowledged
    head = struct.pack(">BHBIII", first, 1 + len(body), 0x33, C["ENTITY_ID_GROUND"], seq, C["ENTITY_ID_SPACECRAFT"])
    return head + bytes([directive]) + body


class FakeCfdp:
    """The satellite's CFDP receive side (cfdp_process_pdu() and the CFDP_RECV states): ACKs the EOF, NAKs what's
    missing (the Metadata as (0, 0)), sends Finished once the file is whole and checks out, and repeats it each turn
    until the ground ACKs it. Like the satellite, its Finished says delivery incomplete."""

    def __init__(self):
        self.txn = None
        self.out = []  # PDUs for the satellite's next turn
        self.files = []  # Files received whole

    def receive(self, pdu):
        f = ccsds.parse_cfdp(pdu)
        assert f["error"] == 0, "the ground sent a PDU PVDXos can't parse"
        seq = f["transaction_seq"]
        if self.txn is None or self.txn["seq"] != seq:
            self.txn = {"seq": seq, "size": None, "data": {}, "eof": None, "fin_sent": False}
        t = self.txn
        if f["pdu_type"] == C["CFDP_PDU_TYPE_FILEDATA"]:
            t["data"][f["offset"]] = f["data"]
            return
        d = f["directive"]
        if d == C["CFDP_DIR_METADATA"]:
            t["size"] = f["file_length"]
        elif d == C["CFDP_DIR_EOF"]:
            t["eof"] = (f["filesize"], f["checksum"])
            self.out.append(sat_pdu(C["CFDP_DIR_ACK"], bytes([C["CFDP_DIR_EOF"] << 4, 0x01]), seq))
            self._check()
        elif d == C["CFDP_DIR_ACK"] and f["ack_directive"] == C["CFDP_DIR_FINISHED"]:
            self.files.append(self._file())
            self.txn = None

    def _file(self):
        size = self.txn["eof"][0]
        buf = bytearray(size)
        for offset, chunk in self.txn["data"].items():
            buf[offset:offset + len(chunk)] = chunk
        return bytes(buf)

    def _missing(self):
        size = self.txn["eof"][0]
        have = np.zeros(size, dtype=bool)
        for offset, chunk in self.txn["data"].items():
            have[offset:offset + len(chunk)] = True
        gaps, start = [], None
        for i, h in enumerate(list(have) + [True]):
            if not h and start is None:
                start = i
            elif h and start is not None:
                gaps.append((start, i))
                start = None
        return gaps

    def _check(self):
        t = self.txn
        if t["size"] is None:
            self.out.append(sat_pdu(C["CFDP_DIR_NAK"], struct.pack(">IIII", 0, 0, 0, 0), t["seq"]))
            return
        gaps = self._missing()
        if gaps:
            body = struct.pack(">II", gaps[0][0], gaps[-1][1]) + b"".join(struct.pack(">II", *g) for g in gaps)
            self.out.append(sat_pdu(C["CFDP_DIR_NAK"], body, t["seq"]))
            return
        assert cfdp.modular_checksum(self._file()) == t["eof"][1]
        self.out.append(sat_pdu(C["CFDP_DIR_FINISHED"], bytes([0 << 4 | 1 << 2, C["CFDP_TLV_FILESTORE_RESPONSE"], 0]),
                                t["seq"]))
        t["fin_sent"] = True

    def turn(self):
        """The PDUs to send in the satellite's turn."""
        if self.txn is not None and not self.out:
            if self.txn["fin_sent"]:
                # No ACK(Finished) yet: the satellite re-sends it each turn (cfdp_turn_retransmit())
                self.out.append(sat_pdu(C["CFDP_DIR_FINISHED"], bytes([1 << 2, C["CFDP_TLV_FILESTORE_RESPONSE"], 0]),
                                        self.txn["seq"]))
            elif self.txn["eof"] is not None:
                self._check()
        out, self.out = self.out, []
        return out


def apply_uplink(tel, file, now):
    """What an uplink file's commands change in the satellite's telemetry (the commands the soak sends)."""
    _callsign, _size, n, timestamp = struct.unpack(">6sIHI", file[:16])
    tel["command_timestamps"] = [timestamp] + tel["command_timestamps"][:-1]
    pos = 16
    for _ in range(n):
        kind, size = struct.unpack(">HI", file[pos:pos + 6])
        data = file[pos + 6:pos + 6 + size]
        pos += 6 + size
        if kind == C["TRANSMIT_ON_OFF"]:
            tel["pending_tx"] = data[0] == 1  # Applied when the pass ends (uplink_commands_pass_exit())
        elif kind == C["SET_UNIX_TIME"]:
            tel["utc_offset"] = struct.unpack(">I", data)[0] - now
            tel["adcs.flags"] |= C["TEL_ADCS_FLAG_UTC_VALID"]
        elif kind == C["SET_ADCS_OP_MODE"]:
            tel["adcs.opmode"] = struct.unpack(">H", data)[0]
        elif kind == C["ENABLE_DISABLE_DEVICE"]:
            device, enabled = struct.unpack(">HB", data)
            if device == C["CAMERA_ID"]:
                tel["camera.health"] = C["TEL_DEVICE_STATUS_OK" if enabled else "TEL_DEVICE_STATUS_DISABLED"]


class FakeSatellite:
    """The satellite's PUSP handlers, transcribed from uhf_process.c, on a LoopbackRadio, with FakeCfdp behind them.
    Sends `downlink` frames and its CFDP replies in its turns, and telemetry beacons while in LISTEN, which show the
    uplinked commands once their file is received."""

    def __init__(self, radio, downlink=(), beacon_every_s=None, link_timeout_s=None):
        self.radio = radio
        self.state = "LISTEN"
        # A link with nothing from the ground for this long drops back to LISTEN (uhf_link_check())
        self.link_timeout_s = C["UHF_LINK_TIMEOUT_MS"] / 1000 if link_timeout_s is None else link_timeout_s
        self.last_heard = time.monotonic()
        self.downlink = list(downlink)
        self.forwarded = []
        self.cfdp = FakeCfdp()
        self.tel = telemetry.decode(_sample())
        self.tel.update({"adcs.flags": 0, "adcs.opmode": 0, "camera.health": 0, "uhf_health": 0, "utc_offset": None,
                         "command_timestamps": [0] * 16, "pending_tx": None, "timestamp": 0, "boot_timestamp": 0})
        self.tx_enabled = True
        self.applied = 0
        self.beacon_every_s = beacon_every_s
        self.next_beacon = time.monotonic()
        self.retry_s = C["UHF_RETRY_MS"] / 1000
        self.retry_at = None
        self.retries = 0
        self.seq = 0
        self.stop = threading.Event()
        self.names = {C[n]: n for n in ground.DIRECTIVES}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, vc_name, apid, data):
        self.seq += 1
        frame = ccsds.wrap(VC[vc_name], apid, data, self.seq, packet_type=C["SPP_PACKET_TYPE_REPORTING"],
                           callsign=ccsds.satellite_callsign())
        self.radio.send(frame)

    def pusp(self, name):
        self.send("RADIO_STREAM_PUSP", C["CCSDS_APID_PUSP"], bytes([C[name], 0]))

    def beacon(self):
        tel = dict(self.tel)
        if tel["utc_offset"] is not None:
            tel["timestamp"] = int(time.time() + tel["utc_offset"])
        self.send("RADIO_STREAM_TELEMETRY", C["CCSDS_APID_TELEMETRY"], telemetry.encode(tel))

    def enter(self, state, retry=False):
        self.state = state
        self.retries = 0
        self.retry_at = time.monotonic() + self.retry_s if retry else None

    def run(self):
        while not self.stop.is_set():
            raw = self.radio.receive(0.02)
            directive = None
            data = False
            if raw is not None:
                self.last_heard = time.monotonic()
                f = ccsds.unwrap(raw.payload)
                if f.apid == C["CCSDS_APID_PUSP"]:
                    assert len(f.data) == 2, "a PUSP packet is the directive and its notifications"
                    directive = self.names.get(f.data[0])
                    # dispatch_pusp_immediate_cmd(): the notifications act before the directive
                    if f.data[1] & C["PUSP_NOTIF_ENABLE_TX"]:
                        self.tx_enabled = True
                    elif f.data[1] & C["PUSP_NOTIF_DISABLE_TX"]:
                        self.tx_enabled = False
                else:
                    data = True
                    self.forwarded.append(f.data)
                    if f.vcid == VC["RADIO_STREAM_UPLINK_CMD"] and f.apid == C["CCSDS_APID_CFDP"]:
                        self.cfdp.receive(f.data)
                        while len(self.cfdp.files) > self.applied:
                            apply_uplink(self.tel, self.cfdp.files[self.applied], time.time())
                            self.applied += 1
            self.step(directive, data)

    def retry(self, name):
        if self.retry_at and time.monotonic() >= self.retry_at:
            if self.retries >= C["UHF_MAX_RETRIES"]:
                self.enter("LISTEN")
                return
            self.retries += 1
            self.pusp(name)
            self.retry_at = time.monotonic() + self.retry_s

    def step(self, directive, data):
        if self.state != "LISTEN" and time.monotonic() - self.last_heard > self.link_timeout_s:
            self.enter("LISTEN")
            if self.tel["pending_tx"] is not None:  # The pass is over: uplinked commands take effect
                self.tx_enabled, self.tel["pending_tx"] = self.tel["pending_tx"], None
        if not self.tx_enabled:
            return  # Listens only: no beacons, and a SYN is ignored (listen_proc())
        if directive == "SYN" and self.state != "LISTEN":  # uhf_link_check(): the ground restarted the link
            self.pusp("SYN_ACK")
            self.enter("SYN_RECV", retry=True)
            return
        if self.state == "LISTEN":
            if directive == "SYN":
                self.pusp("SYN_ACK")
                self.enter("SYN_RECV", retry=True)
            elif self.beacon_every_s and time.monotonic() >= self.next_beacon and self.tx_enabled:
                self.beacon()
                self.next_beacon = time.monotonic() + self.beacon_every_s
        elif self.state == "SYN_RECV":
            if directive == "SRT" or data:
                self.enter("ESTABLISH_RECV")
            else:
                self.retry("SYN_ACK")
        elif self.state == "ESTABLISH_RECV":
            if directive == "YLD":
                self.pusp("YLD_ACK")
                self.enter("TRANS_TO_TX", retry=True)
            elif directive == "YLD_ACK":
                self.pusp("SWC")
        elif self.state == "TRANS_TO_TX":
            if directive == "SWC":
                for vc_name, apid, payload in self.downlink:
                    self.send(vc_name, apid, payload)
                self.downlink = []
                for pdu in self.cfdp.turn():
                    self.send("RADIO_STREAM_CFDP_REPLY", C["CCSDS_APID_CFDP"], pdu)
                self.pusp("YLD")
                self.enter("TRANS_TO_RX", retry=True)
            elif directive == "YLD":
                self.pusp("YLD_ACK")
                self.retry_at = time.monotonic() + self.retry_s
            else:
                self.retry("YLD_ACK")
        elif self.state == "TRANS_TO_RX":
            if directive == "YLD_ACK":
                self.pusp("SWC")
                self.enter("ESTABLISH_RECV")
            elif data:
                self.enter("ESTABLISH_RECV")
            else:
                self.retry("YLD")


@pytest.fixture
def link():
    ground_radio, sat_radio = hackrf.LoopbackRadio.pair()
    made = []

    def make(**kw):
        sat = FakeSatellite(sat_radio, **kw)
        made.append(sat)
        return ground.Ground(ground_radio), sat, ground_radio, sat_radio

    yield make
    for sat in made:
        sat.stop.set()


def _is_cfdp(psdu):
    try:
        return ccsds.unwrap(psdu).apid == C["CCSDS_APID_CFDP"]
    except ccsds.FramingError:
        return False


# ---------- PUSP and CFDP over the link ----------


def test_a_pass_trades_turns_and_collects_the_satellites_frames(link):
    reply = ("RADIO_STREAM_CFDP_REPLY", C["CCSDS_APID_CFDP"], b"cfdp reply")
    gs, sat, _, _ = link(downlink=[reply])
    report = gs.run_pass(20, turns=2)
    assert report.established and not report.failure, report.failure
    assert (report.turns_given, report.turns_taken) == (2, 2)
    assert any(r.frame and r.frame.data == b"cfdp reply" for r in report.received)


def test_a_pass_uplinks_a_command_file(link):
    gs, sat, _, _ = link()
    file = uplink.build([uplink.set_unix_time(1_800_000_000), uplink.set_adcs_op_mode(1)], 1_799_999_999)
    transfer = cfdp.Uplink(file, seq=11)
    report = gs.run_pass(30, uplink=transfer, turns=2)
    assert not report.failure, (report.failure, transfer.events)
    assert transfer.succeeded and transfer.done and transfer.resent_pdus == 0
    assert transfer.delivery_incomplete  # What the satellite's Finished says today
    assert sat.cfdp.files == [file] and ccsds.parse_uplink(sat.cfdp.files[0]) == [C["SET_UNIX_TIME"],
                                                                                    C["SET_ADCS_OP_MODE"]]


def test_lost_uplink_pdus_are_recovered(link):
    """The first File Data PDU, the first EOF and the first ACK(Finished) are lost, and then 20% of all CFDP frames."""
    gs, sat, ground_radio, sat_radio = link()
    rng = random.Random(3)
    first_lost = set()

    def drop(psdu):
        if not _is_cfdp(psdu):
            return False
        f = ccsds.parse_cfdp(ccsds.unwrap(psdu).data)
        kind = "fd" if f["pdu_type"] else f["directive"]
        if kind in ("fd", C["CFDP_DIR_EOF"], C["CFDP_DIR_ACK"]) and kind not in first_lost:
            first_lost.add(kind)
            return True
        return rng.random() < 0.2

    ground_radio.drop = drop
    sat_radio.drop = lambda psdu: _is_cfdp(psdu) and rng.random() < 0.2
    file = uplink.build([uplink.set_unix_time(1_800_000_000)] * 3, 7)
    transfer = cfdp.Uplink(file, seq=12, segment=20)
    report = gs.run_pass(120, uplink=transfer, turns=2, max_extra_turns=12)
    assert not report.failure, (report.failure, transfer.events)
    assert transfer.succeeded and sat.cfdp.files[-1] == file
    assert transfer.resent_pdus > 0 and any(e.startswith("NAK") for e in transfer.events)


def test_lost_directives_are_recovered(link):
    """The first SRT, SWC and YLD_ACK the ground sends, and the first YLD_ACK the satellite sends, are lost."""
    gs, sat, ground_radio, sat_radio = link()

    def drop_first(names):
        dropped = set()

        def drop(psdu):
            name = gs.directive_of(ground.Received(0, ccsds.unwrap(psdu)))
            if name in names and name not in dropped:
                dropped.add(name)
                return True
            return False

        return drop

    ground_radio.drop = drop_first({"SRT", "SWC", "YLD_ACK"})
    sat_radio.drop = drop_first({"YLD_ACK"})
    report = gs.run_pass(60, turns=2)
    assert report.established and not report.failure, report.failure
    assert report.turns_taken == 2 and report.resent >= 3


def test_no_satellite_gives_up_after_the_retries(link):
    gs, sat, _, _ = link()
    sat.stop.set()
    sat.thread.join()
    started = time.monotonic()
    report = gs.run_pass(30)
    assert not report.established and report.failure == "no SYN_ACK"
    assert time.monotonic() - started < (C["UHF_MAX_RETRIES"] + 2) * C["UHF_RETRY_MS"] / 1000


def test_beacons_are_recognised_and_decoded(link):
    gs, sat, _, _ = link(beacon_every_s=0.1)
    beacons = []
    gs.on_receive = lambda rx: beacons.append(rx) if gs.is_beacon(rx) else None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        gs.receive(0.05)
    assert len(beacons) >= 5 and all(b.frame.callsign == ccsds.satellite_callsign() for b in beacons)
    assert telemetry.decode(beacons[0].frame.data)["eps.voltage"][3] == 7.25


# ---------- The soak's radio, uplink and telemetry monitors ----------


def _rx(frame, error="", t=0.0):
    return ground.Received(t, frame, error=error)


def _beacon(count):
    frame = ccsds.unwrap(ccsds.wrap(VC["RADIO_STREAM_TELEMETRY"], C["CCSDS_APID_TELEMETRY"], b"telemetry", 0,
                                    callsign=ccsds.satellite_callsign()))
    frame.vc_frame_count = count
    return frame


def _radio_monitor():
    import monitors

    events = []
    return monitors.RadioLink(events.append, beacon_period_s=70, link_timeout_s=30), events


def test_radio_monitor_counts_bursts_and_missed_ones():
    m, events = _radio_monitor()
    for t, count in [(0, 10), (1, 10), (2, 10), (70, 11), (71, 11), (210, 13)]:
        m.on_tick(t)
        m.on_frame(t, _rx(_beacon(count), t=t), True)
    assert (m.beacons, m.bursts, m.missed_bursts) == (6, 3, 1)
    assert any("missed 1 beacon burst" in e.message for e in events)
    assert not [e for e in events if e.severity == "fail"]


def test_radio_monitor_fails_on_a_beacon_gap_but_not_during_a_pass():
    m, events = _radio_monitor()
    m.on_frame(0, _rx(_beacon(1)), True)
    m.pass_started(10, 600)
    for t in range(0, 600, 5):
        m.on_tick(t)
    assert not [e for e in events if e.severity == "fail"], "beacons stop while the link is up"
    for t in range(600, 900, 5):
        m.on_tick(t)
    assert [e.message for e in events if e.severity == "fail"] == ["no beacon for 175 s"]


def test_radio_monitor_reports_unreadable_frames_failed_passes_and_fec():
    m, events = _radio_monitor()
    for t in range(3):
        m.on_frame(t, _rx(None, error="bad FCS; USLP frame rejected"), False)
    coded = ground.Received(4, _beacon(1), coded=True, fec="viterbi", corrected_bits=3, coded_bits=np.zeros(1000))
    m.on_frame(4, coded, True)
    m.on_pass(5, ground.PassReport(0, established=False, failure="no SYN_ACK"))
    assert m.bad == {"bad FCS; USLP frame rejected": 3}
    assert [e.severity for e in events] == ["warn", "fail"]
    assert m.metrics()["fec_viterbi"] == 1 and m.metrics()["fec_bit_error_rate"] == 0.003


def _uplink_monitor(settle_s=100, silence_grace_s=40):
    import monitors

    events = []
    return monitors.Uplinks(events.append, C, settle_s=settle_s, beacon_age_s=10, silence_grace_s=silence_grace_s), events


def _finished_report(seq=1, start=0):
    transfer = cfdp.Uplink(b"file", seq=seq)
    transfer.finished = {"condition_code": 0, "delivery_code": 1}
    transfer.fin_acked = True
    return ground.PassReport(start, established=True, uplink=transfer)


def _tel(**fields):
    tel = telemetry.decode(_sample())
    tel.update({"adcs.flags": 0, "adcs.opmode": 0, "camera.health": 0, "command_timestamps": [0] * 16})
    tel.update({k.replace("__", "."): v for k, v in fields.items()})
    return tel


def _plan(step, now=1000, **commands):
    import uplinks

    config = {"radio": {"commands": {"set_unix_time": True, "rotation": [step] if step else [], **commands}}}
    return uplinks.plan(config, 0, now)


def test_uplink_monitor_confirms_commands_in_telemetry():
    m, events = _uplink_monitor()
    m.on_pass(1000, _finished_report(), _plan("adcs_detumble", now=999))
    m.on_telemetry(1010, _tel())  # Nothing applied yet
    m.on_tick(1050)
    assert m.confirmed == 0 and not [e for e in events if e.severity == "fail"]
    m.on_telemetry(1060, _tel(timestamp=1058, adcs__flags=C["TEL_ADCS_FLAG_UTC_VALID"],
                              adcs__opmode=C["TEL_ADCS_OPMODE_DETUMBLE_ONLY"], command_timestamps=[999] + [0] * 15))
    m.on_tick(2000)
    assert m.confirmed == 3 and m.unconfirmed == 0 and not m.pending
    assert [e.severity for e in events].count("warn") == 1  # The satellite's Finished says delivery incomplete


def test_uplink_monitor_fails_what_never_shows_and_failed_transfers():
    m, events = _uplink_monitor(settle_s=100)
    m.on_pass(0, _finished_report(), _plan("adcs_detumble", now=5))
    m.on_telemetry(50, _tel(timestamp=50, adcs__flags=C["TEL_ADCS_FLAG_UTC_VALID"], command_timestamps=[5] + [0] * 15))
    m.on_tick(101)
    fails = [e.message for e in events if e.severity == "fail"]
    assert len(fails) == 1 and "ADCS opmode" in fails[0] and m.confirmed == 2
    unfinished = ground.PassReport(0, established=True, failure="the uplink didn't finish", uplink=cfdp.Uplink(b"x", 2))
    m.on_pass(200, unfinished, _plan(None))
    assert m.failed == 1 and "not finished" in events[-1].message


def test_uplink_monitor_warns_on_commands_the_satellite_ignores():
    m, events = _uplink_monitor()
    m.on_pass(0, _finished_report(), _plan("device_off", now=5))
    m.on_line(1, f"[INFO|uplink]: uplink: command type {C['ENABLE_DISABLE_DEVICE']} not implemented yet, ignored")
    m.on_pass(10, _finished_report(), _plan("adcs_params", now=8))
    m.on_line(11, f"[WARNING|adcs]: adcs: operation {C['OPERATION_ADCS_UPDATE_PARAMS']} not implemented, ignoring")
    m.on_pass(20, _finished_report(), _plan("set_power_mode", now=18))
    m.on_line(21, f"[INFO|uplink]: uplink: command type {C['SET_PWR_MODE']} not implemented yet, ignored")
    m.on_tick(1000)
    warned = [e.message for e in events if e.severity == "warn" and "not implemented" in e.message]
    assert [w.split()[3].rstrip(":") for w in warned] == ["ENABLE_DISABLE_DEVICE", "ADCS_UPDATE_PARAMS", "SET_PWR_MODE"]
    assert not [e for e in events if e.severity == "fail" and ("CAMERA" in e.message or "TLE" in e.message)]


def test_uplink_monitor_log_boot_and_silence_checks():
    m, events = _uplink_monitor(settle_s=300, silence_grace_s=40)
    m.on_pass(100, _finished_report(start=90), _plan("update_display"))
    m.on_line(95, "[INFO|uplink]: uplink: display image saved, shown once the pass ends")  # During the pass
    m.on_pass(200, _finished_report(start=190), _plan("reboot"))
    m.on_line(230, "[INFO|main]: [+] Built from commit: abc1234")
    m.on_pass(300, _finished_report(start=290), _plan("transmit_off"), next_pass=500)
    m.on_telemetry(320, _tel())  # Before the grace ends: the satellite's link hadn't timed out yet
    m.on_tick(501)
    confirmed = [e.message for e in events if e.message.startswith("confirmed")]
    assert "confirmed: display image saved" in confirmed and "confirmed: commanded reboot" in confirmed
    assert "confirmed: no beacon after TRANSMIT_ON_OFF off" in confirmed
    m.on_pass(600, _finished_report(start=590), _plan("transmit_off"), next_pass=900)
    m.on_telemetry(700, _tel())
    assert any("beacon was heard" in e.message for e in events if e.severity == "fail")


def test_telemetry_monitor_compares_beacons_with_the_log():
    import monitors

    events = []
    m = monitors.Telemetry(events.append, C, beacon_age_s=10)
    m.on_line(0, "[INFO|fuel_gauge]: fuel_gauge: pack 3 SOC=87500 m%  Vpack=7250 mV  I=-500 mA  T=20000 mC")
    m.on_line(0, "[INFO|task_manager]: state_machine: state 2 -> state 3")
    good = _tel(eps__charge_percent=[0, 0, 0, 87.5], eps__voltage=[0, 0, 0, 7.25], eps__current=[0, 0, 0, -0.5],
                eps__temperature=[0, 0, 0, 20.0])
    m.on_telemetry(5, good)
    assert not events
    bad = dict(good, **{"eps.voltage": [0, 0, 0, 7.0], "uhf_health": C["TEL_DEVICE_STATUS_BROKEN"]})
    bad["state_machine.state_history"] = [5] + bad["state_machine.state_history"][1:]
    m.on_telemetry(6, bad)
    assert sorted(m.mismatches) == ["eps.voltage", "state", "uhf_health"]


def test_ground_station_runs_passes_in_the_soak(tmp_path):
    import json
    import tomllib

    import run
    import schedule

    with open(os.path.join(HERE, "..", "config.toml"), "rb") as f:
        config = tomllib.load(f)
    config["radio"]["enabled"] = True
    config["radio"]["uplink_settle_s"] = 3
    config["radio"]["silence_grace_s"] = 1
    config["radio"]["commands"]["rotation"] = ["adcs_detumble", "transmit_off", "transmit_on"]
    runner = run.Runner(config, str(tmp_path), {"id": "t", "start": time.time(), "config": "", "mode": "live"},
                        echo=False, radio_constants=C)
    ground_radio, sat_radio = hackrf.LoopbackRadio.pair()
    sat = FakeSatellite(sat_radio, beacon_every_s=0.2, link_timeout_s=0.5)
    passes = schedule.Passes(time.time(), interval_s=4.0, pass_s=2.5, offset_s=0.5)
    station = run.GroundStation(runner, config, passes, radio=ground_radio)
    station.start()
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end:
            runner.tick(time.time())
            time.sleep(0.1)
    finally:
        runner.stop.set()
        sat.stop.set()
        station.join(timeout=10)
        station.close()
    runner.drain()
    runner.tick(time.time())
    fails = [e for e in runner.events.all() if e["severity"] == "fail"]
    assert not fails, [(e["monitor"], e["message"]) for e in runner.events.all()]
    assert runner.radio.beacons > 0 and runner.radio.passes >= 2 and runner.radio.passes_failed == 0
    assert runner.uplinks.sent >= 2 and runner.uplinks.failed == 0
    messages = [e["message"] for e in runner.events.all()]
    records = [json.loads(line) for line in (tmp_path / "passes.jsonl").read_text().splitlines()]
    assert [r["step"] for r in records][:3] == ["adcs_detumble", "transmit_off", "transmit_on"]
    assert "confirmed: ADCS opmode TEL_ADCS_OPMODE_DETUMBLE_ONLY" in messages
    assert "confirmed: no beacon after TRANSMIT_ON_OFF off" in messages, messages
    assert "confirmed: beacons after TRANSMIT_ON_OFF on" in messages, messages
    assert all(r["uplink"]["succeeded"] for r in records)
    assert (tmp_path / "telemetry.jsonl").read_text().count('"eps.voltage"') == runner.radio.beacons


# ---------- Uplink command files ----------


def test_uplink_files_parse_with_the_satellites_parser():
    file = uplink.build([uplink.set_unix_time(1_800_000_000), uplink.set_adcs_op_mode(1),
                         uplink.enable_disable_device(C["CAMERA_ID"], False)], 1234)
    assert ccsds.parse_uplink(file) == [C["SET_UNIX_TIME"], C["SET_ADCS_OP_MODE"], C["ENABLE_DISABLE_DEVICE"]]


def test_the_satellite_rejects_a_bad_uplink():
    file = bytearray(uplink.build([uplink.set_unix_time(1)], 0))
    assert ccsds.parse_uplink(bytes(file[:-1])) is None, "truncated"
    file[0] ^= 0x20
    assert ccsds.parse_uplink(bytes(file)) is None, "wrong callsign"
    assert ccsds.parse_uplink(uplink.build([uplink.set_adcs_op_mode(7)], 0)) is None, "an opmode that doesn't exist"


def test_every_step_parses_with_the_satellites_parser():
    import uplinks

    config = {"radio": {"commands": {"set_unix_time": True}}}
    assert uplinks.rotation(config) == list(uplinks.STEPS)
    for number, step in enumerate(uplinks.STEPS):
        p = uplinks.plan(config, number, 1_800_000_000 + number)
        assert p["step"] == step and p["file_timestamp"] == 1_800_000_000 + number
        assert p["commands"][0] == {"name": "SET_UNIX_TIME", "value": 1_800_000_000 + number}
        assert ccsds.parse_uplink(p["data"]) == [C[c["name"]] for c in p["commands"]], step
        assert all(c["type"] == C[c["command"]] for c in p["checks"])
    assert len(uplinks.plan(config, 6, 0)["data"]) > C["UPLINK_DISPLAY_BITMAP_SIZE"]  # update_display


def test_the_rotation_is_the_configured_subset():
    import uplinks

    config = {"radio": {"commands": {"set_unix_time": False, "rotation": ["reboot", "transmit_off"]}}}
    assert [uplinks.plan(config, n, 0)["step"] for n in range(4)] == ["reboot", "transmit_off"] * 2
    assert [c["name"] for c in uplinks.plan(config, 0, 0)["commands"]] == ["REBOOT"]
    assert uplinks.plan(config, 0, 0)["reboot"] and uplinks.plan(config, 1, 0)["transmit_off"]
    assert uplinks.plan({"radio": {"commands": {"set_unix_time": False, "rotation": []}}}, 0, 0)["data"] == b""
    with pytest.raises(ValueError, match="unknown"):
        uplinks.rotation({"radio": {"commands": {"rotation": ["reboot", "warp_drive"]}}})


# ---------- Pinning the RSC code ----------


def test_fit_rsc_finds_the_satellites_code(tmp_path):
    """Beacons from a satellite whose RSC differs from mrfsk.RSC's guess are read from their systematic bits, kept in
    coded.jsonl, and fit_rsc.py names the real code, which then decodes them with the Viterbi decoder."""
    import json

    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    import fit_rsc

    real = mrfsk.Code("real", 0b1011, (mrfsk.SYS, 0b1001))
    rng = np.random.default_rng(9)
    path = tmp_path / "coded.jsonl"
    with open(path, "w") as f:
        for n in (60, 101):
            payload = rng.integers(0, 256, n).astype(np.uint8).tobytes()
            iq = np.concatenate([np.zeros(4000), mrfsk.modulate(mrfsk.frame_bits(payload, real)), np.zeros(4000)])
            frame = mrfsk.Demodulator(code=mrfsk.RSC).feed(iq)[0]
            assert frame.fec == "systematic:0"
            f.write(json.dumps({"fec": frame.fec, "n": len(frame.coded_bits),
                                "coded": bytes(np.packbits(frame.coded_bits)).hex()}) + "\n")
    scores = fit_rsc.fit(fit_rsc.load([path]))
    assert scores[0][0] == 1.0 and scores[1][0] < 1.0
    best = scores[0][1]
    assert (best.feedback, best.outputs) == (real.feedback, real.outputs)
    tails = fit_rsc.tail_and_pad(fit_rsc.load([path]))
    assert all(pad == want for _, pad, want in tails)
    confirmed = mrfsk.Code("RSC", best.feedback, best.outputs)
    iq = np.concatenate([np.zeros(4000), mrfsk.modulate(mrfsk.frame_bits(b"pinned", real)), np.zeros(4000)])
    assert [(f.payload, f.fec) for f in mrfsk.Demodulator(code=confirmed).feed(iq)] == [(b"pinned", "viterbi")]
