#!/usr/bin/env python3
"""
PromLight Native Launcher
ARM64 原生启动脚本，替换原 x86_64 PromLight.app
"""

import os
import sys
import json
import signal
import subprocess
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_PY = os.path.join(APP_DIR, 'native_daemon', 'daemon.py')
CONFIG_DIR = APP_DIR  # 配置文件（events.json, devices.json）目录
LOG_FILE = os.path.join(APP_DIR, 'daemon.log')


def main():
    # 设置环境变量让 hook 脚本能找到配置
    os.environ.setdefault('PROM_LIGHT_CONFIG', CONFIG_DIR)

    # 确保 hidapi 安装了
    try:
        import hid
    except ImportError:
        print("[!] 正在安装 hidapi...")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'hidapi'])
        print("[✓] hidapi 已安装")
        # 重启脚本
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # 启动 daemon
    daemon_cmd = [sys.executable, DAEMON_PY, '--verbose']
    daemon_proc = subprocess.Popen(
        daemon_cmd,
        stdout=open(LOG_FILE, 'a'),
        stderr=subprocess.STDOUT,
    )

    print(f"[✓] PromLight Native Daemon 已启动 (PID={daemon_proc.pid})")
    print(f"    日志: {LOG_FILE}")
    print(f"    控制端口: 47800")
    print(f"    Web UI: http://127.0.0.1:7800")

    # 等待进程
    def _handle_signal(sig, frame):
        daemon_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        daemon_proc.wait()
    except KeyboardInterrupt:
        daemon_proc.terminate()
        daemon_proc.wait()

    print("[✓] Daemon 已停止")


if __name__ == '__main__':
    main()
