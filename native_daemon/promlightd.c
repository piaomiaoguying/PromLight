/**
 * promlightd - ARM64 原生 PromLight 守护进程
 *
 * 纯 C 实现，无 Python/Node 依赖。
 * 功能：
 *   - TCP 端口 47800：接收 agent_hook JSON 命令
 *   - 加载 events.json（事件→宏→命令映射）
 *   - 编译北向命令（led green on 等）为 5E5E 帧
 *   - 通过 IOHIDDeviceSetReport 写入 PromLight 硬件
 *   - session 管理（多 session 支持、idle 恢复）
 *   - HTTP 端口 7800：WebUI / API
 *
 * 编译: clang -o promlightd promlightd.c -framework IOKit -framework CoreFoundation -arch arm64
 */

#include <IOKit/hid/IOHIDManager.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <pthread.h>

// ── 常量 ───────────────────────────────────────────────────
#define CTRL_PORT 47800
#define HTTP_PORT 7800
#define MAX_LINE 1024
#define MAX_FRAMES 32
#define MAX_SESSIONS 64
#define MAX_PATH 512
#define FRAME_SIZE 64
#define FRAME_MAGIC "\x5E\x5E"
#define FRAME_MAGIC_LEN 2
#define MAX_EVENTS 128
#define MAX_MACROS 64
#define MAX_CONFIG_PATHS 8

// ── 5E5E 协议常量 ─────────────────────────────────────────
#define CMD_READ   0x02
#define CMD_WRITE  0x03
#define CMD_LED    0x04

#define CH_GREEN   0x01
#define CH_YELLOW  0x02
#define CH_RED     0x04
#define CH_ALL     0xFF

#define ACT_OFF    0x00
#define ACT_ON     0x01
#define ACT_BLINK  0x02

#define TLV_ONLY   0x01
#define TLV_COUNT  0x02
#define TLV_FREQ   0x03
#define TLV_FADE   0x04

#define ADDR_BRIGHTNESS 0x01
#define ADDR_FREQUENCY  0x02
#define ADDR_FADETIME   0x03
#define ADDR_ADVTIMEOUT 0x04

// ── 数据结构 ───────────────────────────────────────────────
typedef struct {
    IOHIDDeviceRef device;
    IOHIDManagerRef manager;
    pthread_mutex_t lock;
    bool opened;
} HidCtx;

typedef struct {
    char name[128];
    char command[MAX_LINE];
} Macro;

typedef struct {
    char name[128];
    char macro_name[128];
} EventEntry;

typedef struct {
    Macro macros[MAX_MACROS];
    int macro_count;
    EventEntry events[MAX_EVENTS];
    int event_count;
    char loaded_path[MAX_PATH];
    bool loaded;
} Config;

typedef struct {
    char session_id[128];
    char state[MAX_LINE];
    bool active;
} Session;

typedef struct {
    Session sessions[MAX_SESSIONS];
    int count;
    pthread_mutex_t lock;
} SessionManager;

// 帧编译结果
typedef struct {
    uint8_t data[FRAME_SIZE];
    int len;
} Frame;

// 编译脚本的一步
typedef struct {
    enum { STEP_FRAME, STEP_DELAY } type;
    union {
        Frame frame;
        int delay_ms;
    };
} Step;

typedef struct {
    Step steps[MAX_FRAMES];
    int count;
} Script;

// 全局状态
static HidCtx g_hid = {0};
static Config g_config = {0};
static SessionManager g_sessions = {0};
static volatile bool g_running = true;

// ── HID I/O ────────────────────────────────────────────────
static void hid_close_locked(void) {
    if (g_hid.device) {
        IOHIDDeviceClose(g_hid.device, kIOHIDOptionsTypeSeizeDevice);
        g_hid.device = NULL;
    }
    if (g_hid.manager) {
        CFRelease(g_hid.manager);
        g_hid.manager = NULL;
    }
    g_hid.opened = false;
}

static bool hid_open(void) {
    pthread_mutex_lock(&g_hid.lock);
    if (g_hid.opened && g_hid.device) {
        pthread_mutex_unlock(&g_hid.lock);
        return true;
    }

    g_hid.manager = IOHIDManagerCreate(kCFAllocatorDefault, 0);
    if (!g_hid.manager) { pthread_mutex_unlock(&g_hid.lock); return false; }

    CFMutableDictionaryRef dict = CFDictionaryCreateMutable(NULL, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    int v = 0x07D7, p = 0x6B01;
    CFNumberRef nv = CFNumberCreate(NULL, kCFNumberIntType, &v);
    CFNumberRef np = CFNumberCreate(NULL, kCFNumberIntType, &p);
    CFDictionarySetValue(dict, CFSTR("VendorID"), nv);
    CFDictionarySetValue(dict, CFSTR("ProductID"), np);
    CFRelease(nv); CFRelease(np);

    IOHIDManagerSetDeviceMatching(g_hid.manager, dict);
    IOHIDManagerScheduleWithRunLoop(g_hid.manager, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, false);

    CFSetRef devs = IOHIDManagerCopyDevices(g_hid.manager);
    if (!devs || CFSetGetCount(devs) == 0) {
        fprintf(stderr, "[hid] No PromLight device found\n");
        if (devs) CFRelease(devs);
        CFRelease(g_hid.manager); g_hid.manager = NULL;
        pthread_mutex_unlock(&g_hid.lock);
        return false;
    }

    IOHIDDeviceRef dev = NULL;
    CFSetGetValues(devs, (const void**)&dev);
    IOReturn ret = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeSeizeDevice);
    if (ret != kIOReturnSuccess) {
        fprintf(stderr, "[hid] Open failed: %d\n", ret);
        CFRelease(devs); CFRelease(g_hid.manager); g_hid.manager = NULL;
        pthread_mutex_unlock(&g_hid.lock);
        return false;
    }

    // HIDIdleTime = 0
    int idle = 0;
    CFNumberRef idleNum = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &idle);
    IOHIDDeviceSetProperty(dev, CFSTR("HIDIdleTime"), idleNum);
    CFRelease(idleNum);

    g_hid.device = dev;
    g_hid.opened = true;
    CFRelease(devs);
    fprintf(stderr, "[hid] Device opened\n");
    pthread_mutex_unlock(&g_hid.lock);
    return true;
}

static bool hid_send(const uint8_t *data, int len) {
    if (!g_hid.opened || !g_hid.device) return false;
    pthread_mutex_lock(&g_hid.lock);

    uint8_t report[FRAME_SIZE];
    memset(report, 0, FRAME_SIZE);
    int copy = len < FRAME_SIZE ? len : FRAME_SIZE;
    memcpy(report, data, copy);

    IOReturn ret = IOHIDDeviceSetReport(g_hid.device, kIOHIDReportTypeOutput, 2, report, FRAME_SIZE);
    bool ok = (ret == kIOReturnSuccess);
    if (!ok) fprintf(stderr, "[hid] SetReport error: %d\n", ret);

    pthread_mutex_unlock(&g_hid.lock);
    return ok;
}

static void hid_reopen(void) {
    hid_close_locked();
    hid_open();
}

// ── JSON 解析器（极简，仅解析扁平 key-value） ────────────────
// {"cmd":"led green on","message":"test","session":"s1","event":"UserPromptSubmit","release_session":true}
static char *json_get_string(const char *json, const char *key, char *out, int out_size) {
    if (!json || !key) return NULL;
    char pattern[MAX_LINE];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;
    p = strchr(p + strlen(key) + 2, ':');
    if (!p) return NULL;
    p++;
    while (*p && (*p == ' ' || *p == '\t')) p++;
    if (*p != '"') return NULL;
    p++;
    int i = 0;
    while (*p && *p != '"' && i < out_size - 1) {
        if (*p == '\\' && *(p+1)) { p++; }
        out[i++] = *p++;
    }
    out[i] = 0;
    return out;
}

static bool json_get_bool(const char *json, const char *key, bool def) {
    if (!json) return def;
    char val[16] = {0};
    char pattern[MAX_LINE];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return def;
    p = strchr(p + strlen(key) + 2, ':');
    if (!p) return def;
    p++;
    while (*p && (*p == ' ' || *p == '\t')) p++;
    return (strncmp(p, "true", 4) == 0);
}

// ── Config（events.json） ──────────────────────────────────
static char *trim(char *s) {
    while (*s == ' ' || *s == '\t') s++;
    char *e = s + strlen(s) - 1;
    while (e > s && (*e == ' ' || *e == '\t' || *e == '\n' || *e == '\r')) e--;
    *(e+1) = 0;
    return s;
}

static bool config_load(Config *cfg) {
    const char *paths[] = {
        "events.json",
        "../events.json",
        "/Users/zs/Applications/PromLight/events.json",
        "~/.promlight/events.json",
        "/etc/promlight/events.json",
    };

    for (int pi = 0; pi < 5; pi++) {
        char path[MAX_PATH];
        const char *src = paths[pi];
        if (src[0] == '~') {
            const char *home = getenv("HOME");
            if (!home) continue;
            snprintf(path, sizeof(path), "%s%s", home, src + 1);
        } else {
            snprintf(path, sizeof(path), "%s", src);
        }

        FILE *f = fopen(path, "r");
        if (!f) continue;

        // Read entire file
        char *buf = NULL;
        long fsize;
        fseek(f, 0, SEEK_END);
        fsize = ftell(f);
        rewind(f);
        buf = malloc(fsize + 1);
        if (!buf) { fclose(f); continue; }
        fread(buf, 1, fsize, f);
        buf[fsize] = 0;
        fclose(f);

        // Parse: "macros" and "events" sections
        // Find "macros": { ... } block
        cfg->macro_count = 0;
        cfg->event_count = 0;

        char *mstart = strstr(buf, "\"macros\"");
        char *estart = strstr(buf, "\"events\"");

        if (mstart) {
            // Find opening { after "macros":
            mstart = strchr(mstart, '{');
            if (mstart) {
                mstart++;
                char *mend = mstart;
                int brace = 1;
                while (*mend && brace > 0) {
                    if (*mend == '{') brace++;
                    if (*mend == '}') brace--;
                    if (brace > 0) mend++;
                }

                // Parse key-value pairs in macros
                char *p = mstart;
                while (p < mend && cfg->macro_count < MAX_MACROS) {
                    p = strchr(p, '"');
                    if (!p || p >= mend) break;
                    p++;
                    int ni = 0;
                    while (*p && *p != '"' && ni < 127) cfg->macros[cfg->macro_count].name[ni++] = *p++;
                    cfg->macros[cfg->macro_count].name[ni] = 0;
                    if (*p) p++;
                    p = strchr(p, ':');
                    if (!p) break;
                    p++;
                    while (*p && (*p == ' ' || *p == '\t')) p++;
                    if (*p == '"') {
                        p++;
                        int ci = 0;
                        while (*p && *p != '"' && ci < MAX_LINE-1) {
                            if (*p == '\\' && *(p+1)) p++;
                            cfg->macros[cfg->macro_count].command[ci++] = *p++;
                        }
                        cfg->macros[cfg->macro_count].command[ci] = 0;
                        if (*p) p++; // skip closing quote
                        cfg->macro_count++;

                        // skip comma after value
                        while (*p && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',')) p++;
                    }
                }
            }
        }

        if (estart) {
            estart = strchr(estart, '{');
            if (estart) {
                estart++;
                char *eend = estart;
                int brace = 1;
                while (*eend && brace > 0) {
                    if (*eend == '{') brace++;
                    if (*eend == '}') brace--;
                    if (brace > 0) eend++;
                }

                char *p = estart;
                while (p < eend && cfg->event_count < MAX_EVENTS) {
                    p = strchr(p, '"');
                    if (!p || p >= eend) break;
                    p++;
                    int ni = 0;
                    while (*p && *p != '"' && ni < 127) cfg->events[cfg->event_count].name[ni++] = *p++;
                    cfg->events[cfg->event_count].name[ni] = 0;
                    if (*p) p++;
                    p = strchr(p, ':');
                    if (!p) break;
                    p++;
                    while (*p && (*p == ' ' || *p == '\t')) p++;
                    if (*p == '"') {
                        p++;
                        int ci = 0;
                        while (*p && *p != '"' && ci < 127) cfg->events[cfg->event_count].macro_name[ci++] = *p++;
                        cfg->events[cfg->event_count].macro_name[ci] = 0;
                        if (*p) p++; // skip closing quote
                        cfg->event_count++;
                        // skip comma
                        while (*p && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == ',')) p++;
                    }
                }
            }
        }

        free(buf);
        strncpy(cfg->loaded_path, path, MAX_PATH-1);
        cfg->loaded = true;
        fprintf(stderr, "[config] Loaded %d macros, %d events from %s\n",
                cfg->macro_count, cfg->event_count, path);
        return true;
    }
    fprintf(stderr, "[config] No events.json found\n");
    return false;
}

static const char *config_resolve_event(Config *cfg, const char *event_name) {
    for (int i = 0; i < cfg->event_count; i++) {
        if (strcasecmp(cfg->events[i].name, event_name) == 0) {
            // Resolve macro
            for (int j = 0; j < cfg->macro_count; j++) {
                if (strcmp(cfg->macros[j].name, cfg->events[i].macro_name) == 0) {
                    return cfg->macros[j].command;
                }
            }
            return cfg->events[i].macro_name;
        }
    }
    // Try direct macro lookup
    for (int j = 0; j < cfg->macro_count; j++) {
        if (strcasecmp(cfg->macros[j].name, event_name) == 0) {
            return cfg->macros[j].command;
        }
    }
    return event_name;
}

static const char *config_resolve_macro(Config *cfg, const char *name) {
    for (int i = 0; i < cfg->macro_count; i++) {
        if (strcmp(cfg->macros[i].name, name) == 0) {
            return cfg->macros[i].command;
        }
    }
    return name;
}

// ── 5E5E 帧编译 ───────────────────────────────────────────
static uint8_t fcs(const uint8_t *data, int len) {
    uint8_t c = 0;
    for (int i = 0; i < len; i++) c ^= data[i];
    return c;
}

static int pack_frame(uint8_t code, const uint8_t *payload, int payload_len, uint8_t *out) {
    int body_len = 1 + 1 + payload_len; // len + code + data
    uint8_t body[256];
    body[0] = body_len;
    body[1] = code;
    memcpy(body + 2, payload, payload_len);
    out[0] = 0x5E;
    out[1] = 0x5E;
    memcpy(out + 2, body, body_len);
    out[2 + body_len] = fcs(body, body_len);
    return 2 + body_len + 1;
}

// 解析通道名: "green" "yellow" "red" "all" "green+red" "0x05"
static int parse_channel(const char *s) {
    if (!s || !*s) return 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X'))
        return (int)strtol(s, NULL, 16) & 0xFF;
    if (s[0] >= '0' && s[0] <= '9')
        return atoi(s) & 0xFF;

    int mask = 0;
    char copy[128];
    strncpy(copy, s, sizeof(copy)-1);
    char *tok = strtok(copy, "+|,");
    while (tok) {
        char *t = trim(tok);
        if (strcasecmp(t, "green") == 0)  mask |= CH_GREEN;
        else if (strcasecmp(t, "yellow") == 0) mask |= CH_YELLOW;
        else if (strcasecmp(t, "red") == 0)    mask |= CH_RED;
        else if (strcasecmp(t, "all") == 0)    mask |= CH_ALL;
        tok = strtok(NULL, "+|,");
    }
    return mask;
}

static int parse_ms(const char *s) {
    char cpy[64]; strncpy(cpy, s, 63); cpy[63] = 0;
    char *p = cpy + strlen(cpy) - 1;
    while (p >= cpy && (*p == 'm' || *p == 's' || *p == 'M' || *p == 'S')) p--;
    *(p+1) = 0;
    return atoi(cpy);
}

static int parse_duration(const char *s) {
    char cpy[64]; strncpy(cpy, s, 63); cpy[63] = 0;
    int len = strlen(cpy);
    if (len > 1 && cpy[len-1] == 's' && cpy[len-2] != 'm') {
        cpy[len-1] = 0;
        return atoi(cpy) * 1000;
    }
    return parse_ms(cpy);
}

// 扫描命令中的选项 --flag 或 --key=value
static const char *find_opt(const char *cmd, const char *opt, char *val, int val_size) {
    char pattern[128];
    snprintf(pattern, sizeof(pattern), "--%s=", opt);
    const char *p = strstr(cmd, pattern);
    if (p) {
        p += strlen(pattern);
        int i = 0;
        while (*p && *p != ' ' && i < val_size-1) val[i++] = *p++;
        val[i] = 0;
        return val;
    }
    snprintf(pattern, sizeof(pattern), "--%s ", opt);
    p = strstr(cmd, pattern);
    if (!p) {
        snprintf(pattern, sizeof(pattern), "--%s\t", opt);
        p = strstr(cmd, pattern);
    }
    if (!p) {
        snprintf(pattern, sizeof(pattern), "--%s", opt);
        p = strstr(cmd, pattern);
        if (p) {
            char next = *(p + strlen(pattern));
            if (!next || next == ' ' || next == '\t' || next == '\n') {
                val[0] = '1'; val[1] = 0;
                return val;
            }
        }
        return NULL;
    }
    p += strlen(pattern);
    while (*p == ' ') p++;
    int i = 0;
    while (*p && *p != ' ' && i < val_size-1) val[i++] = *p++;
    val[i] = 0;
    return val;
}

static bool has_flag(const char *cmd, const char *flag) {
    char pattern[128];
    snprintf(pattern, sizeof(pattern), "--%s", flag);
    const char *p = strstr(cmd, pattern);
    if (!p) return false;
    char c = *(p + strlen(pattern));
    return (!c || c == ' ' || c == '\t' || c == '\n' || c == '=');
}

static int serialize_tlv(bool only, int count, int freq, int fade, uint8_t *out) {
    int pos = 0;
    if (only) { out[pos++] = TLV_ONLY; out[pos++] = 0xFF; }
    if (count > 0) { out[pos++] = TLV_COUNT; out[pos++] = count & 0xFF; }
    if (freq > 0) {
        int scaled = (freq / 100);
        if (scaled < 2) scaled = 2;
        if (scaled > 50) scaled = 50;
        out[pos++] = TLV_FREQ; out[pos++] = scaled;
    }
    if (fade > 0) {
        int scaled = (fade / 100);
        if (scaled < 1) scaled = 1;
        if (scaled > 50) scaled = 50;
        out[pos++] = TLV_FADE; out[pos++] = scaled;
    }
    return pos;
}

// ── 北向命令编译器 ───────────────────────────────────────
// 输入: "led green on [options]" 或 "write led.freq 1000" 等
// 输出: Script
static int compile_script(const char *cmd_line, Script *script) {
    script->count = 0;

    // 用 ; 分割多步命令
    char line_copy[MAX_LINE];
    strncpy(line_copy, cmd_line, sizeof(line_copy)-1);

    char *part = strtok(line_copy, ";");
    while (part && script->count < MAX_FRAMES) {
        part = trim(part);
        if (!*part) { part = strtok(NULL, ";"); continue; }

        Step *step = &script->steps[script->count];

        // Parse command
        if (strncmp(part, "wait", 4) == 0 && (part[4] == ' ' || part[4] == '\t')) {
            const char *v = part + 5;
            while (*v == ' ') v++;
            step->type = STEP_DELAY;
            step->delay_ms = parse_duration(v);
            script->count++;

        } else if (strncmp(part, "led", 3) == 0 && (part[3] == ' ' || part[3] == '\t')) {
            // led <channel> <action> [options]
            char *p = part + 4;
            while (*p == ' ') p++;
            char ch_str[64] = {0};
            int i = 0;
            while (*p && *p != ' ' && i < 63) ch_str[i++] = *p++;
            ch_str[i] = 0;
            while (*p == ' ') p++;

            char act_str[64] = {0};
            i = 0;
            while (*p && *p != ' ' && i < 63) act_str[i++] = *p++;
            act_str[i] = 0;

            int channel = parse_channel(ch_str);
            int action = ACT_ON;
            if (strcasecmp(act_str, "off") == 0) action = ACT_OFF;
            else if (strcasecmp(act_str, "on") == 0) action = ACT_ON;
            else if (strcasecmp(act_str, "blink") == 0) action = ACT_BLINK;

            bool only = has_flag(part, "only");
            char val[64] = {0};
            int count = 0, freq = 0, fade = 0;
            if (find_opt(part, "count", val, sizeof(val))) count = atoi(val);
            if (find_opt(part, "freq", val, sizeof(val))) freq = parse_ms(val);
            if (find_opt(part, "fade", val, sizeof(val))) fade = parse_ms(val);

            uint8_t payload[256];
            int plen = 0;
            payload[plen++] = channel & 0xFF;
            payload[plen++] = action & 0xFF;
            int tlv_len = serialize_tlv(only, count, freq, fade, payload + plen);
            plen += tlv_len;

            step->type = STEP_FRAME;
            step->frame.len = pack_frame(CMD_LED, payload, plen, step->frame.data);
            script->count++;

        } else if (strncmp(part, "write", 5) == 0) {
            // write <param> <value> or write <param>=<value>
            char *p = part + 5;
            while (*p == ' ') p++;
            char param[128] = {0};
            int pi = 0;
            while (*p && *p != ' ' && *p != '=' && pi < 127) param[pi++] = *p++;
            param[pi] = 0;
            if (*p == '=') p++;
            while (*p == ' ') p++;
            char val_str[64] = {0};
            int vi = 0;
            while (*p && vi < 63) val_str[vi++] = *p++;
            val_str[vi] = 0;

            uint8_t addr = 0;
            if (strcasecmp(param, "led.brightness") == 0 || strcasecmp(param, "led.bright") == 0)
                addr = ADDR_BRIGHTNESS;
            else if (strcasecmp(param, "led.frequency") == 0 || strcasecmp(param, "led.freq") == 0 ||
                     strcasecmp(param, "led.period") == 0)
                addr = ADDR_FREQUENCY;
            else if (strcasecmp(param, "led.fadetime") == 0 || strcasecmp(param, "led.fade") == 0)
                addr = ADDR_FADETIME;
            else if (strcasecmp(param, "ble.advtimeout") == 0)
                addr = ADDR_ADVTIMEOUT;

            int value = atoi(val_str);
            if (addr == ADDR_FREQUENCY || addr == ADDR_FADETIME) {
                if (value > 0) {
                    value = value / 100;
                    if (value < 2) value = 2;
                    if (value > 50) value = 50;
                }
            }

            uint8_t payload[3] = {addr, value & 0xFF};
            step->type = STEP_FRAME;
            step->frame.len = pack_frame(CMD_WRITE, payload, 2, step->frame.data);
            script->count++;

        } else if (strcmp(part, "off") == 0) {
            uint8_t payload[2] = {CH_ALL, ACT_OFF};
            step->type = STEP_FRAME;
            step->frame.len = pack_frame(CMD_LED, payload, 2, step->frame.data);
            script->count++;

        } else if (strncmp(part, "read", 4) == 0) {
            // read param - sends read frame
            char *p = part + 4;
            while (*p == ' ') p++;
            uint8_t addr = 1; // default
            if (strstr(p, "freq")) addr = ADDR_FREQUENCY;
            else if (strstr(p, "fade")) addr = ADDR_FADETIME;
            else if (strstr(p, "bright")) addr = ADDR_BRIGHTNESS;
            step->type = STEP_FRAME;
            uint8_t payload[1] = {addr};
            step->frame.len = pack_frame(CMD_READ, payload, 1, step->frame.data);
            script->count++;
        }

        part = strtok(NULL, ";");
    }

    return script->count;
}

// ── Session 管理 ──────────────────────────────────────────
static int session_find(const char *id) {
    for (int i = 0; i < g_sessions.count; i++)
        if (strcmp(g_sessions.sessions[i].session_id, id) == 0)
            return i;
    return -1;
}

static void session_set(const char *id, const char *state) {
    pthread_mutex_lock(&g_sessions.lock);
    int idx = session_find(id);
    if (idx < 0 && g_sessions.count < MAX_SESSIONS) {
        idx = g_sessions.count++;
        strncpy(g_sessions.sessions[idx].session_id, id, 127);
    }
    if (idx >= 0) {
        strncpy(g_sessions.sessions[idx].state, state, MAX_LINE-1);
        g_sessions.sessions[idx].active = true;
    }
    pthread_mutex_unlock(&g_sessions.lock);
}

static void session_release(const char *id) {
    pthread_mutex_lock(&g_sessions.lock);
    int idx = session_find(id);
    if (idx >= 0) {
        g_sessions.sessions[idx].active = false;
        memset(g_sessions.sessions[idx].state, 0, MAX_LINE);
    }
    pthread_mutex_unlock(&g_sessions.lock);
}

static int session_active_count(void) {
    int n = 0;
    pthread_mutex_lock(&g_sessions.lock);
    for (int i = 0; i < g_sessions.count; i++)
        if (g_sessions.sessions[i].active) n++;
    pthread_mutex_unlock(&g_sessions.lock);
    return n;
}

static void apply_idle_state(void) {
    // All off first
    uint8_t off_frame[16];
    uint8_t payload[2] = {CH_ALL, ACT_OFF};
    int off_len = pack_frame(CMD_LED, payload, 2, off_frame);
    hid_send(off_frame, off_len);
    usleep(50000);

    // Get idle command from config
    const char *idle_cmd = config_resolve_macro(&g_config, "idle");
    if (!idle_cmd || strcmp(idle_cmd, "idle") == 0) {
        idle_cmd = "led green blink --only";
    }

    Script script;
    compile_script(idle_cmd, &script);
    for (int i = 0; i < script.count; i++) {
        if (script.steps[i].type == STEP_FRAME) {
            hid_send(script.steps[i].frame.data, script.steps[i].frame.len);
        } else if (script.steps[i].type == STEP_DELAY) {
            usleep(script.steps[i].delay_ms * 1000);
        }
        usleep(50000);
    }
}

// ── 消息处理 ──────────────────────────────────────────────
static void handle_message(const char *json_raw, int fd) {
    char result[MAX_LINE * 2];
    char cmd[MAX_LINE] = {0};
    char event_name[128] = {0};
    char session[128] = {0};
    char agent[128] = {0};

    json_get_string(json_raw, "cmd", cmd, sizeof(cmd));
    json_get_string(json_raw, "event", event_name, sizeof(event_name));
    json_get_string(json_raw, "session", session, sizeof(session));
    json_get_string(json_raw, "agent", agent, sizeof(agent));
    bool release = json_get_bool(json_raw, "release_session", false);

    if (!session[0]) {
        snprintf(session, sizeof(session), "%08x", (unsigned)time(NULL));
    }

    // Resolve event → command
    char resolved_cmd[MAX_LINE] = {0};
    if (!cmd[0] && event_name[0]) {
        const char *resolved = config_resolve_event(&g_config, event_name);
        strncpy(resolved_cmd, resolved, sizeof(resolved_cmd)-1);
        fprintf(stderr, "[msg] session=%s event=%s -> cmd=%s\n", session, event_name, resolved_cmd);
    } else {
        strncpy(resolved_cmd, cmd, sizeof(resolved_cmd)-1);
    }

    if (release) {
        session_release(session);

        if (session_active_count() > 0) {
            apply_idle_state();
        } else {
            apply_idle_state();
        }

        snprintf(result, sizeof(result),
                 "{\"ok\":true,\"ack\":\"✓\",\"device\":\"\",\"cmd\":\"%s\"}\n", cmd);

    } else if (resolved_cmd[0]) {
        Script script;
        compile_script(resolved_cmd, &script);

        fprintf(stderr, "[msg] session=%s cmd=%s -> %d steps\n", session, resolved_cmd, script.count);

        for (int i = 0; i < script.count; i++) {
            if (script.steps[i].type == STEP_FRAME) {
                hid_send(script.steps[i].frame.data, script.steps[i].frame.len);
            } else if (script.steps[i].type == STEP_DELAY) {
                usleep(script.steps[i].delay_ms * 1000);
            }
            usleep(50000);
        }

        session_set(session, resolved_cmd);

        char device_str[64] = "9C7F643B3B39";
        snprintf(result, sizeof(result),
                 "{\"ok\":true,\"ack\":\"✓\",\"device\":\"%s\",\"cmd\":\"%s\"}\n",
                 device_str, resolved_cmd);
    } else {
        snprintf(result, sizeof(result),
                 "{\"ok\":true,\"ack\":\"-\",\"device\":\"\"}\n");
    }

    write(fd, result, strlen(result));
}

// ── TCP 服务 ──────────────────────────────────────────────
static int create_server(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    listen(fd, 10);
    return fd;
}

static void handle_ctrl_client(int fd) {
    char buf[4096];
    int n = (int)read(fd, buf, sizeof(buf)-1);
    if (n <= 0) { close(fd); return; }
    buf[n] = 0;

    char *line = trim(buf);
    if (*line) handle_message(line, fd);
    close(fd);
}

static void *ctrl_worker(void *arg) {
    int fd = (intptr_t)arg;
    handle_ctrl_client(fd);
    return NULL;
}

static void ctrl_server_loop(int port) {
    int fd = create_server(port);
    if (fd < 0) { fprintf(stderr, "[ctrl] Failed to create server on %d\n", port); return; }
    fprintf(stderr, "[ctrl] Listening on 127.0.0.1:%d\n", port);

    while (g_running) {
        struct sockaddr_in client;
        socklen_t clen = sizeof(client);
        int client_fd = accept(fd, (struct sockaddr*)&client, &clen);
        if (client_fd < 0) { if (g_running) usleep(100000); continue; }

        pthread_t t;
        pthread_create(&t, NULL, ctrl_worker, (void*)(intptr_t)client_fd);
        pthread_detach(t);
    }
    close(fd);
}

// ── 简单 HTTP 响应 ────────────────────────────────────────
static void http_handle(int fd) {
    char buf[4096];
    int n = (int)read(fd, buf, sizeof(buf)-1);
    if (n <= 0) { close(fd); return; }
    buf[n] = 0;

    char method[16] = {0}, path[256] = {0};
    sscanf(buf, "%15s %255s", method, path);

    char resp[8192];
    int rlen = 0;

    if (strcmp(path, "/") == 0) {
        rlen = snprintf(resp, sizeof(resp),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{\"status\":\"ok\",\"message\":\"PromLight daemon\"}");
    } else if (strcmp(path, "/api/status") == 0) {
        rlen = snprintf(resp, sizeof(resp),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{\"bluetooth\":true,\"count\":1,\"opened\":%d,\"version\":\"0.3.0-c\"}",
            g_hid.opened ? 1 : 0);
    } else if (strcmp(path, "/api/devices") == 0) {
        rlen = snprintf(resp, sizeof(resp),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
            "[{\"mac\":\"9C7F643B3B39\",\"label\":\"PromLight\",\"opened\":%d}]",
            g_hid.opened ? 1 : 0);
    } else if (strcmp(path, "/api/config") == 0) {
        // Return loaded macros info
        char macros_json[2048] = {0};
        int mpos = 0;
        mpos += snprintf(macros_json + mpos, sizeof(macros_json) - mpos, "[");
        for (int i = 0; i < g_config.macro_count && mpos < 2000; i++) {
            if (i > 0) macros_json[mpos++] = ',';
            mpos += snprintf(macros_json + mpos, 2000 - mpos,
                "{\"name\":\"%s\",\"cmd\":\"%s\"}",
                g_config.macros[i].name, g_config.macros[i].command);
        }
        if (mpos < 2000) macros_json[mpos++] = ']';

        rlen = snprintf(resp, sizeof(resp),
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{\"loaded\":%d,\"macros\":%s,\"events\":%d,\"source\":\"%s\"}",
            g_config.loaded, macros_json, g_config.event_count,
            g_config.loaded ? g_config.loaded_path : "");
    } else {
        rlen = snprintf(resp, sizeof(resp),
            "HTTP/1.1 404\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n404");
    }

    write(fd, resp, rlen);
    close(fd);
}

static void http_server_loop(int port) {
    int fd = create_server(port);
    if (fd < 0) { fprintf(stderr, "[http] Failed to create server on %d\n", port); return; }
    fprintf(stderr, "[http] Listening on 127.0.0.1:%d\n", port);

    while (g_running) {
        struct sockaddr_in client;
        socklen_t clen = sizeof(client);
        int client_fd = accept(fd, (struct sockaddr*)&client, &clen);
        if (client_fd < 0) { if (g_running) usleep(100000); continue; }

        pthread_t t;
        pthread_create(&t, NULL, (void* (*)(void*))http_handle, (void*)(intptr_t)client_fd);
        pthread_detach(t);
    }
    close(fd);
}

// ── 信号处理 ──────────────────────────────────────────────
static void handle_signal(int sig) {
    fprintf(stderr, "[daemon] Signal %d, shutting down...\n", sig);
    g_running = false;
    hid_close_locked();
    exit(0);
}

// ── 主入口 ────────────────────────────────────────────────
int main(int argc, char **argv) {
    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);
    signal(SIGPIPE, SIG_IGN);

    int ctrl_port = CTRL_PORT;
    int http_port = HTTP_PORT;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--ctrl-port") == 0 && i+1 < argc)
            ctrl_port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--http-port") == 0 && i+1 < argc)
            http_port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--verbose") == 0 || strcmp(argv[i], "-v") == 0)
            ; // verbose mode - debug logs
    }

    fprintf(stderr, "[daemon] PromLight v0.3.0 (ARM64 native C)\n");

    // Fix working directory to find events.json
    char self_dir[MAX_PATH];
    strncpy(self_dir, argv[0], MAX_PATH-1);
    char *slash = strrchr(self_dir, '/');
    if (slash) {
        *slash = 0;
        chdir(self_dir);
    }

    // Load config
    config_load(&g_config);

    // Open HID device
    if (!hid_open()) {
        // Retry once
        sleep(1);
        if (!hid_open()) {
            fprintf(stderr, "[daemon] Failed to open HID device, continuing...\n");
        }
    }

    // Apply idle state
    apply_idle_state();

    // Start HTTP and CTRL servers
    fprintf(stderr, "[daemon] Starting servers...\n");

    pthread_t ctrl_thread, http_thread;
    pthread_create(&ctrl_thread, NULL, (void* (*)(void*))ctrl_server_loop, (void*)(intptr_t)ctrl_port);
    pthread_detach(ctrl_thread);
    pthread_create(&http_thread, NULL, (void* (*)(void*))http_server_loop, (void*)(intptr_t)http_port);
    pthread_detach(http_thread);

    fprintf(stderr, "[daemon] Running (ctrl=%d, http=%d)\n", ctrl_port, http_port);

    // Main loop - keep runloop for IOKit callbacks
    while (g_running) {
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, false);
    }

    hid_close_locked();
    fprintf(stderr, "[daemon] Bye!\n");
    return 0;
}
