/**
 * ground_api.c
 *
 * The ground station's side of the UHF link, built natively from PVDXos's own sources (src/ccsds/spp.c and uslp.c,
 * src/tasks/cfdp/cfdp_pdu.c and uplink_parse.c, src/telemetry/telemetry.c) so the ground wraps, unwraps, parses and
 * decodes exactly as the satellite does, whatever the radio code changes to. Loaded by hil/lib/ccsds.py; the satellite
 * side of each step is in src/tasks/uhf_radio/uhf_radio_task.c (uhf_rx_classify()) and src/tasks/cfdp/radio_routing.c
 * (spp_wrap()). Uses plain byte buffers and flat structs, so Python needs no copy of PVDXos's struct layouts.
 */

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "ccsds/ccsds.h"
#include "ccsds/radio_routing.h"
#include "ccsds/spp.h"
#include "ccsds/uslp.h"
#include "cfdp/cfdp_pdu.h"
#include "cfdp/uplink_parse.h"
#include "telemetry/telemetry.h"
#include "uhf_radio/pusp.h"
#include "uhf_radio/uhf_radio_task.h"

// Each failing stage of ground_unwrap(), so the host can count what went wrong
enum {
    GROUND_OK = 0,
    GROUND_ERR_ARGS = 1,
    GROUND_ERR_USLP = 2,     // uslp_transfer_frame_parse() rejected the frame (header or FECF)
    GROUND_ERR_TFDZ = 3,     // Not an unsegmented frame of space packets
    GROUND_ERR_SPP = 4,      // spp_packet_parse() rejected the packet
    GROUND_ERR_SPP_SIZE = 5, // The packet's length doesn't fit its secondary header
    GROUND_ERR_SPP_WRAP = 6,
    GROUND_ERR_USLP_WRAP = 7,
};

/* ---------- SPP and USLP ---------- */

// One received frame, flattened
typedef struct {
    uint8_t vcid;
    uint8_t map_id;
    uint8_t src_or_dest;
    uint8_t vc_frame_count;
    uint16_t scid;
    uint16_t apid;
    uint8_t packet_type;
    uint8_t secondary_header_flag;
    uint8_t sequence_flags;
    uint16_t sequence_count;
    uint32_t timestamp;
    uint32_t boot_timestamp;
    uint8_t callsign[SPP_SECONDARY_HEADER_CALLSIGN_SIZE];
    uint32_t data_offset; // Of the SPP user data field within the frame
    uint32_t data_len;
} ground_frame_t;

uint32_t ground_frame_size(void) {
    return sizeof(ground_frame_t);
}

uint32_t ground_gmap_id(uint8_t vcid) {
    return RADIO_GMAP_ID(vcid);
}

int ground_wrap(uint8_t vcid, uint16_t apid, uint8_t packet_type, uint16_t sequence_count, uint32_t timestamp,
                const uint8_t *callsign, const uint8_t *data, uint16_t len, uint8_t *out, uint32_t *out_len) {
    if (out == NULL || out_len == NULL || callsign == NULL || (data == NULL && len)) {
        return GROUND_ERR_ARGS;
    }
    spp_primary_packet_header_t primary = {
        .version_number = SPP_VERSION_NUMBER,
        .packet_type = packet_type,
        .secondary_header_flag = SPP_SECONDARY_HEADER_PRESENT,
        .application_process_id = apid,
        .sequence_flags = SPP_SEQ_FLAG_UNSEGMENTED_DATA,
        .sequence_count = sequence_count & 0x3FFF,
    };
    spp_secondary_packet_header_t secondary = {.timestamp = timestamp, .boot_timestamp = 0};
    memcpy(secondary.callsign, callsign, SPP_SECONDARY_HEADER_CALLSIGN_SIZE);

    uint8_t packet[RADIO_DOWNLINK_MAX_SIZE];
    size_t packet_len = 0;
    if (spp_packet_serialize(&primary, &secondary, data, len, packet, sizeof(packet), &packet_len)) {
        return GROUND_ERR_SPP_WRAP;
    }
    size_t frame_len = *out_len;
    if (uslp_mapp_request(out, &frame_len, packet, (uint16_t)packet_len, RADIO_GMAP_ID(vcid), SPP_VERSION_NUMBER, RADIO_SDU_ID,
                          USLP_QOS_EXPEDITED)) {
        return GROUND_ERR_USLP_WRAP;
    }
    *out_len = (uint32_t)frame_len;
    return GROUND_OK;
}

int ground_unwrap(uint8_t *frame, uint32_t len, ground_frame_t *out) {
    if (frame == NULL || out == NULL) {
        return GROUND_ERR_ARGS;
    }
    memset(out, 0, sizeof(*out));
    uslp_transfer_frame_view_t view;
    if (uslp_transfer_frame_parse(&view, frame, len)) {
        return GROUND_ERR_USLP;
    }
    out->vcid = view.primary_header.virtual_channel_id;
    out->map_id = view.primary_header.map_id;
    out->src_or_dest = view.primary_header.src_or_dest;
    out->vc_frame_count = (uint8_t)view.primary_header.vc_frame_count;
    out->scid = view.primary_header.spacecraft_id;
    if (view.data_field_header.tfdz_construction_rules != USLP_TFDZ_NO_SEGMENT ||
        view.data_field_header.protocol_identifier != USLP_UPID_SPACE_PACKETS) {
        return GROUND_ERR_TFDZ;
    }
    spp_packet_view_t packet;
    if (spp_packet_parse(&packet, view.datafield, view.datafield_len)) {
        return GROUND_ERR_SPP;
    }
    out->apid = packet.header.application_process_id;
    out->packet_type = packet.header.packet_type;
    out->secondary_header_flag = packet.header.secondary_header_flag;
    out->sequence_flags = packet.header.sequence_flags;
    out->sequence_count = packet.header.sequence_count;
    // data_length also counts the secondary header (see uhf_rx_classify())
    size_t data_len = (size_t)packet.header.data_length + 1;
    if (packet.header.secondary_header_flag) {
        if (data_len < SPP_SECONDARY_HEADER_SIZE) {
            return GROUND_ERR_SPP_SIZE;
        }
        data_len -= SPP_SECONDARY_HEADER_SIZE;
        out->timestamp = packet.secondary_header.timestamp;
        out->boot_timestamp = packet.secondary_header.boot_timestamp;
        memcpy(out->callsign, packet.secondary_header.callsign, SPP_SECONDARY_HEADER_CALLSIGN_SIZE);
    }
    out->data_offset = (uint32_t)((const uint8_t *)packet.data - frame);
    out->data_len = (uint32_t)data_len;
    return GROUND_OK;
}

/* ---------- Uplink files ---------- */

// Runs PVDXos's uplink parser on an uplink file. Returns -1 if it rejects it, else the number of commands, with each
// command's type in types (up to max_types)
int ground_parse_uplink(const uint8_t *buf, uint32_t len, uint16_t *types, uint32_t max_types) {
    static uplink_packet_t packet;
    if (uplink_packet_parse(&packet, buf, len)) {
        return -1;
    }
    for (uint32_t i = 0; i < packet.n_cmds && i < max_types; i++) {
        types[i] = packet.cmds[i].type;
    }
    return packet.n_cmds;
}

/* ---------- CFDP PDUs ---------- */

// Indices into ground_cfdp_parse()'s output, in the order of CFDP_FIELDS in ccsds.py
enum {
    CF_ERROR,
    CF_PDU_TYPE,
    CF_DIRECTION,
    CF_TRANSMISSION_MODE,
    CF_CRC,
    CF_LARGEFILE,
    CF_PDU_DATA_LENGTH,
    CF_ENTITY_ID_LEN,
    CF_SOURCE_ENTITY_ID,
    CF_TRANSACTION_SEQ,
    CF_DEST_ENTITY_ID,
    CF_HEADER_LEN,
    CF_DIRECTIVE,
    CF_ACK_DIRECTIVE,
    CF_ACK_SUBTYPE,
    CF_CONDITION_CODE,
    CF_TRANSACTION_STATUS,
    CF_DELIVERY_CODE,
    CF_FILE_STATUS,
    CF_START_OF_SCOPE,
    CF_END_OF_SCOPE,
    CF_SEGMENTS,
    CF_CHECKSUM,
    CF_FILESIZE,
    CF_CHECKSUM_TYPE,
    CF_CLOSURE_REQ,
    CF_FILE_LENGTH,
    CF_OFFSET,
    CF_DATA_LEN,
    CF_DATA_OFFSET,
    CF_COUNT,
};

// Parses a PDU with PVDXos's parsers, as cfdp_process_pdu() does. out holds CF_COUNT values, then (start, end) of
// each NAK segment request. CF_ERROR is 1 if the header was rejected, 2 if the body was
void ground_cfdp_parse(const uint8_t *raw, uint32_t len, long *out, uint32_t out_len) {
    memset(out, 0, out_len * sizeof(long));
    cfdp_pdu_header_t header;
    int header_len = cfdp_pdu_header_parse(raw, len, &header);
    if (header_len < 0 || header.pdu_data_length > len - (uint32_t)header_len) {
        out[CF_ERROR] = 1;
        return;
    }
    out[CF_PDU_TYPE] = header.pdu_type;
    out[CF_DIRECTION] = header.direction;
    out[CF_TRANSMISSION_MODE] = header.transmission_mode;
    out[CF_CRC] = header.crc;
    out[CF_LARGEFILE] = header.largefile;
    out[CF_PDU_DATA_LENGTH] = header.pdu_data_length;
    out[CF_ENTITY_ID_LEN] = header.entity_id_len;
    out[CF_SOURCE_ENTITY_ID] = header.source_entity_id;
    out[CF_TRANSACTION_SEQ] = header.transaction_seq;
    out[CF_DEST_ENTITY_ID] = header.dest_entity_id;
    out[CF_HEADER_LEN] = header_len;

    const uint8_t *body = raw + header_len;
    size_t body_len = header.pdu_data_length;
    if (header.pdu_type == CFDP_PDU_TYPE_FILEDATA) {
        cfdp_pdu_filedata_t fd;
        if (cfdp_pdu_filedata_parse(body, body_len, header.largefile, header.segment_metadata_field, &fd) < 0) {
            out[CF_ERROR] = 2;
            return;
        }
        out[CF_OFFSET] = fd.offset;
        out[CF_DATA_LEN] = fd.data.len;
        out[CF_DATA_OFFSET] = fd.data.len ? (long)(fd.data.data - raw) : 0;
        return;
    }
    if (body_len < 1) {
        out[CF_ERROR] = 2;
        return;
    }
    const uint8_t directive = body[0];
    out[CF_DIRECTIVE] = directive;
    body++;
    body_len--;
    int ok = 0;
    switch (directive) {
        case CFDP_DIR_ACK: {
            cfdp_pdu_ack_t ack;
            ok = cfdp_pdu_ack_parse(body, body_len, &ack);
            out[CF_ACK_DIRECTIVE] = ack.directive_code;
            out[CF_ACK_SUBTYPE] = ack.directive_subtype_code;
            out[CF_CONDITION_CODE] = ack.condition_code;
            out[CF_TRANSACTION_STATUS] = ack.transaction_status;
            break;
        }
        case CFDP_DIR_FINISHED: {
            cfdp_pdu_finished_t fin;
            ok = cfdp_pdu_finished_parse(body, body_len, &fin);
            out[CF_CONDITION_CODE] = fin.condition_code;
            out[CF_DELIVERY_CODE] = fin.delivery_code;
            out[CF_FILE_STATUS] = fin.file_status;
            break;
        }
        case CFDP_DIR_NAK: {
            cfdp_pdu_nak_t nak;
            ok = cfdp_pdu_nak_parse(body, body_len, &nak);
            out[CF_START_OF_SCOPE] = nak.start_of_scope;
            out[CF_END_OF_SCOPE] = nak.end_of_scope;
            uint32_t n = 0;
            for (size_t at = 8; ok >= 0 && at + 8 <= body_len; at += 8) {
                cfdp_pdu_segment_request_t seg;
                if (cfdp_pdu_segment_request_parse(body + at, body_len - at, &seg) < 0) {
                    break;
                }
                if (CF_COUNT + 2 * n + 1 < out_len) {
                    out[CF_COUNT + 2 * n] = seg.start_offset;
                    out[CF_COUNT + 2 * n + 1] = seg.end_offset;
                }
                n++;
            }
            out[CF_SEGMENTS] = n;
            break;
        }
        case CFDP_DIR_EOF: {
            cfdp_pdu_eof_t eof;
            ok = cfdp_pdu_eof_parse(body, body_len, header.largefile, &eof);
            out[CF_CONDITION_CODE] = eof.condition_code;
            out[CF_CHECKSUM] = eof.checksum;
            out[CF_FILESIZE] = eof.filesize;
            break;
        }
        case CFDP_DIR_METADATA: {
            cfdp_pdu_metadata_t meta;
            ok = cfdp_pdu_metadata_parse(body, body_len, &meta);
            out[CF_CHECKSUM_TYPE] = meta.checksum_type;
            out[CF_CLOSURE_REQ] = meta.closure_req;
            out[CF_FILE_LENGTH] = meta.file_length;
            break;
        }
        default:
            break; // Prompt, keep alive: only the directive code is reported
    }
    if (ok < 0) {
        out[CF_ERROR] = 2;
    }
}

/* ---------- Telemetry layout ---------- */

// Every field of tel_downlink_t. A renamed or removed field breaks the build; a field added to tel_downlink_t but not
// here shows up as bytes of the serialized downlink that no field covers (ground_telemetry_probe())
typedef struct {
    const char *name;
    size_t offset;    // In tel_downlink_t
    size_t elem_size; // In memory
    uint16_t count;   // 1, or the length of an array
    char kind;        // 'u', 'i', 'f', 'b'; enums are 'u' (their compatible type) and are sent as one byte
} tel_field_t;

#define TEL_TYPE(expr)                                                                                                 \
    _Generic((expr), bool: 'b', float: 'f', double: 'f', int8_t: 'i', int16_t: 'i', int32_t: 'i', uint8_t: 'u',        \
             uint16_t: 'u', uint32_t: 'u', default: 'u')
#define TEL_MEMBER(path) (((tel_downlink_t *)0)->path)
#define TEL_SCALAR(path) {#path, offsetof(tel_downlink_t, path), sizeof(TEL_MEMBER(path)), 1, TEL_TYPE(TEL_MEMBER(path))}
#define TEL_ARRAY(path)                                                                                                \
    {#path, offsetof(tel_downlink_t, path), sizeof(TEL_MEMBER(path)[0]),                                               \
     (uint16_t)(sizeof(TEL_MEMBER(path)) / sizeof(TEL_MEMBER(path)[0])), TEL_TYPE(TEL_MEMBER(path)[0])}

static const tel_field_t TEL_FIELDS[] = {
    TEL_SCALAR(timestamp),
    TEL_SCALAR(boot_timestamp),
    TEL_SCALAR(last_bootloader),
    TEL_SCALAR(magnetometer.revid_register),
    TEL_SCALAR(magnetometer.bist_register),
    TEL_ARRAY(magnetometer.raw_readings),
    TEL_ARRAY(magnetometer.gain_adjusted_readings),
    TEL_ARRAY(magnetorquer.duty),
    TEL_ARRAY(photodiode.raw_readings),
    TEL_ARRAY(gyro.raw_rate),
    TEL_ARRAY(gyro.rate),
    TEL_SCALAR(gyro.health),
    TEL_SCALAR(camera.health),
    TEL_SCALAR(camera.last_capture_succeeded),
    TEL_SCALAR(camera.resolution),
    TEL_SCALAR(display_health),
    TEL_SCALAR(uhf_health),
    TEL_SCALAR(sband.health),
    TEL_SCALAR(sband.last_transmission_timestamp),
    TEL_ARRAY(eps.charge_percent),
    TEL_ARRAY(eps.voltage),
    TEL_ARRAY(eps.current),
    TEL_ARRAY(eps.temperature),
    TEL_ARRAY(state_machine.state_history),
    TEL_ARRAY(state_machine.transition_times),
    TEL_ARRAY(state_machine.input_variables),
    TEL_ARRAY(adcs.position),
    TEL_ARRAY(adcs.attitude),
    TEL_ARRAY(adcs.gyro_bias),
    TEL_ARRAY(adcs.covariance_diag),
    TEL_SCALAR(adcs.opmode),
    TEL_SCALAR(adcs.flags),
    TEL_SCALAR(adcs.filter_resets),
    TEL_SCALAR(adcs.tle_epoch_unix_time),
    TEL_ARRAY(command_timestamps),
    TEL_SCALAR(mram.status),
    TEL_SCALAR(mram.reflash_count),
    TEL_ARRAY(mram.reflash_timestamps),
    TEL_ARRAY(os_errors.error_codes),
    TEL_ARRAY(os_errors.error_timestamps),
};
#define TEL_FIELD_COUNT (sizeof(TEL_FIELDS) / sizeof(TEL_FIELDS[0]))

// Where one value of the downlink is sent
typedef struct {
    uint16_t field; // Index into TEL_FIELDS
    uint16_t index; // Element of an array
    uint16_t offset;
    uint8_t width;
    char kind;
    uint8_t big_endian; // The bytes sent are the value's low `width` bytes, most significant first
} tel_element_t;

uint32_t ground_telemetry_field_count(uint32_t field) {
    return field < TEL_FIELD_COUNT ? TEL_FIELDS[field].count : 0;
}

const char *ground_telemetry_field_name(uint32_t field) {
    return field < TEL_FIELD_COUNT ? TEL_FIELDS[field].name : NULL;
}

// Fills an element of the downlink with a marker: bytes 0x81, 0x82, ... (all non-zero, so every byte sent changes),
// or true for a bool. Returns the value it stands for, as the serializer should send it
static uint64_t tel_mark(tel_downlink_t *d, const tel_field_t *f, uint16_t index) {
    uint8_t *p = (uint8_t *)d + f->offset + (size_t)index * f->elem_size;
    if (f->kind == 'b') {
        *(bool *)p = true;
        return 1;
    }
    uint64_t value = 0;
    for (size_t i = 0; i < f->elem_size; i++) {
        p[i] = (uint8_t)(0x81 + i);
        value |= (uint64_t)p[i] << (8 * i); // Little-endian in memory on both the Mac and the SAMD51
    }
    return value;
}

// Learns the downlink's layout from telemetry_downlink_serialize(): for each element of each field, serializes a zeroed
// downlink with only that element set, and records which bytes changed. covered_to is set to one past the last
// non-zero byte when every byte of tel_downlink_t is set, i.e. where the serialized fields end. Returns the number of
// elements, or a negative code
int ground_telemetry_probe(tel_element_t *out, uint32_t max, int *covered_to) {
    static tel_downlink_t d;
    static uint8_t zero[TELEMETRY_DOWNLINK_SIZE], buf[TELEMETRY_DOWNLINK_SIZE];
    memset(&d, 0, sizeof(d));
    if (telemetry_downlink_serialize(&d, zero, sizeof(zero))) {
        return -1;
    }
    uint32_t n = 0;
    for (uint32_t fi = 0; fi < TEL_FIELD_COUNT; fi++) {
        const tel_field_t *f = &TEL_FIELDS[fi];
        for (uint16_t k = 0; k < f->count; k++) {
            if (n >= max) {
                return -2;
            }
            memset(&d, 0, sizeof(d));
            const uint64_t value = tel_mark(&d, f, k);
            if (telemetry_downlink_serialize(&d, buf, sizeof(buf))) {
                return -3;
            }
            int first = -1, last = -1;
            for (int i = 0; i < TELEMETRY_DOWNLINK_SIZE; i++) {
                if (buf[i] != zero[i]) {
                    first = first < 0 ? i : first;
                    last = i;
                }
            }
            if (first < 0) {
                return -4; // The serializer doesn't send this field at all
            }
            tel_element_t *e = &out[n++];
            e->field = (uint16_t)fi;
            e->index = k;
            e->offset = (uint16_t)first;
            e->width = (uint8_t)(last - first + 1);
            e->kind = f->kind;
            e->big_endian = e->width <= 8;
            for (int i = 0; i < e->width && e->big_endian; i++) {
                const int shift = 8 * (e->width - 1 - i);
                e->big_endian = buf[first + i] == (uint8_t)(value >> shift);
            }
        }
    }
    memset(&d, 0x81, sizeof(d));
    for (uint32_t fi = 0; fi < TEL_FIELD_COUNT; fi++) {
        if (TEL_FIELDS[fi].kind == 'b') {
            for (uint16_t k = 0; k < TEL_FIELDS[fi].count; k++) {
                tel_mark(&d, &TEL_FIELDS[fi], k); // A bool must hold 0 or 1
            }
        }
    }
    if (telemetry_downlink_serialize(&d, buf, sizeof(buf))) {
        return -3;
    }
    *covered_to = 0;
    for (int i = 0; i < TELEMETRY_DOWNLINK_SIZE; i++) {
        if (buf[i]) {
            *covered_to = i + 1;
        }
    }
    return (int)n;
}

// Serializes a downlink with known values, for the host's tests of its decoder (hil/lib/telemetry.py). Returns the
// serializer's result (true on failure)
bool ground_telemetry_sample(uint8_t *buf, uint32_t len) {
    static tel_downlink_t d;
    memset(&d, 0, sizeof(d));
    d.timestamp = 1800000000u;
    d.boot_timestamp = 1799990000u;
    d.last_bootloader = 2;
    d.magnetometer.raw_readings[1] = -123456;
    d.magnetometer.gain_adjusted_readings[2] = -45.5f;
    d.photodiode.raw_readings[PHD_COUNT - 1] = 4095;
    d.gyro.health = TEL_DEVICE_STATUS_BROKEN;
    d.camera.health = TEL_DEVICE_STATUS_DISABLED;
    d.camera.last_capture_succeeded = true;
    d.eps.charge_percent[0] = 87.5f;
    d.eps.voltage[3] = 7.25f;
    d.eps.current[1] = -0.5f;
    d.state_machine.state_history[0] = 3;
    d.state_machine.transition_times[0] = 1799999999u;
    d.adcs.opmode = TEL_ADCS_OPMODE_DETUMBLE_ONLY;
    d.adcs.flags = TEL_ADCS_FLAG_UTC_VALID | TEL_ADCS_FLAG_IN_SUNLIGHT;
    d.adcs.filter_resets = 513;
    d.command_timestamps[0] = 1799999000u;
    d.os_errors.error_timestamps[7] = 0xA1B2C3D4u;
    return telemetry_downlink_serialize(&d, buf, len);
}

/* ---------- Constants ---------- */

// Constants the host reads instead of copying them. Names are the PVDXos macro and enum member names; the table is
// generated by ccsds.py from the headers in HEADER_MACROS and ENUMS
typedef struct {
    const char *name;
    long value;
} ground_constant_t;

static const ground_constant_t CONSTANTS[] = {
#include "ground_constants.inc"
    {"TEL_FIELD_COUNT", TEL_FIELD_COUNT},
};

uint32_t ground_constant_count(void) {
    return sizeof(CONSTANTS) / sizeof(CONSTANTS[0]);
}

const char *ground_constant_name(uint32_t i) {
    return i < ground_constant_count() ? CONSTANTS[i].name : NULL;
}

long ground_constant_value(uint32_t i) {
    return i < ground_constant_count() ? CONSTANTS[i].value : 0;
}
