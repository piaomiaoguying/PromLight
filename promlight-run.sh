#!/bin/bash
# PromLight 一键重启：全部执行完后，按任意键自动关闭终端窗口
# 用法: ./promlight-run.sh   （终端里运行，或双击）
# 自动识别 Terminal.app / iTerm2，关闭当前窗口；关闭动作失败时退化为仅退出 shell
set -uo pipefail

cd "$(dirname "$0")" || exit 1

./promlight-init.sh restart
./promlight-init.sh hooks
sleep 3
./promlight-init.sh status
echo '{"cmd":"led all blink --only --count 5"}' | timeout 1 nc -w 1 127.0.0.1 47800 2>/dev/null || true

echo ""
echo "──────────────────────────────────────────────"
echo "✅ 全部执行完成，按任意键关闭窗口..."
read -n 1 -s -r

case "${TERM_PROGRAM:-}" in
    iTerm.app|iTerm2)
        osascript -e 'tell application "iTerm2" to close current window' 2>/dev/null ;;
    *)
        osascript -e 'tell application "Terminal" to close front window' 2>/dev/null ;;
esac

exit 0
