#!/usr/bin/env bash
# PromLight ARM64 原生 Daemon 安装脚本
# 纯 C 实现（promlightd），无 Python 依赖
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$SCRIPT_DIR/native_daemon"
DAEMON_C="$DAEMON_DIR/promlightd.c"
DAEMON_BIN="$DAEMON_DIR/promlightd"
PLIST_DST="$HOME/Library/LaunchAgents/com.promlight.daemon.plist"

echo "=== PromLight ARM64 原生 Daemon ==="
echo "安装目录: $DAEMON_DIR"

# 编译 promlightd
echo "编译 promlightd..."
if [ ! -f "$DAEMON_C" ]; then
    echo "错误: 找不到 $DAEMON_C"
    exit 1
fi

if clang -o "$DAEMON_BIN" "$DAEMON_C" \
    -framework IOKit -framework CoreFoundation \
    -arch arm64 2>&1; then
    echo "✅ 编译成功: $DAEMON_BIN"
else
    echo "❌ 编译失败"
    exit 1
fi

# 安装 launchd plist
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
        <string>${DAEMON_BIN}</string>
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
launchctl bootout "gui/$(id -u)/com.promlight.daemon" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo ""
echo "✅ PromLight daemon 已安装并启动！"
echo ""
echo "管理命令:"
echo "  launchctl kickstart gui/$(id -u)/com.promlight.daemon   # 重启"
echo "  launchctl bootout gui/$(id -u)/com.promlight.daemon    # 停止"
echo ""
echo "控制端口: 127.0.0.1:47800"
echo "HTTP API: http://127.0.0.1:7800"
echo "日志:     ${DAEMON_DIR}/daemon.log"
echo ""
echo "测试:"
echo '  echo "{\"event\":\"UserPromptSubmit\"}" | timeout 2 nc 127.0.0.1 47800'
