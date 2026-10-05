# -*- coding: utf-8 -*-
"""声音保存档案（voice preservation）—— 档案存储层。

面向**职业用嗓人群**（教师 / 师范生）的嗓音保存：在声音还清晰的时候录一段，
本机克隆保存，日后以「自己的声音」输出。不涉及患者、不做疗效声称 —— 保存的是
「声音的可用性」，不是治疗。

⚠️ **命名别和 `media/voicebank/` 混**（这是两件不同的东西）：
    `media/voicebank/<音色>/reference.wav`  = **RVC 音色的参考音库**（变声用的素材）
    本模块 `media/preserve/<档案 id>/`       = **人的声音档案**（带知情同意、参考音分档、操作日志）
两者互不 import、互不覆盖。

设计约束（都不是随手选的）
--------------------------
1. **档案 id 由本模块生成**（`p` + 时间戳 + 随机后缀），**不接受外部输入** ——
   用户给的是 `label`（可中文）。这样路径穿越在**结构上**不可能，而不是靠一处
   正则去拦。`common.is_valid_voice_id` 那套只放行 ASCII，中文标签过不去，
   所以这里不能直接复用。
2. **同意先于克隆**：`add_reference()` **不检查** consent（录制本身不是克隆），
   但 `gate()` 会把「没有知情同意」判成不可复现。这条语义有测试钉住
   （`test_voicepreserve.py::TestGate`）—— 它是「仅克隆本人或已授权声音」
   这条约束在代码里的落点，写反了就成了一个能克隆任何人的工具。
3. **操作日志只追加**（`events.jsonl`）：谁、何时、对谁的档案做了什么。
   这是那条约束的**可审计**形式 —— 光有同意书而没有记录，事后无从对账。
4. **绝不裸崩**：任何一步失败返回结构化 error，不抛（沿用 `voice_metrics` /
   `voice_qc` 的约定）。
5. **绝不编造数字**：指标算不出来就给 `None`，并把 verdict 降级说明原因，
   不用一个默认值糊过去。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import string
import subprocess
import time
from datetime import datetime
from pathlib import Path

import config as cfg

#: 档案目录名（相对 `MEDIA_DIR`）。**别改** —— 改了等于丢掉所有已保存的档案。
STORE_NAME = "preserve"

#: 档案 id 形状。由 `new_profile_id()` 生成，`_profile_dir()` 会校验 ——
#: 校验不是为了"防呆"，是因为这个值会拼进路径，必须堵死 `..` 与分隔符。
_PROFILE_ID_RE = re.compile(r"^p[0-9]{10}[a-z0-9]{6}$")

#: 知情同意书的版本。**改动条款文本时必须同时改它** ——
#: 否则历史档案里那份同意书到底是哪一版就说不清了。
CONSENT_VERSION = "v1"

#: 同意书的范围枚举。`self` = 只保存/复现本人声音（当前唯一允许值）。
CONSENT_SCOPES = ("self",)

#: 参考音时长档（秒），与 `tools/voicebank_eval.py` 的 `DURATION_LADDER` 一致。
#: 两个模块刻意不互相 import（评测是离线工具、档案是运行时），靠这条注释对齐；
#: 改一处要同时看另一处。
REF_DURATIONS = (3.0, 10.0, 30.0, 60.0)

#: 复现文本条件，同上与评测协议对齐。
REPRO_CONDITIONS = ("same", "cross", "emotion", "long")

_MAX_LABEL = 40
_MAX_NOTE = 500


def ensure_tools_path() -> Path:
    """把 `tools/` 接进 `sys.path` —— `voice_metrics` / `voice_report` 住在那里。

    ★ 为什么必须显式接（2026-10-05 隔离实例实测）：`_metrics_for()` 里是
      `import voice_metrics`，而它在 `tools/` 下、**不在** `m2_server/` 的 import 根里。
      不接 → `ModuleNotFoundError: No module named 'voice_metrics'` →
      `POST /api/preserve/profiles/{pid}/followup` 直接 500（随访整条不可用），
      `gate()` 带参考音时同样炸。
      同目录的 `clips_api.py`（第 273 行）与 `ab_chain.py`（第 70 行）都显式接过，
      本模块当初漏了；测试没抓到，是因为 `test_voicepreserve.py` 用 monkeypatch
      把 `_metrics_for` 顶掉了 —— 于是"真路径"从没被跑到。
      在真机上它偶尔"看着能用"，只是因为别的能力（切片 / 多链路评测）先被访问过、
      顺手把 tools 塞进了**全局** sys.path：随访能不能用取决于用户点过什么，
      这种隐式耦合必须消掉。

    幂等：每次都查一次 `sys.path`（它很短），**不缓存判断结果** —— 缓存会让
    "有人中途改过 sys.path"这种场景静默失效，而本函数存在的全部意义就是兜这个底。
    """
    import sys

    tools = Path(__file__).resolve().parent.parent / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return tools


# ---------------- 录音引导语料（★ 唯一真相源） ----------------
#
# 这段语料**同时**被两处使用，所以它只能有一份定义：
#   ① 产品：引导用户「照着念」（`guide()` 端点 → 前端引导页）
#   ② 研究：`tools/voicebank_eval.py` 的评测矩阵用**同一个脚本**
# 两处各写一份会漂移，而漂移的后果是**实验失效** ——
# 研究测的是 A 脚本的复现质量，产品却让用户念 B 脚本，结论对不上任何真实使用。
# 所以 `tools/voicebank_eval.py` 直接 import 这里，不另存一份
# （`test_voicepreserve.py::TestCorpusIsSingleSource` 钉住这件事）。

#: 参考录音脚本。**按顺序念**，时长档 = 取前 N 秒（前缀截断）。
#:
#: 为什么用前缀截断而不是随机抽句：档位之间**只差「录了多久」**这一个变量，
#: 内容被完全控制住 —— 否则「10s 档比 3s 档好」可能只是抽到了更好念的句子。
#:
#: 选句原则：日常自然（声音保存要念的就是以后真会说的话）+ 尽量覆盖普通话声韵母。
RECORD_SCRIPT: tuple[str, ...] = (
    "你好，很高兴见到你。",
    "今天天气很好，我们一起出去走走吧。",
    "我想喝一杯温水，谢谢。",
    "请帮我打开窗户，房间里有点闷。",
    "妈妈做的饭菜永远是最好吃的。",
    "明天上午九点我们在地铁站见面。",
    "这本书写得很有意思，我看了两遍。",
    "孩子，你要记得按时吃饭。",
    "我有点累了，想早点休息。",
    "春天来了，路边的花都开了。",
    "麻烦你帮我拿一下那个包。",
    "他说话的声音很温柔，让人很安心。",
    "我们家的猫喜欢趴在阳台上晒太阳。",
    "这个周末我打算去看望爷爷奶奶。",
    "请你慢一点说，我听得不太清楚。",
    "学校门口新开了一家面馆，味道不错。",
    "谢谢你一直陪在我身边。",
    "下雨了，出门记得带伞。",
    "我特别喜欢清晨的安静。",
    "他现在在外地工作，过年才回来。",
    "你把钥匙放在桌子上就好。",
    "这首老歌让我想起了小时候。",
    "医生说我需要多喝水，多休息。",
    "我们拍一张合影留作纪念吧。",
    "不管发生什么，我都会支持你。",
    "山上的空气很新鲜，风也凉快。",
    "请你把这段话再念一遍给我听。",
    "晚上八点有一场很好看的电影。",
    "我从来没有去过那么远的地方。",
    "希望你以后每天都开开心心的。",
)

#: 四类复现文本（每个条件念一条）。`style` 是给合成器的语气提示，不是要念出来的字。
#:
#: 四类语义不同，别合并：
#:   same    同文 —— 复现文本就是参考音里念过的句子（考察「背下来的那句话」）
#:   cross   跨文 —— 参考音里没有的日常句（考察泛化，**这才是真实使用场景**）
#:   emotion 跨情感 —— 带情感色彩的句子（考察韵律迁移）
#:   long    长句 —— >30 字（考察长程稳定性，长句最容易崩）
REPRO_TEXTS: dict[str, tuple[dict[str, str], ...]] = {
    "same": (
        {"text": "你好，很高兴见到你。", "style": "平静"},
    ),
    "cross": (
        {"text": "麻烦你把空调的温度调低一点。", "style": "平静"},
        {"text": "我下周要去一趟广州出差。", "style": "平静"},
    ),
    "emotion": (
        {"text": "太好了，我终于等到这一天了！", "style": "高兴"},
        {"text": "你怎么能这样说话呢！", "style": "生气"},
        {"text": "我真的不知道该怎么办了。", "style": "难过"},
    ),
    "long": (
        {"text": "如果你以后有机会去到很远的地方，一定要记得给家里打个电话，"
                 "别让我们担心。", "style": "平静"},
    ),
}

#: 参考脚本的估算语速（字/秒）。4 字/秒 ≈ 240 字/分钟，普通话朗读的常见速率。
#: 用来告诉用户「录到某个档位大约要念几句」。
SCRIPT_CHARS_PER_SEC = 4.0

#: 知情同意书原文（界面展示 + 存档）。**改它必须同时改 `CONSENT_VERSION`。**
CONSENT_STATEMENT = (
    "我确认这段录音是我本人的声音，或我已获得声音所有者的明确授权。"
    "我理解：声音档案与克隆结果全部保存在本机，不会上传到任何服务器；"
    "我同意仅将其用于我本人的语音输出。我可以随时删除这份档案。"
)


def char_count(text: str) -> int:
    """去标点空白后的字数。口径与 `tools/voice_qc.py::_char_count` 一致。"""
    punct = set("，。！？；：、·…—“”‘’（）《》〈〉【】,.!?;:()[]{}<>\"'`~@#$%^&*_+=/\\|- \t\r\n")
    return sum(1 for ch in text if ch not in punct)


def script_capacity_s() -> float:
    """参考脚本念完大约多少秒 —— 用来判断某个时长档能不能被脚本覆盖。"""
    return sum(char_count(s) for s in RECORD_SCRIPT) / SCRIPT_CHARS_PER_SEC


def guide() -> dict:
    """录音引导（脚本 + 档位 + 复现文本 + 同意书）。

    ★ 由后端**现算**再吐给前端，不让前端手抄一份 —— 与 `remote_api` 的
    `capabilities` 同一个手法：只在后端声明一次的东西才算接上了。
    语料改一次，引导页自动跟着变。
    """
    cap = script_capacity_s()
    return {
        "script": list(RECORD_SCRIPT),
        "script_chars": sum(char_count(s) for s in RECORD_SCRIPT),
        "script_capacity_s": round(cap, 1),
        "chars_per_sec": SCRIPT_CHARS_PER_SEC,
        "durations": [
            {
                "duration_s": d,
                "tag": _dur_tag(d),
                # 录到这一档大约要念几句（前缀截断，所以是「前 N 句」）
                "sentences": _sentences_for(d),
                "covered": d <= cap,
            }
            for d in REF_DURATIONS
        ],
        "repro_texts": {k: [dict(t) for t in v] for k, v in REPRO_TEXTS.items()},
        "conditions": list(REPRO_CONDITIONS),
        "consent": {"version": CONSENT_VERSION, "statement": CONSENT_STATEMENT,
                    "scopes": list(CONSENT_SCOPES)},
    }


def _sentences_for(duration_s: float) -> int:
    """按估算语速算「录到这一档大约要念几句」——只用于引导文案，不参与任何测量。"""
    budget = duration_s * SCRIPT_CHARS_PER_SEC
    used = 0
    for i, s in enumerate(RECORD_SCRIPT, 1):
        used += char_count(s)
        if used >= budget:
            return i
    return len(RECORD_SCRIPT)



# ---------------- 路径与 id ----------------

def store_dir() -> Path:
    """档案库根目录。"""
    return Path(cfg.MEDIA_DIR) / STORE_NAME


def new_profile_id() -> str:
    """生成一个档案 id：`p` + 10 位秒级时间戳 + 6 位随机。

    为什么要随机后缀：同一秒内建两个档案时纯时间戳会撞车，而撞车意味着
    **两个档案互相覆盖**（最坏情况是同意书被换掉）。6 位 36 进制 ≈ 2.2e9 种。
    """
    stamp = f"{int(time.time()):010d}"
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"p{stamp}{rnd}"


def is_valid_profile_id(pid: str) -> bool:
    return bool(pid) and bool(_PROFILE_ID_RE.match(str(pid)))


def profile_dir(pid: str) -> Path:
    """档案目录。id 非法时抛 ValueError（调用方转成 400）。"""
    if not is_valid_profile_id(pid):
        raise ValueError(f"档案 id 非法: {pid!r}")
    return store_dir() / str(pid)


# ---------------- 时间与 IO ----------------

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clip(text: str, limit: int) -> str:
    return str(text or "").strip()[:limit]


def _dur_tag(sec: float) -> str:
    """时长档标签。与 `tools/voicebank_eval.py::_dur_tag` 同口径：
    < 120s 用秒（`60s`），≥ 120s 且整分用分（`5min`）。"""
    s = int(round(float(sec)))
    if s < 120 or s % 60:
        return f"{s}s"
    return f"{s // 60}min"


# ---------------- 档案 CRUD ----------------

def create_profile(label: str, note: str = "") -> dict:
    """建一个空档案。返回 profile dict；label 为空时抛 ValueError。

    建的时候**不写 consent** —— 同意是用户下一步显式做的动作，
    替用户默认同意会让「同意先于克隆」这条约束形同虚设。
    """
    label = _clip(label, _MAX_LABEL)
    if not label:
        raise ValueError("档案名不能为空")
    pid = new_profile_id()
    d = profile_dir(pid)
    (d / "refs").mkdir(parents=True, exist_ok=True)
    (d / "truth").mkdir(parents=True, exist_ok=True)
    profile = {
        "id": pid,
        "label": label,
        "note": _clip(note, _MAX_NOTE),
        "created_at": _now(),
        # ★ 毫秒时间戳，**只用于排序**。`created_at` 是给人看的（秒级），
        #   而秒级精度排不出「同一秒内建的两个档案」的先后 ——
        #   实测：连建两个档案时列表顺序变成不确定的（2026-10-02 测试抓到）。
        #   列表顺序不确定的后果是用户看到"我刚建的那个不在最上面"，
        #   而这类 bug 极难复现（取决于两次点击的间隔）。
        "created_ms": int(time.time() * 1000),
        "updated_at": _now(),
        "consent": None,
        "refs": [],          # [{duration_s, tag, file, actual_s, bytes, recorded_at}]
        "truth": [],         # [{condition, file, bytes, recorded_at}]
    }
    _write_json(d / "profile.json", profile)
    log_event(pid, "create", label=label)
    return profile


def get_profile(pid: str) -> dict | None:
    return _read_json(profile_dir(pid) / "profile.json")


def list_profiles() -> list[dict]:
    """列出全部档案（按创建时间倒序）。目录坏了就跳过该条，不让一条脏数据毁掉整页。"""
    root = store_dir()
    if not root.is_dir():
        return []
    full: list[dict] = []
    for d in root.iterdir():
        if not d.is_dir() or not is_valid_profile_id(d.name):
            continue
        p = _read_json(d / "profile.json")
        if p is not None:
            full.append(p)
    # ★ 用 `created_ms` 排序，不用 `created_at`：后者是秒级、排不出同一秒内的先后
    #   （见 `create_profile` 的注释）。老档案没有该字段时回落到 0，
    #   它们会排在最后 —— 但不会丢，也不会报错。
    full.sort(key=lambda p: (int(p.get("created_ms") or 0),
                             str(p.get("created_at") or "")), reverse=True)
    return [{
        "id": p.get("id", ""),
        "label": p.get("label", ""),
        "created_at": p.get("created_at", ""),
        "consent_ok": bool(p.get("consent")),
        "n_refs": len(p.get("refs") or []),
        "max_duration_s": _max_ref_duration(p),
    } for p in full]


def delete_profile(pid: str) -> bool:
    """删除档案（含录音、同意书、日志）。档案不存在返回 False。

    ★ 路径守卫是**双层**的：`profile_dir()` 先按 id 形状校验，
    这里再断言结果确实落在 `store_dir()` 之内。删用户录音这种事，
    宁可多一层 —— 一次路径穿越就是不可逆的数据丢失。
    """
    d = profile_dir(pid)
    root = store_dir().resolve()
    try:
        if not d.resolve().is_relative_to(root):
            raise ValueError(f"拒绝删除档案库之外的路径: {d}")
    except AttributeError:  # pragma: no cover - Python < 3.9
        if not str(d.resolve()).startswith(str(root)):
            raise ValueError(f"拒绝删除档案库之外的路径: {d}")
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# ---------------- 知情同意 ----------------

def record_consent(pid: str, *, scope: str = "self", statement: str = "",
                   operator: str = "") -> dict:
    """记录一次知情同意。**已同意过则覆盖并留日志**（改主意也算一次事件）。

    参数：
        scope      同意范围，必须是 `CONSENT_SCOPES` 之一
        statement  同意书原文（留档用；界面展示的那段话）
        operator   操作者标识（谁点的这个勾）

    为什么要把 statement 存下来：事后争议里「他当时到底同意了哪一条」必须有据可查，
    而条款文本会随版本改。存原文 + 版本号，两者缺一不可。
    """
    if scope not in CONSENT_SCOPES:
        raise ValueError(f"同意范围必须是 {CONSENT_SCOPES} 之一，实际 {scope!r}")
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    consent = {
        "version": CONSENT_VERSION,
        "scope": scope,
        "statement": _clip(statement, 2000),
        "operator": _clip(operator, 80),
        "agreed_at": _now(),
    }
    p["consent"] = consent
    p["updated_at"] = _now()
    _write_json(profile_dir(pid) / "profile.json", p)
    log_event(pid, "consent", scope=scope, version=CONSENT_VERSION,
              operator=consent["operator"])
    return consent


def has_consent(pid: str) -> bool:
    p = get_profile(pid)
    return bool(p and p.get("consent"))


# ---------------- 参考音 ----------------

def _max_ref_duration(p: dict) -> float | None:
    vals = [float(r.get("duration_s") or 0) for r in (p.get("refs") or [])]
    return max(vals) if vals else None


def _to_wav(src: Path, dest: Path) -> tuple[bool, str]:
    """把任意 ffmpeg 认得的音频转成**单声道 16-bit PCM wav**。返回 (成功?, 原因)。

    ★ 为什么必须转，而不是把上传字节直接写 `.wav`：
      前端用 `MediaRecorder` 录出来的是 **webm/opus**（见
      `web/src/pages/OfflineVc/useOfflineVc.ts` 的录制段）。把 webm 字节命名成
      `.wav` 存下来**不会报任何错** —— 它会安安静静地躺在那儿，直到几秒后
      `voice_metrics` 读不出浊音段、或者克隆出来的声音不对，那时已经查不到原因了。
      （同一条纪律：`docs/犯错指南.md` 速查表第 92 条「以为生效了其实没有」。）

    采样率**保持源采样率**（只强制单声道 + PCM）：降采样会不可逆地削掉高频，
    而克隆要的正是高频里的音色信息。容器换成 wav 是为了下游能读。
    """
    from common import find_ffmpeg

    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    if r.returncode != 0 or not dest.exists():
        tail = (r.stderr or "").strip().splitlines()[-2:]
        return False, "音频转换失败: " + " | ".join(tail)[:300]
    return True, ""


def add_reference(pid: str, src: Path, duration_s: float) -> dict:
    """把一段录音（**任意 ffmpeg 认得的格式**）转成 wav 存进档案。

    ⚠️ 这里**不检查 consent** —— 录制本身不是克隆。把同意卡在录制上会让用户
    没法先试录一段听听效果。真正的前置在 `gate()`（复现路径）里。

    返回记录 dict；失败返回 `{"error": ...}`（不抛）。
    """
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        return {"error": "参考音为空", "error_stage": "参数"}
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        return {"error": f"时长档非法: {duration_s!r}", "error_stage": "参数"}
    if duration_s <= 0:
        return {"error": "时长档必须为正", "error_stage": "参数"}

    tag = _dur_tag(duration_s)
    dest = profile_dir(pid) / "refs" / f"{tag}.wav"
    ok, why = _to_wav(src, dest)
    if not ok:
        return {"error": why, "error_stage": "音频转换"}

    actual = _wav_seconds(dest)
    rec = {
        "duration_s": duration_s,
        "tag": tag,
        "file": dest.name,
        "actual_s": actual,
        "bytes": dest.stat().st_size,
        "recorded_at": _now(),
    }
    # 同一档重录 = 替换，不是追加（否则 gate 会拿一堆重复档去算，中位数失去意义）
    refs = [r for r in (p.get("refs") or []) if r.get("tag") != tag]
    refs.append(rec)
    refs.sort(key=lambda r: float(r.get("duration_s") or 0))
    p["refs"] = refs
    p["updated_at"] = _now()
    _write_json(profile_dir(pid) / "profile.json", p)
    log_event(pid, "add_reference", tag=tag, bytes=rec["bytes"], actual_s=actual)
    return rec


def add_reference_batch(pid: str, src: Path,
                        durations: tuple[float, ...] | None = None) -> dict:
    """**一次录音 → 前缀切四档**。返回 `{"refs": [...], "total_s": ...}` 或 `{"error": ...}`。

    ★ 为什么需要它（这是评测协议能不能落地的关键）：
      `docs/嗓音保存评测协议.md` §3.1 把四档定义为「**同一段录音的前缀截取**」，
      控制变量是"同一人 / 同一麦 / 同一房间 / **同一条 take**"。
      而逐个调 `add_reference` 的做法要求用户**录四次**（前端本来就是"先选档再录"），
      四次就是**四条不同的 take** —— 录到第 60s 时嗓子状态、坐姿、距离都可能变了，
      于是"时长"这个自变量被"这次录得好不好"混淆，**结论归因不成立**。

      这个函数把协议要求的那一步补上：录一条，后端切前 N 秒。

    ⚠️ 总时长不足最大档时**直接报错**，不做"能切多少切多少" ——
      静默产生半套数据比报错危险得多（缺一档的矩阵在 `analyze` 里会变成
      "数据不足以判定"，而用户以为自己录完了）。

    ⚠️ 同样**不检查 consent**（与 `add_reference` 保持一致）：录制本身不是克隆。
    """
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        return {"error": "录音为空", "error_stage": "参数"}

    durs = tuple(float(d) for d in (durations or REF_DURATIONS))
    if not durs:
        return {"error": "没有时长档", "error_stage": "参数"}

    refs_dir = profile_dir(pid) / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)

    # 先整体转一次 wav，再在本地切 —— 只转一次，四次切片不重复解码。
    full = refs_dir / f"_source_{_now().replace(':', '').replace('-', '')}.wav"
    ok, why = _to_wav(src, full)
    if not ok:
        return {"error": why, "error_stage": "音频转换"}

    total = _wav_seconds(full)
    if total is None:
        return {"error": "读不出录音时长（文件可能损坏）", "error_stage": "探测时长"}

    need = max(durs)
    if total + 1e-6 < need:
        # 明确说清差多少、该怎么补 —— 只报"失败了"用户不知道下一步做什么。
        return {
            "error": (f"录音 {total:.1f}s，不足最高档 {_dur_tag(need)}"
                      f"（差 {need - total:.1f}s）。请把引导脚本念完再录一次；"
                      f"脚本容量约 {script_capacity_s():.0f}s。"),
            "error_stage": "时长不足",
            "total_s": total,
            "needed_s": need,
        }

    from common import find_ffmpeg

    out: list[dict] = []
    for d in sorted(durs):
        tag = _dur_tag(d)
        dest = refs_dir / f"{tag}.wav"
        r = subprocess.run(
            [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(full),
             "-t", f"{d:.3f}", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300)
        if r.returncode != 0 or not dest.exists():
            tail = (r.stderr or "").strip().splitlines()[-2:]
            return {"error": "切片失败: " + " | ".join(tail)[:300],
                    "error_stage": "切片", "tag": tag}
        actual = _wav_seconds(dest)
        rec = {
            "duration_s": d,
            "tag": tag,
            "file": dest.name,
            "actual_s": actual,
            "bytes": dest.stat().st_size,
            "recorded_at": _now(),
            "from_batch": True,
        }
        # 与 add_reference 同一套"同档替换"语义：重录就是替换，不是追加。
        refs = [x for x in (p.get("refs") or []) if x.get("tag") != tag]
        refs.append(rec)
        refs.sort(key=lambda x: float(x.get("duration_s") or 0))
        p["refs"] = refs
        out.append(rec)

    p["updated_at"] = _now()
    _write_json(profile_dir(pid) / "profile.json", p)
    # 源文件只在切片期间存在，切完就删 —— 不留一份未切分的冗余录音（体积翻倍且易混淆）。
    try:
        full.unlink()
    except Exception:
        pass
    log_event(pid, "add_reference_batch", total_s=total,
              tags=[r["tag"] for r in out], source=src.name)
    return {"refs": out, "total_s": total, "tags": [r["tag"] for r in out]}


def _wav_seconds(path: Path) -> float | None:
    """读 wav 实际时长。读不出来给 None —— 不编一个"标称时长"顶上去。

    ★ 标称档（3s）与实际时长（可能 2.4s）**必须分开记**：报结论时要用实际值，
    拿标称值冒充就是把自变量写错（同 `docs/嗓音保存评测协议.md` §4.3）。
    """
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return round(float(info.frames) / float(info.samplerate), 3)
    except Exception:
        return None


def add_truth(pid: str, condition: str, src: Path) -> dict:
    """写一段「真实录音」（听测 ABX 的对照）。条件名必须是 `REPRO_CONDITIONS` 之一。

    与 `add_reference` 一样走 ffmpeg 归一 —— 前端录的是 webm，直接当 wav 存会
    得到一个「后缀对、内容错」的文件，且不报错。
    """
    if condition not in REPRO_CONDITIONS:
        return {"error": f"条件必须是 {REPRO_CONDITIONS} 之一，实际 {condition!r}",
                "error_stage": "参数"}
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        return {"error": "录音为空", "error_stage": "参数"}
    dest = profile_dir(pid) / "truth" / f"{condition}.wav"
    ok, why = _to_wav(src, dest)
    if not ok:
        return {"error": why, "error_stage": "音频转换"}
    rec = {"condition": condition, "file": dest.name,
           "bytes": dest.stat().st_size, "recorded_at": _now()}
    truth = [r for r in (p.get("truth") or []) if r.get("condition") != condition]
    truth.append(rec)
    p["truth"] = sorted(truth, key=lambda r: r["condition"])
    p["updated_at"] = _now()
    _write_json(profile_dir(pid) / "profile.json", p)
    log_event(pid, "add_truth", condition=condition, bytes=rec["bytes"])
    return rec


def reference_path(pid: str, duration_s: float | None = None) -> Path | None:
    """取某一档参考音的路径。`duration_s=None` 取**最长档**（质量最好的那一档）。

    复现路径用它把「档案里的参考音」喂给现有 TTS 流程 ——
    本模块**不自己实现合成**，那条链路已经有 `qwen3_tts` / `tts_api` 了。
    """
    p = get_profile(pid)
    if p is None:
        return None
    refs = p.get("refs") or []
    if not refs:
        return None
    if duration_s is None:
        rec = max(refs, key=lambda r: float(r.get("duration_s") or 0))
    else:
        tag = _dur_tag(duration_s)
        rec = next((r for r in refs if r.get("tag") == tag), None)
    if rec is None:
        return None
    path = profile_dir(pid) / "refs" / str(rec.get("file") or "")
    return path if path.exists() else None


# ---------------- 评测桥（档案 → 实验目录） ----------------

#: 导出实验的目录名前缀。实验名一律带上它，避免与人工建的实验（`vb01` / `pilotB`）
#: 混在同一个 `outputs/voicebank/` 下 —— 人工实验是"临时脚本拼的"，导出实验是
#: "从档案直接落的"，两者的可信度不同，目录上就该看得出来。
EXPORT_EXP_PREFIX = "pres_"

#: 实验名允许的字符。**由本模块生成**（`export_experiment` 里拼），不接受外部输入 ——
#: 与档案 id 同一个理由：它会拼进路径，形状先堵死穿越。
_EXP_NAME_RE = re.compile(r"^[a-z0-9_]{1,60}$")


def eval_root() -> Path:
    """评测实验根目录（与 `tools/voicebank_eval.py::_default_root` 同口径）。"""
    return Path(cfg.OUTPUTS_DIR) / "voicebank"


def export_experiment(pid: str, exp: str = "",
                      durations: tuple[float, ...] | None = None) -> dict:
    """把档案导出成一个**评测实验目录**，供 `tools/voicebank_eval.py` 直接 measure。

    ★ 为什么需要它（这是产品与研究之间缺的那座桥）：
      档案把录音存在 `media/preserve/<pid>/{refs,truth}/`，而评测引擎读的是
      `outputs/voicebank/<exp>/{refs/<spk>/<档>.wav, truth/<spk>/<条件>.wav}`。
      两套目录约定之间原本**没有任何桥** —— 录完音只能手工把文件拷过去，
      而手工拷贝会带来两个安静的错：拷漏一档（矩阵缺格 → `analyze` 报
      "数据不足以判定"，错因指向统计而实际在采集）、或拷错档（把 10s 的文件
      命名成 30s，于是自变量标签是错的）。两者都不报错。
      所以这里**按结构拷**：档位名从档案记录里现读，不靠人记。

    语义：
      - `exp` 省略时按档案 id 生成（`pres_<pid>`），**幂等**：同一档案重复导出
        只是覆盖同一目录，不会越导越多。
      - 说话人 id 固定 `profile`（实验内该档案就是唯一说话人；`matrix.json`
        里的 `speaker` 字段是给"多说话人对比"用的，单档案导出不需要区分）。
      - 只拷档案里**真的有**的档与 truth；缺什么在 `missing` 里如实列出 ——
        不凭空补文件（补出来的空文件会让矩阵"看起来是满的"）。

    返回 `{"exp", "root", "refs": [...], "truth": [...], "missing": [...], "matrix_hint"}`；
    失败返回 `{"error": ..., "error_stage": ...}`，不抛。
    """
    try:
        p = get_profile(pid)
    except ValueError as e:
        return {"error": str(e), "error_stage": "参数"}
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}

    name = str(exp or f"{EXPORT_EXP_PREFIX}{pid}").strip()
    if not _EXP_NAME_RE.match(name):
        return {"error": f"实验名非法（只允许小写字母/数字/下划线）: {name!r}",
                "error_stage": "参数"}

    want_durs = tuple(float(d) for d in (durations or REF_DURATIONS))
    base = eval_root() / name
    speaker = "profile"

    # 档案里的 refs 按档索引（tag -> 记录），避免"信调用方传的档位顺序"
    have = {str(r.get("tag")): r for r in (p.get("refs") or [])}
    have_truth = {str(r.get("condition")): r for r in (p.get("truth") or [])}

    refs_out: list[dict] = []
    missing: list[str] = []
    for d in sorted(want_durs):
        tag = _dur_tag(d)
        rec = have.get(tag)
        src = profile_dir(pid) / "refs" / str((rec or {}).get("file") or "")
        if rec is None or not src.exists():
            missing.append(f"refs/{speaker}/{tag}.wav（档案里没有 {tag} 档）")
            continue
        dest = base / "refs" / speaker / f"{tag}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        refs_out.append({"tag": tag, "duration_s": d, "actual_s": rec.get("actual_s"),
                         "bytes": dest.stat().st_size, "from": src.name})

    truth_out: list[dict] = []
    for cond in REPRO_CONDITIONS:
        rec = have_truth.get(cond)
        src = profile_dir(pid) / "truth" / str((rec or {}).get("file") or "")
        if rec is None or not src.exists():
            missing.append(f"truth/{speaker}/{cond}.wav（档案里没有 {cond} 的真值录音）")
            continue
        dest = base / "truth" / speaker / f"{cond}.wav"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        truth_out.append({"condition": cond, "bytes": dest.stat().st_size,
                          "from": src.name})

    for sub in ("samples",):
        (base / sub / speaker).mkdir(parents=True, exist_ok=True)

    # 导出一个「交接说明」，让下一个打开这个目录的人（或 agent）知道它是什么、
    # 缺什么、下一步该跑哪条命令 —— 否则 `missing` 只活在这次 HTTP 响应里。
    (base / "README-导出.md").write_text(_export_readme(p, name, refs_out, truth_out,
                                                       missing), encoding="utf-8")
    log_event(pid, "export_experiment", exp=name, refs=len(refs_out),
              truth=len(truth_out), missing=len(missing))
    return {
        "exp": name,
        "root": str(base),
        "speaker": speaker,
        "refs": refs_out,
        "truth": truth_out,
        "missing": missing,
        "n_refs": len(refs_out),
        "n_truth": len(truth_out),
        # 下一步该跑什么，直接给出来（前端显示 / agent 接手都用它）
        "matrix_hint": (f"python tools/voicebank_eval.py plan --exp {name} "
                        f"--speakers {speaker}"),
        "measure_hint": (f"python tools/voicebank_eval.py measure --exp {name} "
                         f"--with-emb --with-asr"),
    }


def _export_readme(p: dict, exp: str, refs: list[dict], truth: list[dict],
                   missing: list[str]) -> str:
    """导出目录的交接说明。**它不会被 measure/analyze 读取** —— 纯给人看的。"""
    L = [f"# 评测实验 `{exp}`（从声音档案导出）", "",
         f"- 档案：`{p.get('label', '')}`（`{p.get('id', '')}`）",
         f"- 导出时间：{_now()}",
         f"- 知情同意：{'已记录 ' + str((p.get('consent') or {}).get('version', '')) if p.get('consent') else '**未记录**'}",
         "", "## 已导出", ""]
    if refs:
        L.append("| 档位 | 标称 | 实际 | 字节 |")
        L.append("|---|---|---|---|")
        for r in refs:
            L.append(f"| {r['tag']} | {r['duration_s']}s | {r['actual_s']}s | {r['bytes']} |")
    else:
        L.append("（无参考音）")
    L += ["", f"- truth：{', '.join(t['condition'] for t in truth) or '（无）'}", ""]
    if missing:
        L += ["## ⚠️ 缺件（矩阵会缺格，`analyze` 会报「数据不足以判定」）", ""]
        L += [f"- {m}" for m in missing]
        L += [""]
    L += ["## 下一步", "",
          "```bash",
          f"python tools/voicebank_eval.py measure --exp {exp} --with-emb --with-asr",
          f"python tools/voicebank_eval.py analyze --exp {exp}",
          "```", "",
          "★ 合成样本要放进 `samples/<说话人>/<档位>/<条件>.wav`（引擎按这个形状扫）。", ""]
    return "\n".join(L)


# ---------------- 复现桥（注册为音色库音色） ----------------

#: 复现桥生成的音色 id 前缀。`preserve_` + 档案 id：档案 id 本身受
#: `_PROFILE_ID_RE` 约束（小写字母数字），拼上固定前缀后仍满足
#: `common._VOICE_ID_RE`（`[A-Za-z0-9_-]+`）—— 音色 id 会拼路径，形状先堵死穿越。
VOICE_ID_PREFIX = "preserve_"


def _ref_text_for(duration_s: float) -> str:
    """某档参考音对应的文字稿 = 引导脚本的前缀。

    ★ 这**不是猜的**：时长档的定义就是「按顺序念脚本、取前 N 秒」
    （见 `RECORD_SCRIPT` 的注释），所以第 N 档的文字稿就是脚本按估算语速
    截到该时长的前缀。ref_text 有内容 Qwen3-TTS 才走 ICL 语气克隆
    （`tts_api.synth_wav`：ref_text 为空会静默回退纯声纹模式，语气会丢）。
    """
    budget = float(duration_s) * SCRIPT_CHARS_PER_SEC
    used = 0
    out: list[str] = []
    for s in RECORD_SCRIPT:
        out.append(s)
        used += char_count(s)
        if used >= budget:
            break
    return "".join(out)


def register_as_voice(pid: str) -> dict:
    """把档案的**最长档**参考音注册成音色库音色（「去复现」桥）。

    为什么落点是音色库而不是在本模块里合成：TTS / 有声书 / OpenAI 兼容层
    整条复现链路的入口都是 `voice_id`（`common.voice_ref`），注册成音色 =
    一次接入所有现有链路，本模块依然一行合成代码都不用写。

    语义（都有测试钉住，`test_voicepreserve.py::TestReproduce`）：
      - **知情同意是硬门禁**：没有 consent 这一步必须失败 —— 注册成音色就是
        交给克隆链路，这里是「仅克隆本人或已授权声音」的落点。
      - **幂等**：重复调用 = 用当前最长档刷新 reference / ref_text ——
        重录之后点一次「去复现」就同步过去。
      - **注册后设为当前选中音色**：前端跳到输字页即可直接合成。
      - **档案仍是唯一真相源**：音色库里这个音色被删不影响档案；
        被手工改坏了也以档案为准（再点一次就刷新回来）。

    失败返回 `{"error": ..., "error_stage": ...}`，不抛。
    """
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    if not p.get("consent"):
        # 与 gate() 的结构性判定同口径，但不跑指标（复现不需要体检数字，别让人多等 1 秒）
        return {"error": "还没有记录知情同意 —— 复现前必须先确认授权", "error_stage": "同意门禁"}
    ref = reference_path(pid)
    if ref is None:
        return {"error": "还没有参考音 —— 先按引导录至少一档", "error_stage": "参考音"}
    rec = next((r for r in (p.get("refs") or []) if r.get("file") == ref.name), {})
    voice_id = VOICE_ID_PREFIX + str(pid)
    d = Path(cfg.MEDIA_DIR) / "voicebank" / voice_id
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ref, d / "reference.wav")
    (d / "ref_text.txt").write_text(
        _ref_text_for(float(rec.get("duration_s") or 0)), encoding="utf-8")
    # meta 带档案回链：音色库里看到这个音色时，能查到它出自哪份档案、哪版同意书
    (d / "meta.json").write_text(json.dumps({
        "display_name": p.get("label") or voice_id,
        "source": "preserve",
        "preserve_profile": pid,
        "consent_version": (p.get("consent") or {}).get("version", ""),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(cfg.MEDIA_DIR) / "voicebank" / "selected_voice.json").write_text(
        json.dumps({"voice_id": voice_id}), encoding="utf-8")
    log_event(pid, "reproduce", voice_id=voice_id, ref=ref.name)
    return {"voice_id": voice_id, "label": p.get("label", ""), "ref": ref.name,
            "duration_s": rec.get("duration_s")}


# ---------------- 操作日志 ----------------

def events_path(pid: str) -> Path:
    return profile_dir(pid) / "events.jsonl"


def log_event(pid: str, kind: str, **fields) -> None:
    """追加一条操作日志。**只追加、不重写** —— 可审计的前提是历史不可改。

    写失败**不抛**：日志不该成为主流程的单点故障（一次磁盘满不该让用户录不了音）。
    """
    try:
        d = profile_dir(pid)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"at": _now(), "kind": str(kind), **fields}
        with open(events_path(pid), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_events(pid: str, limit: int = 200) -> list[dict]:
    """读操作日志（最新的在后面）。行坏了跳过，不让一行脏数据毁掉整份日志。"""
    f = events_path(pid)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out[-limit:] if limit > 0 else out


# ---------------- 纵向随访 ----------------
#
# 「一次保存」是快照，「纵向随访」是曲线：隔一段时间复录一小段，
# 每次都现算一遍门禁指标，按时间追加成序列 —— 前端把它画成三灯趋势小图。
#
# ★ 措辞红线（申报书与界面同守）：这是「发声状态趋势参考」，**不是**医疗诊断。
#   「诊断 / 健康监测 / 疗效」这类词不许出现在任何面向用户的文案里
#   （pytest 与 vitest 各有一条守卫钉住这件事）。

#: 随访备注上限。
_MAX_FOLLOWUP_NOTE = 200

#: 趋势口径的一句话说明（接口随 trend 下发，前端直接显示）。
#: ★ 措辞红线的落点之一 —— 守卫测试咬的就是这段真实文案。
FOLLOWUP_HINT = "发声状态趋势参考：给自己看变化用的，数值不构成任何医疗建议。"


def followups_dir(pid: str) -> Path:
    """随访目录：`media/preserve/<pid>/followups/`（wav 与 index.jsonl 都在这里）。"""
    return profile_dir(pid) / "followups"


def _followups_index(pid: str) -> Path:
    """随访索引（JSONL，**只追加**）。与 events.jsonl 同一形态：历史不可改 ——
    随访点的价值在「当时测出来是多少」，事后能改就什么都不是了。"""
    return followups_dir(pid) / "index.jsonl"


def _metrics_for(wav: Path) -> dict:
    """对一段录音跑一遍「门禁指标 + 三灯」。

    返回 `{metrics, lamps, error}`：算不出时 error 非空、另两项为 None ——
    与 gate() 同一哲学（如实降级，不编数字顶上去）。指标集合用 `METRIC_KEYS`，
    与体检页同一份定义。
    """
    ensure_tools_path()  # ← 不接这一步就是 500，见 ensure_tools_path 的注释
    import voice_metrics
    import voice_report

    m = voice_metrics.extract(wav)
    if m.get("error"):
        return {"metrics": None, "lamps": None, "error": str(m["error"])}
    metrics = {k: m.get(k) for k in METRIC_KEYS}
    lamps = voice_report.three_lamps(
        emb_sim=None, hnr=m.get("hnr"), h1_h2=m.get("h1_h2"),
        spectral_tilt=m.get("spectral_tilt"))
    return {"metrics": metrics, "lamps": lamps, "error": ""}


def _emb_sim_vs_baseline(pid: str, wav: Path, timeout_s: int = 60) -> float | None:
    """随访音 vs 基线参考音（当前最长档）的声纹余弦相似度。

    ★ **尽力而为**，任何一步走不通都返回 None，绝不抛、绝不阻塞随访主流程：
      - 档案还没有参考音（没有基线可比）→ None
      - TTS worker（qwen3_tts，独立环境）没在跑 → None —— 绝不为了一个加分项
        把 GPU 服务拉起来；那是用户在「输字变声」页才需要的东西
      - 提取失败 / 超时 → None
    与 `tools/voicebank_eval.py measure --with-emb` 的降级约定同口径：
    emb_sim=None 只意味着「这一项没测」，不是数据坏了（灯显示「未测」）。
    """
    try:
        ref = reference_path(pid)
        if ref is None:
            return None
        import numpy as np
        import qwen3_tts

        if not qwen3_tts.worker_alive():
            return None
        v1 = np.asarray(json.loads(
            qwen3_tts.post("/emb", {"path": str(wav)}, timeout=timeout_s))["emb"],
            dtype=float)
        v2 = np.asarray(json.loads(
            qwen3_tts.post("/emb", {"path": str(ref)}, timeout=timeout_s))["emb"],
            dtype=float)
        denom = float(np.linalg.norm(v1) * np.linalg.norm(v2))
        if denom <= 0:
            return None
        return round(float(np.dot(v1, v2)) / denom, 4)
    except Exception:
        return None


def add_followup(pid: str, src: Path, note: str = "") -> dict:
    """追加一次「声音随访」：录音 → wav 归一 → 现算门禁指标 → 按时间入库。

    与参考音的关键区别（都有测试钉住，`test_voicepreserve.py::TestFollowups`）：
      - **追加，不替换**：随访的意义就在「不同时间的多次」，每次都是一条新记录；
        「同档替换」只属于参考音。
      - **不检查 consent**：与 `add_reference` 同一语义 —— 录制与给自己建档不是克隆。
      - **指标算不出来不是失败**（与 gate() 的 partial 同哲学）：录音照样入库，
        metrics / lamps 记 None 并留 error 说明 —— 编一个数字顶上去比留白危险得多。
      - **相似度是加分项**：worker 没跑 / 没有基线都降级 None（见 `_emb_sim_vs_baseline`）。

    返回记录 dict；失败返回 `{"error": ..., "error_stage": ...}`，不抛。
    """
    p = get_profile(pid)
    if p is None:
        return {"error": f"档案不存在: {pid}", "error_stage": "读取档案"}
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        return {"error": "录音为空", "error_stage": "参数"}

    # 文件名带秒级时间戳 + 4 位随机：同一秒录两次也不互相覆盖（同 new_profile_id 的理由）。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    dest = followups_dir(pid) / f"fu_{stamp}_{rnd}.wav"
    ok, why = _to_wav(src, dest)
    if not ok:
        return {"error": why, "error_stage": "音频转换"}

    calc = _metrics_for(dest)
    emb_sim = _emb_sim_vs_baseline(pid, dest)
    lamps = calc.get("lamps")
    if emb_sim is not None and calc.get("metrics"):
        # 相似度拿到了就把三灯重算一遍，让「相似度」灯从「未测」变成有值。
        # 阈值函数还是 voice_report 那一份 —— 不存在第二套标准。
        try:
            import voice_report

            m = calc["metrics"]
            lamps = voice_report.three_lamps(
                emb_sim=emb_sim, hnr=m.get("hnr"), h1_h2=m.get("h1_h2"),
                spectral_tilt=m.get("spectral_tilt"))
        except Exception:
            pass

    rec = {
        "at": _now(),
        "at_ms": int(time.time() * 1000),
        "file": dest.name,
        "actual_s": _wav_seconds(dest),
        "bytes": dest.stat().st_size,
        "note": _clip(note, _MAX_FOLLOWUP_NOTE),
        "metrics": calc.get("metrics"),
        "lamps": lamps,
        "emb_sim": emb_sim,
        "metrics_error": calc.get("error") or "",
    }
    try:
        with open(_followups_index(pid), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # 索引写不进去 = 这条随访点查无实据 —— wav 也不该留（不留对不上的半套数据）。
        try:
            dest.unlink()
        except Exception:
            pass
        return {"error": f"随访记录写入失败（{type(e).__name__}）", "error_stage": "写入"}
    log_event(pid, "followup", file=rec["file"], has_metrics=rec["metrics"] is not None)
    return rec


def read_followups(pid: str) -> list[dict]:
    """读全部随访点，按 `at_ms` 升序。行坏了跳过 —— 不让一行脏数据毁掉整条曲线。"""
    f = _followups_index(pid)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    out.sort(key=lambda r: (int(r.get("at_ms") or 0), str(r.get("at") or "")))
    return out


def trend(pid: str) -> dict:
    """三灯趋势（**发声状态趋势参考**）。只读、不重算 —— 重算是 POST followup 的事，
    与「GET 只读 / POST 现算」约定一致。

    非法 id / 空档案返回 n=0 的合法结构，不抛 —— 趋势是辅助信息，
    它坏了不该拖垮详情页。
    """
    try:
        pts = read_followups(pid)
    except Exception:
        pts = []
    return {"n": len(pts), "points": pts, "hint": FOLLOWUP_HINT}


# ---------------- 质量门禁 ----------------

#: 客观指标集合。**gate() 与随访（add_followup）共用这一份** ——
#: 两处各写一份必漂移（漂移的后果：趋势图与体检页的指标口径对不上）。
METRIC_KEYS = ("hnr", "h1_h2", "spectral_tilt",
               "jitter_local", "shimmer_local",
               "f0_median", "voiced_ratio", "duration_s")


def gate(pid: str) -> dict:
    """这份档案现在能不能用来复现？

    返回：
        verdict      "ready" | "partial" | "not_ready"
        reasons      不能用的原因（人能看懂的一句话，**分因**，不是"出错了"）
        consent_ok   有没有知情同意
        refs         已有的参考音档
        lamps        三灯体检（**仅供参考，不参与 verdict** —— 见下）
        metrics      最长档参考音的客观指标

    ★ `lamps` 为什么不参与 verdict：
      三灯的阈值锚在**袋鼠那 73 条自录切片**的中位数分布上
      （`docs/音色体检三灯.md` §6.1 明说「基线只代表这批素材，换人换麦就会偏」）。
      拿它去否决一位教师的档案，等于用别人的嗓音标准判他的声音 ——
      那是错的。所以这里只把它当**信息**呈现，verdict 只看结构性事实
      （有没有参考音、有没有同意、指标算不算得出来）。
    """
    try:
        p = get_profile(pid)
    except ValueError as e:
        return {"verdict": "not_ready", "reasons": [str(e)], "consent_ok": False,
                "refs": [], "lamps": None, "metrics": None}
    if p is None:
        return {"verdict": "not_ready", "reasons": [f"档案不存在: {pid}"],
                "consent_ok": False, "refs": [], "lamps": None, "metrics": None}

    reasons: list[str] = []
    refs = p.get("refs") or []
    consent_ok = bool(p.get("consent"))

    if not consent_ok:
        reasons.append("还没有记录知情同意 —— 保存/复现前必须先在档案里确认授权")
    if not refs:
        reasons.append("还没有参考音 —— 先按引导录至少一档")

    ref_path = reference_path(pid)
    metrics = None
    lamps = None
    if ref_path is not None:
        try:
            ensure_tools_path()  # ← 同 _metrics_for：不接就是 ModuleNotFoundError
            import voice_metrics
            import voice_report

            m = voice_metrics.extract(ref_path)
            if m.get("error"):
                reasons.append(f"参考音的客观指标算不出来（{m['error']}）")
            else:
                metrics = {k: m.get(k) for k in METRIC_KEYS}
                lamps = voice_report.three_lamps(
                    emb_sim=None, hnr=m.get("hnr"), h1_h2=m.get("h1_h2"),
                    spectral_tilt=m.get("spectral_tilt"))
        except Exception as e:
            reasons.append(f"参考音体检失败（{type(e).__name__}）")

    if not consent_ok or not refs:
        verdict = "not_ready"
    elif metrics is None:
        verdict = "partial"
    else:
        verdict = "ready"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "consent_ok": consent_ok,
        "refs": refs,
        "max_duration_s": _max_ref_duration(p),
        "lamps": lamps,
        "metrics": metrics,
    }


def summary() -> dict:
    """档案库总览：给首页/设置页显示"你保存了几份声音"。"""
    profs = list_profiles()
    return {
        "dir": str(store_dir()),
        "count": len(profs),
        "with_consent": sum(1 for p in profs if p.get("consent_ok")),
        "with_refs": sum(1 for p in profs if p.get("n_refs")),
        "profiles": profs,
    }
