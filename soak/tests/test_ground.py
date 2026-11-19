"""The ground station's framing, modem and PUSP side, without hardware. Needs PVDXos's radio code (the radio-task
branch): run with PVDX_ROOT pointing at a checkout of it; skipped otherwise.

The PUSP tests run the ground against FakeSatellite, a small transcription of the satellite's handlers in
src/tasks/uhf_radio/uhf_process.c (LISTEN, SYN_RECV, ESTABLISH_RECV, TRANS_TO_TX, ESTABLISH_SEND, TRANS_TO_RX). It
checks the ground's half of the handshake against that reading of the satellite code; the real check is on the bench.
"""

import os
import sys
import threading
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "hil", "lib"))

import paths  # noqa: E402

if not os.path.exists(os.path.join(paths.PVDX, "src", "ccsds", "radio_routing.h")):
    pytest.skip("PVDX_ROOT has no radio-task sources (src/ccsds/radio_routing.h)", allow_module_level=True)

import ccsds  # noqa: E402
import ground  # noqa: E402
import hackrf  # noqa: E402
import mrfsk  # noqa: E402

C = ccsds.constants()


# ---------- Framing and modem ----------


def test_framing_round_trip_and_rejection():
    frame = ccsds.wrap(ccsds.routes()["RADIO_STREAM_PUSP"][0], C["CCSDS_APID_PUSP"], bytes([C["SYN"]]), 5)
    parsed = ccsds.unwrap(frame)
    assert (parsed.apid, parsed.sequence_count, parsed.data) == (C["CCSDS_APID_PUSP"], 5, bytes([C["SYN"]]))
    assert parsed.callsign == ccsds.ground_callsign()
    with pytest.raises(ccsds.FramingError):
        ccsds.unwrap(frame[:-1] + bytes([frame[-1] ^ 1]))


def test_vc_frame_counts_advance_per_vc():
    vc = ccsds.routes()["RADIO_STREAM_UPLINK_CMD"][0]
    counts = [ccsds.unwrap(ccsds.wrap(vc, C["CCSDS_APID_CFDP"], b"x", 0)).vc_frame_count for _ in range(3)]
    assert (counts[1] - counts[0]) % 256 == 1 and (counts[2] - counts[1]) % 256 == 1


def test_crc16_matches_the_802154_check_value():
    assert mrfsk.crc16(b"123456789") == 0x2189  # CRC-16/KERMIT, the 802.15.4 FCS


def _channel(iq, rng, snr_db, offset_hz, lead):
    n = np.arange(len(iq))
    iq = iq * np.exp(2j * np.pi * offset_hz * n / mrfsk.SAMPLE_RATE + 0.4j)
    sigma = 10 ** (-snr_db / 20) / np.sqrt(2)
    noise = lambda k: (rng.normal(size=k) + 1j * rng.normal(size=k)) * sigma  # noqa: E731
    return np.concatenate([noise(lead), iq + noise(len(iq)), noise(lead)])


@pytest.mark.parametrize("snr_db, offset_hz", [(20, 0), (6, 8000), (3, -15000)])
def test_modem_recovers_frames(snr_db, offset_hz):
    rng = np.random.default_rng(snr_db)
    payloads = [rng.integers(0, 256, 300).astype(np.uint8).tobytes(), b"short"]
    iq = np.concatenate([_channel(mrfsk.modulate(mrfsk.frame_bits(p)), rng, snr_db, offset_hz, 7000) for p in payloads])
    demod = mrfsk.Demodulator()
    out = []
    for i in range(0, len(iq), 33333):  # Frames straddle the reads
        out += demod.feed(iq[i:i + 33333])
    assert [f.payload for f in out if f.fcs_ok] == payloads
    assert all(abs(f.freq_offset_hz - offset_hz) < 2000 for f in out)


def test_modem_finds_nothing_in_noise():
    rng = np.random.default_rng(7)
    demod = mrfsk.Demodulator()
    noise = rng.normal(size=400_000) + 1j * rng.normal(size=400_000)
    assert demod.feed(noise) == []


def test_modem_flags_coded_frames():
    bits = mrfsk.frame_bits(b"coded frame")
    sync_end = 8 * mrfsk.PREAMBLE_OCTETS
    bits[sync_end:sync_end + 16] = mrfsk.sfd_bits(mrfsk.SFD_CODED)
    out = mrfsk.Demodulator().feed(np.concatenate([np.zeros(4000), mrfsk.modulate(bits), np.zeros(4000)]))
    assert out and out[0].coded


# ---------- PUSP ----------


class FakeSatellite:
    """The satellite's PUSP handlers, transcribed from uhf_process.c, on a LoopbackRadio. Sends `downlink` frames in
    its turns, and beacons while in LISTEN."""

    def __init__(self, radio, downlink=(), beacon_every_s=None):
        self.radio = radio
        self.state = "LISTEN"
        self.downlink = list(downlink)
        self.forwarded = []
        self.beacon_every_s = beacon_every_s
        self.next_beacon = time.monotonic()
        self.retry_s = C["UHF_RETRY_MS"] / 1000
        self.retry_at = None
        self.retries = 0
        self.seq = 0
        self.stop = threading.Event()
        self.vc = {name: vcid for name, (vcid, _) in ccsds.routes().items()}
        self.names = {C[n]: n for n in ground.DIRECTIVES}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def send(self, vc_name, apid, data):
        self.seq += 1
        frame = ccsds.wrap(self.vc[vc_name], apid, data, self.seq, packet_type=C["SPP_PACKET_TYPE_REPORTING"],
                           callsign=ccsds.satellite_callsign())
        self.radio.send(frame)

    def pusp(self, name):
        self.send("RADIO_STREAM_PUSP", C["CCSDS_APID_PUSP"], bytes([C[name]]))

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
                f = ccsds.unwrap(raw.payload)
                if f.apid == C["CCSDS_APID_PUSP"]:
                    directive = self.names.get(f.data[0])
                else:
                    data = True
                    self.forwarded.append(f.data)
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
        if directive == "SYN" and self.state != "LISTEN":  # uhf_link_check(): the ground restarted the link
            self.pusp("SYN_ACK")
            self.enter("SYN_RECV", retry=True)
            return
        if self.state == "LISTEN":
            if directive == "SYN":
                self.pusp("SYN_ACK")
                self.enter("SYN_RECV", retry=True)
            elif self.beacon_every_s and time.monotonic() >= self.next_beacon:
                self.send("RADIO_STREAM_TELEMETRY", C["CCSDS_APID_TELEMETRY"], b"telemetry")
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


def test_a_pass_opens_the_link_and_trades_turns(link):
    reply = ("RADIO_STREAM_CFDP_REPLY", C["CCSDS_APID_CFDP"], b"cfdp reply")
    gs, sat, _, _ = link(downlink=[reply])
    report = gs.run_pass(20, uplink=[("RADIO_STREAM_UPLINK_CMD", C["CCSDS_APID_CFDP"], b"command file pdu")], turns=2)
    assert report.established and not report.failure, report.failure
    assert (report.turns_given, report.turns_taken) == (2, 2)
    assert sat.forwarded == [b"command file pdu"]
    assert any(r.frame and r.frame.data == b"cfdp reply" for r in report.received)


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


def test_beacons_are_recognised(link):
    gs, sat, _, _ = link(beacon_every_s=0.1)
    beacons = []
    gs.on_receive = lambda rx: beacons.append(rx) if gs.is_beacon(rx) else None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        gs.receive(0.05)
    assert len(beacons) >= 5 and all(b.frame.callsign == ccsds.satellite_callsign() for b in beacons)


# ---------- The soak's radio monitor and ground station ----------


def _rx(frame, error="", t=0.0):
    return ground.Received(t, frame, error=error)


def _beacon(count):
    vc = ccsds.routes()["RADIO_STREAM_TELEMETRY"][0]
    frame = ccsds.unwrap(ccsds.wrap(vc, C["CCSDS_APID_TELEMETRY"], b"telemetry", 0, callsign=ccsds.satellite_callsign()))
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


def test_radio_monitor_reports_unreadable_frames_and_failed_passes():
    m, events = _radio_monitor()
    for t in range(3):
        m.on_frame(t, _rx(None, error="bad FCS; USLP frame rejected"), False)
    m.on_pass(5, ground.PassReport(0, established=False, failure="no SYN_ACK"))
    assert m.bad == {"bad FCS; USLP frame rejected": 3}
    assert [e.severity for e in events] == ["warn", "fail"]


def test_ground_station_runs_passes_in_the_soak(tmp_path):
    import tomllib

    import run
    import schedule

    with open(os.path.join(HERE, "..", "config.toml"), "rb") as f:
        config = tomllib.load(f)
    config["radio"]["enabled"] = True
    runner = run.Runner(config, str(tmp_path), {"id": "t", "start": time.time(), "config": "", "mode": "live"},
                        echo=False, radio_constants=C)
    ground_radio, sat_radio = hackrf.LoopbackRadio.pair()
    sat = FakeSatellite(sat_radio, beacon_every_s=0.2)
    passes = schedule.Passes(time.time(), interval_s=3.0, pass_s=2.0, offset_s=1.0)
    station = run.GroundStation(runner, config, passes, radio=ground_radio)
    station.start()
    try:
        end = time.monotonic() + 5.5
        while time.monotonic() < end:
            runner.tick(time.time())
            time.sleep(0.1)
    finally:
        runner.stop.set()
        sat.stop.set()
        station.join(timeout=10)
        station.close()
    runner.drain()
    assert runner.radio.beacons > 0
    assert runner.radio.passes >= 1 and runner.radio.passes_failed == 0
    assert (tmp_path / "passes.jsonl").read_text().count('"established": true') >= 1
    assert (tmp_path / "frames.jsonl").read_text().count('"beacon": true') == runner.radio.beacons


# ---------- Uplink command files ----------


def test_uplink_files_parse_with_the_satellites_parser():
    import uplink

    file = uplink.build([uplink.set_unix_time(1_800_000_000), uplink.reboot(), uplink.enable_disable_device(3)], 1234)
    assert ccsds.parse_uplink(file) == [C["SET_UNIX_TIME"], C["REBOOT"], C["ENABLE_DISABLE_DEVICE"]]


def test_the_satellite_rejects_a_bad_uplink():
    import uplink

    file = bytearray(uplink.build([uplink.set_unix_time(1)], 0))
    assert ccsds.parse_uplink(bytes(file[:-1])) is None, "truncated"
    file[0] ^= 0x20
    assert ccsds.parse_uplink(bytes(file)) is None, "wrong callsign"
