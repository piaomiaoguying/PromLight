# PromLight —— BLE 桌面信号灯

**macOS ARM64 原生控制，零依赖守护进程。**

[PromLight](https://mediapig.cc/) 是一个 BLE 蓝牙红绿灯硬件，通过不同的灯色和闪烁模式指示 AI 助手（Claude Code 等）的工作状态：

- 🟢 绿灯闪烁 → AI 正在工作
- 🟡 黄灯闪烁 → AI 等待你授权/输入
- 🔴 红灯闪烁 → 出错了
- 🎨 三色渐变 → 空闲

---

## 目录

- [快速开始](#快速开始)
- [配置](#配置)
- [使用](#使用)
- [架构](#架构)
- [命令参考](#命令参考)
- [管理](#管理)
- [文件清单](#文件清单)
- [FAQ](#faq)

---

## 快速开始

### 前置条件

- Apple Silicon Mac（M1/M2/M3/M4）
- PromLight 硬件已在系统蓝牙中配对
- Xcode Command Line Tools（`clang`）

```bash
clang --version
```

### 一键安装

```bash
cd /path/to/PromLight
chmod +x promlight-init.sh
./promlight-init.sh install
```

安装脚本自动完成：

1. 编译 `promlightd`（ARM64 原生二进制，~72KB）
2. 安装 hook 脚本到 `~/.claude/scripts/promlight-hook.sh`
3. 注册 launchd 开机自启（立即启动）
4. 配置 Claude Code 事件钩子到 `settings.json`

> **项目可以放在任意路径**，安装脚本和 daemon 都通过相对路径定位，不受位置影响。

### 分步命令

```bash
./promlight-init.sh compile    # 只编译
./promlight-init.sh hooks      # 只装 hook 配置
./promlight-init.sh status     # 查看完整状态
```

---

## 配置

### events.json — 事件→灯色映射

配置文件为项目根目录的 `events.json`，daemon 启动时自动加载。

```json
{
  "macros": {
    "working": "led green blink --only",
    "idle":   "led all blink --freq 1500 --fade 500 --count 60",
    "awaiting": "led yellow blink --only",
    "waiting": "led yellow on --only",
    "error":  "led red blink --only",
    "start":  "led all blink --only --count 2 ; led all blink --freq 1500 --fade 500 --count 5",
    "end":    "led all off"
  },
  "events": {
    "UserPromptSubmit": "working",
    "PreToolUse":       "working",
    "PostToolUseFailure": "error",
    "PermissionRequest":  "awaiting",
    "Stop":             "idle",
    "SessionStart":     "start",
    "SessionEnd":       "end"
  }
}
```

**宏（macros）**：命令模板，支持 `;` 串联多条命令。  
**事件（events）**：事件名称 → 宏名映射，大小写不敏感。

> 编辑后无需重启——daemon 每次处理新事件时重新扫描配置文件，自动热加载。

### launchd plist

launchd 配置模板位于 `native_daemon/com.promlight.daemon.plist`，安装时复制到 `~/Library/LaunchAgents/` 并注册。它通过 shell wrapper 脚本间接调用二进制，所以项目移动后只需更新 plist 中的路径即可：

```xml
<key>ProgramArguments</key>
<array>
    <string>.../native_daemon/promlightd.sh</string>
</array>
<key>WorkingDirectory</key>
<string>.../native_daemon</string>
```

### Claude Code hooks

Hook 脚本 `promlight-hook.sh` 安装到 `~/.claude/scripts/`，`settings.json` 中注册了以下事件：

| 事件 | 触发时机 |
|------|----------|
| `SessionStart` | 会话开始 |
| `UserPromptSubmit` | 提交提问给 AI |
| `PreToolUse` / `PostToolUse` | 工具执行前后 |
| `PostToolUseFailure` | 工具执行失败 |
| `PermissionRequest` | 请求授权 |
| `PermissionDenied` | 授权被拒 |
| `Elicitation` | AI 等待用户回复 |
| `Stop` | 会话停止 |
| `StopFailure` | 停止失败 |
| `SessionEnd` | 会话结束 |

---

## 使用

### 自动触发

Claude Code 安装 hook 后自动发送事件到 PromLight，无需手动操作：

| 事件 | 灯色 |
|------|------|
| 会话开始 | 快闪 2 次 → 渐变闪 5 次 |
| 提交提问 / 工具执行 | 🟢 绿灯闪烁 |
| 工具失败 | 🔴 红灯闪烁 |
| 请求授权 | 🟡 黄灯闪烁 |
| AI 等待输入 | 🟡 黄灯常亮 |
| 会话停止 | 🎨 三色渐变 |
| 会话结束 | 全关 |

### 手动测试

```bash
# 直接发命令 —— 绿灯常亮
echo '{"cmd":"led green on"}' | timeout 2 nc 127.0.0.1 47800

# 发事件名称 —— 触发 events.json 映射
echo '{"event":"UserPromptSubmit"}' | timeout 2 nc 127.0.0.1 47800

# 释放 session（回到空闲状态）
echo '{"session":"my-test","release_session":true}' | timeout 2 nc 127.0.0.1 47800

# 多步命令 —— 闪烁 3 次后关灯
echo '{"cmd":"led green blink --only --count 3 ; led all off"}' | nc 127.0.0.1 47800
```

### 命令格式

```
led <channel> <action> [options]     # 控制 LED
off                                   # 全关
write <param> <value>                 # 写参数
wait <ms>                             # 延时（用于 ; 串联）
```

| 参数 | 可选值 |
|------|--------|
| `channel` | `green`, `yellow`, `red`, `all`, `green+red` |
| `action` | `on`, `off`, `blink` |
| `--only` | 仅操作指定的通道，不影响其他通道 |
| `--count N` | 闪烁次数 |
| `--freq Nms` | 闪烁周期（毫秒，默认 1000） |
| `--fade Nms` | 渐入渐出时间（毫秒） |

示例：

```bash
# 只亮绿灯
echo '{"cmd":"led green on --only"}' | nc 127.0.0.1 47800

# 绿灯渐闪
echo '{"cmd":"led green blink --only --freq 2000 --fade 500"}' | nc 127.0.0.1 47800

# 红灯闪 10 次
echo '{"cmd":"led red blink --only --count 10"}' | nc 127.0.0.1 47800

# 写参数：LED 频率 500ms
echo '{"cmd":"write led.freq 500"}' | nc 127.0.0.1 47800
```

### HTTP API

Daemon 内置 HTTP 服务（端口 7800），可用于状态查询和远程管理：

| 路径 | 说明 |
|------|------|
| `http://127.0.0.1:7800/` | 健康检查 |
| `http://127.0.0.1:7800/api/status` | Daemon 状态（含 HID 连接状态） |
| `http://127.0.0.1:7800/api/devices` | 已连接的设备信息 |
| `http://127.0.0.1:7800/api/config` | 已加载的宏和事件配置 |

```bash
curl -s http://127.0.0.1:7800/api/status | python3 -m json.tool
```

---

## 架构

```
┌─────────────────────────────────────────────────────┐
│  Claude Code / Cursor / 其他 AI 工具                 │
│  hooks → promlight-hook.sh → TCP 127.0.0.1:47800    │
└──────────────────────────┬──────────────────────────┘
                           │ JSON 命令/事件
                           ▼
┌─────────────────────────────────────────────────────┐
│  promlightd  (ARM64 原生 C, ~72KB)                   │
│                                                       │
│  ┌──────────┐   ┌─────────────┐   ┌──────────────┐ │
│  │ events   │   │ 命令编译     │   │ session      │ │
│  │ .json    │→  │ → 5E5E 帧   │→  │ 管理         │ │
│  └──────────┘   └──────┬──────┘   └──────────────┘ │
│                         │                             │
│            HTTP API (7800) — 状态查询                 │
└──────────────────────────┼───────────────────────────┘
                           │ IOHIDDeviceSetReport()
                           ▼
┌─────────────────────────────────────────────────────┐
│  PromLight 蓝牙硬件 (nRF52 BLE SoC)                   │
│  5E5E 南向协议帧 → LED 控制                          │
└─────────────────────────────────────────────────────┘
```

### 数据流

1. Claude Code 触发 hook → 运行 `promlight-hook.sh`
2. Hook 脚本发送 `{"event":"UserPromptSubmit"}` 到 `127.0.0.1:47800`
3. `promlightd` 收到后查 `events.json`：`UserPromptSubmit → working`
4. 查 macros：`working → led green blink --only`
5. 编译人可读命令为 5E5E 南向帧：`5E5E0604010201FFFF`
6. 通过 `IOHIDDeviceSetReport()` 写入 BLE HID 设备

### 技术说明

- **纯 C 实现**，直接调用 IOKit 框架的 `IOHIDDeviceSetReport`，不走系统蓝牙抽象层
- **64 字节固定报告长度** + `kIOHIDOptionsTypeSeizeDevice` 独占模式
- **多 session 管理**：支持同时管理多个 AI 工具的灯色状态，最后一个 session 释放后自动回到 idle
- **ARM64 only**：当前仅支持 Apple Silicon Mac，需要 Rosetta 2 的原版 daemon 见下方 FAQ

### 为什么不用 Python daemon？

macOS ARM64 的 Python `hidapi` 对 BLE HID 设备调用 `IOHIDDeviceSetReport` 时，返回成功但数据不发送到实际设备。必须通过 ARM64 原生 C 直接调用 IOKit 函数。

---

## 管理

### promlight-init.sh

项目自带的管理脚本，所有操作通过它完成：

```bash
./promlight-init.sh install     # 全量安装（编译 + hooks + launchd）
./promlight-init.sh compile     # 仅重新编译 promlightd
./promlight-init.sh hooks       # 重新部署 hook 脚本和配置
./promlight-init.sh restart     # 重启 daemon
./promlight-init.sh status      # 查看完整状态
./promlight-init.sh uninstall   # 卸载
```

### 手动管理

```bash
# launchd
launchctl kickstart gui/$(id -u)/com.promlight.daemon   # 启动
launchctl bootout gui/$(id -u)/com.promlight.daemon     # 停止
launchctl list | grep promlight                         # 查看运行状态

# 直接启动（调试）
./native_daemon/promlightd -v

# 查看日志
tail -f native_daemon/daemon.log
```

### 项目移动

项目整体挪到其他目录后，只需两步：

```bash
# 1. 停掉 daemon
launchctl bootout gui/$(id -u)/com.promlight.daemon

# 2. 移动项目后更新 plist 路径
#   编辑 ~/Library/LaunchAgents/com.promlight.daemon.plist
#   把 old_path 改为 new_path，然后：
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.promlight.daemon.plist
```

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `native_daemon/promlightd.c` | C 源码（ARM64 daemon，~1000 行） |
| `native_daemon/promlightd` | 编译好的 ARM64 二进制（~72KB） |
| `native_daemon/promlightd.sh` | Launchd wrapper 脚本（自动定位二进制路径） |
| `native_daemon/com.promlight.daemon.plist` | Launchd 配置模板 |
| `promlight-hook.sh` | Claude Code hook 发送器 |
| `promlight-init.sh` | 安装/管理脚本（编译、部署、卸载） |
| `events.json` | 事件→灯色映射配置文件（用户可编辑） |

---

## FAQ

### 灯不亮？

```bash
./promlight-init.sh status
```

检查输出：
- daemon 运行中（PID 存在）
- Binary 已编译
- CTRL 端口 47800 可用
- HTTP API 7800 可用
- 日志中有无 error 信息

### 蓝牙找不到设备？

确认 PromLight 已在系统蓝牙中配对：`系统设置 → 蓝牙 → PromLight → 已连接`。

### 想改灯色？

编辑 `events.json`，daemon 自动热加载。例如把"工作中"从绿色改为黄色：

```json
"working": "led yellow blink --only"
```

不改事件名，不影响 hook 配置。

### 想自定义空闲动画？

修改 `events.json` 中的 `idle` 宏：

```json
"idle": "led green blink --only --freq 3000 --fade 1000"
```

### 启动日志在哪里？

`native_daemon/daemon.log`。launchd 模式下 stderr/stdout 都写到这里。

### 如何完全卸载？

```bash
./promlight-init.sh uninstall
```

手动清理残留：
```bash
rm -f ~/.claude/scripts/promlight-hook.sh
# settings.json 中的 hooks 需要手动删除
```

### 如何不装 lauchd、只跑一次？

```bash
# 编译后直接运行
./promlight-init.sh compile
./native_daemon/promlightd
# 按 Ctrl+C 退出
```

### 和原版 PromLight.app 的关系？

**完全替代。** 原版是 x86_64 Nuitka 编译的 Python，通过 Rosetta 2 运行。`promlightd` 是 ARM64 原生 C，性能更好、零依赖、更省电。

---

## 协议

MIT
