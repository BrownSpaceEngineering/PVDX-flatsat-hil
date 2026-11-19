/**
 * ground_api.c
 *
 * The ground station's side of the UHF framing, built natively from PVDXos's own CCSDS sources (src/ccsds/spp.c and
 * uslp.c) so the ground wraps and unwraps frames exactly as the satellite does, whatever radio-task changes. Loaded by
 * hil/lib/ccsds.py; the satellite side of each step is in src/tasks/uhf_radio/uhf_radio_task.c (spp_wrap(),
 * uhf_rx_process()). Uses plain byte buffers and ground_frame_t, so Python needs no copy of PVDXos's struct layouts.
 */

#include <string.h>

#include "ccsds/ccsds.h"
#include "ccsds/radio_routing.h"
#include "ccsds/spp.h"
#include "ccsds/uslp.h"
#include "cfdp/uplink_parse.h"

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
    // data_length also counts the secondary header (see uhf_rx_process())
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

// Constants the host reads instead of copying them. Names are the PVDXos macro names
typedef struct {
    const char *name;
    long value;
} ground_constant_t;

static const ground_constant_t CONSTANTS[] = {
    {"CCSDS_SCID", CCSDS_SCID},
    {"CCSDS_APID_CFDP", CCSDS_APID_CFDP},
    {"CCSDS_APID_PUSP", CCSDS_APID_PUSP},
    {"CCSDS_APID_TELEMETRY", CCSDS_APID_TELEMETRY},
    {"SPP_PACKET_TYPE_REPORTING", SPP_PACKET_TYPE_REPORTING},
    {"SPP_PACKET_TYPE_REQUESTING", SPP_PACKET_TYPE_REQUESTING},
    {"SPP_SECONDARY_HEADER_CALLSIGN_SIZE", SPP_SECONDARY_HEADER_CALLSIGN_SIZE},
    {"RADIO_PAYLOAD_MAX_SIZE", RADIO_PAYLOAD_MAX_SIZE},
    {"RADIO_DOWNLINK_MAX_SIZE", RADIO_DOWNLINK_MAX_SIZE},
    {"UHF_FREQ_HZ", UHF_FREQ_HZ},
    {"UHF_RETRY_MS", UHF_RETRY_MS},
    {"UHF_MAX_RETRIES", UHF_MAX_RETRIES},
    {"UHF_LINK_TIMEOUT_MS", UHF_LINK_TIMEOUT_MS},
    {"UHF_TX_WINDOW_MS", UHF_TX_WINDOW_MS},
    {"UHF_BEACON_PERIOD_MS", UHF_BEACON_PERIOD_MS},
    {"UHF_BEACON_BURST_MS", UHF_BEACON_BURST_MS},
    {"UHF_PHY_CONFIG", UHF_PHY_CONFIG},
    {"SYN", SYN},
    {"SYN_ACK", SYN_ACK},
    {"SRT", SRT},
    {"YLD", YLD},
    {"YLD_ACK", YLD_ACK},
    {"SWC", SWC},
    {"RADIO_STREAM_PEROVSKITE", RADIO_STREAM_PEROVSKITE},
    {"RADIO_STREAM_IMAGE", RADIO_STREAM_IMAGE},
    {"RADIO_STREAM_TELEMETRY", RADIO_STREAM_TELEMETRY},
    {"RADIO_STREAM_CFDP_REPLY", RADIO_STREAM_CFDP_REPLY},
    {"RADIO_STREAM_UPLINK_CMD", RADIO_STREAM_UPLINK_CMD},
    {"RADIO_STREAM_PUSP", RADIO_STREAM_PUSP},
    {"RADIO_STREAM_COUNT", RADIO_STREAM_COUNT},
    {"UPLINK_PREAMBLE_SIZE", UPLINK_PREAMBLE_SIZE},
    {"UPLINK_CMD_HEADER_SIZE", UPLINK_CMD_HEADER_SIZE},
    {"UPLINK_CALLSIGN_LENGTH", UPLINK_CALLSIGN_LENGTH},
    {"UPLINK_MAX_CMDS", UPLINK_MAX_CMDS},
    {"ENABLE_DISABLE_DEVICE", ENABLE_DISABLE_DEVICE},
    {"SET_DEVICE_BROKEN_UNBROKEN", SET_DEVICE_BROKEN_UNBROKEN},
    {"UPDATE_DISPLAY", UPDATE_DISPLAY},
    {"SLEEP_TRANSMIT", SLEEP_TRANSMIT},
    {"DISABLE_TRANSMIT", DISABLE_TRANSMIT},
    {"REBOOT", REBOOT},
    {"TAKE_PHOTO", TAKE_PHOTO},
    {"SEND_PHOTO", SEND_PHOTO},
    {"SET_KEPLER_COEFF", SET_KEPLER_COEFF},
    {"SET_UNIX_TIME", SET_UNIX_TIME},
    {"SET_ADCS_OP_MODE", SET_ADCS_OP_MODE},
    {"ADCS_UPDATE_PARAMS", ADCS_UPDATE_PARAMS},
    {"SET_PWR_MODE", SET_PWR_MODE},
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
