"""The UHF ground station: the ground's side of the satellite's radio protocol, over a radio from hackrf.py.

Framing is PVDXos's own (ccsds.py). The link protocol is PUSP (src/tasks/uhf_radio/pusp.h), whose satellite side is
src/tasks/uhf_radio/uhf_process.c; this is its mirror image:

    open:      SYN ->              <- SYN_ACK        SRT ->            (the ground has the turn)
    handover:  YLD ->              <- YLD_ACK        SWC ->            (whoever has the turn gives it)

The side waiting for a reply to its own directive re-sends it every UHF_RETRY_MS, up to UHF_MAX_RETRIES times; the
other side re-sends its reply only when a duplicate shows it was lost. There is no close: a side that hears nothing
from the other for UHF_LINK_TIMEOUT_MS drops the link. Outside a link the satellite sends telemetry beacon bursts.

Each PUSP packet is the directive and a notification byte (pusp_packet_t); the ground sends no notifications. A pass
can carry a CFDP uplink (cfdp.Uplink): its PDUs go out in the ground's turns on the uplink command VC, and the
satellite's replies (on the CFDP reply VC, in its turns) go back to it. Every frame the ground sends carries the time,
which the satellite sets its clock from (uhf_rx_classify()).

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
    fec: str = ""  # How a coded frame was decoded (mrfsk.Received.fec)
    corrected_bits: int = None
    coded_bits: object = None  # The frame's coded bits, kept for fit_rsc.py


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
    uplink: object = None  # The cfdp.Uplink sent in the pass, if any


class Ground:
    def __init__(self, radio, on_receive=None):
        self.radio = radio
        self.on_receive = on_receive  # Called with every Received, in and out of passes
        c = ccsds.constants()
        self.c = c
        routes = ccsds.routes()
        self.vc = dict(routes)
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
        self.radio.send(ccsds.wrap(self.vc[stream], apid, data, seq, timestamp=time.time()))

    def send_directive(self, name, notifs=0):
        self.send("RADIO_STREAM_PUSP", self.c["CCSDS_APID_PUSP"], bytes([self.directive[name], notifs]))

    def receive(self, timeout):
        """The next frame the radio heard, unwrapped (Received), or None after timeout seconds."""
        raw = self.radio.receive(timeout)
        if raw is None:
            return None
        rx = Received(getattr(raw, "received_at", time.time()), None, fcs_ok=raw.fcs_ok, coded=raw.coded,
                      power_db=raw.power_db, freq_offset_hz=raw.freq_offset_hz, fec=getattr(raw, "fec", ""),
                      corrected_bits=getattr(raw, "corrected_bits", None), coded_bits=getattr(raw, "coded_bits", None))
        if raw.coded and not raw.fcs_ok:
            rx.error = "FEC-coded frame that didn't decode"
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

    def is_cfdp(self, rx):
        f = rx.frame if rx else None
        return f is not None and f.vcid == self.vc["RADIO_STREAM_CFDP_REPLY"] and f.apid == self.c["CCSDS_APID_CFDP"]

    def _heard(self, report, rx):
        report.received.append(rx)
        if report.uplink is not None and self.is_cfdp(rx):
            report.uplink.receive(rx.frame.data)

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
                self._heard(report, rx)
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

    def run_pass(self, duration_s, uplink=None, turns=1, max_extra_turns=3):
        """Opens a link and trades turns with the satellite: `turns` handovers each way, plus up to max_extra_turns
        more while an uplink (cfdp.Uplink) isn't done. In each of the ground's turns it sends the uplink's PDUs, and
        everything the satellite sends in its turns is collected (and CFDP PDUs passed to the uplink). Then it stops
        transmitting and lets the link time out. Returns a PassReport."""
        report = PassReport(time.time(), uplink=uplink)
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
        turn = 0
        while turn < turns or (uplink is not None and not uplink.done and turn < turns + max_extra_turns):
            turn += 1
            # Our turn: send, then yield
            if uplink is not None:
                for pdu in uplink.turn_pdus():
                    self.send("RADIO_STREAM_UPLINK_CMD", self.c["CCSDS_APID_CFDP"], pdu)
                    report.sent += 1
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
        if uplink is not None and not uplink.done:
            report.failure = "the uplink didn't finish"
        return report

    def _collect_until_yield(self, report, deadline):
        window = self.c["UHF_TX_WINDOW_MS"] / 1000 + self.link_timeout_s / 2
        end = min(deadline, time.monotonic() + window)
        while time.monotonic() < end:
            rx = self.receive(0.05)
            if rx is None:
                continue
            self._heard(report, rx)
            name = self.directive_of(rx)
            if name == "YLD":
                return rx
            if name == "YLD_ACK":  # Our SWC was lost: the satellite is still waiting for it
                self.send_directive("SWC")
                report.resent += 1
        return None
