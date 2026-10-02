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


# ---------------- 质量门禁 ----------------

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
            import voice_metrics
            import voice_report

            m = voice_metrics.extract(ref_path)
            if m.get("error"):
                reasons.append(f"参考音的客观指标算不出来（{m['error']}）")
            else:
                metrics = {k: m.get(k) for k in
                           ("hnr", "h1_h2", "spectral_tilt",
                            "jitter_local", "shimmer_local",
                            "f0_median", "voiced_ratio", "duration_s")}
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
