"""实时变声本地设置：输入设备选择 + 输入降噪开关 + 性能档位。

为什么要独立设置文件
--------------------
输入设备原来写死在环境变量（VM_LIVE_INPUT_DEVICE）/默认值里，用户没法在界面上
换麦克风（手机当麦克风、USB 麦克风、调音台都接不进实时链路）。降噪开关
（RVC 自带 I_noise_reduce）原来只在 RVC GUI 里有，无头模式下永远 False。
性能档位（perf_profile）控制实时变声的推理参数与伴随进程：
  - balanced  现状参数，字幕/自我监听按需开关
  - game      更省占用：block_time 加大、extra_time/index_rate 降低，
              启动时不自动拉起字幕与自我监听（适合边打游戏边变声）

设置落在 outputs/live_settings.json，三方共用：
  - rvc_live._resolve_device_names：输入设备候选的第一优先级
  - rvc_live._apply_model_config：每次启动变声把 denoise 写进 RVC config.json
  - cascade_stream：采集输入设备关键词（级联链路同样要能换麦）

约定：
  - input_device 为空字符串 = 跟随系统默认录音设备（插耳机自动切耳机麦）
  - 非空 = 设备名关键词（大小写不敏感的包含匹配，见 rvc_live._device_matches）
  - 文件损坏/缺键一律回退默认值，绝不阻塞变声启动
"""

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

# 项目根下的 outputs 目录（与 ASR 状态、cascade 状态同处）。
# 独立推导而非 import config：cascade_stream 在 RVC venv 里跑，
# 路径必须自包含，避免 import 链条的 cwd 差异。
_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.environ.get("VM_LIVE_SETTINGS", _ROOT / "outputs" / "live_settings.json"))

PERF_BALANCED = "balanced"
PERF_GAME = "game"
DEFAULTS = {"input_device": "", "denoise": True, "perf_profile": PERF_BALANCED, "scene": ""}


# ============ 场景包（2026-09-29）============
# 为什么要有这一层：用户面对的是**一堆互相牵连的开关**（性能档 / 降噪 / 自我监听 /
# 字幕 / 麦克风），而每加一个能力就多一个开关 —— 界面复杂度是乘法增长的。但用户的
# 心里话其实是「我现在要**开会**」，不是「我要 index_rate=0.5、关掉字幕、开降噪」。
#
# 场景 = 给这段话起个名字，一键把整组开关写到位。
#
# ★ 设计铁律：场景表**只做加法映射**，每个字段都必须落到一个**已经存在**的状态上。
#   不引入新设置项 —— 否则场景会变成第二套平行配置，与单开关互相打架（读哪个？
#   谁赢？），这正是要避免的复杂度。
#
# ★★ 两类字段必须分开，别混为一谈（2026-09-29 差点写错）：
#   · **持久设置**（in_settings）：落 live_settings.json，跨重启保持。
#     └ perf_profile / denoise
#   · **启动伴随项**（on_start）：**不落盘**，只在「一键应用」时随 start 传下去
#     （monitor / subtitle 是 rvc_live_start 的入参，本来就不是设置项）。
#     所以它们的效果是「应用场景那一刻生效」，而不是「以后一直生效」——
#     界面文案必须说实话，不能承诺一个存不下来的状态。
#
# ★ perf_profile 只认 balanced/game 两档（rvc_live.PROFILE_TUNING 的键），
#   所以四个场景映射到这**两档**上是刻意的一对多：开黑/微信语音/会议走 game
#   （都要求省占用、随时在后台），直播走 balanced（音质优先，且要字幕）。
#   这样不用改 RVC 参数表 —— 场景的差异体现在**伴随行为**（字幕/监听/降噪）上。
SCENES: dict[str, dict] = {
    "gaming": {
        "label": "开黑",
        "desc": "边打游戏边变声。省占用、不抢显存，队友全程听不出破绽。",
        "icon": "Gamepad2",
        # order 决定 UI 排序：把最高频的放最前
        "order": 10,
        "in_settings": {"perf_profile": PERF_GAME, "denoise": True},
        "on_start": {
            "monitor": False,   # 自我监听引入延迟感，开黑时反而碍事
            "subtitle": False,  # 字幕要 ASR，吃 GPU/CPU，游戏中必关
        },
    },
    "wechat": {
        "label": "微信语音",
        "desc": "发语音消息 / 语音通话。出去的是变声后的声音，且不受 60 秒上限影响。",
        "icon": "MessageCircle",
        "order": 20,
        "in_settings": {"perf_profile": PERF_GAME, "denoise": True},  # 手机麦底噪大，降噪最关键
        "on_start": {"monitor": False, "subtitle": False},
    },
    "stream": {
        "label": "直播",
        "desc": "音质与稳定优先。开自我监听随时听自己的声音，字幕同步给观众看。",
        "icon": "Radio",
        "order": 30,
        "in_settings": {"perf_profile": PERF_BALANCED, "denoise": True},
        "on_start": {"monitor": True, "subtitle": True},  # 主播需要听自己 + 给观众字幕
    },
    "meeting": {
        "label": "会议",
        "desc": "长时间挂着不占资源。自我监听开着，以便确认自己在说话。",
        "icon": "Users",
        "order": 40,
        "in_settings": {"perf_profile": PERF_GAME, "denoise": True},
        "on_start": {"monitor": True, "subtitle": False},  # 会议内容敏感，默认不落字幕
    },
}

DEFAULT_SCENE = ""          # 空 = 没用过场景（跟随各单开关的现值）
#: 写入 live_settings.json 的场景键（用于记住「上次用的是哪个场景」）。
SETTING_KEYS = ("input_device", "denoise", "perf_profile", "scene")

_lock = threading.Lock()


def _read_raw() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 损坏的设置文件等价于没有设置：回退默认值，下次保存会覆盖
        return {}


def get() -> dict:
    """读当前设置（缺键补默认值；返回副本，调用方修改不影响存储）。"""
    raw = _read_raw()
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    # denoise 必须是 bool；input_device 必须是 str；perf_profile 只认两个档位
    out["denoise"] = bool(out["denoise"])
    out["input_device"] = str(out["input_device"] or "").strip()
    if out["perf_profile"] not in (PERF_BALANCED, PERF_GAME):
        out["perf_profile"] = PERF_BALANCED
    # scene：只认 SCENES 里有的键。认不出的（旧版本写的/手改坏的）当没用过，
    # 而不是原样回吐给前端 —— 否则界面会高亮一个不存在的场景。
    out["scene"] = out["scene"] if out["scene"] in SCENES else DEFAULT_SCENE
    return out


def update(
    input_device: str | None = None,
    denoise: bool | None = None,
    perf_profile: str | None = None,
    scene: str | None = None,
) -> dict:
    """合并写设置；未传的字段保持原值。返回写入后的完整设置。"""
    with _lock:
        data = get()
        if input_device is not None:
            data["input_device"] = str(input_device).strip()
        if denoise is not None:
            data["denoise"] = bool(denoise)
        if perf_profile is not None and perf_profile in (PERF_BALANCED, PERF_GAME):
            # 非法档位直接忽略（保持原值），不该静默写成坏值
            data["perf_profile"] = perf_profile
        if scene is not None:
            # 空串 = 清除场景标记（用户手动改了单个开关，就不再属于任何场景）。
            # 未知场景名同样拒绝，理由同 get()。
            data["scene"] = scene if scene in SCENES else DEFAULT_SCENE
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再替换，避免并发读到大半个 JSON
        fd, tmp = tempfile.mkstemp(dir=str(SETTINGS_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, SETTINGS_PATH)
        finally:
            if os.path.exists(tmp):
                with contextlib.suppress(OSError):
                    os.remove(tmp)
        return data


# ============ 场景包：读与判定 ============


def scene_list() -> list[dict]:
    """供前端渲染的场景清单（按 order 升序）。

    只回**可序列化**的字段。in_settings / on_start 分开给，是因为前者落盘、
    后者只在应用那一刻生效 —— 前端文案要说这个差别，不能混着展示。
    """
    out = []
    for key, s in SCENES.items():
        out.append({
            "key": key,
            "label": s["label"],
            "desc": s["desc"],
            "icon": s.get("icon", ""),
            "order": s.get("order", 999),
            "in_settings": dict(s["in_settings"]),
            "on_start": dict(s["on_start"]),
        })
    return sorted(out, key=lambda x: x["order"])


def scene_get(key: str) -> dict | None:
    """取一个场景定义（不存在返回 None）。"""
    s = SCENES.get(key)
    if s is None:
        return None
    return {
        "key": key,
        "label": s["label"],
        "desc": s["desc"],
        "icon": s.get("icon", ""),
        "in_settings": dict(s["in_settings"]),
        "on_start": dict(s["on_start"]),
    }


def scene_matches(key: str, current: dict | None = None) -> bool:
    """当前设置是否仍「符合」该场景 —— 只比对**落盘的**那几项。

    ★ 它答的是「还符不符合」，**不是**「现在是哪个场景」。这两个问题不等价：
      gaming / wechat / meeting 的 in_settings 完全一样，所以「拿设置反推」
      对三者都返回 True —— 只能当布尔校验用，不能拿它选场景键
      （选键要看 live_settings.get()["scene"]）。

    为什么要这个校验：用户点完「开黑」后又手动改了降噪，界面就不该继续高亮
    「开黑」（否则是假信息）。on_start 类的项（监听/字幕）**不参与**判定 ——
    它们不落盘，无从比对。
    """
    s = SCENES.get(key)
    if s is None:
        return False
    cur = current if current is not None else get()
    return all(cur.get(k) == v for k, v in s["in_settings"].items())

