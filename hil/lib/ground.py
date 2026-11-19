"""The UHF ground station: the ground's side of the satellite's radio protocol, over a radio from hackrf.py.

Framing is PVDXos's own (ccsds.py). The link protocol is PUSP (src/tasks/uhf_radio/pusp.h), whose satellite side is
src/tasks/uhf_radio/uhf_process.c; this is its mirror image:

    open:      SYN ->              <- SYN_ACK        SRT ->            (the ground has the turn)
    handover:  YLD ->              <- YLD_ACK        SWC ->            (whoever has the turn gives it)

The side waiting for a reply to its own directive re-sends it every UHF_RETRY_MS, up to UHF_MAX_RETRIES times; the
other side re-sends its reply only when a duplicate shows it was lost. There is no close: a side that hears nothing
from the other for UHF_LINK_TIMEOUT_MS drops the link. Outside a link the satellite sends telemetry beacon bursts.

Every timing value and identifier comes from PVDXos's sources through ccsds.py.
"""

import time
from dataclasses import dataclass, field

import ccsds

DIRECTIVES = ("SYN", "SYN_ACK", "SRT", "YLD", "YLD_ACK", "SWC")


@dataclass
class Received:
    """A frame the ground heard: unwrapped, or the reason it couldn't be."""

    t: float
    frame: object  # ccsds.Frame, or None
    error: str = ""  # Why it wasn't unwrapped (bad FCS, framing)
    fcs_ok: bool = True
    coded: bool = False
    power_db: float = 0.0
    freq_offset_hz: float = 0.0


@dataclass
class PassReport:
    start: float
    established: bool = False
    establish_s: float = None  # From the first SYN to SYN_ACK
    turns_given: int = 0  # Handovers to the satellite that completed (SWC sent)
    turns_taken: int = 0  # Handovers from the satellite that completed (SWC received)
    sent: int = 0
    resent: int = 0
    received: list = field(default_factory=list)  # Received frames during the pass
    failure: str = ""  # Why the pass ended early


class Ground:
    def __init__(self, radio, on_receive=None):
        self.radio = radio
        self.on_receive = on_receive  # Called with every Received, in and out of passes
        c = ccsds.constants()
        self.c = c
        routes = ccsds.routes()
        self.vc = {name: vcid for name, (vcid, _) in routes.items()}
        self.directive = {name: c[name] for name in DIRECTIVES}
        self.directive_name = {v: k for k, v in self.directive.items()}
        self.retry_s = c["UHF_RETRY_MS"] / 1000
        self.max_retries = c["UHF_MAX_RETRIES"]
        self.link_timeout_s = c["UHF_LINK_TIMEOUT_MS"] / 1000
        self.sequence = {}  # APID -> next SPP sequence count
        self.sat_callsign = ccsds.satellite_callsign()

    # ---------- Frames ----------

    def send(self, stream, apid, data):
        seq = self.sequence.get(apid, 0)
        self.sequence[apid] = (seq + 1) & 0x3FFF
        self.radio.send(ccsds.wrap(self.vc[stream], apid, data, seq))

    def send_directive(self, name):
        self.send("RADIO_STREAM_PUSP", self.c["CCSDS_APID_PUSP"], bytes([self.directive[name]]))

    def receive(self, timeout):
        """The next frame the radio heard, unwrapped (Received), or None after timeout seconds."""
        raw = self.radio.receive(timeout)
        if raw is None:
            return None
        rx = Received(getattr(raw, "received_at", time.time()), None, fcs_ok=raw.fcs_ok, coded=raw.coded,
                      power_db=raw.power_db, freq_offset_hz=raw.freq_offset_hz)
        if raw.coded:
            rx.error = "FEC-coded frame (this modem can't decode RSC yet)"
        else:
            try:
                rx.frame = ccsds.unwrap(raw.payload)
                if not raw.fcs_ok:
                    rx.error = "bad FCS but a good USLP frame: the ground's FCS check disagrees with the chip's"
            except ccsds.FramingError as e:
                rx.error = f"{'bad FCS; ' if not raw.fcs_ok else ''}{e}"
        if self.on_receive:
            self.on_receive(rx)
        return rx

    def directive_of(self, rx):
        """The PUSP directive name in a received frame from the satellite, or None."""
        f = rx.frame if rx else None
        if f is None or f.vcid != self.vc["RADIO_STREAM_PUSP"] or f.apid != self.c["CCSDS_APID_PUSP"] or len(f.data) < 1:
            return None
        return self.directive_name.get(f.data[0])

    def is_beacon(self, rx):
        f = rx.frame if rx else None
        return f is not None and f.vcid == self.vc["RADIO_STREAM_TELEMETRY"] and f.apid == self.c["CCSDS_APID_TELEMETRY"]

    # ---------- A pass ----------

    def _await(self, report, wanted, resend, deadline, replies=()):
        """Waits for one of the directives in wanted, re-sending `resend` every retry period (UHF_MAX_RETRIES times).
        replies maps a duplicate the satellite sends (showing our last reply was lost) to what to send back. Returns the
        directive received, or None on giving up or reaching deadline."""
        tries = 0
        next_send = time.monotonic() + self.retry_s
        while time.monotonic() < deadline:
            rx = self.receive(min(0.05, max(0.0, next_send - time.monotonic())))
            if rx is not None:
                report.received.append(rx)
                name = self.directive_of(rx)
                if name in wanted:
                    return name
                if name in replies:
                    self.send_directive(replies[name])
                    report.resent += 1
                    next_send = time.monotonic() + self.retry_s
                continue
            if time.monotonic() >= next_send:
                if tries >= self.max_retries:
                    return None
                tries += 1
                self.send_directive(resend)
                report.resent += 1
                next_send = time.monotonic() + self.retry_s
        return None

    def run_pass(self, duration_s, uplink=(), turns=1):
        """Opens a link, sends the uplink frames ((stream, apid, data) each) in the ground's first turn, then hands the
        turn over and back `turns` times, collecting what the satellite sends. Then stops transmitting and lets the
        link time out. Returns a PassReport."""
        report = PassReport(time.time())
        deadline = time.monotonic() + duration_s
        t0 = time.monotonic()
        self.send_directive("SYN")
        report.sent += 1
        if self._await(report, {"SYN_ACK"}, "SYN", deadline) is None:
            report.failure = "no SYN_ACK"
            return report
        report.established = True
        report.establish_s = time.monotonic() - t0
        self.send_directive("SRT")
        report.sent += 1
        pending = list(uplink)
        for _ in range(turns):
            # Our turn: send, then yield
            for stream, apid, data in pending:
                self.send(stream, apid, data)
                report.sent += 1
            pending = []
            self.send_directive("YLD")
            report.sent += 1
            # A SYN_ACK duplicate means our SRT was lost and the satellite is still in SYN_RECV
            if self._await(report, {"YLD_ACK"}, "YLD", deadline, replies={"SYN_ACK": "SRT"}) is None:
                report.failure = "no YLD_ACK"
                return report
            self.send_directive("SWC")
            report.sent += 1
            report.turns_given += 1
            # The satellite's turn: it sends until it yields (within UHF_TX_WINDOW_MS)
            got = self._collect_until_yield(report, deadline)
            if got is None:
                report.failure = "the satellite never yielded"
                return report
            self.send_directive("YLD_ACK")
            report.sent += 1
            # A YLD_ACK duplicate means our SWC was lost; a YLD duplicate means our YLD_ACK was
            if self._await(report, {"SWC"}, "YLD_ACK", deadline, replies={"YLD_ACK": "SWC", "YLD": "YLD_ACK"}) is None:
                report.failure = "no SWC"
                return report
            report.turns_taken += 1
        return report

    def _collect_until_yield(self, report, deadline):
        window = self.c["UHF_TX_WINDOW_MS"] / 1000 + self.link_timeout_s / 2
        end = min(deadline, time.monotonic() + window)
        while time.monotonic() < end:
            rx = self.receive(0.05)
            if rx is None:
                continue
            report.received.append(rx)
            name = self.directive_of(rx)
            if name == "YLD":
                return rx
            if name == "YLD_ACK":  # Our SWC was lost: the satellite is still waiting for it
                self.send_directive("SWC")
                report.resent += 1
        return None
