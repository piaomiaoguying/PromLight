"""
PromLight 原生 ARM64 daemon
在 Apple Silicon Mac 上以 ARM64 原生运行，无需 Rosetta 2 转译。

HID 写入通过 arm64 原生 C helper (promlight_helper) 实现，
直接调用 IOKit IOHIDDeviceSetReport，解决 hidapi 在 ARM64 BLE HID
设备上 write 返回成功但数据无法到达设备的问题。

架构：
- 监听 47800 (agent_hook 发来的北向命令) 和 7800 (Web UI)
- 接收北向文本命令 → hid_northbound 编译 → 传给 C helper 写入 HID
- 多设备、多 session 支持
"""
import argparse
import enum
import json
import logging
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

# ── 常量 ───────────────────────────────────────────────────────
DAEMON_PORT = 7800  # HTTP API / SSE
CTRL_PORT = 47800  # agent_hook TCP socket（北向命令通道）
DEVICE_VID = 0x07D7
DEVICE_PID = 0x6B01
HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'promlight_helper')

LOG = logging.getLogger("promlight")

# ── HID 抽象层（通过 C helper 子进程） ──────────────────────────
class HidDevice:
    """HID 设备封装 - 通过 ARM64 原生 C helper 写入"""

    def __init__(self, vid=None, pid=None):
        self.vid = vid or DEVICE_VID
        self.pid = pid or DEVICE_PID
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._info = {}

    def enumerate(self):
        """列举设备 - 快速测试 C helper 是否可用"""
        if not os.path.exists(HELPER_PATH):
            return []
        try:
            result = subprocess.run(
                [HELPER_PATH, "5E5E0404FF00FF"],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return [{'path': b'promlight_helper', 'serial': '9C7F643B3B39',
                         'product': 'PromLight', 'manufacturer': 'MediaPig'}]
        except Exception:
            pass
        return []

    def open(self, path=None):
        """启动 C helper daemon 进程并建立管道"""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True
            try:
                self._proc = subprocess.Popen(
                    [HELPER_PATH, '--daemon'],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                ready = self._proc.stderr.readline()
                if ready.strip() != b'READY':
                    LOG.error("C helper ready check failed: %s", ready)
                    self._proc.kill()
                    self._proc = None
                    return False
                self._info = {
                    'manufacturer': 'MediaPig',
                    'product': 'PromLight',
                    'serial': '9C7F643B3B39',
                }
                LOG.info("C helper process started (PID=%d)", self._proc.pid)
                return True
            except Exception as e:
                LOG.error("Failed to start C helper: %s", e)
                self._proc = None
                return False

    def is_open(self):
        return self._proc is not None and self._proc.poll() is None

    def send(self, data: bytes) -> bool:
        """发送 HID Output Report - 通过 C helper 进程的 stdin"""
        if not self._proc or self._proc.poll() is not None:
            return False
        with self._lock:
            try:
                hex_frame = data.hex()
                self._proc.stdin.write((hex_frame + '\n').encode())
                self._proc.stdin.flush()
                response = self._proc.stdout.readline()
                return response.strip() == b'OK'
            except Exception as e:
                LOG.error("C helper write error: %s", e)
                return False

    def read(self, timeout_ms=1000):
        return None

    def close(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    self._proc.kill()
                self._proc = None

    @property
    def serial(self):
        return self._info.get('serial', '')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── 南向帧协议（hid_atomic） ───────────────────────────────────
class AtomicCode(enum.IntEnum):
    CMD_READ = 0x02
    CMD_WRITE = 0x03
    CMD_LED = 0x04

class LedChannel(enum.IntEnum):
    CH_GREEN = 0x01
    CH_YELLOW = 0x02
    CH_RED = 0x04
    CH_ALL = 0xFF

class LedAction(enum.IntEnum):
    ACT_OFF = 0x00
    ACT_ON = 0x01
    ACT_BLINK = 0x02

# 参数地址
ADDR_BRIGHTNESS = 0x01  # 亮度 5..100%
ADDR_FREQUENCY = 0x02  # 闪烁频率 200..5000ms (100ms 步进)
ADDR_FADETIME = 0x03  # 渐变时长 0..5000ms (100ms 步进)
ADDR_FADETIME = 0x03  # 渐变时长 0..5000ms (100ms 步进)
ADDR_BLE_ADVTIMEOUT = 0x04  # BLE 广播超时

# TLV Tags
TLV_ONLY = 0x01
TLV_COUNT = 0x02
TLV_FREQ = 0x03
TLV_FADE = 0x04

FRAME_MAGIC = b'\x5E\x5E'


def _fcs(data: bytes) -> int:
    """FCS = XOR 校验"""
    c = 0
    for b in data:
        c ^= b
    return c & 0xFF


def pack_frame(code: AtomicCode, data: bytes = b'') -> bytes:
    """打包南向帧：5E 5E <len> <code> <data> <fcs>
    len 包括自身；fcs 从 len 字节 XOR 到 data 末尾"""
    length = 1 + 1 + len(data)  # 1(self) + 1(code) + len(data)
    body = bytes([length, code]) + data
    frame = FRAME_MAGIC + body + bytes([_fcs(body)])
    return frame


def serialize_tlv(only: bool = False, count: int = 0,
                  freq: int = 0, fade: int = 0) -> bytes:
    """序列化 TLV 段（固定顺序: only → count → freq → fade）"""
    parts = []
    if only:
        parts.append(bytes([TLV_ONLY, 0xFF]))
    if count > 0:
        parts.append(bytes([TLV_COUNT, count & 0xFF]))
    if freq > 0:
        # 频率单位是 100ms
        scaled = max(2, min(50, round(freq / 100)))
        parts.append(bytes([TLV_FREQ, scaled]))
    if fade > 0:
        scaled = max(1, min(50, round(fade / 100)))
        parts.append(bytes([TLV_FADE, scaled]))
    return b''.join(parts)


def build_led(channel: int, action: int, only: bool = False,
              count: int = 0, freq: int = 0, fade: int = 0) -> bytes:
    """编译 LED 控制帧"""
    tlv = serialize_tlv(only, count, freq, fade)
    data = bytes([channel & 0xFF, action & 0xFF]) + tlv
    return pack_frame(AtomicCode.CMD_LED, data)


def build_write(addr: int, value: int) -> bytes:
    """编译参数写入帧"""
    return pack_frame(AtomicCode.CMD_WRITE, bytes([addr, value & 0xFF]))


def build_read(addr: int) -> bytes:
    """编译参数读取帧"""
    return pack_frame(AtomicCode.CMD_READ, bytes([addr]))


# ── 北向命令编译器（hid_northbound） ──────────────────────────
CHANNEL_NAMES = {
    'green': LedChannel.CH_GREEN,
    'yellow': LedChannel.CH_YELLOW,
    'red': LedChannel.CH_RED,
    'all': LedChannel.CH_ALL,
}

PARAM_ADDR = {
    'led.brightness': ADDR_BRIGHTNESS,
    'led.bright': ADDR_BRIGHTNESS,
    'led.frequency': ADDR_FREQUENCY,
    'led.freq': ADDR_FREQUENCY,
    'led.period': ADDR_FREQUENCY,
    'led.fadetime': ADDR_FADETIME,
    'led.fade': ADDR_FADETIME,
    'ble.advtimeout': ADDR_BLE_ADVTIMEOUT,
}


def _parse_channel(ch_str: str) -> int:
    """解析通道文本 → 位掩码。支持 'green', 'green+red', 'green,yellow', '0x05' 等"""
    ch_str = ch_str.strip().lower()
    if ch_str.startswith('0x') or ch_str.startswith('0b') or ch_str.isdigit():
        base = 10
        if ch_str.startswith('0x'):
            base = 16
        elif ch_str.startswith('0b'):
            base = 2
        return int(ch_str, base) & 0xFF
    mask = 0
    for name in re.split(r'[+,|]', ch_str):
        name = name.strip()
        if name in CHANNEL_NAMES:
            mask |= CHANNEL_NAMES[name]
    return mask


def _parse_args(argv):
    """类 argparse 北向命令解析"""
    class Args:
        channel = 0
        action = ''
        only = False
        count = 0
        freq = 0
        fade = 0

    args = Args()
    argv = [a for a in argv if a]

    if not argv:
        raise ValueError("空命令")

    if argv[0] == 'led' and len(argv) >= 3:
        args.channel = _parse_channel(argv[1])
        args.action = argv[2].lower()
        i = 3
        while i < len(argv):
            opt = argv[i]
            if opt in ('-o', '--only'):
                args.only = True
                i += 1
            elif opt in ('-c', '--count') and i + 1 < len(argv):
                args.count = int(argv[i + 1])
                i += 2
            elif opt in ('-f', '--freq') and i + 1 < len(argv):
                args.freq = _parse_ms(argv[i + 1])
                i += 2
            elif opt == '--fade' and i + 1 < len(argv):
                args.fade = _parse_ms(argv[i + 1])
                i += 2
            elif opt.startswith('--count='):
                args.count = int(opt.split('=', 1)[1])
                i += 1
            elif opt.startswith('--freq='):
                args.freq = _parse_ms(opt.split('=', 1)[1])
                i += 1
            elif opt.startswith('--fade='):
                args.fade = _parse_ms(opt.split('=', 1)[1])
                i += 1
            else:
                i += 1
        return args
    elif argv[0] == 'led' and len(argv) == 3 and argv[1] == 'all' and argv[2] == 'off':
        args.channel = LedChannel.CH_ALL
        args.action = 'off'
        return args

    raise ValueError(f"未知命令: {' '.join(argv)}")


def _parse_ms(ms_str: str) -> int:
    s = ms_str.strip().lower().replace('ms', '')
    return int(s)


def _duration_ms(dur_str: str) -> int:
    s = dur_str.strip().lower()
    if s.endswith('s'):
        return int(s[:-1]) * 1000
    return int(s.replace('ms', ''))


def compile_line(line: str):
    """编译单条北向命令 → 南向帧 bytes 或 Delay 时长（毫秒）"""
    tokens = line.strip().split()
    if not tokens:
        return None

    cmd = tokens[0]

    if cmd == 'led':
        args = _parse_args(tokens)
        action_map = {'on': LedAction.ACT_ON, 'off': LedAction.ACT_OFF,
                      'blink': LedAction.ACT_BLINK}
        act = action_map.get(args.action, LedAction.ACT_ON)
        if args.action == 'off':
            return build_led(args.channel, LedAction.ACT_OFF)
        return build_led(args.channel, act, args.only, args.count, args.freq, args.fade)

    elif cmd == 'wait':
        if len(tokens) < 2:
            raise ValueError("wait 需要 <时长>")
        ms = _duration_ms(tokens[1])
        if ms < 0 or ms > 60000:
            raise ValueError(f"wait 时长需在 0..60000ms: {ms}")
        return ('delay', ms)

    elif cmd == 'write':
        if len(tokens) < 2:
            raise ValueError("write 需要 <参数名> <值>")
        if '=' in tokens[1]:
            param, val_str = tokens[1].split('=', 1)
            tokens = ['write', param, val_str] + tokens[2:]
            return compile_line(' '.join(tokens))
        if len(tokens) < 3:
            raise ValueError("write 需要 <参数名> <值>")
        param = tokens[1].lower()
        addr = PARAM_ADDR.get(param)
        if addr is None:
            raise ValueError(f"未知参数: {param}")
        val_str = tokens[2].rstrip('%')
        value = int(val_str)
        if addr in (ADDR_FREQUENCY, ADDR_FADETIME) and value > 0:
            value = max(2, min(50, round(value / 100)))
        return build_write(addr, value)

    elif cmd == 'read':
        if len(tokens) < 2:
            raise ValueError("read 需要参数名")
        param = tokens[1].lower()
        addr = PARAM_ADDR.get(param)
        if addr is None:
            raise ValueError(f"未知参数: {param}")
        return build_read(addr)

    elif cmd == 'off':
        return build_led(LedChannel.CH_ALL, LedAction.ACT_OFF)

    else:
        raise ValueError(f"未知命令: {cmd}")


def compile_script(script: str):
    """编译北向脚本（';' 串联）→ 步列表"""
    steps = []
    for part in script.split(';'):
        part = part.strip()
        if part:
            steps.append(compile_line(part))
    return steps


# ── 设备管理器 ────────────────────────────────────────────────
@dataclass
class DeviceInfo:
    """已连接的 PromLight 设备"""
    serial: str
    label: str
    opened: bool = False
    sessions: int = 0

    def mac_display(self) -> str:
        return ':'.join(self.serial[i:i + 2] for i in range(0, len(self.serial), 2))


class DeviceManager:
    """设备管理：发现、连接、会话路由"""

    def __init__(self):
        self._hid: Optional[HidDevice] = None
        self._lock = threading.Lock()
        self._devices: dict[str, DeviceInfo] = {}
        self._sessions: dict[str, str] = {}
        self._device_lock = threading.Lock()

    def find_devices(self) -> list[DeviceInfo]:
        if not os.path.exists(HELPER_PATH):
            return []
        h = HidDevice()
        devices = []
        for d in h.enumerate():
            serial = d.get('serial', '')
            if serial:
                dev = DeviceInfo(serial=serial, label=serial[-4:] if len(serial) >= 4 else serial)
                devices.append(dev)
                with self._lock:
                    self._devices[serial] = dev
        return devices

    def open_device(self, serial: str = '') -> bool:
        with self._device_lock:
            if self._hid and self._hid.is_open():
                return True
            h = HidDevice()
            if h.open():
                self._hid = h
                LOG.info("Helper started")
                return True
            return False

    def send_frame(self, frame: bytes) -> bool:
        with self._device_lock:
            if not self._hid or not self._hid.is_open():
                return False
            return self._hid.send(frame)

    def send_led(self, channel: int = LedChannel.CH_GREEN,
                 action: int = LedAction.ACT_ON, **kwargs) -> bool:
        frame = build_led(channel, action, **kwargs)
        return self.send_frame(frame)

    def assign_session(self, session_id: str, cwd: str = ''):
        with self._lock:
            self._sessions[session_id] = self._resolve_device(cwd)
            dev_serial = self._sessions[session_id]
            if dev_serial and dev_serial in self._devices:
                self._devices[dev_serial].sessions += 1

    def release_session(self, session_id: str):
        with self._lock:
            dev_serial = self._sessions.pop(session_id, '')
            if dev_serial and dev_serial in self._devices:
                self._devices[dev_serial].sessions = max(0, self._devices[dev_serial].sessions - 1)

    def _resolve_device(self, cwd: str) -> str:
        return ''

    def get_device_info(self) -> list[dict]:
        with self._lock:
            return [
                {'mac': d.serial, 'label': d.label, 'sessions': d.sessions,
                 'opened': self._hid is not None and self._hid.is_open()}
                for d in self._devices.values()
            ]

    @property
    def hid(self):
        return self._hid

    def close(self):
        if self._hid:
            self._hid.close()
            self._hid = None


# ── Session / 状态管理 ────────────────────────────────────────
class StateMachine:
    """灯状态机 + 多 session 管理"""

    def __init__(self, device_mgr: DeviceManager):
        self._device = device_mgr
        self._lock = threading.Lock()
        self._session_states: dict[str, str] = {}
        self._current_cmd: Optional[bytes] = None
        self._history: list[dict] = []
        self._max_history = 500

    def handle_event(self, cmd_text: str, message: str, session: str,
                     agent: str = '', release: bool = False, cwd: str = '',
                     notice: str = '') -> dict:
        result = {'ok': True, 'ack': '✓', 'device': '', 'cmd': cmd_text}

        with self._lock:
            if release:
                if session in self._session_states:
                    del self._session_states[session]
                self._device.release_session(session)
                LOG.info("[%s] session released", session[:8])
                if self._session_states:
                    top_session = list(self._session_states.keys())[-1]
                    top_state = self._session_states[top_session]
                    self._apply_idle_state()
                else:
                    self._apply_idle_state()
            elif cmd_text:
                self._session_states[session] = cmd_text
                self._device.assign_session(session, cwd)
                steps = compile_script(cmd_text)
                for step in steps:
                    if step is None:
                        continue
                    if isinstance(step, tuple) and step[0] == 'delay':
                        time.sleep(step[1] / 1000.0)
                    elif isinstance(step, bytes):
                        self._device.send_frame(step)
                        time.sleep(0.05)
                LOG.info("[%s] cmd=%s -> %d steps", session[:8], cmd_text[:40],
                         len(steps) if isinstance(steps, list) else 1)
                result['device'] = self._device.get_device_info()[0].get('mac', '') if self._device.get_device_info() else ''

            entry = {
                'ts': time.strftime('%H:%M:%S'),
                'agent': agent,
                'session': session,
                'message': message,
                'ok': True,
                'ack': '✓',
                'device': result['device'],
            }
            self._history.append(entry)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        return result

    def _apply_idle_state(self):
        """应用空闲状态（绿灯闪烁）"""
        frame = build_led(LedChannel.CH_GREEN, LedAction.ACT_BLINK)
        self._device.send_frame(frame)

    def get_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return self._history[-limit:]

    def get_status(self) -> dict:
        with self._lock:
            return {
                'sessions': len(self._session_states),
                'active_sessions': list(self._session_states.keys())[-5:],
            }


# ── HTTP/SSE 服务 ────────────────────────────────────────────
class DaemonHTTPHandler(BaseHTTPRequestHandler):
    """Web UI + SSE 事件流"""
    state_machine: StateMachine = None
    device_mgr: DeviceManager = None

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, content_type):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_json({'error': 'not found'}, 404)

    def do_GET(self):
        if self.path == '/':
            ui_path = os.path.join(os.path.dirname(__file__), '..', 'webui.html')
            if os.path.exists(ui_path):
                self._serve_file(ui_path, 'text/html; charset=utf-8')
            else:
                self._send_json({'status': 'ok', 'message': 'PromLight daemon'})
        elif self.path == '/api/status':
            devices = self.device_mgr.get_device_info() if self.device_mgr else []
            self._send_json({
                'bluetooth': True,
                'count': len(devices),
                'opened': sum(1 for d in devices if d.get('opened')),
                'devices': devices,
                'app_version': '0.2.0-arm64',
                'instance': os.getpid(),
            })
        elif self.path == '/api/devices':
            self._send_json(self.device_mgr.get_device_info() if self.device_mgr else [])
        elif self.path == '/api/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            try:
                seq = 0
                while True:
                    msg = f'data: {json.dumps({"kind": "heartbeat", "ts": time.time()})}\n\n'
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_POST(self):
        self._send_json({'error': 'not found'}, 404)


def start_http(port: int, state_machine: StateMachine,
               device_mgr: DeviceManager):
    DaemonHTTPHandler.state_machine = state_machine
    DaemonHTTPHandler.device_mgr = device_mgr
    server = HTTPServer(('127.0.0.1', port), DaemonHTTPHandler)
    LOG.info("HTTP API on http://127.0.0.1:%d", port)
    server.serve_forever()


# ── 北向 TCP 服务 ────────────────────────────────────────────
def handle_ctrl_connection(conn, addr, state_machine: StateMachine):
    try:
        data = conn.recv(4096)
        if not data:
            return
        payload = json.loads(data.decode('utf-8').strip())
        cmd = payload.get('cmd', '')
        message = payload.get('message', '')
        session = payload.get('session', str(uuid.uuid4()))
        agent = payload.get('agent', '')
        cwd = payload.get('cwd', '')
        release = payload.get('release_session', False)
        notice = payload.get('notice', '')

        if cmd or release:
            result = state_machine.handle_event(
                cmd_text=cmd, message=message, session=session,
                agent=agent, release=release, cwd=cwd, notice=notice,
            )
            ack = json.dumps(result) + '\n'
            conn.sendall(ack.encode('utf-8'))
    except json.JSONDecodeError:
        pass
    except Exception as e:
        LOG.error("ctrl connection error: %s", e)
    finally:
        conn.close()


def start_ctrl_server(port: int, state_machine: StateMachine):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(10)
    LOG.info("CTRL socket on 127.0.0.1:%d", port)

    while True:
        conn, addr = server.accept()
        t = threading.Thread(target=handle_ctrl_connection,
                             args=(conn, addr, state_machine), daemon=True)
        t.start()


# ── 主入口 ────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='PromLight Native Daemon')
    parser.add_argument('--ctrl-port', type=int, default=CTRL_PORT,
                        help='控制端口 (default: %d)' % CTRL_PORT)
    parser.add_argument('--http-port', type=int, default=DAEMON_PORT,
                        help='HTTP API 端口 (default: %d)' % DAEMON_PORT)
    parser.add_argument('--verbose', '-v', action='store_true', help='详细日志')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[%(name)s] %(message)s',
        stream=sys.stderr,
    )

    LOG.info("PromLight Native Daemon v0.2 (ARM64)")
    LOG.info("Python: %s on %s", sys.version, sys.platform)

    # 检查 C helper
    if not os.path.exists(HELPER_PATH):
        LOG.error("C helper not found at %s", HELPER_PATH)
        LOG.error("Run: cd %s && clang -o promlight_helper promlight_helper.c -framework IOKit -framework CoreFoundation -arch arm64",
                  os.path.dirname(HELPER_PATH))
        sys.exit(1)

    # 初始化
    device_mgr = DeviceManager()
    state_machine = StateMachine(device_mgr)

    # 发现并打开设备
    devices = device_mgr.find_devices()
    if devices:
        LOG.info("Found device(s): %s", devices)
        if device_mgr.open_device():
            LOG.info("Device opened successfully")
        else:
            LOG.warning("Failed to open device")
    else:
        LOG.warning("No PromLight devices found (C helper not working?)")

    # 启动控制端口
    ctrl_thread = threading.Thread(
        target=start_ctrl_server, args=(args.ctrl_port, state_machine),
        daemon=True, name='ctrl-server'
    )
    ctrl_thread.start()

    # 启动 HTTP 管理接口
    http_thread = threading.Thread(
        target=start_http, args=(args.http_port, state_machine, device_mgr),
        daemon=True, name='http-server'
    )
    http_thread.start()

    LOG.info("Daemon started")
    LOG.info("  CTRL: 127.0.0.1:%d", args.ctrl_port)
    LOG.info("  HTTP: 127.0.0.1:%d", args.http_port)

    # 发送启动默认状态（绿灯闪烁）
    if device_mgr.hid:
        device_mgr.send_led(LedChannel.CH_GREEN, LedAction.ACT_BLINK)
        LOG.info("Sent initial idle state (green blink)")

    # 保持主线程
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOG.info("Shutting down...")
        device_mgr.close()
        LOG.info("Bye!")


if __name__ == '__main__':
    main()
