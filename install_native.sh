#!/usr/bin/env bash
# PromLight ARM64 原生 daemon 安装/切换脚本
# ARM64 原生 C helper (IOHIDDeviceSetReport) + Python daemon
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$SCRIPT_DIR/native_daemon"
HELPER_C="$DAEMON_DIR/promlight_helper.c"
HELPER_BIN="$DAEMON_DIR/promlight_helper"
DAEMON_PY="$DAEMON_DIR/daemon.py"

echo "=== PromLight ARM64 原生 Daemon ==="

# 编译 C helper
echo "编译 ARM64 原生 C helper..."
if [ ! -f "$HELPER_C" ]; then
    echo "错误: 找不到 $HELPER_C"
    exit 1
fi

if clang -o "$HELPER_BIN" "$HELPER_C" \
    -framework IOKit -framework CoreFoundation \
    -arch arm64 2>&1; then
    echo "✅ C helper 编译成功: $HELPER_BIN"
else
    echo "❌ C helper 编译失败"
    exit 1
fi

# 安装 launchd plist（登录时自启动）
PLIST_SRC="$DAEMON_DIR/com.promlight.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.promlight.daemon.plist"

# 生成 plist（使用 daemon.py，它会自动调用 C helper）
cat > /tmp/com.promlight.daemon.plist <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.promlight.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>${DAEMON_PY}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DAEMON_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DAEMON_DIR}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>${DAEMON_DIR}/daemon.log</string>
    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
PLISTEOF

echo "安装 LaunchAgent..."
cp /tmp/com.promlight.daemon.plist "$PLIST_DST"
# 先卸载已有的再加载
launchctl bootout "gui/$(id -u)/com.promlight.daemon" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo ""
echo "✅ PromLight ARM64 原生 daemon 已安装并启动！"
echo ""
echo "管理命令:"
echo "  launchctl kickstart gui/$(id -u)/com.promlight.daemon   # 重启"
echo "  launchctl bootout gui/$(id -u)/com.promlight.daemon    # 停止"
echo "  launchctl bootstrap gui/\$(id -u) $PLIST_DST           # 加载"
echo ""
echo "HTTP 控制台:  http://127.0.0.1:7800"
echo "控制端口:     127.0.0.1:47800"
echo "日志:         ${DAEMON_DIR}/daemon.log"
echo ""
echo "测试命令:"
echo '  echo '"'"'{"cmd":"led green on","message":"test","session":"test1"}'"'"' | timeout 2 nc 127.0.0.1 47800'
