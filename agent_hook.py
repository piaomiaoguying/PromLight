"""
agent_hook.py — hook 客户端

约束：
  - 任何失败都静默 exit 0。
  - 绝不向 stdout 打印——**例外：阻塞/门控型 hook**：Cursor（agent=="cursor" 且事件命中 _CURSOR_STDOUT）
    与 Antigravity（agent=="antigravity"，每个事件都回写 allow）必须向 stdout 回写决策 JSON（其协议要求），
    否则会挂起/阻断用户的 agent。对 Claude/Codex 等仍绝不打印。
"""

# 本文件自身版本（独立编号，与 daemon 版本无关；仅本文件内容变化时 +1，供客户升级对照）
__version__ = "1.1.2"

import json
import os
import socket
import sys

_HOST = "127.0.0.1"
_DEFAULT_PORT = 47800
_CONNECT_TIMEOUT = 0.3  # 秒

# 会话结束事件（触发释放占用的灯）：Claude=SessionEnd；Cursor=sessionEnd。Codex 无会话结束事件。
_SESSION_END_EVENTS = {"SessionEnd", "sessionEnd"}

_KNOWN_AGENTS = ("claude", "codex", "cursor", "copilot", "qoder", "codebuddy", "antigravity")

# Cursor 阻塞型 hook 必须向 stdout 回写决策（其余事件不碰 stdout）。固定放行/不抢决策：
#   beforeSubmitPrompt → {"continue": true}（永远放行用户提交）
#   beforeShellExecution / beforeMCPExecution → {"permission": "ask"}（交回 Cursor/用户正常审批，不替其决定）
_CURSOR_STDOUT = {
    "beforeSubmitPrompt":   {"continue": True},
    "beforeShellExecution": {"permission": "ask"},
    "beforeMCPExecution":   {"permission": "ask"},
}

# Antigravity 的 hook 是门控型：读 stdin、期望 stdout 回写含 decision 的 JSON（allow/deny/ask）。
# 状态灯绝不替用户决策，故对 Antigravity 注册的**每个**事件都先向 stdout 回写放行（与该事件是否真门控
# 无关，最安全），再去连 socket——即便后续连不上或异常，用户的工具调用也已被放行、绝不被阻断。
# 待真机核对：确切的放行字段（decision=allow 为多来源一致说法）。详见 docs/antigravity接入方案.md。
_ANTIGRAVITY_ALLOW = {"decision": "allow"}

# 内置默认表（外部配置缺失/非法时回退）。与 events.json 同构、同逻辑：
# 事件 → 分类名(macro)，macro → 原始北向命令。两层缺一即回退到这里的内置默认。
_DEFAULT_MACROS = {
    "work": "led yellow on --only",
    "await": "led yellow blink --only",
    "idle": "led green on --only",
    "error": "led red blink --only",
    "start": "led all blink --only --count 2; wait 2s; led green on",
    "end": "led green blink --freq 4000 --fade 2000",
}

# 优先读取外部 events.json 文件。失败时使用下列的内置默认表。
# 默认事件分类表：事件 → 分类名(macro)。分类名即上表的 key；事件分类后会执行对应的原始命令。
_DEFAULT_EVENTS = {
    # Antigravity（PascalCase，与 Claude 同名同义）：SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop
    #   均命中下方条目，无需新增（事件全集/会话结束事件待真机核对，见 docs/antigravity接入方案.md）。
    # Claude / Codex / Qoder / CodeBuddy（PascalCase；Qoder 的 UserPromptSubmit/PreToolUse/PostToolUse/Stop 与此同名同义，复用；
    # Qoder 独有的 PostToolUseFailure 见下；Qoder 无 SessionStart/SessionEnd。CodeBuddy 是 Claude schema 翻版、
    # 事件名同为 PascalCase 且为 Claude 全集的子集——SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/
    # PostToolUseFailure/PermissionRequest/Stop/SessionEnd/StopFailure 全部命中此处，无需新增条目）
    "SessionStart": "start",
    "UserPromptSubmit": "work",
    "PreToolUse": "work",
    "PostToolUse": "work",
    "PostToolUseFailure": "error",  # Qoder（也是 Claude 全集事件之一）：工具失败 → 红灯报错
    "PermissionRequest": "await",
    "PermissionDenied": "await",
    "Elicitation": "await",
    "SubagentStart": "work",
    "SubagentStop": "work",
    "PreCompact": "work",
    "PostCompact": "work",
    "Stop": "idle",
    "SessionEnd": "end",
    "StopFailure": "error",
    # Cursor（camelCase，与上面大小写不同、互不冲突）
    "sessionStart": "start",
    "beforeSubmitPrompt": "work",
    "afterFileEdit": "work",
    "postToolUse": "work",
    "beforeShellExecution": "await",
    "beforeMCPExecution": "await",
    "stop": "idle",
    "sessionEnd": "end",
    # Copilot CLI（camelCase；sessionStart/postToolUse/sessionEnd 与 Cursor 同名同义，复用上面的条目，
    # 此处只补 Copilot 独有的事件）。只映射非阻塞事件——preToolUse/permissionRequest 刻意不接（fail-closed）。
    "userPromptSubmitted": "work",
    "agentStop": "idle",
    "errorOccurred": "error",
}
_MAX_MACRO_DEPTH = 8

######################################### 以下脚本请勿修改 #########################################

_DETAIL_FIELDS = {
    "SessionStart": ["source"],
    "UserPromptSubmit": ["prompt"],
    "PreToolUse": ["tool_name"],
    "PostToolUse": ["tool_name"],
    "PostToolUseFailure": ["tool_name"],  # Qoder：工具失败，展示工具名
    "PermissionRequest": ["tool_name"],
    "PermissionDenied": ["tool_name"],
    "SubagentStart": ["agent_type"],
    "SubagentStop": ["agent_type"],
    "PreCompact": ["trigger"],
    "PostCompact": ["trigger"],
    "Stop": ["stop_hook_active"],
    "SessionEnd": ["reason"],
    "StopFailure": ["error_type"],
    # Cursor 事件展示字段（纯展示，不影响点灯）
    "beforeSubmitPrompt": ["prompt"],
    "afterFileEdit": ["file_path"],
    "beforeShellExecution": ["command"],
    "stop": ["status"],
    # Copilot CLI 事件展示字段（Copilot payload 用 camelCase：toolName/initialPrompt 等；纯展示）
    "userPromptSubmitted": ["prompt"],
    "postToolUse": ["toolName"],
    "agentStop": ["stopReason"],
    "errorOccurred": ["error"],
}
_TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied")
_TOOL_INPUT_FIELDS = {
    "Bash": ["command"], "Read": ["file_path"], "Edit": ["file_path"], "Write": ["file_path"],
    "NotebookEdit": ["notebook_path"], "Grep": ["pattern"], "Glob": ["pattern"],
    "Task": ["description"], "WebFetch": ["url"], "WebSearch": ["query"],
}
_TOOL_INPUT_FALLBACK = ["command", "file_path", "path", "pattern", "query", "url", "description"]


def _config_path() -> str:
    """config.json 路径：env PROM_LIGHT_CONFIG 优先，否则本脚本同目录。"""
    v = os.environ.get("PROM_LIGHT_CONFIG", "").strip()
    if v:
        return v
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _get_port() -> int:
    """连接端口：env PROM_LIGHT_PORT > config.json 的 "port" > 默认。"""
    raw = os.environ.get("PROM_LIGHT_PORT", "")
    try:
        p = int(raw)
        if 1 <= p <= 65535:
            return p
    except (TypeError, ValueError):
        pass
    try:
        with open(_config_path(), "r", encoding="utf-8-sig") as f:
            p = int(json.load(f).get("port"))
        if 1 <= p <= 65535:
            return p
    except Exception:
        pass
    return _DEFAULT_PORT


def _events_path() -> str:
    """events.json 路径：env PROM_LIGHT_EVENTS 优先，否则本脚本同目录。"""
    v = os.environ.get("PROM_LIGHT_EVENTS", "").strip()
    if v:
        return v
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.json")


def _load_config():
    """读取 events.json 一次，返回 `(cfg, notice)`。

    **静默回退是防呆要点**：文件**不存在** = 零配置正常运行，`notice` 为空；文件**存在但读不出/JSON 非法/
    顶层不是对象/`events`·`macros` 段类型不对** = 用户改坏了、其编辑被整份或分段忽略并回退内置默认——此时
    `notice` 填一句用户友好提示（现象+位置+一步动作），由 `main` 经线协议送 daemon 在终端/Web 事件流显示，
    免得用户「改了没反应」一头雾水。返回的 `cfg` 始终可安全交给 `_events_from`/`_macros_from`（非法即回退默认）。"""
    path = _events_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}, ""
    except Exception as e:
        return {}, "events.json 格式有误、已临时改用默认设置；请检查并修正：%s（%s）" % (path, e)
    if not isinstance(cfg, dict):
        return {}, "events.json 顶层应是一个对象 {…}、已临时改用默认设置；请检查：%s" % path
    bad = [name for name in ("events", "macros") if name in cfg and not isinstance(cfg.get(name), dict)]
    if bad:
        return cfg, "events.json 的 %s 段格式有误、被忽略并回退默认；请检查：%s" % ("、".join(bad), path)
    return cfg, ""


def _events_from(cfg: dict) -> dict:
    """从已读 cfg 解析事件→宏映射。

    **文件叠加在内置默认之上**（而非整张替换）：内置 `_DEFAULT_EVENTS` 含全部已知 agent 的事件（含 Cursor），
    用户 events.json 的条目覆盖默认。如此老用户**未被触碰的旧 events.json** 也自动支持新增 agent/事件，无需手改
    （updater 从不覆盖用户 events.json）。代价：原先「删掉某键=禁用」变为「删掉=回落内置默认」，要禁用须显式改值。"""
    events = dict(_DEFAULT_EVENTS)
    raw = cfg.get("events") if isinstance(cfg, dict) else None
    if isinstance(raw, dict):
        events.update({str(k): str(v).strip() for k, v in raw.items() if isinstance(v, str) and v.strip()})
    return events


def _macros_from(cfg: dict) -> dict:
    """从已读 cfg 解析 macros 表；缺失/非法回退内置默认。"""
    macros = dict(_DEFAULT_MACROS)
    raw = cfg.get("macros") if isinstance(cfg, dict) else None
    if isinstance(raw, dict):
        macros = {
            str(k).strip().lower(): str(v).strip()
            for k, v in raw.items()
            if isinstance(v, str) and v.strip()
        }
    return macros


def _load_events() -> dict:
    """加载事件→宏映射；任何问题都回退内置默认（独立入口，供测试/外部复用）。"""
    return _events_from(_load_config()[0])


def _load_macros() -> dict:
    """加载 macros 表；缺失/非法回退内置默认（独立入口，供测试/外部复用）。"""
    return _macros_from(_load_config()[0])


def _expand_macros(cmd: str, macros: dict) -> str:
    """展开映射；任何异常都退化为原样返回。"""
    def expand(text, depth, seen):
        out = []
        for clause in str(text).split(";"):
            c = clause.strip()
            if not c:
                continue
            key = c.lower()
            if key in macros and depth < _MAX_MACRO_DEPTH and key not in seen:
                out.append(expand(macros[key], depth + 1, seen | {key}))
            else:
                out.append(c)
        return " ; ".join(out)

    try:
        return expand(cmd, 0, frozenset())
    except Exception:
        return cmd


def _clip(value, n: int = 120) -> str:
    """转字符串并截断，折叠换行/多空格。"""
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[:n] + "…"


def _summarize(event: str, payload: dict) -> dict:
    """抽取该事件值得展示的字段（截断长串）。"""
    out = {}
    for key in _DETAIL_FIELDS.get(event, []):
        if payload.get(key) not in (None, ""):
            out[key] = _clip(payload[key])
    if event in _TOOL_EVENTS:
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            tool = str(payload.get("tool_name", ""))
            for k in _TOOL_INPUT_FIELDS.get(tool, _TOOL_INPUT_FALLBACK):
                if tool_input.get(k) not in (None, ""):
                    out[k] = _clip(tool_input[k])
                    break
    if not out and payload.get("message") not in (None, ""):
        out["message"] = _clip(payload["message"])
    return out


def _build_message(event: str, payload: dict) -> str:
    """拼成一行展示串：事件名 + 详情字段。"""
    detail = _summarize(event, payload)
    tail = "  ".join(f"{k}={v}" for k, v in detail.items())
    return (f"{event:16} {tail}").rstrip()


def _read_payload() -> dict:
    """读取 stdin 的 JSON；读不到/解析失败一律返回 {}。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.buffer.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8-sig", "replace"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _parse_args(argv):
    """抽出 `--agent <val>` / `--agent=<val>`，剩余首个位置参数作回退。"""
    agent, positional = "", ""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            agent = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a.startswith("--agent="):
            agent = a[len("--agent="):]
            i += 1
            continue
        if not positional and not a.startswith("-"):
            positional = a
        i += 1
    return agent.strip(), positional


def _emit_stdout(obj) -> None:
    """向 stdout 写一行 JSON（仅阻塞/门控型 hook 用：Cursor / Antigravity）。任何异常都吞掉，绝不影响 agent。"""
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except Exception:
        pass


def _norm_session(payload: dict):
    """会话键：Claude/Codex 用 session_id；Cursor 多数事件只给 conversation_id（会话内稳定）；
    Copilot CLI 用 camelCase sessionId。"""
    return payload.get("session_id") or payload.get("conversation_id") or payload.get("sessionId")


def _norm_cwd(payload: dict):
    """工作目录：Claude/Codex 给 cwd；Cursor 给 workspace_roots；Antigravity 给 workspacePaths
    （数组，元素可能是 str 或 {"path":...}；字段名待真机核对，见 docs/antigravity接入方案.md）。
    cwd 路由是首选路由机制，必须为各 agent 补上。"""
    cwd = payload.get("cwd")
    if cwd:
        return cwd
    roots = payload.get("workspace_roots") or payload.get("workspacePaths")
    if isinstance(roots, list) and roots:
        first = roots[0]
        if isinstance(first, dict):
            first = first.get("path") or first.get("root") or first.get("uri")
        if isinstance(first, str) and first:
            return first
    return None


def main(argv) -> int:
    payload = _read_payload()
    agent, positional = _parse_args(argv)
    agent = agent.lower()
    if agent not in _KNOWN_AGENTS:
        agent = ""
    event = (payload.get("hook_event_name")
             or positional
             or os.environ.get("PROM_LIGHT_HOOK_EVENT", ""))
    if not event:
        return 0

    # 阻塞/门控型 hook：必须先把决策写回 stdout——放在所有可能失败的步骤（含 socket）之前，
    # 即便 daemon 不通或后续抛异常，宿主 agent 也已拿到放行决策，绝不被挂起/阻断。
    #   Cursor：仅命中 _CURSOR_STDOUT 的事件回写（其余不碰 stdout）。
    #   Antigravity：注册的每个事件都回写 allow（见 _ANTIGRAVITY_ALLOW）。
    if agent == "cursor" and event in _CURSOR_STDOUT:
        _emit_stdout(_CURSOR_STDOUT[event])
    elif agent == "antigravity":
        _emit_stdout(_ANTIGRAVITY_ALLOW)

    cfg, notice = _load_config()           # 读一次：cfg 供下方解析，notice 为空=正常/非空=用户改坏了需告知
    cmd = _events_from(cfg).get(event)
    release = event in _SESSION_END_EVENTS
    if cmd is None and not release:
        return 0

    if cmd:
        cmd = _expand_macros(cmd, _macros_from(cfg))

    msg = json.dumps({
        "cmd": cmd or "",
        "message": _build_message(event, payload),
        "session": _norm_session(payload),
        "cwd": _norm_cwd(payload),
        "agent": agent,
        "release_session": release,
        "notice": notice,            # events.json 坏掉的防呆提示（空则不打扰）；daemon 去重后显示
    }) + "\n"
    try:
        with socket.create_connection((_HOST, _get_port()), timeout=_CONNECT_TIMEOUT) as s:
            s.sendall(msg.encode("utf-8"))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
