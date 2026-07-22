#!/bin/bash
# promlight-hook.sh - Claude Code hook → PromLight 信号灯
# 安装位置: ~/.claude/scripts/promlight-hook.sh
# 通过 promlight-init.sh install 自动部署

EVENT="$1"
[ -z "$EVENT" ] && exit 0

# 发送事件到 PromLight daemon（不等待响应，不阻塞 hook）
echo "{\"event\":\"$EVENT\"}" | timeout 1 nc -w 1 127.0.0.1 47800 2>/dev/null || true
