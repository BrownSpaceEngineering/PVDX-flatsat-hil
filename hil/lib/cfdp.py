"""The ground's side of CFDP for uplinking a command file: one acknowledged (class 2) transaction, as the satellite's
CFDP task receives it (src/tasks/cfdp/cfdp_task.c, cfdp_process_pdu() and the CFDP_RECV states).

    ground turn:    Metadata, File Data..., EOF
    satellite turn: ACK(EOF), then Finished (or a NAK for missing data, or for the Metadata if it was lost)
    ground turn:    ACK(Finished), and whatever was NAKed or never acknowledged

PDUs are built here (CCSDS 727.0-B-5 with the satellite's field sizes: 4-byte entity IDs and sequence numbers, no
CRC, small files) and the satellite's are read with PVDXos's own parsers (ccsds.parse_cfdp()); tests check every PDU
built here against those parsers too. Entity IDs and directive codes come from PVDXos's cfdp_pdu.h.
"""

import struct
from dataclasses import dataclass, field

import ccsds

ENTITY_ID_BYTES = 4  # cfdp_prepare_pdu_header() always sends 4-byte entity IDs and sequence numbers
SEQ_BYTES = 4
HEADER_BYTES = 4 + 2 * ENTITY_ID_BYTES + SEQ_BYTES
DIRECTION_TO_RECEIVER = 0
MODE_ACKNOWLEDGED = 0
CHECKSUM_MODULAR = 0
TRANSACTION_TERMINATED = 0b10


def modular_checksum(data):
    """CFDP's modular checksum, as cfdp_calculate_modular_checksum() computes it: the sum of the file's big-endian
    32-bit words, the last one padded with zeros."""
    data = bytes(data) + b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def header(pdu_type, data_length, seq, source, dest):
    c = ccsds.constants()
    first = (0b001 << 5) | (pdu_type << 4) | (DIRECTION_TO_RECEIVER << 3) | (MODE_ACKNOWLEDGED << 2)
    lengths = ((ENTITY_ID_BYTES - 1) << 4) | (SEQ_BYTES - 1)
    assert c["CFDP_VERSION_NUMBER"] == 0b001
    return struct.pack(">BHBIII", first, data_length, lengths, source, seq, dest)


def directive(code, body, seq, source, dest):
    return header(ccsds.constants()["CFDP_PDU_TYPE_DIRECTIVE"], 1 + len(body), seq, source, dest) + bytes([code]) + body


def metadata(file_size, seq, source, dest):
    """No file names: the satellite treats every file the ground sends as an uplink command file."""
    body = struct.pack(">BIBB", CHECKSUM_MODULAR & 0x0F, file_size, 0, 0)  # Closure not requested, empty LVs
    return directive(ccsds.constants()["CFDP_DIR_METADATA"], body, seq, source, dest)


def file_data(offset, data, seq, source, dest):
    body = struct.pack(">I", offset) + bytes(data)
    return header(ccsds.constants()["CFDP_PDU_TYPE_FILEDATA"], len(body), seq, source, dest) + body


def eof(checksum, file_size, seq, source, dest, condition=0):
    body = struct.pack(">BII", (condition & 0x0F) << 4, checksum, file_size)
    return directive(ccsds.constants()["CFDP_DIR_EOF"], body, seq, source, dest)


def ack(acked_directive, condition, seq, source, dest, subtype=1):
    """subtype 1 acknowledges a Finished PDU (CCSDS 727 5.2.4), as the satellite's own ACK(FIN) does."""
    body = bytes([((acked_directive & 0x0F) << 4) | (subtype & 0x0F), ((condition & 0x0F) << 4) | TRANSACTION_TERMINATED])
    return directive(ccsds.constants()["CFDP_DIR_ACK"], body, seq, source, dest)


@dataclass
class Uplink:
    """One uplink file, sent as a class 2 transaction over as many turns as it takes. The ground turn's PDUs come from
    turn_pdus(); every CFDP PDU heard from the satellite goes to receive()."""

    data: bytes
    seq: int
    segment: int = 0  # Largest file data per PDU; 0 for what fits in one space packet
    sent_pdus: int = 0
    resent_pdus: int = 0
    eof_acked: bool = False
    finished: dict = None  # The satellite's Finished PDU (parsed), once heard
    fin_acked: bool = False  # Our ACK(Finished) has been handed to the radio
    naks: list = field(default_factory=list)  # (start, end) ranges the satellite asked for again
    metadata_nak: bool = False
    events: list = field(default_factory=list)  # What happened, for the pass report
    turns: int = 0

    def __post_init__(self):
        c = ccsds.constants()
        self.source = c["ENTITY_ID_GROUND"]
        self.dest = c["ENTITY_ID_SPACECRAFT"]
        if not self.segment:
            self.segment = c["RADIO_PAYLOAD_MAX_SIZE"] - HEADER_BYTES - 4  # Header and the offset field
        self.checksum = modular_checksum(self.data)

    @property
    def done(self):
        return self.finished is not None and self.fin_acked

    @property
    def succeeded(self):
        """The satellite finished with no error. Its delivery code isn't checked here: the receive side never sets
        delivery_complete (only handle_finished_pdu() does, for sends), so its Finished always says "incomplete"
        (delivery_incomplete reports that)."""
        f = self.finished
        return f is not None and f["condition_code"] == ccsds.constants()["CFDP_COND_NOERROR"]

    @property
    def delivery_incomplete(self):
        return self.finished is not None and bool(self.finished["delivery_code"])

    def _file_data(self, start, end):
        return [file_data(off, self.data[off:min(end, off + self.segment)], self.seq, self.source, self.dest)
                for off in range(start, end, self.segment)]

    def turn_pdus(self):
        """The PDUs to send in the ground's next turn."""
        c = ccsds.constants()
        self.turns += 1
        pdus = []
        if self.turns == 1:
            pdus.append(metadata(len(self.data), self.seq, self.source, self.dest))
            pdus += self._file_data(0, len(self.data))
            pdus.append(eof(self.checksum, len(self.data), self.seq, self.source, self.dest))
            self.sent_pdus += len(pdus)
            return pdus
        if self.finished is not None:
            if not self.fin_acked:
                self.events.append("ACK(Finished) sent")
            # Re-sent every turn the satellite repeats its Finished, since it only does that when our ACK was lost
            pdus.append(ack(c["CFDP_DIR_FINISHED"], self.finished["condition_code"], self.seq, self.source, self.dest))
            self.fin_acked = True
        else:
            if self.metadata_nak:
                pdus.append(metadata(len(self.data), self.seq, self.source, self.dest))
                self.metadata_nak = False
            for start, end in self.naks:
                pdus += self._file_data(start, min(end, len(self.data)) if end else len(self.data))
            self.naks = []
            if not self.eof_acked:
                pdus.append(eof(self.checksum, len(self.data), self.seq, self.source, self.dest))
        self.resent_pdus += sum(1 for p in pdus if ccsds.parse_cfdp(p)["directive"] != c["CFDP_DIR_ACK"])
        self.sent_pdus += len(pdus)
        return pdus

    def receive(self, pdu):
        """Handles a CFDP PDU from the satellite. Returns its parse (ccsds.parse_cfdp()), or None if it isn't for this
        transaction."""
        c = ccsds.constants()
        f = ccsds.parse_cfdp(pdu)
        if f["error"] == 1 or f["transaction_seq"] != self.seq or f["source_entity_id"] != self.source:
            return None
        if f["error"]:
            self.events.append(f"PVDXos's parser rejects the satellite's own {f['directive']:#x} PDU")
        d = f["directive"]
        if d == c["CFDP_DIR_ACK"] and f["ack_directive"] == c["CFDP_DIR_EOF"]:
            if not self.eof_acked:
                self.events.append(f"ACK(EOF), condition {f['condition_code']}")
            self.eof_acked = True
        elif d == c["CFDP_DIR_FINISHED"]:
            if self.finished is not None:
                self.events.append("Finished repeated (our ACK was lost)")
                self.fin_acked = False
            else:
                self.events.append(f"Finished: condition {f['condition_code']}, delivery "
                                   f"{'incomplete' if f['delivery_code'] else 'complete'}")
            self.finished = f
        elif d == c["CFDP_DIR_NAK"]:
            for start, end in f["segment_requests"]:
                if start == 0 and end == 0:
                    self.metadata_nak = True
                else:
                    self.naks.append((start, end))
            self.events.append(f"NAK {f['segment_requests']}")
        else:
            self.events.append(f"unexpected PDU {d:#x}")
        return f
