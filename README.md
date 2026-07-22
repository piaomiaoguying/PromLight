# PromLight —— BLE 桌面信号灯

**macOS ARM64 原生控制，无 Python/Node 依赖。**

[PromLight](https://mediapig.cc/) 是一个 BLE 蓝牙红绿灯硬件，通过不同的灯色和闪烁模式指示 AI 助手（Claude Code、Cursor 等）的工作状态：

- 🟢 绿灯闪烁 → AI 正在工作
- 🟡 黄灯闪烁 → AI 等待你授权/输入
- 🔴 红灯闪烁 → 出错了
- 🎨 三色渐变 → 空闲

---

## 目录

- [安装](#安装)
- [配置](#配置)
- [使用](#使用)
- [架构](#架构)
- [管理命令](#管理命令)
- [文件清单](#文件清单)
- [FAQ](#faq)

---

## 安装

### 前置条件

- Apple Silicon Mac（M1/M2/M3/M4）
- PromLight 硬件已配对蓝牙
- Xcode Command Line Tools（`clang` 编译器）

```bash
# 检查 clang
clang --version
```

### 一键安装

```bash
cd /path/to/PromLight
chmod +x promlight-init.sh
./promlight-init.sh install
```

脚本会自动：
1. **编译** promlightd（ARM64 原生二进制）
2. **安装 hook 脚本** 到 `~/.claude/scripts/promlight-hook.sh`
3. **注册 launchd** 开机自启
4. **配置 settings.json** 添加 Claude Code hook 事件

### 分步安装

```bash
# 只编译
./promlight-init.sh compile

# 只装 hooks
./promlight-init.sh hooks

# 完整查看状态
./promlight-init.sh status
```

### events.json

事件配置文件在项目根目录 `/Users/zs/Applications/PromLight/events.json`，daemon 启动时自动加载。

结构：

```json
{
  "macros": {
    "working": "led green blink --only",
    "idle":   "led all blink --freq 1500 --fade 500 --count 60",
    "error":  "led red blink --only",
    "start":  "led all blink --only --count 2 ; led all blink --freq 1500 --fade 500 --count 5",
    "end":    "led all off"
  },
  "events": {
    "UserPromptSubmit": "working",
    "Stop":             "idle",
    "SessionStart":     "start",
    "SessionEnd":       "end"
  }
}
```

**宏（macros）**：命令模板，支持 `;` 串联多条命令。  
**事件（events）**：事件名称 → 宏名，支持大小写变体。

---

## 使用

### Claude Code 自动触发

安装后 Claude Code 会自动发送事件到 PromLight：

| 事件 | 灯色 |
|------|------|
| 开始会话 (`SessionStart`) | 快闪 2 次 → 渐变闪 5 次 |
| 提交提问 (`UserPromptSubmit`) | 🟢 绿灯闪烁 |
| 使用工具 (`PreToolUse`) | 🟢 绿灯闪烁 |
| 工具失败 (`PostToolUseFailure`) | 🔴 红灯闪烁 |
| 请求授权 (`PermissionRequest`) | 🟡 黄灯闪烁 |
| 会话停止 (`Stop`) | 🎨 三色渐变 |
| 会话结束 (`SessionEnd`) | 全关 |

### 手动测试

```bash
# 直接发命令
echo '{"cmd":"led green on"}' | timeout 2 nc 127.0.0.1 47800

# 发事件名称
echo '{"event":"UserPromptSubmit"}' | timeout 2 nc 127.0.0.1 47800

# 释放 session（回到空闲状态）
echo '{"session":"my-test","release_session":true}' | timeout 2 nc 127.0.0.1 47800
```

### 命令格式

```
led <channel> <action> [options]
off                         # 全关
write <param> <value>       # 写参数
wait <ms>                   # 延时

channel: green, yellow, red, all, green+red
action:  on, off, blink
options: --only, --count N, --freq Nms, --fade Nms
```

实例：
```bash
echo '{"cmd":"led yellow on --only"}' | nc 127.0.0.1 47800
echo '{"cmd":"led green blink --only --freq 2000 --fade 500"}' | nc 127.0.0.1 47800
echo '{"cmd":"led red blink --only --count 10"}' | nc 127.0.0.1 47800
```

---

## 架构

```
┌─────────────────────────────────────────────────┐
│  Claude Code                                    │
│  hooks: UserPromptSubmit → promlight-hook.sh    │
└──────────────┬──────────────────────────────────┘
               │ TCP 47800
               ▼
┌─────────────────────────────────────────────────┐
│  promlightd (ARM64 原生 C, ~70KB)               │
│                                                   │
│  ┌──────────┐  ┌─────────────┐  ┌────────────┐ │
│  │ events   │  │ 命令编译     │  │ session     │ │
│  │ .json    │→ │ → 5E5E 帧   │→ │ 管理        │ │
│  └──────────┘  └──────┬──────┘  └────────────┘ │
└────────────────────────┼─────────────────────────┘
                         │ IOHIDDeviceSetReport()
                         ▼
┌─────────────────────────────────────────────────┐
│  PromLight 蓝牙硬件                               │
│  芯片: 大概率 nRF52 系列 BLE SoC                  │
└─────────────────────────────────────────────────┘
```

### 数据流

1. Claude Code 触发 hook → 运行 `promlight-hook.sh`
2. Hook 脚本发送 `{"event":"UserPromptSubmit"}` 到 `127.0.0.1:47800`
3. `promlightd` 收到后查 `events.json`：`UserPromptSubmit → working`
4. 查 macros：`working → led green blink --only`
5. 编译命令为 5E5E 南向帧：`5E5E0604010201FFFF`
6. 通过 `IOHIDDeviceSetReport()` 写入 BLE HID 设备

### 为什么不用 Python daemon？

macOS ARM64 的 Python `hidapi` 对 BLE HID 设备调用 `IOHIDDeviceSetReport` 时，返回成功但数据不发送到实际设备。必须通过 ARM64 原生 C 直接调用 IOKit 的 `IOHIDDeviceSetReport` + `kIOHIDOptionsTypeSeizeDevice` + 64 字节固定报告长度。

---

## 管理命令

所有管理通过 `promlight-init.sh`：

```bash
./promlight-init.sh install    # 全量安装（编译 + hooks + launchd）
./promlight-init.sh restart    # 重启 daemon
./promlight-init.sh status     # 查看状态
./promlight-init.sh hooks      # 重新部署 hook 配置
./promlight-init.sh compile    # 仅重新编译
./promlight-init.sh uninstall  # 卸载
```

### 手动管理

```bash
# 启动/停止
launchctl kickstart gui/$(id -u)/com.promlight.daemon
launchctl bootout gui/$(id -u)/com.promlight.daemon

# 查看日志
tail -f /Users/zs/Applications/PromLight/native_daemon/daemon.log
```

### HTTP API

| 路径 | 说明 |
|------|------|
| `http://127.0.0.1:7800/` | 状态 |
| `http://127.0.0.1:7800/api/status` | daemon 状态 |
| `http://127.0.0.1:7800/api/devices` | 设备信息 |
| `http://127.0.0.1:7800/api/config` | 已加载的宏/事件配置 |

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `native_daemon/promlightd.c` | C 源码（ARM64 daemon） |
| `native_daemon/promlightd` | 编译好的 ARM64 二进制 |
| `promlight-hook.sh` | Claude Code hook 脚本 |
| `promlight-init.sh` | 初始化/管理脚本 |
| `events.json` | 事件→灯色映射（用户配置） |
| `native_daemon/daemon.log` | 运行日志 |

---

## Git 管理

```bash
cd /path/to/PromLight
git init
git add .
git commit -m "Initial commit"
```

`.gitignore` 建议：
```
*.log
native_daemon/promlightd
__pycache__/
```

---

## FAQ

### Q: 灯不亮？

```bash
./promlight-init.sh status
```

检查输出：
- daemon 运行中？
- Binary 存在？
- CTRL 端口可用？
- 日志里有无 error？

### Q: 蓝牙找不到设备？

确认 PromLight 已在系统蓝牙设置中配对。`系统设置 → 蓝牙 → PromLight → 已连接`。

### Q: 想改灯色？

编辑 `events.json`，daemon 自动热加载。

### Q: 想自定义空闲状态？

`events.json` 中的 `idle` 宏：
```json
"idle": "led green blink --only --freq 3000 --fade 1000"
```

### Q: 启动脚本放哪里？

已通过 `launchd` 自动启动。也可使用其他进程管理器（pm2、supervisor 等）。

### Q: 和原版 Rosetta daemon 的关系？

**完全替代。** 原版 PromLight.app 是 x86_64 Nuitka 编译的 Python，通过 Rosetta 2 运行。`promlightd` 是 ARM64 原生 C，性能更好、零依赖、更省电。

---

## 协议

MIT
