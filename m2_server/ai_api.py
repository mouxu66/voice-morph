"""AI 调试接口（第一期：只读快照）—— 把「这台机器上应用内部发生了什么」变成一份报告。

背景与设计：`docs/AI调试端点设计.md`（2026-10-05 立项，两处拍板已记在文内）。
一句话：**AI 已经有眼睛（tools/desktop-control 的截图 + UIA），缺的是内部状态** ——
这次为什么降级、哪个能力没挂上、上一条发送的 warning 是什么。本模块补的就是这一层。

三条关键取舍（改动前先读上面那份设计稿）：

1. **归 `core.system`，零门槛**（用户拍板）。快照是只读的，报障时用户还没装/开
   任何东西就该能用 —— 所以它不走可选插件的开关。写动作（第二期）才进
   `hook.ai` 插件，且另有 `VM_AI_CONTROL` + `VM_AI_TOKEN` 两道门。
2. **复用不重写**：`diagnose` / `plugins` / `warmup` / `precheck` 一律调既有函数，
   不在本模块里重新探测一遍。造第二真相源 = 两边漂移时没人知道该信谁。
3. **每处空值必须带原因**：采集失败/能力被关/日志文件不存在，都要在 `notes` 里
   逐条说清（同 `60秒魔法时刻.md` 的两条诚实性不变量口径）。

只读面为什么按「回环 only」而不是沿用现有 API 的 LAN 规则：快照里含用户路径、
最近记录摘要与日志尾部，`_TokenMiddleware` 的默认（未设 token 时 LAN 全放行）
在这里等于对外广播。所以非回环来源一律 403 —— 比现有 API 更严，是刻意的。

脱敏规则（写进响应的 `redaction` 段，用户能看懂自己交出去的是什么）：
  · `input_text` 等用户文字字段**一律剔除**（历史只留白名单字段）；
  · 用户主目录 → `%USERPROFILE%`；
  · 日志行截断到 200 字符；
  · `VM_API_TOKEN` / `VM_AI_TOKEN` 的值出现即替换。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

import config as cfg
import history
import plugin_loader
import plugin_manifest
import system_api
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from runtime import API_PREFIX, ROOT

SCHEMA = 1

#: 历史段只保留这些字段（白名单而不是黑名单：params/input_text 里可能有用户文字，
#: 黑名单漏一个就会把私密内容带出去）。
_HISTORY_KEEP = ("id", "ts", "kind", "voice_id", "duration_s", "starred", "tags", "warning")
_HISTORY_N = 20

#: 日志尾部扫描字节数与返回行数上限（日志是 stdout 直写，可能很大，只读尾部）。
_LOG_SCAN_BYTES = 64 * 1024
_LOG_LINE_MAX = 200
_LOG_MAX_LINES = 200

#: 「疑似错误」的启发式判据。宁可多带几行也别漏掉 Traceback；
#: 它只是**筛选**，不是结论 —— 报告里写的是「疑似」。
_LOG_SUSPECT = re.compile(r"(?i)(traceback|\berror\b|\bexception\b|\bwarn|failed|失败|错误|告警|不可用)")

#: ★ 日志行里「引号包裹的用户原话」。这是本模块最要紧的一条脱敏规则。
#:
#: 泄漏源唯一且已确认：`cascade_stream.py` 的 `print(f"[live-asr] 「{text}」 ...")`
#: —— 把 **ASR 识别出的用户原话**写进 stdout → Electron 落 backend.log →
#: 本模块读它。而 `_LOG_SUSPECT` 命中「失败/错误/warn」，于是游戏/直播场景里
#: 用户嘴里的「这局失败了」「不行」会**带着银行卡号一起**进诊断报告
#: （2026-10-06 实测复现，见 `.workbuddy-ai/memory/2026-10-06.md`）。
#: 讽刺的是：正因为它是用户抱怨的那句话，才更容易命中错误筛选。
#:
#: 为什么替换引号内而不是丢掉整行：引号本身是**诊断信号** ——
#: 保留「发生过一次转写」与行尾耗时，抹掉的只是原话本身。
_LOG_QUOTED_USER_TEXT = re.compile(r"(「[^」]*」|『[^』]*』)")

#: 号码类兜底：引号规则万一漏掉的路径（如异常文本里直接夹带数字串）。
#: 两侧 `(?<!\d)`/`(?!\d)` 卡死边界 —— 否则耗时(0.35)、时间戳、音色编号
#: 会被当成「号码」抹掉，把报告变成一堆无从解读的星号。
_SENSITIVE_NUMBERS = re.compile(
    r"(?<!\d)(?:"
    r"\d{17}[\dXx]"  # 身份证 18 位
    r"|\d{16,19}"  # 银行卡 16-19 位
    r"|1[3-9]\d{9}"  # 手机号 11 位
    r")(?!\d)"
)

#: 允许访问只读面 / 写面的来源主。`testclient` 是 Starlette TestClient 的**合成 peer**，
#: 真实 socket 的对端地址不可能是这个字符串（服务端读的是 socket，不是请求头）。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

_DROP_KEYS = {"input_text"}


def _client_host(request: Request) -> str:
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    # IPv4-mapped IPv6（::ffff:127.0.0.1）在部分 Windows 栈上会出现
    return host.startswith("::ffff:") and host[7:] in _LOOPBACK_HOSTS


def _require_loopback(request: Request) -> str:
    host = _client_host(request)
    if not _is_loopback(host):
        raise HTTPException(
            status_code=403,
            detail=f"AI 调试接口仅限本机访问（来源 {host or '未知'}）—— 快照含本机路径与日志尾部",
        )
    return host


# ★ 回环限制挂成 **router 级依赖**，不是每个端点手写 `_require_loopback()`。
# 手写的致命处：新增端点时忘了写 = 默认对 LAN 开放，而快照里含本机路径与日志尾部
# —— 「忘了写一行代码」造成的后果不能靠自觉。挂成依赖后新增端点自动受保护；
# 要放开某个端点得显式覆盖（届时会被 review 看见），而不是「忘了删一行」。
#门禁 `test_ai_api.py::test_every_ai_endpoint_is_loopback_only` 钉住这条不变量。
router = APIRouter(prefix=API_PREFIX, dependencies=[Depends(_require_loopback)])


def _home_prefixes() -> list[str]:
    out = []
    for getter in (lambda: str(Path.home()), lambda: os.path.expanduser("~")):
        try:
            p = getter()
        except Exception:
            continue
        if p and p not in out:
            out.append(p)
    return out


def _token_values() -> list[str]:
    return [t for t in (cfg.API_TOKEN, os.environ.get("VM_AI_TOKEN", "")) if t]


def _redact_str(value: str) -> str:
    # 顺序有讲究：先整段抹掉「引号里的用户原话」，再扫号码 —— 反过来的话，
    # 号码正则可能在原话里只匹配到一半，剩下的半截照样出去。
    value = _LOG_QUOTED_USER_TEXT.sub("「<用户原话已省略>」", value)
    value = _SENSITIVE_NUMBERS.sub("<疑似号码已隐藏>", value)
    for home in _home_prefixes():
        if home and home in value:
            value = value.replace(home, "%USERPROFILE%")
    for token in _token_values():
        if token in value:
            value = value.replace(token, "<redacted-token>")
    return value


def _redact(obj: Any) -> Any:
    """递归脱敏：剔除文字字段、替换用户目录与 token。"""
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in _DROP_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _redact_str(obj)
    return obj


def _try(section: str, fn: Callable[[], Any], notes: list[dict], default: Any = None) -> Any:
    """采集一段：失败不把整个快照打 500，但必须留下原因。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 —— 快照的职责是「缺得最多时也要活着」
        notes.append({"section": section, "reason": _redact_str(f"采集失败：{type(exc).__name__}: {exc}")[:300]})
        return default


# ---------------------------------------------------------------- 各段采集


def _app_section() -> dict:
    version = None
    pkg = ROOT / "web" / "package.json"
    try:
        version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
    except Exception:
        version = None
    # 形态判据（启发式，别当结构保证）：安装版 ROOT==resources/backend，
    # 有 web_dist 而没有源码目录 web/；源码模式两者都在。
    install_mode = "packaged" if (ROOT / "web_dist").is_dir() and not (ROOT / "web").is_dir() else "source"
    return {
        "root": str(ROOT),
        "port": cfg.SERVER_PORT,
        "python": sys.version.split()[0],
        "frontend_version": version,
        "install_mode": install_mode,
    }


def _plugins_section() -> dict:
    plugins = plugin_manifest.by_id()
    off = plugin_manifest.disabled_ids()
    on = plugin_manifest.enabled_ids(off)
    items: list[dict] = []
    for pid in sorted(plugins):
        p = plugins[pid]
        state, reasons = plugin_manifest.state_of(p, off)
        items.append(
            {
                "id": pid,
                "name": p.name,
                "state": state,
                "enabled": pid in on,
                "reasons": reasons,
            }
        )
    res = plugin_loader.results()
    routers = [r for r in res if r.purpose == plugin_loader.ROUTER_PURPOSE]
    return {
        "counts": {
            "total": len(plugins),
            "core": sum(1 for p in plugins.values() if p.is_core),
            "optional": sum(1 for p in plugins.values() if not p.is_core),
        },
        "enabled": sorted(on),
        "disabled": sorted(off),
        "broken": [i for i in items if i["state"] == plugin_manifest.STATE_BROKEN],
        "items": items,
        "loaders": {
            "routers": len(routers),
            "loaded": sum(1 for r in routers if r.ok),
            "broken": [
                {"module": r.module, "reason": r.reason} for r in routers if not r.ok
            ],
        },
    }


def _history_section() -> dict:
    got = history.query(limit=_HISTORY_N)
    items = [
        {k: r.get(k) for k in _HISTORY_KEEP if k in r}
        for r in (got.get("items") or [])
    ]
    return {"items": items, "total": got.get("total", 0)}


def _log_path() -> Path | None:
    override = os.environ.get("VM_BACKEND_LOG", "")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "voice-morph-desktop" / "backend.log"


def _errors_section(logs: int, notes: list[dict]) -> dict:
    """读「疑似错误」的日志尾部。

    为什么是读文件而不是装 logging handler：本项目的后端日志**就是 stdout**
    （`plugin_loader.report()` 的注释原文；Electron 的 `backend.cjs` 把子进程
    stdout 落成 `%APPDATA%/voice-morph-desktop/backend.log`），全仓没有
    `logging.basicConfig` —— 装 handler 只会捕到零头，把「没日志」伪装成
    「没错误」，正是本模块最不该犯的错。
    """
    path = _log_path()
    if logs <= 0:
        return {"items": [], "source": str(path) if path else "", "note": "调用方要求跳过（logs=0）"}
    if path is None or not path.is_file():
        notes.append({"section": "errors", "reason": "没找到后端日志文件（后端独立运行 / 未由桌面端拉起时不存在；stdout 即日志）"})
        return {
            "items": [],
            "source": str(path) if path else "",
            "note": "未找到日志文件；后端独立运行时日志只在 stdout",
        }
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > _LOG_SCAN_BYTES:
                f.seek(-_LOG_SCAN_BYTES, os.SEEK_END)
            raw = f.read()
    except OSError as exc:
        notes.append({"section": "errors", "reason": f"日志读取失败：{exc}"})
        return {"items": [], "source": str(path), "note": "日志读取失败"}
    text = raw.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # ★ 先脱敏、再截断。反过来的话，截断会把「」拦腰切断（用户原话很长，
    # 而截断上限 200 字符），闭合引号没了引号规则就匹配不上 —— 一个只写在
    # 文档里没人验证的顺序漏洞：报告照样带出半句原话。
    suspect = [_redact_str(ln)[:_LOG_LINE_MAX] for ln in lines if _LOG_SUSPECT.search(ln)]
    return {
        "items": suspect[-min(logs, _LOG_MAX_LINES):],
        "source": str(path),
        "scanned_bytes": min(size, _LOG_SCAN_BYTES),
        "note": "",
    }


def _audio_section(notes: list[dict]) -> dict:
    # core.audio 恒在启用集（核心不可关），直接采集；PowerShell 缺失时
    # build_send_chain_report 会把它变成一条「枚举失败」的明细，而不是抛。
    def _collect() -> dict:
        import audio_api

        return audio_api.audio_send_chain()

    return _try("audio", _collect, notes, default={"ok": False, "all_ok": False, "items": []})


def _warmup_section(on: set[str], notes: list[dict]) -> dict:
    if "sound.tts" not in on:
        notes.append({"section": "warmup", "reason": "sound.tts 未启用（预热是它的启动钩子）"})
        return {"enabled": False, "reason": "sound.tts 未启用"}

    def _collect() -> dict:
        import warmup

        return warmup.status()

    data = _try("warmup", _collect, notes)
    # `enabled` 键在开/关两种状态下都必须存在：消费方（AI/前端）不该靠
    # 「段里有没有某个字段」猜这个能力是不是开着。
    return {**(data if isinstance(data, dict) else {}), "enabled": True}


def _wechat_section(on: set[str], notes: list[dict]) -> dict:
    if "hook.wechat" not in on:
        # 关掉的能力不 import（插件化的硬要求）：只报「它关着」。
        notes.append({"section": "wechat", "reason": "hook.wechat 未启用，未采集发送预检"})
        return {"enabled": False, "reason": "hook.wechat 未启用"}

    def _collect() -> dict:
        import wechat_voice

        return wechat_voice.precheck()

    data = _try("wechat", _collect, notes)
    # 同上：开着的比关着的信息多，但 `enabled` 这个判据本身必须两侧都可靠。
    return {**(data if isinstance(data, dict) else {}), "enabled": True}


def build_context(logs: int = 50) -> dict:
    """组装只读快照。纯函数（除了调用既有采集函数），方便测试直接调用。"""
    notes: list[dict] = []
    plugins = _plugins_section()
    on = set(plugins["enabled"])
    payload = {
        "schema": SCHEMA,
        "generated_ts": int(time.time()),
        "app": _app_section(),
        "health": _try("health", system_api.health, notes),
        "plugins": plugins,
        "diagnose": _try("diagnose", system_api.diagnose, notes),
        "audio": _audio_section(notes),
        "warmup": _warmup_section(on, notes),
        "wechat": _wechat_section(on, notes),
        "history": _try("history", _history_section, notes, default={"items": [], "total": 0}),
        "errors": _errors_section(logs, notes),
        "notes": notes,
        "redaction": {
            "rules": [
                "用户主目录 → %USERPROFILE%",
                "input_text 等用户文字字段一律剔除（历史只留白名单字段）",
                "日志里引号包裹的用户原话（「」/『』）→ 「<用户原话已省略>」",
                "日志里的身份证/银行卡/手机号→ <疑似号码已隐藏>",
                f"日志行截断到 {_LOG_LINE_MAX} 字符（脱敏在截断之前）",
                "VM_API_TOKEN / VM_AI_TOKEN 的值出现即替换",
            ],
            "dropped_fields": sorted(_DROP_KEYS),
        },
    }
    redacted = _redact(payload)
    # 「实际发生了哪些替换」如实回报（同两条诚实性不变量的口径）
    blob = json.dumps(redacted, ensure_ascii=False)
    redacted["redaction"]["applied"] = {
        "home_replaced": "%USERPROFILE%" in blob,
        "token_replaced": "<redacted-token>" in blob,
        "spoken_text_redacted": "<用户原话已省略>" in blob,
        "numbers_redacted": "<疑似号码已隐藏>" in blob,
    }
    return redacted


#: 第二期的动作表在 `ai_actions.py`（中性模块，读侧与写侧共用唯一真相源）。
#: **拒绝清单现在就答**，且是声明式的 ——「表里没有的一律 404」靠的是默认拒绝，
#: 不是「记得不要实现它」。这条清单与 `ai_actions.is_allowed()` 是**双向**的：
#: 任何 action_id 要么在动作表里、要么在拒绝清单里，两边都没有即为设计漏洞。
_DENIED = [
    {"id": "shell.exec", "reason": "红线：任何形态的任意命令/脚本执行都不做"},
    {"id": "wechat.send_text", "reason": "不可逆的外部副作用（发进的是别人的微信），要人确认"},
    {"id": "wechat.send_voice", "reason": "同上：发送类动作永不由 AI 静默执行"},
    {"id": "voices.delete", "reason": "删除类：破坏性且难以恢复"},
    {"id": "system.storage_clean", "reason": "删除类：同上"},
    {"id": "history.delete", "reason": "删除类：同上"},
    {"id": "market.uninstall", "reason": "删除类：同上"},
    {"id": "fs.read_any", "reason": "文件系统任意读写：越权面太大，产物一律走 media/ 与 outputs/ 约定"},
    {"id": "audio.set_any", "reason": "改任意系统设置：二期只允许 setup_best + restore 成对动作"},
]


@router.get("/ai/context")
def ai_context(request: Request, response: Response, format: str = "json", logs: int = 50):
    """只读诊断快照；`format=md` 返回可直接粘贴给 AI/客服的脱敏报告。

    仅限回环访问（见模块注释的鉴权取舍）。无副作用 —— 它调用的采集函数都是只读的
    （音频那段会跑一次只读的 PowerShell 枚举，同 `/audio/send_chain`）。
    """
    if format not in ("json", "md"):
        raise HTTPException(status_code=400, detail="format 只能是 json 或 md")
    logs = max(0, min(int(logs), _LOG_MAX_LINES))
    payload = build_context(logs=logs)
    headers = {"Cache-Control": "no-store"}
    if format == "md":
        return PlainTextResponse(
            render_markdown(payload), media_type="text/markdown; charset=utf-8", headers=headers
        )
    for key, value in headers.items():
        response.headers[key] = value
    return payload


@router.get("/ai/actions")
def ai_actions_catalog(request: Request, response: Response):
    """动作目录：能做什么（schema + 风险等级）+ 明确不能做什么（拒绝清单）。

    第二期起`write_enabled` 由三重门实时决定 —— 用户设了 token 但忘了
    `VM_AI_CONTROL=1` 时，这里要如实答false 并点名缺哪一项，
    而不是笼统说「未启用」（设计稿 §3.3：拒绝也要说清为什么）。
    """
    response.headers["Cache-Control"] = "no-store"

    import ai_actions as _table  # 中性动作表：hook.ai 关着也能读

    reasons: list[str] = []
    if os.environ.get("VM_AI_CONTROL", "") != "1":
        reasons.append("缺 VM_AI_CONTROL=1（总开关）")
    if not os.environ.get("VM_AI_TOKEN", ""):
        reasons.append("缺 VM_AI_TOKEN（未设 token 即视为无写能力，不回退 VM_API_TOKEN）")

    # 路由在不在 = 第 1 门（hook.ai 启用）。判据用加载器账本里的真实记录，
    # 不靠 import 已关闭插件的模块去猜（那会破坏插件化边界）。
    router_alive = plugin_loader.is_loaded("ai_actions_api")
    if not router_alive:
        reasons.append("hook.ai 未启用（写动作路由未挂载）")

    return {
        "schema": SCHEMA,
        "write_enabled": not reasons,
        "write_reason": "写动作三重门全部满足" if not reasons else "；".join(reasons),
        "missing_gates": reasons,
        "actions": _table.catalog() if router_alive else [],
        "denied": _DENIED,
    }


@router.get("/ai/audit")
def ai_audit_tail(request: Request, response: Response, limit: int = 100):
    """AI 操作记录（只读）。第二期起`hook.ai` 写动作的账本。

    **刻意不在只读端点里 import `ai_audit` 的写入路径之外的任何东西**，
    且不要求 hook.ai 启用 —— 用户关掉插件后仍然该看得见「关掉前AI 做过什么」。
    """
    response.headers["Cache-Control"] = "no-store"
    import ai_audit

    limit = max(1, min(int(limit), 500))
    return {"schema": SCHEMA, **ai_audit.tail(limit=limit)}


# ---------------------------------------------------------------- Markdown 报告


def _md_items(items: list[dict]) -> list[str]:
    lines: list[str] = []
    for it in items:
        mark = "✓" if it.get("ok") else ("!" if it.get("warn") else "✗")
        detail = it.get("detail") or ""
        hint = it.get("hint") or ""
        lines.append(f"- {mark} **{it.get('label', it.get('key', '?'))}**：{detail}")
        if hint:
            lines.append(f"  - 排查：{hint}")
    return lines


def render_markdown(payload: dict) -> str:
    """把快照渲染成 Markdown 报告（用户点「复制诊断报告」拿到的就是它）。"""
    app = payload.get("app") or {}
    out: list[str] = [
        "# 变声工坊 · 诊断报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(payload.get('generated_ts', 0)))}",
        f"- 版本：{app.get('frontend_version') or '未知'} · 形态：{app.get('install_mode') or '未知'}"
        f" · Python {app.get('python') or '?'}",
        "- 本文档由本机生成，已按下列规则脱敏：" + "；".join((payload.get("redaction") or {}).get("rules") or []),
        "",
    ]

    diag = payload.get("diagnose") or {}
    out += ["## 环境体检", ""]
    out += _md_items(diag.get("items") or []) or ["- （没有体检项）"]
    for s in diag.get("skipped") or []:
        out.append(f"- （跳过）{s.get('label', '?')}：{s.get('reason', '')}")
    out.append("")

    pl = payload.get("plugins") or {}
    counts = pl.get("counts") or {}
    out += [
        "## 能力（插件）",
        "",
        f"- 总数 {counts.get('total', '?')}（核心 {counts.get('core', '?')} / 可选 {counts.get('optional', '?')}）",
        f"- 已启用 {len(pl.get('enabled') or [])}；已关闭 {(pl.get('disabled') or []) or '无'}",
    ]
    for b in pl.get("broken") or []:
        out.append(f"- ✗ **{b.get('name', b.get('id'))}**（{b.get('id')}）：{'；'.join(b.get('reasons') or [])}")
    loaders = pl.get("loaders") or {}
    if loaders:
        out.append(f"- 路由模块 {loaders.get('loaded', '?')}/{loaders.get('routers', '?')} 已加载")
    out.append("")

    audio = payload.get("audio") or {}
    out += ["## 音频设备（只读枚举）", ""]
    out += _md_items(audio.get("items") or []) or ["- （没有采集到设备信息）"]
    out.append("")

    warm = payload.get("warmup") or {}
    out += ["## 预热", "", f"- {json.dumps(warm, ensure_ascii=False)}", ""]

    wx = payload.get("wechat") or {}
    out += ["## 微信发送预检", ""]
    if wx.get("enabled") is False:
        out.append(f"- （未采集）{wx.get('reason', '')}")
    else:
        for key in (
            "restart_needed",
            "reason",
            "wechat_running",
            "last_input_device",
            "block_reason",
        ):
            if key in wx:
                out.append(f"- {key}：{wx.get(key)}")
    out.append("")

    hist = payload.get("history") or {}
    out += ["## 最近产出（只留结构化字段，不含文字内容）", ""]
    for r in (hist.get("items") or [])[:10]:
        warn = f" ⚠ {r.get('warning')}" if r.get("warning") else ""
        out.append(
            f"- {r.get('ts', '?')} · {r.get('kind', '?')} · {r.get('voice_id') or '（无音色）'}"
            f" · {r.get('duration_s', '?')}s{warn}"
        )
    if not (hist.get("items") or []):
        out.append("- （无记录）")
    out.append("")

    err = payload.get("errors") or {}
    out += ["## 疑似错误日志（最近）", ""]
    if err.get("note"):
        out.append(f"- （{err['note']}）")
    out += [f"```\n{ln}\n```" for ln in (err.get("items") or [])] or []
    if not err.get("items") and not err.get("note"):
        out.append("- （未筛出疑似错误行）")
    out.append("")

    notes = payload.get("notes") or []
    out += ["## 采集不到 / 已跳过的项", ""]
    out += [f"- **{n.get('section', '?')}**：{n.get('reason', '')}" for n in notes] or ["- （无）"]
    out.append("")
    return "\n".join(out)
