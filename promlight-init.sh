#!/bin/bash
# PromLight 初始化/管理脚本
# 用法: ./promlight-init.sh [install|restart|status|uninstall|hooks]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$SCRIPT_DIR/native_daemon"
DAEMON_BIN="$DAEMON_DIR/promlightd"
DAEMON_C="$DAEMON_DIR/promlightd.c"
HOOK_SCRIPT="$SCRIPT_DIR/promlight-hook.sh"
EVENTS_JSON="$SCRIPT_DIR/events.json"
PLIST_DST="$HOME/Library/LaunchAgents/com.promlight.daemon.plist"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1"; }

# ── 编译 daemon ──────────────────────────────────────────────
cmd_compile() {
    echo "编译 promlightd..."
    if [ ! -f "$DAEMON_C" ]; then err "找不到 $DAEMON_C"; return 1; fi
    if clang -o "$DAEMON_BIN" "$DAEMON_C" \
        -framework IOKit -framework CoreFoundation -arch arm64 2>&1
    then info "编译成功: $DAEMON_BIN"
    else err "编译失败"; return 1; fi
}

# ── 安装 hook 脚本 ──────────────────────────────────────────
cmd_install_hooks() {
    local hook_dst="$HOME/.claude/scripts/promlight-hook.sh"
    mkdir -p "$HOME/.claude/scripts"
    if [ -f "$HOOK_SCRIPT" ]; then
        cp "$HOOK_SCRIPT" "$hook_dst"
        chmod +x "$hook_dst"
        info "Hook 脚本已安装到 $hook_dst"
    else
        warn "Hook 脚本不存在: $HOOK_SCRIPT"
    fi

    # 更新 Claude Code settings.json
    local settings="$HOME/.claude/settings.json"
    if [ ! -f "$settings" ]; then
        warn "settings.json 不存在，跳过 hook 配置"
        return 0
    fi

    # 合并 hook 配置到 settings.json
    python3 -c "
import json, sys
settings_path = '$settings'
hook_cmd = '$hook_dst'

with open(settings_path) as f:
    cfg = json.load(f)

if 'hooks' not in cfg:
    cfg['hooks'] = {}

# 已有 hooks 不做覆盖
existing_hooks = set(cfg['hooks'].keys())

events = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PostToolUseFailure', 'PermissionRequest', 'PermissionDenied',
          'Elicitation', 'Stop', 'SessionEnd', 'StopFailure']

added = 0
for ev in events:
    if ev in existing_hooks:
        # 已有 hook 组但没有 promlight，追加进去
        found_promlight = False
        for group in cfg['hooks'][ev]:
            for h in group.get('hooks', []):
                if hook_cmd in h.get('command', ''):
                    found_promlight = True
        if not found_promlight:
            cfg['hooks'][ev].append({'hooks': [{
                'type': 'command',
                'command': f'{hook_cmd} {ev}',
                'async': True
            }]})
            added += 1
    else:
        cfg['hooks'][ev] = [{'hooks': [{
            'type': 'command',
            'command': f'{hook_cmd} {ev}',
            'async': True
        }]}]
        added += 1

with open(settings_path, 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)

print(f'Added {added} hook events to settings.json')
"
    info "Hook 配置已完成"
}

# ── launchd 管理 ────────────────────────────────────────────
cmd_launchd_install() {
    local plist_src="$DAEMON_DIR/com.promlight.daemon.plist"
    cat > "$plist_src" <<PLISTEOF
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

    cp "$plist_src" "$PLIST_DST"
    launchctl bootout "gui/$(id -u)/com.promlight.daemon" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
    info "launchd 已注册"
}

cmd_launchd_uninstall() {
    launchctl bootout "gui/$(id -u)/com.promlight.daemon" 2>/dev/null || true
    rm -f "$PLIST_DST"
    info "launchd 已卸载"
}

# ── 命令 ─────────────────────────────────────────────────────
cmd_install() {
    echo "=== PromLight 全量安装 ==="
    cmd_compile
    cmd_install_hooks
    cmd_launchd_install
    echo ""
    info "安装完成！daemon 已启动。"
}

cmd_restart() {
    echo "重启 promlightd..."
    launchctl bootout "gui/$(id -u)/com.promlight.daemon" 2>/dev/null || true
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || {
        warn "launchd 未安装，直接启动..."
        # 直接启动
        killall promlightd 2>/dev/null || true
        sleep 0.5
        "$DAEMON_BIN" &
    }
    info "daemon 已重启"
}

cmd_status() {
    echo "=== PromLight 状态 ==="
    # daemon
    if launchctl print "gui/$(id -u)/com.promlight.daemon" 2>/dev/null | grep -q "state = running"; then
        local pid=$(launchctl print "gui/$(id -u)/com.promlight.daemon" 2>/dev/null | grep "pid" | awk '{print $NF}')
        info "daemon 运行中 (PID=$pid)"
    elif pgrep -x promlightd >/dev/null 2>&1; then
        info "daemon 运行中 (直接启动)"
    else
        warn "daemon 未运行"
    fi

    # binary
    if [ -f "$DAEMON_BIN" ]; then
        local size_kb=$(( $(wc -c < "$DAEMON_BIN") / 1024 ))
        info "Binary: $DAEMON_BIN (${size_kb}KB)"
    else
        warn "Binary 不存在"
    fi

    # hook
    local hook="$HOME/.claude/scripts/promlight-hook.sh"
    if [ -f "$hook" ]; then
        info "Hook 脚本: $hook"
    else
        warn "Hook 脚本未安装"
    fi

    # settings.json hooks
    local hook_count=$(grep -c "$hook" "$HOME/.claude/settings.json" 2>/dev/null || echo 0)
    if [ "$hook_count" -gt 0 ]; then
        info "Claude Code hooks: $hook_count 个"
    else
        warn "settings.json 中无 promlight hooks"
    fi

    # events.json
    if [ -f "$EVENTS_JSON" ]; then
        local evcount=$(grep -c ':' "$EVENTS_JSON" 2>/dev/null || echo 0)
        info "events.json: $EVENTS_JSON ($evcount 配置项)"
    else
        warn "events.json 不存在"
    fi

    # HTTP API
    if nc -z -w 1 127.0.0.1 7800 2>/dev/null; then
        info "HTTP API: http://127.0.0.1:7800 (可用)"
    else
        warn "HTTP API 不可用"
    fi

    # CTRL port
    if nc -z -w 1 127.0.0.1 47800 2>/dev/null; then
        info "CTRL 端口: 127.0.0.1:47800 (可用)"
    else
        warn "CTRL 端口不可用"
    fi
}

cmd_uninstall() {
    echo "=== 卸载 PromLight ==="
    cmd_launchd_uninstall
    killall promlightd 2>/dev/null || true
    rm -f "$HOME/.claude/scripts/promlight-hook.sh"
    warn "settings.json 中的 hooks 未自动移除，请手动清理"
    info "卸载完成"
}

cmd_hooks() {
    echo "=== 重新安装 Hook 配置 ==="
    cmd_install_hooks
    info "Hook 配置完成"
}

# ── 主入口 ──────────────────────────────────────────────────
case "${1:-install}" in
    install)  cmd_install ;;
    restart)  cmd_restart ;;
    status)   cmd_status ;;
    uninstall) cmd_uninstall ;;
    hooks)    cmd_hooks ;;
    compile)  cmd_compile ;;
    *)
        echo "用法: $0 [install|restart|status|uninstall|hooks|compile]"
        exit 1
        ;;
esac
