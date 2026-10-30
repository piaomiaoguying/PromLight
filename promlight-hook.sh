#!/bin/bash
# promlight-hook.sh - Claude Code hook → PromLight 信号灯
# 安装位置: ~/.claude/scripts/promlight-hook.sh
# 通过 promlight-init.sh install 自动部署
#
# daemon 不常驻：端口通就直接发；不通则用 launchctl 拉起再发。
# 由 launchd 负责单例，多个 hook 并发触发也只会起一个进程。

EVENT="$1"
[ -z "$EVENT" ] && exit 0

PORT=47800
LABEL="gui/$(id -u)/com.promlight.daemon"

send() {
    printf '{"event":"%s"}' "$EVENT" | timeout 1 nc -w 1 127.0.0.1 "$PORT" 2>/dev/null
}

# 快路径：daemon 已在运行
if nc -z -w 1 127.0.0.1 "$PORT" 2>/dev/null; then
    send
    exit 0
fi

# 慢路径：拉起 daemon 后再发
launchctl kickstart "$LABEL" 2>/dev/null

# 等待端口就绪（hid 初始化约 1s，最多等 3s）
for _ in 1 2 3 4 5 6; do
    sleep 0.5
    if nc -z -w 1 127.0.0.1 "$PORT" 2>/dev/null; then
        send
        exit 0
    fi
done

exit 0
