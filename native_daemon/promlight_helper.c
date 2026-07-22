/**
 * promlight_helper - ARM64 原生 HID 写入守护进程
 * 
 * 两种模式:
 *   单次: promlight_helper <hex-frame> [<hex-frame>...]
 *   常驻: promlight_helper --daemon    # 从 stdin 读 hex 帧, 每行一帧
 * 
 * 编译: clang -o promlight_helper promlight_helper.c -framework IOKit -framework CoreFoundation -arch arm64
 */
#include <IOKit/hid/IOHIDManager.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>

static IOHIDDeviceRef gDevice = NULL;
static IOHIDManagerRef gManager = NULL;

static int hex2bytes(const char *hex, uint8_t *out, int maxlen) {
    int len = 0;
    while (*hex && *(hex+1) && len < maxlen) {
        char b[3] = {*hex, *(hex+1), 0};
        out[len++] = (uint8_t)strtol(b, NULL, 16);
        hex += 2;
    }
    return len;
}

static int open_device(void) {
    if (gDevice) return 0;
    
    gManager = IOHIDManagerCreate(kCFAllocatorDefault, 0);
    if (!gManager) return -1;
    
    CFMutableDictionaryRef dict = CFDictionaryCreateMutable(NULL, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    int v = 0x07D7, p = 0x6B01;
    CFNumberRef nv = CFNumberCreate(NULL, kCFNumberIntType, &v);
    CFNumberRef np = CFNumberCreate(NULL, kCFNumberIntType, &p);
    CFDictionarySetValue(dict, CFSTR("VendorID"), nv);
    CFDictionarySetValue(dict, CFSTR("ProductID"), np);
    CFRelease(nv); CFRelease(np);
    
    IOHIDManagerSetDeviceMatching(gManager, dict);
    IOHIDManagerScheduleWithRunLoop(gManager, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, false);
    
    CFSetRef devs = IOHIDManagerCopyDevices(gManager);
    if (!devs || CFSetGetCount(devs) == 0) {
        fprintf(stderr, "ERROR: No PromLight device found\n");
        return -1;
    }
    
    CFSetGetValues(devs, (const void**)&gDevice);
    IOReturn ret = IOHIDDeviceOpen(gDevice, kIOHIDOptionsTypeSeizeDevice);
    if (ret != kIOReturnSuccess) {
        fprintf(stderr, "ERROR: Failed to open device: %d\n", ret);
        gDevice = NULL;
        return -1;
    }
    
    // HIDIdleTime = 0
    int idle = 0;
    CFNumberRef idleNum = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &idle);
    IOHIDDeviceSetProperty(gDevice, CFSTR("HIDIdleTime"), idleNum);
    CFRelease(idleNum);
    
    return 0;
}

static int send_report(const uint8_t *data, int len) {
    if (!gDevice) return -1;
    
    uint8_t report[64];
    memset(report, 0, 64);
    int copy_len = len < 64 ? len : 64;
    memcpy(report, data, copy_len);
    
    IOReturn ret = IOHIDDeviceSetReport(gDevice, kIOHIDReportTypeOutput, 2, report, 64);
    if (ret != kIOReturnSuccess) {
        fprintf(stderr, "ERROR: SetReport failed: %d (0x%x)\n", ret, ret);
        return -1;
    }
    return 0;
}

static void close_device(void) {
    if (gDevice) {
        IOHIDDeviceClose(gDevice, kIOHIDOptionsTypeSeizeDevice);
        gDevice = NULL;
    }
    if (gManager) {
        CFRelease(gManager);
        gManager = NULL;
    }
}

static void cleanup(int sig) {
    close_device();
    _exit(0);
}

int main(int argc, char **argv) {
    signal(SIGINT, cleanup);
    signal(SIGTERM, cleanup);
    
    int daemon_mode = 0;
    int first_arg = 1;
    
    if (argc > 1 && strcmp(argv[1], "--daemon") == 0) {
        daemon_mode = 1;
        first_arg = 2;
    }
    
    if (open_device() != 0) {
        return 1;
    }
    
    if (daemon_mode) {
        // Daemon mode: read hex frames from stdin, line by line
        // Write "OK\n" or "ERR\n" for each frame
        fprintf(stderr, "READY\n");
        fflush(stderr);
        
        char line[512];
        uint8_t buf[64];
        while (fgets(line, sizeof(line), stdin)) {
            // Strip whitespace
            char *end = line + strlen(line) - 1;
            while (end >= line && (*end == '\n' || *end == '\r' || *end == ' ')) *end-- = 0;
            
            if (line[0] == '#' || line[0] == 0) {
                fprintf(stdout, "SKIP\n");
                fflush(stdout);
                continue;
            }
            
            // Support multiple frames on one line (space or ; separated)
            char *token = strtok(line, " ;");
            int ok = 1;
            while (token) {
                if (token[0] == '#') break;
                int len = hex2bytes(token, buf, 64);
                if (len > 0) {
                    if (send_report(buf, len) != 0) {
                        ok = 0;
                    }
                }
                token = strtok(NULL, " ;");
            }
            
            fprintf(stdout, "%s\n", ok ? "OK" : "ERR");
            fflush(stdout);
        }
    } else {
        // Single-shot mode
        uint8_t buf[64];
        for (int i = first_arg; i < argc; i++) {
            int len = hex2bytes(argv[i], buf, 64);
            if (len > 0) {
                send_report(buf, len);
                usleep(50000);
            }
        }
    }
    
    close_device();
    return 0;
}
