/**
 * cmd.c
 *
 * Host commands over USB serial: one command per line, one JSON object per reply line, always with "ok". The host
 * side is hil/lib/tester.py, which documents each command. Shared by both tester firmwares (see tester_platform.h).
 */

#include "cmd.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ina226_model.h"
#include "mcp23017_model.h"
#include "pixel_model.h"
#include "tester_platform.h"
#include "pmb_model.h"
#include "stxg2_model.h"

#define LINE_MAX 128u
#define REPLY_MAX 4096u
#define MAX_TOKENS 6u
#define LOG_ENTRIES_PER_REPLY 48u

static char line[LINE_MAX];
static size_t line_len = 0;
static uint32_t last_command_ms = 0;
static char reply[REPLY_MAX];

static void send(int len) {
    if (len < 0) {
        return;
    }
    if ((size_t)len >= REPLY_MAX - 2) {
        len = snprintf(reply, REPLY_MAX, "{\"ok\":false,\"error\":\"reply too long\"}");
    }
    reply[len++] = '\n';
    platform_link_write(reply, (size_t)len);
}

static void error(const char *message) {
    send(snprintf(reply, REPLY_MAX, "{\"ok\":false,\"error\":\"%s\"}", message));
}

static void ok(void) {
    send(snprintf(reply, REPLY_MAX, "{\"ok\":true}"));
}

// Replies {"ok":true, <members>} from a function that writes the members (or returns -1 if it can't)
static void ok_with(int (*members)(char *, size_t)) {
    int n = snprintf(reply, REPLY_MAX, "{\"ok\":true,");
    const int added = members(reply + n, REPLY_MAX - (size_t)n);
    if (added < 0) {
        error("not available on this tester");
        return;
    }
    n += added;
    n += snprintf(reply + n, REPLY_MAX - (size_t)n, "}");
    send(n);
}

static bool parse_float(const char *s, float *value) {
    char *end;
    *value = strtof(s, &end);
    return end != s && *end == '\0';
}

static bool parse_uint(const char *s, uint32_t *value) {
    char *end;
    *value = (uint32_t)strtoul(s, &end, 0);
    return end != s && *end == '\0';
}

/* ---------- COMMANDS ---------- */

static int out_members(char *buf, size_t len) {
    int n = snprintf(buf, len, "\"armed\":%s,\"full_scale_mv\":%d,\"volts\":{", analog_out_armed() ? "true" : "false",
                     (int)(analog_out_full_scale() * 1000.0f));
    for (uint8_t out = 0; out < analog_out_count(); out++) {
        n += snprintf(buf + n, len - (size_t)n, "%s\"%s\":%d", out ? "," : "", analog_out_name(out), (int)(analog_out_get(out) * 1000.0f));
    }
    return n + snprintf(buf + n, len - (size_t)n, "}");
}

static void cmd_out(char **tok, size_t count) {
    if (count == 2 && strcmp(tok[1], "get") == 0) {
        ok_with(out_members);
        return;
    }
    float volts;
    if (count == 3 && strcmp(tok[1], "fullscale") == 0 && parse_float(tok[2], &volts)) {
        analog_out_set_full_scale(volts);
        ok_with(out_members);
        return;
    }
    if (count != 4 || strcmp(tok[1], "set") != 0 || !parse_float(tok[3], &volts)) {
        error("usage: out get | out set <name|all> <volts> | out fullscale <volts>");
        return;
    }
    if (strcmp(tok[2], "all") == 0) {
        for (uint8_t out = 0; out < analog_out_count(); out++) {
            analog_out_set(out, volts);
        }
    } else {
        const int out = analog_out_find(tok[2]);
        if (out < 0) {
            error("unknown output (this tester may not drive it)");
            return;
        }
        analog_out_set((uint8_t)out, volts);
    }
    ok();
}

static void cmd_pixel(char **tok, size_t count) {
    float isc, voc;
    uint32_t pixel = 0;
    if (count == 2 && strcmp(tok[1], "get") == 0) {
        int n = snprintf(reply, REPLY_MAX, "{\"ok\":true,\"pixels\":[");
        for (uint8_t p = 0; p < PIXEL_COUNT; p++) {
            pixel_get(p, &isc, &voc);
            n += snprintf(reply + n, REPLY_MAX - (size_t)n, "%s[%d,%d]", p ? "," : "", (int)isc, (int)voc);
        }
        n += snprintf(reply + n, REPLY_MAX - (size_t)n, "]}");
        send(n);
        return;
    }
    const bool all = count == 5 && strcmp(tok[2], "all") == 0;
    if (count != 5 || strcmp(tok[1], "set") != 0 || (!all && (!parse_uint(tok[2], &pixel) || pixel >= PIXEL_COUNT)) ||
        !parse_float(tok[3], &isc) || !parse_float(tok[4], &voc)) {
        error("usage: pixel get | pixel set <0-15|all> <isc_ua> <voc_mv>");
        return;
    }
    for (uint8_t p = 0; p < PIXEL_COUNT; p++) {
        if (all || p == pixel) {
            pixel_set(p, isc, voc);
        }
    }
    ok();
}

static int pmb_members(char *buf, size_t len) {
    int n = snprintf(buf, len, "\"pvd_crnt_mv\":%d,\"i2c_transactions\":%lu,\"i2c_nacks\":%lu,\"mcp23017\":{",
                     (int)(pmb_pvd_crnt_v() * 1000.0f), (unsigned long)pmb_transactions(), (unsigned long)pmb_nacks());
    n += mcp23017_json(buf + n, len - (size_t)n);
    n += snprintf(buf + n, len - (size_t)n, "},\"ina226\":{");
    n += ina226_json(buf + n, len - (size_t)n);
    return n + snprintf(buf + n, len - (size_t)n, "},\"log_count\":%lu", (unsigned long)ina226_log_count());
}

static void cmd_pmb(char **tok, size_t count) {
    if (count == 2 && strcmp(tok[1], "get") == 0) {
        ok_with(pmb_members);
        return;
    }
    uint32_t from = 0;
    if (count != 3 || strcmp(tok[1], "log") != 0 || !parse_uint(tok[2], &from)) {
        error("usage: pmb get | pmb log <from index>");
        return;
    }
    // Entries [from, next): each is [selected pixel mask, follower mV, current nA]. Entries already overwritten are skipped.
    int n = snprintf(reply, REPLY_MAX, "{\"ok\":true,\"entries\":[");
    const uint32_t total = ina226_log_count();
    uint32_t index = from;
    uint32_t sent = 0;
    for (; index < total && sent < LOG_ENTRIES_PER_REPLY; index++) {
        ina226_sample_t s;
        if (ina226_log_entry(index, &s)) {
            n += snprintf(reply + n, REPLY_MAX - (size_t)n, "%s[%u,%d,%ld]", sent ? "," : "", s.selected, s.follow_mv, (long)s.current_na);
            sent++;
        }
    }
    n += snprintf(reply + n, REPLY_MAX - (size_t)n, "],\"next\":%lu,\"total\":%lu}", (unsigned long)index, (unsigned long)total);
    send(n);
}

static void cmd_fault(char **tok, size_t count) {
    if (count != 3) {
        error("usage: fault <stxg2|ina226|mcp23017|pmb_bus> <mode>");
        return;
    }
    const char *const device = tok[1];
    const char *const mode = tok[2];
    if (strcmp(device, "stxg2") == 0) {
        static const char *const MODES[] = {"none", "nack", "bad_id", "hold_bus"};
        for (int i = 0; i < 4; i++) {
            if (strcmp(mode, MODES[i]) == 0) {
                stxg2_set_fault((stxg2_fault_t)i);
                ok();
                return;
            }
        }
    } else if (strcmp(device, "ina226") == 0) {
        static const char *const MODES[] = {"none", "nack", "no_cvrf", "bad_id"};
        for (int i = 0; i < 4; i++) {
            if (strcmp(mode, MODES[i]) == 0) {
                ina226_set_fault((ina226_fault_t)i);
                ok();
                return;
            }
        }
    } else if (strcmp(device, "mcp23017") == 0) {
        static const char *const MODES[] = {"none", "nack", "stuck"};
        for (int i = 0; i < 3; i++) {
            if (strcmp(mode, MODES[i]) == 0) {
                mcp23017_set_fault((mcp23017_fault_t)i);
                ok();
                return;
            }
        }
    } else if (strcmp(device, "pmb_bus") == 0 && (strcmp(mode, "none") == 0 || strcmp(mode, "hold_bus") == 0)) {
        platform_hold_pmb_bus(strcmp(mode, "hold_bus") == 0);
        ok();
        return;
    }
    error("unknown device or fault mode");
}

// Everything back to power-on: emulator registers, statistics, faults, outputs at 0 V and dark pixels, no recording.
// Keeps the arm state.
static void reset_all(void) {
    watch_stop(NULL, 0);
    stxg2_set_fault(STXG2_FAULT_NONE);
    ina226_set_fault(INA226_FAULT_NONE);
    mcp23017_set_fault(MCP23017_FAULT_NONE);
    platform_hold_pmb_bus(false);
    stxg2_reset();
    pmb_reset();
    for (uint8_t out = 0; out < analog_out_count(); out++) {
        analog_out_set(out, 0.0f);
    }
    for (uint8_t p = 0; p < PIXEL_COUNT; p++) {
        pixel_set(p, 0.0f, 0.0f);
    }
}

static void cmd_watch(char **tok, size_t count) {
    if (count == 2 && strcmp(tok[1], "start") == 0) {
        ok_with(watch_start);
    } else if (count == 2 && strcmp(tok[1], "stop") == 0) {
        ok_with(watch_stop);
    } else if (count == 2 && strcmp(tok[1], "get") == 0) {
        ok_with(watch_get);
    } else {
        error("usage: watch <start|stop|get>");
    }
}

static void execute(char *text) {
    char *tok[MAX_TOKENS];
    size_t count = 0;
    for (char *t = strtok(text, " \t"); t != NULL && count < MAX_TOKENS; t = strtok(NULL, " \t")) {
        tok[count++] = t;
    }
    if (count == 0) {
        return;
    }
    const char *const name = tok[0];
    if (strcmp(name, "ping") == 0) {
        send(snprintf(reply, REPLY_MAX, "{\"ok\":true,\"firmware\":\"%s\",\"protocol\":1,\"uptime_ms\":%lu}", platform_name(),
                      (unsigned long)platform_millis()));
    } else if (strcmp(name, "arm") == 0) {
        analog_out_arm(true);
        ok();
    } else if (strcmp(name, "disarm") == 0) {
        analog_out_arm(false);
        ok();
    } else if (strcmp(name, "out") == 0) {
        cmd_out(tok, count);
    } else if (strcmp(name, "pixel") == 0) {
        cmd_pixel(tok, count);
    } else if (strcmp(name, "pmb") == 0) {
        cmd_pmb(tok, count);
    } else if (strcmp(name, "stxg2") == 0 && count == 2 && strcmp(tok[1], "get") == 0) {
        ok_with(stxg2_json);
    } else if (strcmp(name, "fault") == 0) {
        cmd_fault(tok, count);
    } else if (strcmp(name, "reset") == 0) {
        reset_all();
        ok();
    } else if (strcmp(name, "watch") == 0) {
        cmd_watch(tok, count);
    } else if (strcmp(name, "bootloader") == 0) {
        analog_out_arm(false);
        ok();
        if (!platform_enter_bootloader()) {
            error("not available on this tester");
        }
    } else {
        error("unknown command");
    }
}

void cmd_poll(void) {
    char chunk[64];
    const size_t n = platform_link_read(chunk, sizeof(chunk));
    for (size_t i = 0; i < n; i++) {
        const char c = chunk[i];
        if (c == '\n' || c == '\r') {
            line[line_len] = '\0';
            last_command_ms = platform_millis();
            execute(line);
            line_len = 0;
        } else if (line_len < LINE_MAX - 1) {
            line[line_len++] = c;
        }
    }
}

uint32_t cmd_last_command_ms(void) {
    return last_command_ms;
}
