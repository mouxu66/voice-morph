"""本地 Qwen3.5-9B（GGUF）生成「该怎么回」—— 只读消息，绝不发送。

为什么是它
----------
本机能写中文、又能塞进 8G 显存的模型里，它答得最像人。其余模型全是音频：
Qwen3-TTS 只能把字读成声音、Whisper 只能听写、RVC/HuBERT/Demucs 只能变声和分离。

**换过模型**（2026-10-24）：原先用 Qwen3-VL-4B。用 117 群 9 组真实语料同题横评，
4B 的通病是「只接情绪不接事实」还老替用户答应（「我帮你买一瓶荔枝水」）；
9B 会先问清再答（「行，买什么牌子？」），9 组里没有一组再乱许诺。
代价：单次 0.6s → 4.7s，显存 2.5G → 5.4G，冷启 4.6s → 10.7s。

★ Qwen3.5 是**思考型**模型：不开 `enable_thinking=False` 时答案全落在
`reasoning_content`、`content` 是空串（实测 9 条全空，看着像模型挂了）。
`_chat()` 默认关思考；要让它们想，设 `VM_REPLY_THINK=1`。

生命周期：**懒加载 + 空闲 90 秒自杀**。它只在用户点「怎么回 / 换一批」时活着，
绝不常驻 —— 实时变声要求显存余量 >2GB，而 9B Q4 + 视觉塔要 ~6.3GB。空闲退出后下次再拉起。

失败必须降级而不是报错
----------------------
未装模型 / 端口被占 / 进程崩 / 输出不是合法 JSON —— 一律退回 `_rule_replies()`
的模板建议，并在 `note` 里说清原因。桌宠只负责把话显示出来，不该出现「点了没反应」。
"""

from __future__ import annotations

import atexit
import base64
import difflib
import glob
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 环境变量（全部可选，默认值就是本机实测的那套）
ENV_EXE = "VM_REPLY_LLAMA_SERVER"
ENV_MODEL = "VM_REPLY_MODEL"
ENV_PORT = "VM_REPLY_PORT"
ENV_NGL = "VM_REPLY_NGL"
ENV_CTX = "VM_REPLY_CTX"
ENV_IDLE = "VM_REPLY_IDLE_S"
ENV_TIMEOUT = "VM_REPLY_TIMEOUT_S"
ENV_BOOT = "VM_REPLY_BOOT_S"
ENV_MMPROJ = "VM_REPLY_MMPROJ"
ENV_MMPROJ_CPU = "VM_REPLY_MMPROJ_CPU"
ENV_KV = "VM_REPLY_KV"

#: 默认模型：**Ornith-1.5-9B**（Q4_K_M，5.4GiB）。演进：
#: Qwen3VL-4B → Qwen3.5-9B（2026-10-24 横评：4B 只接情绪不接事实、还替用户答应）→
#: Ornith-1.5-9B（2026-10-25 用户拍板；同为 9B、同速 1.1s，**更像群友说话**，
#: 且视觉 A/B 就是拿它做的）。它俩是同一代基座，差别只在语气：
#: Ornith 更爱接梗，Qwen3.5 更爱追问。想换回来：VM_REPLY_MODEL 指 Qwen3.5 那份即可。
DEFAULT_MODEL = r"D:\Ornith-1.5-9B-Q4_K_M.gguf"
MODEL_LABEL = "ornith-1.5-9b"

#: 视觉权重（mmproj）是**独立的一个文件**，不是模型自带的。9B 两个模型都是原生多模态
#: （GGUF 元数据 general.architecture=qwen35、projection_dim=4096），缺的就是这 ~918MB 投影器。
#: ⚠️ 4B 的 mmproj 是 2560 维，**接不到 9B 的 4096**，别混用（llama.cpp 会直接报形状错）。
#: 按模型名配表；`VM_REPLY_MMPROJ` 可覆盖，设空串则强制纯文本。
MMPROJ_BY_MODEL = (
    ("ornith", r"D:\mmproj-Ornith-1.5-9B-f16.gguf"),
    ("qwen3.5-9b", r"D:\mmproj-Qwen3.5-9B-F16.gguf"),
    ("qwen3vl-4b", r"D:\mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"),
)

#: 9B Q4 **带视觉塔**常驻时的显存占用（2026-10-25 实测：整卡 used 7220MB − 桌面基线 1112MB
#: ≈ 6.1G；视觉塔留 CPU 时只要 5.2G，差的就是那 876MB mmproj）。**只用于日志里说人话**，
#: 不参与任何判断 —— 要不要让路看的是「谁在跑」，不是这个估算值。
MODEL_VRAM_MB = 6300

#: 推理超时。CPU 档实测 5.9s，留 10 倍余量给「三者同卡」那种极端情况。
CHAT_TIMEOUT = 90.0

#: 生成参数：2-3 条短句，别让它写小作文。
MAX_TOKENS = 220
TEMPERATURE = 0.4

#: 一次喂给模型的最大消息条数（去掉时间行后）。20 条肉眼够判断语境，
#: prompt 也只有 ~300 token —— 再长对 4B 只会更慢更糊涂。
FEED_MESSAGES = 14

#: 关不关「思考链」。思考型模型（Qwen3.5 / Qwen3-*-Thinking）不关的话，
#: 答案会全部落在 `reasoning_content`、`content` 是空串 —— 2026-10-24 实测
#: 9 条测例全空，看起来像模型挂了。设 VM_REPLY_THINK=1 才会放它想。
NO_THINK = os.environ.get("VM_REPLY_THINK", "0") != "1"

_SYSTEM = (
    "你是微信聊天助手，帮用户决定「该怎么回」。"
    "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码块。格式："
    '{"should_reply":true,"why":"一句话理由（不超过20字）",'
    '"replies":["回复1","回复2","回复3"]}'
    "\n内容按这个优先级来："
    "①先看对方的实质诉求 —— 问位置/时间/买什么/怎么操作，就把答案或明确态度说出来，"
    "三条里**至少一条**必须落到这件事上；"
    "②再看口气 —— 像真人随手打的，可以有梗、可以接话茬，但**梗不能替代答案**；"
    "③对方只是在起哄、发表情、说闲话时，才可以只接梗。"
    "\n分寸是硬约束：不要替用户答应任何事、不要承诺（请客/帮忙/马上办）、"
    "不要道谢道贺、不要替用户做决定 —— 需要答应时写成问一句，不要写成已经答应了。"
    # 群聊里最高频的误判：一群人刷「高手」「佩服」「牛」，模型读成在夸自己，
    # 替你发「谢谢」「过奖啦」（2026-10-25 实测 3 次里 2 次，光写「不要道谢」拦不住）。
    # 但别写成「遇到夸就别回」—— 实测那样会把「高手/佩服」后面的吐槽和问事一起压掉，
    # 连带该接的话头也丢了。要压的是「把自己当被夸对象」，不是「不许说话」。
    "群里有人刷「高手」「佩服」「牛」「大佬」时，那是在夸**别的群友**、不是在夸你 —— "
    "不要回「谢谢」「过奖」「客气」，跟着夸一句就行。"
    # 别在这里举具体句子当例子：上一版写的是「可以跟着夸一句（『这运气绝了』『太离谱了吧』）」，
    # 结果 40 条采样里 4 条原样抄出「这运气绝了」—— 举例子等于给模型发模板。
    "这几句后面还跟着吐槽、问事、叫人时，优先接那个，别只盯着夸。"
    "不要复述对方原话，不要写「建议你」「你可以回」这类旁白，不要加引号。"
    "看不到 / 没核实的事**不要替用户断言**（「确实没录」「我没开过」「就是这样的」），"
    "不确定就写成问一句，或写「我看看」。"
    # 2026-10-25 在 3864 群 40 个真实回话时机上实测：最致命的一类不是「答得不好」，
    # 而是**编用户的处境** —— 「行，明早我准时上线挖」「我控号都给驻防没动你号」
    # 「黑蒜小号在忙，没顾上给你送」，每个都 3/3 稳定复现。用户点一下发出去，
    # 就是替自己许了一个根本没打算做的安排，或者替自己编了个借口。
    "你**不知道用户的情况**：在不在家、有没有空、号里有什么、打到多少分。"
    "所以不许写用户的具体安排（「明早我上线」「周五我打」）、不许写具体数字和战绩、"
    "不许替用户解释自己的号/装备/进度。要表态就表态「行/不行/不想」，要接话就接对方这句。"
    # 风格改短之后冒出的新一类（2026-10-25 实测「我专八了」「我这号还没弄」）：
    # 短自述比长承诺更难被规则抓 —— 它没有承诺动词，语法上就是一句陈述。
    # 这里不写例句，只写规则（第 205 条：写过的例句会被原样抄进回复）。
    "也不要**用「我……」交代自己的情况**（学历、等级、号里有什么、有没有空、忙不忙），"
    "这些只有用户自己知道。"
    # 「编背景」是去掉示例后立刻冒头的一类：接不住具体内容时，模型会自己补前情
    # （「烤肠已就位」「花收到」「国庆调休那天补的」「小号也努力了」）。规则要正着写，
    # 否则它只会换一种编法。
    "接不住具体内容时，只写一句**对眼前这件事**的反应，不要补背景、不要补前情、"
    "不要提到记录里没出现过的人和事。"
    # 2026-10-25 拿 3864 群 474 条用户本人的实质发言量出来的「风格指纹」：
    # 长度中位 8 字、p75 16 字、19% 在 4 字以内；88% 的回复**完全不带标点**，
    # 叹号 0%、句号 2%、逗号 9%；书面语几乎为零（「建议」0%、「可以」3%）。
    # 模型原来写的是「哈哈过奖啦，一起加油」这类带逗号的完整句 —— 内容不差，
    # 但一看就不是本人发的，用户根本不会点。风格和内容一样是能点发的前提。
    # 仍然不许举例句（第 205 条：举过的句子会被原样抄进回复）。
    "**按用户本人的习惯写**（这是他真实的打字方式，不是可选风格）："
    "短：多数 3~10 个字，别凑成完整句子，能一个词说清就一个词；"
    "几乎不打标点：不要句号、逗号、叹号、省略号，要断开就直接断开；"
    "不写书面语（建议/可以/应该/确实/其实/的话/或许）；"
    # 2026-10-25 在 474 条本人发言上量的口癖表：他**一次都没说过**「哈哈/收到/
    # 好嘞/嗯/哦/牛/厉害/不错/确实/谢谢」（全部 0 次），附和时打「666」（开头词
    # 第一名，20 次），吐槽说「我靠」，觉得好笑发 [奸笑]。模型原来满屏「哈哈」
    # 「收到」，内容不差但一眼假。这里写进去的「666」是**他真话**，抄了也是对的。
    "附和、觉得好笑时用「666」或表情名，**不要用「哈哈」**；"
    "不用「收到」「好嘞」「嗯」「哦」「牛」「厉害」「不错」「确实」「谢谢」这类客套 —— "
    "用户从来不这么说；"
    "三条候选**长度要错开**：一条极短（五个字以内）、一条中等、一条稍长可以带梗 —— "
    "不要三条一样长，更不要三条都是完整句。"
    "每次最多 30 个汉字。"
    # 记录仍是单一时间线（分块试过，A/B 净收益为负，见 build_prompt 的说明）。
    # 能给的只有这一句方向：最后一句是谁说的，决定要不要接。
    "不要写「对方说的」「用户」「我：」这类标签，只写要发出去的那句话。"
    "最后一条是用户自己发的、对方没有留下新问题时，should_reply 为 false，"
    "replies 给空数组。"
)

_KIND_LABEL = {"voice": "语音", "media": "图片/文件"}

#: 自动托管用的系统提示：**只出一条**、**不许旁白**。
#: 与 `_SYSTEM` 分开而不是复用：列表里给 3 条时模型会倾向「给选项」，
#: 托管要的是「现在就把这句话发出去」，多给一条反而增加发错的风险。
_SYSTEM_ONE = (
    "你是微信聊天助手。读下面的聊天记录，写**一条**现在就能直接发出去的中文口语回复。"
    "只输出一个 JSON 对象，不要解释、不要 markdown、不要代码块。格式："
    '{"reply":"要发出去的那句话","why":"一句话理由（不超过20字）"}'
    "reply 不超过 30 个汉字，像真人随手打的；不要复述对方原话，"
    "不要写「建议你」「你可以」「你可以回」这类旁白，不要加引号，不要加句号以外的装饰。"
    # 自动托管是**真的发出去**，所以分寸比建议模式更硬：一句「我马上帮你买」就是替用户
    # 许下了承诺（2026-10-24 评测里模型确实这么写过）。
    "优先回答对方问的那件具体事；可以有梗，但梗不能替代答案。"
    "绝不替用户答应、承诺、道谢或做决定 —— 需要答应时写成问一句。"
    "最后一条标着「我：」时不要回。"
)

MAX_ONE_TOKENS = 96
ONE_TEMPERATURE = 0.5

_LOCK = threading.RLock()
_PROC: subprocess.Popen | None = None
_PORT: int = 0
_LAST_USED: float = 0.0
_WATCHER: threading.Thread | None = None
_PORT_OVERRIDE: int | None = None  # 测试用：指定已启动的端口
#: 正在等模型回话的请求数。>0 时**空闲自杀必须让路** —— 见 `_should_kill()`。
_BUSY: int = 0


# ------------------------------------------------------------------ 定位


def _find_exe() -> str | None:
    """llama-server.exe：环境变量优先，否则在本机几个 llama.cpp 构建里挑最新的。

    **生产路径由 `.env` 的 `VM_REPLY_LLAMA_SERVER` 钉死**（2026-10-26 起指向
    `D:\\llama-b11881-cuda13.4\\llama-server.exe`），下面的 mtime 兜底只在没配
    env 时生效 —— 因为「下载即换引擎」太危险：本机 `D:\\llama-b8833-*` 目录里
    实际是 build 9102，若它成为 mtime 最新，prefill 掉 4.3 倍且不报错。

    不能按目录名字典序 —— `b8833` 会排在 `b10488` 后面（"8" > "1"），
    挑到的反而是旧包。按 mtime 取最新。

    2026-10-26 升级实测（RTX 5060 8G / Ornith-9B / c=8192 + q8_0 KV / 2560×1600 图）：
      b11881（0.7.0-dev, CUDA 13.4, `ARCHS` 含 1200/1210）→ 端到端 20.7s
      b10488（CUDA 12 包，`ARCHS` 只到 900）            → 端到端 23.3s
    ⚠️ b10488 的 ggml-cuda.dll **没有 sm_120 cubin**（只有 sm_86/89，靠 JIT 跑），
    却比更老的「原生 sm_120」包（build 9102）prefill 快 4.3 倍（701 vs 164 tok/s）
    —— 代码代差 > 架构匹配度，**别再为「原生 kernel」去降级旧包**。
    """
    env = os.environ.get(ENV_EXE)
    if env and Path(env).is_file():
        return env
    cands = glob.glob(r"D:\llama-*\llama-server.exe")
    if not cands:
        return None
    return max(cands, key=lambda p: Path(p).stat().st_mtime)


def _find_model() -> str | None:
    env = os.environ.get(ENV_MODEL)
    if env and Path(env).is_file():
        return env
    return DEFAULT_MODEL if Path(DEFAULT_MODEL).is_file() else None


def _find_mmproj(model: str | None = None) -> str | None:
    """给当前模型配视觉投影器。没有就返回 None（纯文本，与以前行为一致）。

    `VM_REPLY_MMPROJ`：
      · 不设   → 按模型名查表（ornith / qwen3.5-9b / qwen3vl-4b）
      · 设路径 → 用它
      · 设空串 → **强制纯文本**（不想吃这 ~918MB 显存时的开关）
    """
    env = os.environ.get(ENV_MMPROJ)
    if env is not None:
        env = env.strip()
        return env if env and Path(env).is_file() else None
    name = Path(model or _find_model() or "").name.lower()
    for key, path in MMPROJ_BY_MODEL:
        if key in name and Path(path).is_file():
            return path
    return None


def available() -> tuple[bool, str]:
    """能不能用本地模型。返回 (能否, 不能的人话原因)。"""
    if not _find_exe():
        return False, "没找到 llama-server.exe（可用 VM_REPLY_LLAMA_SERVER 指定）"
    if not _find_model():
        return False, f"没找到本地模型 {DEFAULT_MODEL}（可用 VM_REPLY_MODEL 指定）"
    return True, ""


#: 视觉塔搬上 GPU 需要多少**空闲**显存（MiB）。
#: 2026-10-25 实测（RTX 5060 Laptop 8151MiB，基线占用 1112MiB、c=2048）：
#: 视觉塔留 CPU 时进程吃 5218MiB，上 GPU 吃 6108MiB —— 差的就是那 876MB 投影器，
#: 再加一点计算图峰值。6500 是「留约 400MiB 缓冲」的门槛。
MMPROJ_NEED_MIB = 6500


def _vram_free_mib() -> int | None:
    """当前空闲显存（MiB）；查不到就给 None（无独显 / 没装 nvidia-smi / 超时）。

    必须在 llama-server **拉起前**调用 —— 模型一加载就吃掉 5G，事后再量是量不出来的。
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # 多卡时取第一行（llama.cpp 默认用 dev 0）
    first = out.stdout.strip().splitlines()
    if not first:
        return None
    try:
        return int(first[0].strip())
    except ValueError:
        return None


def _mmproj_to_gpu() -> bool:
    """视觉塔要不要上 GPU。默认 auto：够显存就上，不够才留 CPU。

    为什么不干脆永远放 CPU：看图会从 1.1s 变成 7.2s（实测 6.6 倍差）。纯文本两边都是
    0.3~0.4s，所以这 876MB 只在「真要看图」时才有回报，不值得为它牺牲别的东西。

    为什么不干脆永远放 GPU：整卡只剩 ~800MiB 余量。用户开着游戏或几个浏览器标签时，
    Windows 会**静默**把显存分页到内存（不报错），每次 forward 跨 PCIe 搬权重 ——
    单次从 1s 变几十秒，且极难归因。所以按现场空闲量决定，宁可这次慢一点。

    `VM_REPLY_MMPROJ_CPU`：`1` 强制留 CPU，`0` 强制上 GPU，其余/不设走 auto。
    """
    want = (os.environ.get(ENV_MMPROJ_CPU) or "auto").strip().lower()
    if want == "1":
        return False
    if want == "0":
        return True
    free = _vram_free_mib()
    if free is None:  # 判断不了就别赌：慢 6 秒比整个面板 40 秒不动好
        return False
    return free >= MMPROJ_NEED_MIB


# ------------------------------------------------------------------ 进程生命周期


def _port() -> int:
    if _PORT_OVERRIDE:
        return _PORT_OVERRIDE
    return int(os.environ.get(ENV_PORT, "8093"))


def _health(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def _kill_locked() -> None:
    global _PROC
    proc, _PROC = _PROC, None
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _should_kill(idle_s: float, now: float | None = None) -> bool:
    """这次轮询要不要把 llama-server 杀掉。

    ⚠️ **`_BUSY > 0` 时必须让路**。这是 2026-10-26 实测踩到的坑：
    `_LAST_USED` 只在 `_ensure_server()` 里更新，一次读图推理要是比 `idle_s` 长
    （调试时把 `VM_REPLY_IDLE_S` 设成 8 秒、推理要 23 秒），watcher 就会在第 8 秒
    把正在干活的服务杀掉。前端看到的只是
    `[WinError 10054] 远程主机强迫关闭了一个现有的连接` —— 看着像模型崩了，
    其实是被自己的空闲计时器打死的，极难归因。
    默认 `idle_s` 是 90 秒、正常推理 20~30 秒，平时踩不到；但显存紧张到触发
    Windows 静默分页时（实测能到 180s），这个坑就会把「慢」变成「莫名报错」。
    """
    return (
        _PROC is not None
        and _BUSY == 0
        and (time.time() if now is None else now) - _LAST_USED >= idle_s
    )


def _watch_idle() -> None:
    """空闲自杀。5 秒粒度足够 —— 目的不是省显存到极致，而是「不用时别赖着」。"""
    while True:
        time.sleep(5)
        idle_s = float(os.environ.get(ENV_IDLE, "90"))
        with _LOCK:
            if _should_kill(idle_s):
                _kill_locked()


def _start_locked(port: int) -> None:
    """拉起 llama-server 并等它 ready。调用方必须持锁。"""
    global _PROC
    exe, model = _find_exe(), _find_model()
    assert exe and model, "available() 已保证两者存在"
    ngl = os.environ.get(ENV_NGL, "99")
    # 8192 而不是 2048：**一张攻略图就吃 3.5k~4k token**。
    # 2026-10-25 实测（2315×1550 的万圣节安排表）：
    #   c=2048 → HTTP 400 `request (3475 tokens) exceeds the available context size`
    # 图是按 token 计价的，但**引擎自己有 4096 token/图 的上限**（2026-10-26 实测：
    # 16M 像素的方图只算出 4096 token）—— 所以「把图缩得更小」省不下多少，能省的只有 KV。
    # 2026-10-26 实测（RTX 5060 8G，Ornith-9B，视觉塔在 GPU，同一张 2560×1600）：
    #   c=6144  f16 KV  → 14.8s，峰值 7680/8151
    #   c=8192  f16 KV  → **180s**（两次复现；不报错、不 OOM，静默分页）
    #   c=8192  q8_0 KV → 13~20s，峰值 7629 —— 比 6144 更低，因为 KV 768→512 MiB
    #   c=16384 q8_0 KV → 182s（必挂，别试）
    # 所以 context 上到 8192 **必须配套 KV 量化**；只加 -c 不加量化会慢 12 倍。
    ctx = os.environ.get(ENV_CTX, "8192")
    kv = (os.environ.get(ENV_KV, "q8_0") or "").strip().lower()
    boot_s = float(os.environ.get(ENV_BOOT, "90"))
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    cmd = [
        exe, "-m", model,
        "--host", "127.0.0.1", "--port", str(port),
        "-ngl", str(ngl), "-c", str(ctx), "-t", "8", "--no-warmup",
    ]
    # KV 量化是「显存换 context」唯一便宜的杠杆（实测见上）。q8_0 对 KV 近似无损
    # （上游社区在 Qwen2.5-7B 上量到 ppl +0.42%），f16 是退路：VM_REPLY_KV=f16。
    # ⚠️ V 量化要求 flash attention；当前 b11881（0.7.0-dev）默认 `-fa auto` 已启用，
    # 且 `FA_QUANTS` 里带 `q8_0-q8_0` 专用 kernel，**不要显式加 `-fa on`** —— 更老的
    # llama-server 把 -fa 当开关，多给一个 on 会直接启动失败。
    # ⚠️ 也别为了省显存去调小 `-ub`：多模态的非因果图 batch 必须装进一个 ubatch，
    # 调小会撞 `n_ubatch >= n_tokens` 断言（上游 issue #21550），而不是变慢。
    # 2026-10-26 在 0.7.0-dev 复核：新增的 `mtmd_build_inp_attn_mask`(PR #30259)
    # 只是把 mask 在 FA 下改成 f16，**没有放松**这个约束。
    if kv and kv not in ("f16", "none", "off"):
        cmd += ["-ctk", kv, "-ctv", kv]
    mmproj = _find_mmproj(model)
    if mmproj:
        cmd += ["-mm", mmproj]
        # 视觉塔去不去 GPU，按「**拉起前**的空闲显存」现场决定，见 `_mmproj_to_gpu()`。
        if not _mmproj_to_gpu():
            cmd.append("--no-mmproj-offload")
    _PROC = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    deadline = time.time() + boot_s
    while time.time() < deadline:
        if _PROC.poll() is not None:
            raise RuntimeError(f"llama-server 启动即退出（code {_PROC.returncode}）")
        if _health(port):
            return
        time.sleep(0.4)
    _kill_locked()
    raise RuntimeError(f"llama-server 在 {boot_s:.0f}s 内没 ready")


def _ensure_server() -> int:
    """拿到一个可用的端口。已活着就复用；死了就重启。"""
    global _LAST_USED, _WATCHER, _PROC
    port = _port()
    with _LOCK:
        _LAST_USED = time.time()
        if _PROC is not None and _PROC.poll() is None and _health(port):
            return port
        if _PROC is None and _health(port):
            # 端口上已有一个（上次后端留下的）llama-server：直接复用，不重复加载。
            return port
        _kill_locked()
        _start_locked(port)
        if _WATCHER is None:
            _WATCHER = threading.Thread(target=_watch_idle, name="reply-idle", daemon=True)
            _WATCHER.start()
        return port


def shutdown() -> None:
    """后端退出时顺手带走子进程（否则任务管理器里躺一个 2.3GB 的 llama-server）。"""
    with _LOCK:
        _kill_locked()


def is_running() -> bool:
    """子进程是否活着。纯内存态，不探端口 —— 调用方只想问「现在占没占着显存」。"""
    proc = _PROC
    return proc is not None and proc.poll() is None


def _realtime_busy() -> str:
    """实时变声是否在跑；在跑返回人话原因，不在跑返回空串。

    为什么本模块要主动让路：9B 一进显存就是 5.4G，而 RVC 实时链路要 1.37G、
    且要求余量 ≥2G（`rvc_live.MIN_LIVE_FREE_VRAM_MB`）。2026-10-25 实测：
    9B 起来后整卡只剩 ~780MB —— 此时再开变声就是 OOM（无声 + 声卡残留）。
    与其让变声跑到一半崩，不如这条建议先给模板。

    探测按「查不到就当不忙」处理：`rvc_live` 导入失败、探测本身抛异常，
    都不该让人点不动按钮。
    """
    try:
        from rvc_live import _live_proc_alive

        if _live_proc_alive():
            return "实时变声正在运行（显存让它先占着），先给模板建议"
    except Exception:  # noqa: BLE001 —— 探测失败不是「忙」
        return ""
    return ""


atexit.register(shutdown)


# ------------------------------------------------------------------ 提示词与解析

#: 微信 4.1.15.13 的引用消息读出来是两段拼在一起：
#:     不是 是竞技场匹配的
#:     引用 阿沁 的消息 : 这次的国战？
#: 第二段是**别人的旧话**。不剥掉会同时污染三处，2026-10-26 在真实 3864 群实测到：
#:   ① prompt —— 模型把引用里的旧问题当成对方刚说的
#:   ② `_recent_other` 的 blob —— 引用里带「？」（「这次的国战？」）就判成 direct
#:   ③ 检索词 —— `pick_question` 挑中了一整段带换行的引用块当 query
_QUOTE_CUT = "\n引用"


def _strip_quote(text: str) -> str:
    """去掉引用块，只留本人这一句。"""
    t = str(text or "").strip()
    cut = t.find(_QUOTE_CUT)
    if cut > 0:
        return t[:cut].strip()
    if cut == 0 or t.startswith("引用"):
        # 引用在前、正文在后：正文是最后一行；整条只有引用 → 没有正文可说
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        return lines[-1] if len(lines) > 1 else ""
    return t


def _line_of(m: dict) -> str:
    """一条消息压成一行。语音/图片走占位符，时间行由调用方丢掉。"""
    kind = (m or {}).get("kind") or "text"
    text = _strip_quote(str((m or {}).get("text") or ""))
    if kind == "voice":
        return f"[语音 {m.get('seconds', '?')} 秒]"
    if kind == "media":
        return f"[{text}]"
    return text


def build_prompt(messages: list[dict], chat: str = "", last_side: str | None = None) -> str:
    """拼用户消息。`last_side` 是雷达给的确定信号：right = 最后一条是我发的。

    ⚠️ 两种「标明谁说的」都试过，都回退了，别再改回去：

    1. 每行前面标「我：/对方：」（2026-10-24）—— 前缀会被写进回复正文，
       用户点一下就真发出「我：晚上打球吗」。
    2. 拆成「【对方说的】/【用户自己已经发过的】」两块（2026-10-25）——
       在 3864 群 40 个真实时机上 A/B：禁词（哈哈/谢谢/确实）从 1/36 涨到 3/39，
       该「不回」的从 4 条掉到 1 条（模型看到「用户已经说过话」就更想接），
       其余指标持平。**净收益为负**，回退。

    所以记录保持**单一时间线、不带发言人**，只靠 `last_side` 这一句方向提示。
    分不清「谁在跟谁说什么」是这个尺寸模型的真实上限，不是提示词写法问题 ——
    要根治得换结构（按话题分段 + 带发言人），那是另一件事。
    """
    lines: list[str] = []
    for m in messages:
        text = str((m or {}).get("text") or "").strip()
        if not text or ((m or {}).get("kind") or "text") in ("time", "system"):
            continue
        lines.append(_line_of(m))
    lines = lines[-FEED_MESSAGES:]

    head = f"当前会话：{chat or '未知'}\n" if chat else ""
    if last_side == "right":
        tail = "\n（注：最后一条是用户自己发的，对方还没回。）"
    elif last_side == "left":
        tail = "\n（注：最后一条是对方发的。）"
    else:
        tail = ""
    body = "\n".join(lines) if lines else "（没有读到文字消息）"
    return f"{head}最近的聊天记录（按时间顺序）：\n{body}{tail}\n\n该怎么回？"


def close_unbalanced_json(text: str) -> str | None:
    """把「只差几个收尾括号」的 JSON 补齐；补不了返回 None。

    为什么需要：Ornith-1.5-9B 有 ~40% 的输出会**少一个结尾 `}`**
    （`finish_reason=stop`、token 也没用完，纯粹是没写），
    实测 27 次里 11 次因此被判成「输出非法」，白降级成模板。
    这里只做一件事：从第一个 `{` 扫到底，数清还没闭合的 `{` / `[`，按栈逆序补上。
    字符串没闭合、括号提前闭合（比开的多）、超长，一律返回 None —— 不做任何猜测，
    「宁可退回模板，也不把旁白塞进输入框」这条不变。
    """
    start = (text or "").find("{")
    if start < 0:
        return None
    body = text[start:]
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and (not stack or stack.pop() != ch):
            return None              # 结构本来就乱了，不硬修
    if in_str or not stack or len(stack) > 3:
        return None
    return body + "".join(reversed(stack))


#: 模型可能把提示词里的分区标题当成正文抄出来（「对方说的：行」「我：行」）。
#: 这条坑已经踩过两次：第 203 条是行内「我：」前缀被写进正文，现在是分区标题。
#: 所以清洗放在**输出侧**做，不指望提示词拦住。
_LABEL_RE = re.compile(
    r"^\s*(?:【[^】]{1,10}】"
    r"|(?:对方说的|用户自己已经发过的|用户说的|对方|用户|回复|答复|候选\s*\d*)\s*[:：]"
    r"|我\s*[:：])\s*"
)


def strip_section_label(text: str) -> str:
    """剥掉候选开头的分区标签 / 编号，只留真正要发出去的那句话。

    宁可多剥一层也不放过：「【对方说的】行」和「1. 我：行」都要变成「行」。
    只在**开头**剥，句中出现的「我：」保持不动（那可能是正常的冒号用法）。
    """
    s = (text or "").strip()
    for _ in range(3):          # 「1. 【对方说的】行」会叠三层
        before = s
        s = _LABEL_RE.sub("", s)
        s = re.sub(r"^\s*\d+\s*[.、)]\s*", "", s)
        s = s.strip().strip('"“”「」').strip()
        if s == before:
            break
    return s


def parse_reply_json(text: str) -> dict | None:
    """从模型输出里抠出 {why, replies}。校验不通过返回 None（调用方降级）。

    模型偶尔会加 ```json 围栏或前后废话，所以取第一个 { 到最后一个 }。
    括号没闭合的（Ornith 常犯）先尝试补齐一次；补不了就放弃。
    其余校验**刻意严格**：宁可退回模板，也不把「建议你…」这种旁白塞进输入框。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            data = None
    if data is None:
        repaired = close_unbalanced_json(text)
        if repaired is None:
            return None
        try:
            data = json.loads(repaired)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    # advice 是「给本人看的要点」，读者不同，清洗规则也不同（这里是**宽松**的：
    # 可以有标点、可以有数字），所以不跟 replies 走同一个循环。
    advice = clean_advice(data.get("advice"))
    raw = data.get("replies")
    # 只有 advice 没有 replies 也算成功：用户点「看图」最想要的往往正是那几条要点，
    # 这时候一条回复都没写出来是正常的（图讲的是排期，不是非接不可的话头）。
    if not isinstance(raw, list):
        if advice:
            return {"why": "", "replies": [], "advice": advice, "should_reply": False}
        return None
    replies: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = strip_section_label(item.strip().strip('"“”').strip())
        if not s or len(s) > 40:
            continue
        if s not in replies:
            replies.append(s)
    why = data.get("why")
    why = why.strip()[:40] if isinstance(why, str) else ""
    should = data.get("should_reply")
    if not replies:
        # 「不用回」是模型的**结论**，不是解析失败。以前一律当失败 → 退回模板，
        # 于是用户点了「怎么回」收到「收到/哈哈哈/好嘞」，而模型其实在说「这条不用回」。
        if should is False or advice:
            return {"why": why, "replies": [], "advice": advice, "should_reply": False}
        return None
    return {
        "why": why,
        "replies": replies[:3],
        "advice": advice,
        "should_reply": should is not False,
    }


#: 「替你答应」的句式。4B 在真实语料上反复写这类话（2026-10-24 评测：
#: 「我帮你买一瓶荔枝水」「好的，我马上帮你买！」「收到红包，谢谢！」），
#: 提示词里写了「不要承诺」它照写 —— 所以必须在**输出侧**兜一道。
#: 这是确定性过滤，不依赖模型听话：这些话一旦被点一下就真的发出去，
#: 代价是替用户许下承诺，或者把别人的红包当成自己的。
_COMMIT_RE = re.compile(
    # 帮他做某事：动词表要够宽 —— 2026-10-24 实测漏过「我帮你拿」「我去买一瓶」。
    # 2026-10-25 又漏一轮：原来只列了「买/送/取」这类**跑腿**动词，而群聊里真正
    # 高频的是**接活**（我帮你打 / 我帮你刷）——「五毛就五毛，周二那车我帮你打满12亿」
    # 3/3 稳定通过旧规则。所以这里改成「帮你+任意动词」，不再枚举。
    r"我(?:来|去|先|就)?(?:帮|给|替)(?:你|他|她|大家|他们)[^，。！？]{0,8}"
    r"|我(?:马上|立刻|这就)[^，。]{0,6}(?:买|下单|付|转|办|弄|拿|送)"
    r"|我(?:去|来)(?:买|拿|带|送|取|办|弄|订|叫)"
    r"|我(?:来)?(?:请客|买单|付款|请你)"
    # 替你排计划：这种事在游戏群/约饭群里最多，一旦点发就变成「你已经答应了」。
    # 2026-10-25 在 3864 群实测（每个样本重跑 3 次）：
    #   「那我先刷王城，你打熊我记下了」3/3、「周五熊我打，你安心搬炉子」3/3、
    #   「行，明早我准时上线挖」3/3、「收到，我报名」也有。
    r"|我(?:先|就|去|来|再|尽量|准时|马上|慢慢)[^，。！？不没别]{0,2}"
    r"(?:打|刷|挖|做|弄|搞|排|报|上线|开|搬|跑|练|存|换|抢|挂|控|送|带|记"
    r"|囤|摆|盯|守|看|问|查|想|等|接|用|试|写|做)"
    # 「我明天就试试」：时间词在「我」**后面**，和前一条顺序相反，得单独列
    r"|我(?:明早|明天|今晚|晚上|周[一二三四五六日末]|等会|一会儿|下次)[^，。！？不没别]{0,4}"
    r"(?:试|打|刷|挖|上|去|来|帮|弄|搞|报|搬|跑|控|记|送|做|练)"
    # 答应词打头 + 我的动作：行/好嘞/收到 之后接「我报名」「我这就来」
    r"|(?:行|好嘞|好的|收到|可以|没问题|成)[，,。！!]?\s*我[^，。！？]{0,3}"
    r"(?:报|打|刷|挖|上|来|去|帮|弄|搞|搬|跑|控|参加|排|记)"
    # 答应词后面即便没有「我」，也是点头：「好嘞，马上安排」「行，这就去」
    r"|(?:行|好嘞|好的|收到|可以|没问题|成)[，,。！!]?\s*我?(?:马上|这就|立刻)[^，。！？]{0,8}"
    # 揽活：「那熊归你，峡谷我扛着」
    r"|我(?:扛|顶|包|兜)(?:着|了|下来|住)"
    # 先给时间再给动作：「明早我准时上线挖」「周五熊我打」「明天矿我先挖为敬」
    r"|(?:明早|明天|今晚|晚上|周[一二三四五六日末]|周末|等下|一会儿)"
    r"[^，。！？不没别]{0,4}我[^，。！？不没别]{0,4}"
    r"(?:打|刷|挖|上|来|去|帮|弄|搞|报|搬|跑|控|记|送|盯|守|接|(?:扛|顶)(?!不住))"
    r"|收到(?:红包|转账)"
    r"|包在我身上"
    # 2026-10-25 第三轮：把回复改短之后（中位 5 字）承诺变得更**难**抓 ——
    # 「我这就来」「行 我摆着」「那我先囤着」「你打熊我盯会战」「在呢马上」
    # 5 条全部通过上面的规则（旧动词表里没有 来/摆/囤/盯）。短句里「我+动作」
    # 几乎没有别的含义，所以这三类按句式抓，不再往动词表里塞词。
    # ① 到场类
    #    「下来/下去/上去/上来/出门」是 2026-10-26 收紧「问地点」评测后冒出来的：
    #    对方说「我到楼下了」，模型给「我马上下来」—— 和「我马上到」是同一类承诺，
    #    原来只枚举了 来/到/上号/上线/过去，竖向移动一个都没算。顺带补上「就」，
    #    「我马上就下来」这种最常见的说法以前也漏（中间插一个「就」就断开了）。
    r"|我?(?:这就|马上|立刻)就?(?:来|到|上号|上线|过去|下来|下去|上去|上来|出门)"
    r"|我(?:直接|马上|这就|先去)?上号"
    r"|在呢[，,]?马上"
    # ② 持续承诺：我摆着 / 我囤着 / 我盯着
    r"|我[^，。！？不没别]{0,3}(?:摆|囤|盯|守|放|留|存|挂|排)(?:着|了)"
    # ③ 你把活给我、我接下：你打熊我盯会战
    r"|你[^，。！？]{0,8}我[^，。！？]{0,4}(?:盯|守|扛|顶|管|负责|接)"
    # ④ 裸答应 —— 2026-10-26 实测的漏洞：上面所有规则都要求答应词后面跟
    #    「我/马上/这就」，于是**最朴素的答应形式全漏**。10 个样本（好 / 行 /
    #    没问题 / 可以啊 / 行吧 / 行，楼下哪家？ / 行，几点去拿？）判定全是 False，
    #    而这些正是"替用户答应"最直接的样子 —— 红线漏得最彻底。
    #    拆两支是为了不误伤：「好热」「行不行」「可以吗」不能被当成答应，
    #    所以短答应词后面必须紧跟标点/语气词或到结尾。
    r"|^(?:好的|好嘞|行吧|行啊|可以啊|没问题|收到|成)"
    # 「嗯」故意不算：它更多是「在听/知道了」的状态词，实测把它算承诺会误删
    # 「嗯嗯在听」这类正常应答（2026-10-26 语音场景 2/2 轮被误删）。
    # 「成」是 2026-10-26 实测补上的：模型写「成，放哪？」「成，我这就去」，
    # 原来只有一个字不在词表里，整句漏过 —— 而它是最短、最常见的北方口语答应词。
    r"|^(?:好|行|可以|成)\s*(?:$|[，,。！!呀啊吧~～…])"
)

#: 「答应词 + 逗号」打头。只说这个形状能拆 —— 拆完还得看剩下的半句是什么。
_AGREE_HEAD_RE = re.compile(
    r"^(?:好的|好嘞|好呀|好啊|好|行吧|行啊|行|可以啊|可以的|可以|没问题|收到|成)[，,、]\s*"
)


def salvage_question(text: str) -> str | None:
    """把「答应 + 问细节」拆出那句问话；不是这个形状就返回 None。

    为什么值得做（2026-10-26 真机 6 轮实测）：「被求助」场景模型 **6/6 轮**都写
    「行，几点去拿？」「行，买啥？」这类，被 `drop_reason` 整条删掉后只剩
    `['666', '[奸笑]']`，或者三条全删光退回模板 —— 而删掉的那半句
    「几点去拿？」本身完全可以发：**问句永远不构成承诺**。

    只在残留部分是**问句**时放行，防止借着拆分塞回承诺：
    「行，我马上去」拆出「我马上去」还是承诺（`drop_reason` 拦住）；
    「行，那我买两份」拆出来是替用户做决定，不含问句，也不放行。
    """
    m = _AGREE_HEAD_RE.match(text or "")
    if not m:
        return None
    rest = (text[m.end() :] or "").strip()
    if not rest or not _ASK_RE.search(rest):
        return None
    if drop_reason(rest):
        return None
    return rest

#: 微信自己用来占位的伪文本。模型偶尔把它当正文输出 —— 2026-10-25 实测 #27
#: 直接给了「动画表情」四个字，用户点「发送」就真的在群里发出这四个字。
_PLACEHOLDER = frozenset(
    {"动画表情", "图片", "视频", "文件", "位置", "语音", "表情", "链接", "小程序", "聊天记录"}
)


def is_placeholder(text: str) -> bool:
    """整条候选是不是微信的占位符文字（不是能发的内容）。

    `[奸笑]` 这类是**合法**的微信表情名，必须保留；被拦的只有不带方括号的裸占位符。
    """
    t = (text or "").strip()
    if not t:
        return True
    m = re.fullmatch(r"\[([^\]]{1,6})\]", t)
    if m:
        # `[奸笑]`「[捂脸]」是**合法**微信表情名，发出去会转成表情，必须留。
        # 但 `[动画表情]`「[图片]」只是把占位标签包了层方括号 —— 微信里并没有叫
        # 「动画表情」的表情，点发出去对方看到的就是一串字。2026-10-26 评测里
        # 模型确实写过 `[动画表情]`（表情包场景），旧规则一律放行。
        return m.group(1) in _PLACEHOLDER
    first = t.split()[0] if t.split() else t
    return first in _PLACEHOLDER or t in _PLACEHOLDER


#: 用户本人**一次都没说过**的套话。2026-10-25 在 474 条本人实质发言上统计，
#: 下列词频全部为 0；而模型 40 条采样里「哈哈」「收到」遍布 —— 内容不差，
#: 但这不是他说话的样子，用户看一眼就知道不是自己打的，不会点发。
#: 注意只拦「整条就是这个套话」的情况，不拦句中出现（否则「哈哈哈这波血赚」也会被砍）。
_OFFSTYLE = frozenset(
    """
    收到 好嘞 嗯 哦 哈哈 哈哈哈 哈哈哈哈 谢谢 谢谢啦 厉害 不错 确实 在呢 等下 等会
    稍等 牛 牛逼 我看看 我看下 我试试 明白了 学习一下 受教了 有道理
    """.split()
)


def looks_like_offstyle(text: str) -> bool:
    """整条候选是不是一句用户从不说的客套/附和（只剩这个词，没有实质内容）。"""
    core = re.sub(r"[\s，。！？!?~～…、,.]", "", text or "")
    if core in _OFFSTYLE:
        return True
    # 任意长度的纯笑声：「哈哈哈哈哈哈」不在固定词表里，但用户同样一次没说过
    # （2026-10-25 实测 v7 就漏了 6 个哈）。他笑是打「666」或发 [奸笑]。
    return bool(re.fullmatch(r"[哈呵嘿嘻]{2,}", core))

#: 判「重复别人」时的最短长度：短句（「666」「哈哈」）本来就人人重复，不算抄。
_ECHO_MIN = 6
#: 与别人某句话的相似度阈值。0.65 是实测出来的：对方「我控号都给驻防」被我方复述成
#: 「我控号只给驻防，没动你号」时相似度 0.67（差一个「只/都」），必须能拦；
#: 而「驻防呢？谁在防？」对「没人驻防一下吗」只有 0.46，不该被误杀。
_ECHO_RATIO = 0.65


def _norm_text(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～…\-—\"'“”‘’（）()【】\[\]@]", "", s or "")


def looks_like_promise(text: str) -> bool:
    """这句是不是在**替用户**答应/承诺（含「收到红包谢谢」这种张冠李戴）。"""
    return bool(_COMMIT_RE.search(text or ""))


#: 群里的「广播通知」语境。2026-10-26 真机实测（微信 4.1.15.13，2561 班群）：
#: 班委发「@所有人 请在校的同学及时进行晚签」，这正是「收到」该出场的场景，
#: 但 `_COMMIT_RE` 的裸答应词分支（`^收到`）把它判成「替你答应」删掉，
#: 而模型模仿通知者口吻写的「@所有人 在校的别忘晚签」反而放行 —— 判反了。
#:
#: 差别只有一个：对方是在**请求用户做事**（回「收到」= 答应了那件事），
#: 还是在**广播通知**（回「收到」= 我知道了）。前者拦，后者必须放。
_NOTICE_RE = re.compile(
    r"@所有人|通知|公告|提醒|务必|记得|截止|报名|签到|打卡|晚签"
    r"|请[^，。！？]{0,8}(?:同学|大家|各位|及时|注意|于|在|尽快)"
)

#: 纯应答词：整句只有一个「收到/好的」，没带任何具体动作。
#: 带动作的（「收到，我报名」）仍按承诺拦 —— 那句是真的替用户应下来了。
_BARE_ACK_RE = re.compile(
    r"^(?:好的|好嘞|好滴|好吧|行|行吧|行啊|可以|可以啊|没问题|收到|收到啦|收到收到"
    r"|成|ok|OK|Ok|👌)[。！!～~，,\s]*$"
)


def bare_ack(text: str) -> bool:
    """整句只是一个应答词（收到/好的），不含具体动作。"""
    return bool(_BARE_ACK_RE.match((text or "").strip()))


def is_notice_ctx(messages: list[dict] | None) -> bool:
    """对方最近说的那句是**广播通知**，而不是在请求用户做事？

    问句一律不算通知：「请我吃饭吗」里有个「请」，但那是请求，
    回「收到」等于答应请客，必须继续按承诺拦。
    """
    if not messages:
        return False
    from chat_read import is_question

    for m in reversed(messages):
        if m.get("side") != "left":
            continue
        t = (m.get("text") or "").strip()
        if not t:
            continue
        if is_question(t) or t.endswith(("?", "？")):
            return False
        return bool(_NOTICE_RE.search(t))
    return False


def repeats_notice(reply: str, messages: list[dict]) -> bool:
    """模型把**别人的 @所有人 公告**又发了一遍（身份搞混成通知者）。

    真机实测原句：对方「@所有人 请在校的同学及时进行晚签」→
    候选「@所有人 在校的别忘晚签」「@所有人 晚签别拖」3 条全是二次广播。
    用户点发就是在群里再喊一遍，比不回复更尴尬。

    用户自己发过 @所有人（他是群主）时不算 —— 那是他的正常语气。
    """
    if "所有人" not in _norm_text(reply):
        return False
    for m in messages:
        if m.get("side") == "right" and "所有人" in _norm_text(m.get("text") or ""):
            return False
    return any(
        m.get("side") == "left" and "所有人" in _norm_text(m.get("text") or "")
        for m in messages
    )


#: 「声称已经做了某件事」。和「承诺」是两回事：承诺是**将来**（我明天打），
#: 这条是**完成态**（发你了 / 转你了）—— 用户根本没发过，点发就是一句假话。
#: 2026-10-25 117 群实测：用户问「码发给我」、对方回了两个取餐码，模型给的候选是
#: 「发你了」，三次里两次。这比答得差严重得多，它让用户在群里声称自己办了事。
_CLAIM_DONE_RE = re.compile(
    r"(?:^|[\s，,。！？@])(?:我)?(?:已经)?"
    r"(?:发|转|传|寄|送|给|弄|搞|做|办|拍|买|充|打|写|整理)"
    r"(?:你|你们|他|她|大家|群里)"
    # 「发你了」和光秃秃的「发你」都是声称已发（后者是前者省略，语义一样）
    r"(?:了|啦|过了|$)"
    # 「给你发了」：动词在「你」后面，上面那条覆盖不到
    r"|(?:^|[\s，,。！？@])(?:我)?(?:已经)?给(?:你|你们|他|她|大家)"
    r"(?:发|转|传|寄|送|弄|做|买|充)(?:了|啦|过了)"
)


def claims_done(text: str) -> bool:
    """这句是不是在声称「我已经把东西给你了」（用户其实没做）。"""
    return bool(_CLAIM_DONE_RE.search(text or ""))


#: 用户**身体所在**的地点词。只收这一类 —— 「群里」「游戏里」「号上」是虚指，
#: 写错了也不构成假话，不该进这张表。
_WHERE_NOUN = (
    "家|楼下|楼上|门口|小区|路上|公司|学校|宿舍|单位|店里|车上|车里|地铁|电梯"
    "|屋里|网吧|食堂|医院|机场|车站|半路"
)

#: 「我（我在哪）」。四类形状，全是**用户自己的处境**：
#:   ① 我 + 状态副词 + 在/到 + 地点 —— 我在家呢 / 我也在楼下 / 我现在就在公司
#:   ② 裸「在家呢」「在楼下」—— 中文省略主语，这就是在回答「你在哪」
#:   ③ 进度自述 —— 我还没动呢 / 我出门了 / 我下楼了
#:   ④ 省略主语的坐标自述 —— 刚到家 / 到家了
#: 副词写成 `{0,2}` 而不是只留一个位：只留一个的时候「我也快到了」整句漏过
#: （2026-10-26 收紧评测后 r2 实测，那条候选就这样被放行了）。
_SELF_WHERE_RE = re.compile(
    r"我(?:也|还|现在|正|正好|刚好|刚|已经|就|快|马上|这就|本来){0,2}"
    # 「到了」后面没有地点词，但照样是在报自己的进度（我也快到了 / 我到了）
    r"(?:在|到)(?:" + _WHERE_NOUN + r"|了)"
    # 裸「在楼下吗」是**问对方**，不是替用户报坐标 —— 尾字是疑问语气就放过。
    r"|(?:^|[，,。！？\s])(?:也|还|就)?在(?:" + _WHERE_NOUN + r")(?!吗|么|嘛|\?)"
    r"|我(?:也|还|现在|刚|已经|就|快)?"
    r"(?:没动|没出门|没出发|还没动|出门|出发|上路|下楼|上楼)(?:了|呢|身)?"
    # 「刚到家」「到家了」—— 省略主语的坐标自述。中文里这就是在回答「你在哪」，
    # 2026-10-26 全量评测 r3 实测「刚到家」躲过了上面所有规则，和「我在家呢」同罪。
    r"|^(?:刚|才|已经|也|就|快)?到(?:" + _WHERE_NOUN + r")(?:了|呢)?$"
)
_WHERE_NOUN_RE = re.compile(_WHERE_NOUN)


def fabricates_where(reply: str, messages: list[dict] | None = None) -> bool:
    """这句是不是在**替用户说他现在在哪 / 到没到**（用户没说过就是编的）。

    为什么这不是补个正则就完事：`drop_reason` 和 `claims_done` 都不管这一类，
    而它和「替你答应」是两回事 —— 承诺是将来时，这是**现在时的事实**。
    2026-10-26 把评测判据从「含『在』就算过」收紧之后，12 轮采样里 6 条全部暴露：
    `我也到楼下了`（对方刚说「我到楼下了」，等于替用户复述+加码）、`我也快到了`、
    `我在门口`、`我也在楼下`、`我在家呢`（和「我到楼下了」直接矛盾）、`我还没动呢`。
    用户点一下发出去，就是替自己报了一个不存在的位置。

    为什么可以「只允许复用用户已说过的话」而不整条禁掉：这类事实有两个来源 ——
    用户自己刚说过，或者用户此刻真的在那儿。后者模型永远不知道，前者是**可查的**，
    所以判据是：命中了「我在哪」，就去用户自己（`side == "right"`）发过的话里找依据；
    找不到就拦。对方说的地点**不算依据** —— 「我到楼下了」是对方的坐标，
    拿它给用户背书正好造出「我也到楼下了」。

    `messages` 为空时一律拦：没有依据就是编的，宁可少一条候选，也不让用户点发一句假话。
    """
    text = reply or ""
    m = _SELF_WHERE_RE.search(text)
    if not m:
        return False
    hit = m.group(0).strip(" ，,。！？")
    nouns = _WHERE_NOUN_RE.findall(hit)
    for msg in messages or []:
        if (msg or {}).get("side") != "right":
            continue
        own = str((msg or {}).get("text") or "")
        if hit and hit in own:
            return False                     # 用户自己就是这么说的，属于复用
        if any(f"在{n}" in own or f"到{n}" in own for n in nouns):
            return False                     # 地点有据：用户说过「在/到 + 那个地方」
    return True


def echoes_context(reply: str, messages: list[dict]) -> bool:
    """这句是不是把**别人刚说的话**当成自己的话又说了一遍。

    根因在 `build_prompt`：历史里不带发言人（试过加「我：」前缀，会让模型把前缀
    也写进回复，已回退），于是模型分不清记录里的「我」是谁。2026-10-25 实测：
    群友说「我控号都给驻防」，模型给用户的候选是「我控号只给驻防，没动你号」——
    点一下就等于用户把别人的话认成自己的。
    """
    r = _norm_text(reply)
    if len(r) < _ECHO_MIN:
        return False
    for m in messages:
        if m.get("side") != "left":
            continue
        t = _norm_text(m.get("text") or "")
        if len(t) < _ECHO_MIN:
            continue
        # 只看**对方的「我」**：真正的坑是「我指的到底是谁」。
        # 对方说「我控号都给驻防」被复述成「我控号都给驻防，没动你号」—— 这是抄。
        # 而「巴黎才下课吗」被回复成「@巴黎才下课吗 我没叫你打乔伊呀」是正常接话，
        # 按子串判会误杀（2026-10-25 实测误杀过一条跟真人原话几乎一样的候选）。
        if "我" not in t:
            continue
        if t in r or r in t:
            return True
        if difflib.SequenceMatcher(None, t, r).ratio() >= _ECHO_RATIO:
            return True
    return False


def repeats_self(reply: str, messages: list[dict]) -> bool:
    """这句是不是用户**自己刚说过**的那句原话。

    `echoes_context` 只管「别人的话」，管不到这一侧。2026-10-25 跑真实 117 群：
    用户上一句是「码发给我」，模型照攻略写完又递了一条「码发我」——
    点下去等于把刚打过的字再打一遍。自己说过的原句永远不该进候选，
    这条判据纯字符串比对，不看语义，也就不会误杀。
    """
    r = _norm_text(reply)
    if len(r) < 3:
        return False
    for m in messages or []:
        if (m or {}).get("side") != "right":
            continue
        # 自己那句话可能带 @某人 前缀，比之前先切掉：`_norm_text` 只去掉 @ 符号，
        # 名字会留下来，于是「@瑜清 码发给我」跟「码发给我」对不上（2026-10-25 实测）。
        t = re.sub(r"^\s*@\S+\s*", "", str(m.get("text") or ""))
        if _norm_text(t) == r:
            return True
    return False


def drop_reason(reply: str, messages: list[dict] | None = None) -> str:
    """这条候选为什么会被 `drop_promises` 删掉。返回空串 = 不会被删。

    **为什么要把判定单独抽出来**：`drop_promises` 原来是一个大推导式，跑完只剩
    「删了几条」这个数字（note 里那句「有 1 条在替你答应」），**删掉的是哪句、
    哪条规则命中的，全部丢失**。2026-10-26 跑评测，24 轮里 7 轮有候选被删，
    但完全不知道模型到底在编什么，也就没法判断该改 prompt 还是该补规则。
    这里把判定摊开，`drop_promises` 反过来调它 —— 只有一份判据，不会漂。

    顺序就是短路的顺序（先便宜的、后需要上下文的），和原来的推导式一致。
    """
    if looks_like_promise(reply):
        # 通知语境下的裸「收到」是「我知道了」，不是「替你答应」——
        # 2026-10-26 真机实测：班群 @所有人 晚签通知，唯一正确的回复被这条规则吃掉，
        # 剩下全是复述公告的句子。豁免只给「整句只有一个应答词」，
        # 「收到，我报名」这种带了动作的照旧拦。
        if not (bare_ack(reply) and is_notice_ctx(messages)):
            return "替你答应"
    if claims_done(reply):
        return "假称已经做了"
    # 和上面两条同一类（替用户说假话），但方向不同：许诺的是**将来**，这条是**现在**。
    # 放在占位符/套话之前 —— 它比那两类严重得多，被归因成「套话」会带偏排查方向。
    if fabricates_where(reply, messages):
        return "替你编了你在哪"
    if is_placeholder(reply):
        return "微信占位符"
    if messages and repeats_notice(reply, messages):
        return "复述对方通知"
    if looks_like_offstyle(reply):
        return "用户从不说的套话"
    if messages:
        if echoes_context(reply, messages):
            return "复述聊天记录里的话"
        if repeats_self(reply, messages):
            return "复读用户自己刚说的"
    return ""


def drop_promises(replies: list[str], messages: list[dict] | None = None) -> list[str]:
    """去掉替用户点头、复述别人、复读自己、裸占位符、以及用户从不说的套话。空 = 调用方决定怎么降级。"""
    return [r for r in replies if not drop_reason(r, messages)]


def _drop_why(dropped: list[dict]) -> str:
    """把若干条 `{"text","reason"}` 的原因去重，拼成给人看的一句话。"""
    seen: list[str] = []
    for d in dropped:
        why = d.get("reason") or ""
        if why and why not in seen:
            seen.append(why)
    return "、".join(seen)


def parse_one_json(text: str) -> dict | None:
    """从模型输出里抠出**一条**可直接发送的 `{reply, why}`。校验不过返回 None。

    校验比 `parse_reply_json` 更严：托管是**真的会发出去**，所以
    「建议你…」「你可以回…」这类旁白、带换行的长文、引号包裹、空串都要拦下。
    调用方拿到 None 必须**放弃本次回复**（不是退回模板再发 —— 模板是给人点的，
    自动发出去就成了复读机）。
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    data = None
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except Exception:
            data = None
    if data is None:
        # 与 parse_reply_json 同一套补齐：Ornith 常少写结尾的 `}`
        repaired = close_unbalanced_json(text)
        if repaired is None:
            return None
        try:
            data = json.loads(repaired)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    raw = data.get("reply")
    if not isinstance(raw, str):
        return None
    reply = strip_section_label(raw.strip().strip('"“”').strip())
    if not reply or len(reply) > 40:
        return None
    if "\n" in reply or "\r" in reply:
        return None
    for bad in ("建议你", "你可以回", "你可以回复", "回复建议", "这是一条回复"):
        if reply.startswith(bad):
            return None
    why = data.get("why")
    why = why.strip()[:40] if isinstance(why, str) else ""
    return {"reply": reply, "why": why}


#: 聊天区截图的最长边。470×1024 在真机 A/B 里吃 408~819 token（2026-10-25 实测），
#: 再大只会更慢；再小连「结算画面/聊天截图」都认不出。
SNAP_MAX_SIDE = 1024


def chat_snapshot_b64(max_side: int = SNAP_MAX_SIDE, quality: int = 85) -> str | None:
    """抓当前聊天区，压成 base64 JPEG —— 给模型「看一眼图」。

    为什么截**整个聊天区**而不是只裁那张图：雷达块与 UIA 消息没有稳定的一一对应
    （一条消息可能切两段、相邻两条可能并成一块，实测 8 行对 11 块），
    先按整区送进去；真机 A/B 证明这样已经能从「当成起哄」变成「看出是结算画面、在问录没录」。

    抓不到（微信最小化 / 被判不可见 / PIL 异常）返回 None —— 调用方退回纯文本，不报错。
    """
    try:
        from chat_radar import capture as cap
        from PIL import Image

        frame = cap.capture_frame()
        if frame is None:
            return None
        img = Image.fromarray(frame.roi.crop(frame.image)).convert("RGB")
        w, h = img.size
        scale = max_side / max(w, h)
        if scale < 1:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _needs_look(messages: list[dict], window: int = 6) -> bool:
    """最近几条里有没有图片/视频 —— 没有就不付截图的代价（多 400~800 token + 200ms）。"""
    tail = list(messages or [])[-window:]
    return any((m or {}).get("kind") == "media" for m in tail)


# ------------------------------------------------------------------ 意图分级

#: 「对方在要东西」—— 要的是只有用户手里才有的那几个物件。
#: 2026-10-25 在 117 群实测的原始事故就落在这条上：对方要收款码，模型给的三条是
#: 「发你了 / 码给你 / @你 我发你了」，而用户一个字节都没发出去。这不是措辞不好，
#: 是**替用户声称做了一件没做的事** —— 点一下就真进群了。
#: 所以命中的一律不摆「可直接点发」的候选，改成一句「只有你能给」。
_WANT_THING_RE = re.compile(
    # 「扫」也排除：2026-10-26 真机实测，班群通知里「填表报名成功后记得扫码进群」
    # 被判成「对方在要码」→ 整条消息被 block 掉，而它只是个通知，没有人在要任何东西。
    r"(收款码|付款码|(?<![起号密打加编解扫])二维码|(?<![起号密打加编解扫])码"
    r"|地址|链接|账号|密码|截图|原图)"
)
#: 「问用户自己的情况」—— 在不在、几点、打不打、号里有什么、几级。
#: 答案只存在于用户脑子里；模型写的任何具体回答都是编的（已 3/3 复现）。
_YOU_STATE_RE = re.compile(
    r"(你在哪|你那儿|你那边|你有空|你几点|你什么时候|你多少|你几级|你的号|你号|你的装备|你的分"
    r"|你上来|你在线"
    # 不带「你」的动词式问安排：「今晚打不打」「明天来不来」。中文里省略主语是常态，
    # 光靠「你」会漏掉一大半 —— 2026-10-25 单测里「你今晚打不打」就因为中间插了
    # 「今晚」而漏判。
    r"|打不打|来不来|去不去|上不上|有空没|有没有空|几点上|几点来|几点到"
    r"|什么时候上|什么时候来|在不在)"
)
#: 「要查一下才知道」—— 游戏机制/数值/排行这类**公共知识**。
#: 与 `_YOU_STATE_RE` 的分界很重要：攻略是能搜的，用户的处境不能搜。
#: 命中时给 `can_search=True`，前端把「查攻略」亮出来，搜完再生成候选 ——
#: 这就是用户说的「我看着攻略，再让他做更多额外的事」。
_KNOW_RE = re.compile(
    r"(攻略|阵容|搭配|配比|怎么玩|怎么打|怎么过|怎么升|值得|排行|强度|天赋|加点"
    r"|几级解锁|兑换码|专武"
    # 2026-10-26 真机实测漏的：「打熊该上什么英雄」一个词都不命中，判成 ack。
    # 只收「什么英雄 / 哪个英雄 / 上谁 / 用谁」这类**在选择**的说法 —— 光收「英雄」
    # 会把「这英雄真强」也判成 need_info，而 need_info 是要清空候选的，代价太高。
    r"|什么英雄|哪个英雄|上什么英雄|上谁|用谁|配队|编队|兵种|科技树|升级顺序|活动奖励)"
)
#: 纯表态：不含任何具体安排，答错了也不会替用户许下什么。
_STANCE_RE = re.compile(
    r"^(行|不行|好|不好|可以|不可以|来|不来|打|不打|去|不去|要|不要|没空|有空|等会|晚点|在)"
)
#: 有没有在问。比 chat_read.is_question 宽一点（那个只认问号和十几个词），
#: 但**只用来区分 ack 和 direct**，判错了最多是「多给一条候选」。
_ASK_RE = re.compile(
    r"[?？]|(吗|呢|咋|怎么|什么|多少|几点|哪|谁|为什么|是不是|有没有|要不要|行不行|好不好)"
)
#: 雷达没抓到左右分栏时的提醒。2026-10-25 现场：微信不在前台 → `label_sides`
#: 全返回 unknown → suggest 既不能短路（不知道最后一条是不是自己发的），也给不了
#: 「最后一条是对方发的」这句方向提示 —— 模型于是抓了 8 条之前的「带一份」当当前
#: 话题，答出「带啥/带吃的」。方向提示不是锦上添花，没有它模型会跑偏。
_SIDE_WARN = "读不到左右分栏（微信可能不在前台），方向不保证"


def _side_warn(messages: list[dict] | None) -> str:
    """侧栏读不到时提醒一句；调用方**根本没给 side 字段**时不提醒。

    这两种情况要分清：`chat_read` 给的每条消息都有 side 键（值可能是空串，
    表示雷达没抓到左右）；而有些调用方（测试、或别的入口）压根不带这个键 ——
    那不是「读不到」，是「不关心」。对着后者喊「微信不在前台」就是狼来了。
    """
    msgs = list(messages or [])
    if not msgs or not any("side" in (m or {}) for m in msgs):
        return ""
    return "" if any((m or {}).get("side") for m in msgs) else _SIDE_WARN


def _n(*parts: str) -> str:
    """拼 note：只留非空段，用「·」连。前端把 note 当一行显示。"""
    return " · ".join(str(p) for p in parts if p)


def _voice_warn(messages: list[dict]) -> str:
    """note 里那句「语音听不到」的提示（不需要提示时返回空串）。"""
    if not _voice_only_tail(messages):
        return ""
    return "对方发的是语音，内容我读不到 —— 你听完再说一句更准"
#: 没有正文的消息。`media` 的 text 就是「图片」两个字，拿它当"对方说的话"
#: 会判出奇怪的意图（「对方在要图片」）。
_BARE_MEDIA = ("图片", "视频", "动画表情", "文件", "语音")
#: 纯笑（「哈哈哈哈哈」「嘿嘿」）—— 没有信息量，不能当"对方在问什么"。
_LAUGH_RE = re.compile(r"^[哈呵嘿嘻]{2,}$")


def _recent_other(messages: list[dict], keep: int = 2) -> list[str]:
    """最近几条**对方发的、有实质内容**的消息（从旧到新，最多 `keep` 条）。

    为什么不只看最后一条：群聊里一句话经常被拆成好几条，最后一条往往是「左下」
    「行」这种收尾词，或者一个表情。2026-10-25 拿 3864 群 40 个真实回话时机回放，
    只看最后一条 → **37/40 被判成「没有实质问题」**（ack），而真正的问题
    （「点哪个」「3000还是600」）就在前一条。

    `keep=2` 是折中：再多会把几句不相干的话拼成一个假问题，误伤率上升。
    """
    out: list[str] = []
    for m in reversed(messages or []):
        kind = (m or {}).get("kind") or "text"
        if kind in ("time", "system"):
            continue
        if (m or {}).get("side") == "right":
            break                       # 最后一条是自己发的：对方没说话，没什么可判
        text = _strip_quote(str((m or {}).get("text") or ""))
        if not text or text in _BARE_MEDIA or text.startswith("[") or _LAUGH_RE.match(text):
            continue
        if len(text) < 2:
            continue
        out.append(text)
        if len(out) >= keep:
            break
    out.reverse()
    return out


#: 群里用来催、占位、刷存在感的收尾话。它们几乎总在最后一条，但**一个字都搜不出东西**。
#: 2026-10-26 实测：拿「在线等」当检索词 → `无尽冬日 在线等 攻略`，搜回来
#: 《无尽冬日攻略详解大全》这种泛泛的通关指南，跟「打熊上什么英雄」毫无关系；
#: 换成真正那句问题 → `无尽冬日 打熊该上什么英雄 攻略`，三条全是打熊阵容，
#: 摘要里直接写着「火男比谢尔盖打熊伤害更高」。
_FILLER_RE = re.compile(r"^(在线等|有人吗|在吗|急|求|救命|蹲|等|等等|\d{1,3})$")


def pick_question(messages: list[dict], span: int = 4) -> str:
    """最近几条对方消息里，最像「一件要查的事」的那条。没有就返回空串。

    为什么不直接取最后一条：群里的问句后面永远跟着「在线等」「我现在T5就俩」，
    `_recent_other(keep=2)` 又只装得下两句 —— 「打熊该上什么英雄」就这样同时
    造成三个错：① 被挤出判断范围 → `classify_intent` 判成 ack；
    ② `asked` 取成「在线等」；③ 检索词也取成「在线等」。一个根因，三处症状。

    打分而不是"第一个带问号的"：群里也常有人问「你吃了吗」这种不需要查的，
    光看问号会把检索词带偏。命中知识词才算数（阈值 4 保证至少有一个有效信号）。
    """
    cands: list[str] = []
    for m in reversed(messages or []):
        if len(cands) >= span:
            break
        if (m or {}).get("side") == "right":
            break                       # 最后一条是自己发的：对方没在问
        if (m.get("kind") or "text") != "text":
            continue
        text = _strip_quote(str((m or {}).get("text") or ""))
        if len(text) < 3 or text.startswith("[") or text in _BARE_MEDIA:
            continue
        cands.append(text)

    best, best_score = "", 0.0
    for text in cands:
        score = 0.0
        if _KNOW_RE.search(text):
            score += 3
        if _ASK_RE.search(text):
            score += 2
        if _FILLER_RE.match(text):
            score -= 3
        score += min(len(text), 12) / 4
        if score > best_score:
            best, best_score = text, score
    return best if best_score >= 4 else ""


def _voice_only_tail(messages: list[dict]) -> bool:
    """最后一条是**对方发的语音**，而且前面没有可用的实质内容。

    语音在 prompt 里只有 `[语音 8 秒]` 这个占位符（`_line()`），内容是拿不到的。
    但用户本人在微信里听得到 —— 这是**产品读不到、用户读得到**的一种信息差，
    和「只有你能答」是同一类。不说清楚，用户会以为模型听懂了那条语音。

    2026-10-26 评测实测：只给一条 8 秒语音时，模型输出 `['在的','能']` /
    `['能','在']` —— 它在瞎猜语音里问了什么，而面板上没有任何提示说明这一点。
    """
    msgs = list(messages or [])
    if not msgs:
        return False
    last = msgs[-1] or {}
    if (last.get("kind") or "") != "voice":
        return False
    if last.get("side") == "right":      # 这条是用户自己发的语音
        return False
    # 前面还有别的话时不当「只听得到语音」—— 那些内容足够写一句接话了
    return not _recent_other(msgs)


def classify_intent(messages: list[dict], replies: list[str] | None = None) -> dict:
    """判这类话**该不该由 AI 给正文**。返回 `{intent, gap, asked, can_search}`。

    四档，只有 `direct` 会摆出能直接点发的候选：

    - `direct`    记录里有答案，或者只是接个话茬。给候选。
    - `ack`       对方在起哄/发表情/丢一串数字，没有实质问题。只给附和的一句。
    - `need_you`  答案只在用户脑子里（对方要东西、问用户在不在/几级）。**不给正文**。
    - `need_info` 公共知识（攻略/数值/排行），搜一下就有。给「查攻略」入口。

    为什么不问模型「你知不知道」：9B 的自评和它的答案一样不可靠 —— 它在 117 群那三条
    「发你了」上是**确信**的，why 里写的是「对方要收款码，给个明确回应」。
    判据必须是能从文本上复核的规则，判错了能指着某一行改。
    """
    near = _recent_other(messages)
    blob = " ".join(near)
    # 真正那句问题常常被「在线等」挤出 keep=2 的范围（「打熊该上什么英雄」就是），
    # 所以单独再挑一次，补进判断范围。near 仍是 ack/direct 的底子 —— 它代表
    # 「对方最近在说什么」，只扩到「有没有一件要查的事」这一层。
    picked = pick_question(messages)
    asked = picked or (near[-1] if near else "")
    if picked and picked not in near:
        blob = picked + " " + blob
    if not blob:
        return {"intent": "ack", "gap": "", "asked": asked, "can_search": False, "block": False}
    thing = _WANT_THING_RE.search(blob)
    if thing:
        # block：这类连模型都不用跑 —— 跑出来也只有「发你了」那三条，
        # 白等 2 秒还多一个点错的机会。
        return {
            "intent": "need_you",
            "gap": f"对方在要{thing.group(1)}，只有你能给",
            "asked": asked,
            "can_search": False,
            "block": True,
        }
    # 群通知/公告：没有一个「问题」需要回答，最实用的建议是「这条不用回」。
    # 2026-10-26 真机实测（2561 班群 @所有人 晚签）：模型给的 3 条全是把公告
    # 又广播一遍（「@所有人 在校的别忘晚签」），复述规则砍完就只剩 666 ——
    # 对一条「请及时晚签」回 666，比什么都不给更糟。所以这里明确标 notice，
    # 由 suggest 给一句「不用回」，而不是硬凑候选。
    # 放在 _WANT_THING 之后：通知里真要东西（「把报名表发我」）仍按 need_you 拦。
    if _NOTICE_RE.search(blob) and not _ASK_RE.search(blob):
        return {
            "intent": "notice",
            "gap": "",
            "asked": asked,
            "can_search": False,
            "block": False,
            "quiet": True,
        }
    if _YOU_STATE_RE.search(blob):
        # 已经有一句纯表态（「行」「来」）就不用拦：表态不需要知道细节。
        if any(_STANCE_RE.match(r or "") and len(r) <= 6 for r in (replies or [])):
            return {"intent": "direct", "gap": "", "asked": asked, "can_search": False, "block": False}
        return {
            "intent": "need_you",
            "gap": "你在不在/几点/打不打，只有你自己知道",
            "asked": asked,
            "can_search": False,
            "block": False,
        }
    if _KNOW_RE.search(blob):
        return {
            "intent": "need_info",
            "gap": "这条查一下才知道",
            "asked": asked,
            "can_search": True,
            "block": False,
        }
    if not _ASK_RE.search(blob):
        return {"intent": "ack", "gap": "", "asked": asked, "can_search": False, "block": False}
    return {"intent": "direct", "gap": "", "asked": asked, "can_search": False, "block": False}


#: 对方在**委托**（帮他带/买/拿/打），不是在闲聊。
#: 这条只影响兜底模板：模型正常时走不到这里，但模型挂了正好撞上「帮我带一份」的话，
#: 落到「666」就特别刺眼（2026-10-26 真机实测 r4）。
_REQUEST_RE = re.compile(
    r"(帮我|帮忙|麻烦你|回来.{0,3}带|顺手|捎|带一[份个下]|买一[份个下]|拿一[份个下]|取一[份个下])"
)

#: 对方在问「你们在哪」，或者在报「我到楼下了」。
#: 这类话模型最爱编一个坐标（2026-10-26 采样：我在家呢 / 我也到楼下了 / 我在门口），
#: 被 `fabricates_where` 全拦掉之后，如果兜底落到「666」，等于一句都没回答。
#: 问一句在哪既不含坐标也不含承诺，是这条路上唯一安全的答案。
_WHERE_ASK_RE = re.compile(r"在哪|到楼下了|到哪了|在楼下|到门口了|到了没|到哪呢")


#: 判成 `need_info`（攻略/数值/排行）时，要不要**真的自己搜一次**。
#:
#: 这一档原本不给正文（9B 没有公共知识，给了就是编），出路全靠用户手点「搜攻略」。
#: 自动搜把这一步补上：搜到就照着资料写，搜不到才退回那句「点查攻略」。
#: 代价是这条路从 1.5s 变成 3~5s（检索 2~3s + 热态生成 1.1s），所以留了开关。
#: 关掉用 `VM_REPLY_AUTO_SEARCH=0`。
_AUTO_SEARCH = (os.environ.get("VM_REPLY_AUTO_SEARCH") or "1").strip().lower() not in (
    "0", "false", "off", "no",
)


def _auto_digest(messages: list[dict], chat: str) -> dict | None:
    """自己去搜一次。没搜到 / 搜挂了 / 词拼不出来都返回 None（调用方退回原话）。"""
    try:
        from web_digest import build_query, search

        q = build_query(chat, None, pick_question(messages, span=6))
        if not q:
            return None
        out = search(q, limit=4, timeout=5.0)
    except Exception:  # noqa: BLE001 —— 搜不到不是错误，只是没有额外信息
        return None
    return out if out.get("ok") and out.get("items") else None


def _rule_replies(messages: list[dict], last_side: str | None = None) -> list[str]:
    """模型不在时的兜底。只求「能直接点着发」，不假装懂语境。

    兜底文案也必须走用户本人的口癖：原来这里给的是「收到 / 哈哈哈 / 好嘞」，
    而这三个词在他 474 条真实发言里**一次都没出现过**（第 209 条）。
    模型挂了就够糟糕了，不该再让用户看到三句一眼不是自己说的话。
    """
    if last_side == "right":
        return ["等对方回就行"]
    texts = [
        str((m or {}).get("text") or "").strip()
        for m in (messages or [])
        if ((m or {}).get("kind") or "text") == "text"
        and not str((m or {}).get("text") or "").strip().startswith("[")
    ]
    texts = [t for t in texts if t]
    last = texts[-1] if texts else ""
    if last:
        from chat_read import is_question

        # 先处理「在哪」：`is_question` 也会命中这句，但它给的「在的怎么了」
        # 对「你们现在在哪呢」答非所问，而这里能给出真正接得上的两句。
        if any(_WHERE_ASK_RE.search(t) for t in texts[-2:]):
            return ["到哪了", "哪个门"]
        if is_question(last):
            return ["在的怎么了", "刚看到 你说"]
        # 对方在**委托**（帮他带/买/拿），不是在闲聊。这种时候落到「666」最刺眼 ——
        # 人家刚求你带份饭，面板回一句「666」（2026-10-26 真机实测踩到，让带东西 r4）。
        # 问细节永远是安全的：不含承诺，也不知道要带什么，不会答错。
        if any(_REQUEST_RE.search(t) for t in texts[-2:]):
            return ["要啥", "啥时候要"]
    return ["666", "[奸笑]"]


def _sniff_mime(image_b64: str) -> str:
    """按 base64 头几个字节判图片真实格式。

    `iVBORw0KGgo` = PNG / `/9j/` = JPEG / `UklGR` = WebP。判不出来就当 PNG ——
    微信截图和裁切出来的都是 PNG，猜错的概率远小于硬写 jpeg。
    """
    head = (image_b64 or "")[:8]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/png"


#: 送进视觉塔前，图片长边压到这个像素以内。
#: 6144 的 context 里，图的预算约 4500 token（要留 prompt 与输出）；
#: 按 Qwen3-VL 的 patch 切分，2315×1550 实测 3475 token，约等于 2600 宽的极限。
#: 2400 是「不缩原图、又给别的留出余地」的交点。
VISION_MAX_SIDE = 2400


def image_payload(path: str, max_side: int = VISION_MAX_SIDE) -> str:
    """读一张图 → （必要时先缩小再）base64。读不了就抛，调用方降级。

    缩图是**按长边**缩，保持比例：直接 resize 到固定宽高会把表格拉变形，字也糊。
    2026-10-25 实测这张安排表：缩到 1200 宽只剩 3.6s 但小榜活动全读错（把总结栏
    当成活动），所以**能用原图就别缩**，只有超限才动手。
    """
    import base64
    import io as _io

    with open(path, "rb") as f:
        raw = f.read()
    try:
        from PIL import Image

        im = Image.open(_io.BytesIO(raw))
        if max(im.size) > max_side:
            k = max_side / max(im.size)
            im = im.convert("RGB").resize(
                (max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS
            )
            buf = _io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            raw = buf.getvalue()
    except Exception:  # noqa: BLE001 —— 没装 Pillow 也照样能把原图发过去
        pass
    return base64.b64encode(raw).decode()


def _chat(
    port: int,
    prompt: str,
    timeout: float,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    image_b64: str | None = None,
) -> str:
    user_content = prompt
    if image_b64:
        # OpenAI 视觉格式；llama-server 认 data URI。纯文本请求保持字符串，
        # 免得给不带 mmproj 的模型塞一个它解析不了的结构。
        # mime 按 magic bytes 判，**不要照抄 `image/jpeg`**：我们传的是截图裁出来的
        # PNG，硬写 jpeg 只是靠 llama.cpp 自己嗅探才没炸，换个解码器就 400。
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{_sniff_mime(image_b64)};base64,{image_b64}"}},
        ]
    payload = {
        "model": "qwen3vl",
        "messages": [
            {"role": "system", "content": system or _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": MAX_TOKENS if max_tokens is None else max_tokens,
        "temperature": TEMPERATURE if temperature is None else temperature,
    }
    if NO_THINK:
        # Qwen3.5 这类**思考型**模型：不关思考时，答案全在 reasoning_content 里，
        # content 是空的（2026-10-24 实测：9 条测例全空，看着像模型挂了）。
        # 这里对非思考模型是空操作 —— 模板里没有这个变量，llama.cpp 直接忽略。
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # 推理期间把自己标成「忙」，别让 `_watch_idle` 按空闲把服务杀掉（见 `_should_kill`）。
    # 用 try/finally：超时抛异常也要把计数还回去，否则 _BUSY 永远 >0，服务再也不会自己退出。
    global _BUSY, _LAST_USED
    with _LOCK:
        _BUSY += 1
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    finally:
        with _LOCK:
            _BUSY -= 1
            _LAST_USED = time.time()  # 从「最后一次真正用完」开始重新计空闲
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    # ⚠️ 不要拿 reasoning_content 兜底：那是思考过程（还常常是英文），
    # 里面的格式样例 `"replies":["reply1","reply2"]` 会被当成回复真发出去
    # （2026-10-24 用 Qwopus3.5-9B 实测，9 条里 2 条吐了 reply1/reply2）。
    # 空就让它空，调用方会降级成模板。
    return content


#: 解析失败后重试时追加的一句。只在**已经失败**的请求上加，96% 的请求看不到它。
RETRY_TAIL = (
    "\n\n（上一次输出不是合法 JSON，没法解析。这次只输出一个 JSON 对象，"
    "不要解释、不要 ``` 围栏、不要分点。）"
)


def _ask_json(
    port: int,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int | None = None,
    image_b64: str | None = None,
    timeout: float = CHAT_TIMEOUT,
) -> tuple[dict | None, str]:
    """要一段 JSON：第一次不合格就低温重来一次，并把**不合格的原文**带出来。

    为什么值得多花一次推理：实测解析失败率约 1/27（2026-10-26 评测 `问地点` r3
    就是），而一旦失败整条建议退化成模板 —— 用户点「怎么回」收到「666」。
    重试只在失败时发生，平均下来每次请求多花不到 0.05 次推理，正常路径零延迟。

    返回值第二项是坏掉的原文，成功时为空串。以前这个信息直接丢掉，于是那次失败
    只留下「模型输出没通过校验」七个字，查不出模型到底坏在哪。
    """
    kw = {"system": system, "max_tokens": max_tokens, "image_b64": image_b64}
    content = _chat(port, prompt, timeout, **kw)
    parsed = parse_reply_json(content)
    if parsed is not None:
        return parsed, ""
    try:
        retry = _chat(port, prompt + RETRY_TAIL, timeout, temperature=0.0, **kw)
    except Exception:  # noqa: BLE001 —— 重试失败不该盖掉「第一次就不合格」这个事实
        return None, content
    parsed = parse_reply_json(retry)
    if parsed is not None:
        return parsed, ""
    return None, retry


# ------------------------------------------------------------------ 对外入口


def _judge_replies(
    raw_replies: list[str], messages: list[dict]
) -> tuple[list[dict], list[dict]]:
    """逐条判定候选：留 / 拆 / 删。返回 (逐条留档, 拆分记录)。

    抽出来是因为 `suggest()` 和 `suggest_from_digest()` 各有一份，迟早漂。判据只有
    `drop_reason` 一个来源；这里多做一件**拆**的事：「行，几点去拿？」整条删掉太浪费，
    后半句是问句，可以单独发（见 `salvage_question`）。
    """
    judged: list[dict] = []
    salvaged: list[dict] = []
    seen: set[str] = set()
    for r in raw_replies:
        reason = drop_reason(r, messages)
        if not reason:
            if r not in seen:
                seen.add(r)
                judged.append({"text": r, "reason": ""})
            continue
        fixed = salvage_question(r) if reason == "替你答应" else None
        if fixed and fixed not in seen:
            seen.add(fixed)
            salvaged.append({"from": r, "to": fixed})
            judged.append({"text": fixed, "reason": ""})
            continue
        # 拆不出来、或拆完和另一条候选撞车 —— 都按原样记成被删。
        judged.append({"text": r, "reason": reason})
    return judged, salvaged


def suggest(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict:
    """读最近消息 → 2-3 条可直接发的回复。永远返回可渲染的 dict，不抛异常。

    engine=local 表示真模型给的；engine=rule 是降级模板（note 里写原因）。
    """
    start = time.perf_counter()
    # 前端可能不传 last_side（/replies 的注释里写着「不给也行」），所以自己再推一次：
    # 往前找最后一条有 side 的消息。时间行（center）没有发送方，跳过。
    if not last_side:
        for m in reversed(messages or []):
            s = (m or {}).get("side")
            if s in ("left", "right"):
                last_side = s
                break
    # 最后一条是用户自己发的：对方还没开口，「怎么回」这道题没有正确答案。
    # 这里**故意不调模型**——实测它会硬编一个：2026-10-25 在 117 群，用户刚发完
    # 「码发给我 @瑜清」，模型给出「在的，地址发你了」「地址给你」，而整段记录里
    # 根本没有「地址」这两个字。宁可明确说「不用接」，也不能摆出三条编出来的话。
    if last_side == "right":
        return {
            "engine": "local",
            "model": MODEL_LABEL,
            "replies": [],
            # why 留空：面板会把 why 和 note 用「·」拼起来，这里只要一句话就够
            "why": "",
            "note": "你刚说完，对方还没回 —— 不用接",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    # 对方在要东西（码/地址/图/账号）：这件事的答案不在记录里，只在用户手里。
    # 这里**连模型都不调** —— 调了也只有 117 群那三条「发你了」，白等 2 秒，
    # 还多一次点错的机会。给一句「只有你能给」，比三条编的话有用。
    pre = classify_intent(messages, None)
    if pre["block"]:
        return {
            "engine": "local",
            "model": MODEL_LABEL,
            "replies": [],
            "why": "",
            "note": _n(_side_warn(messages), pre["gap"]),
            "intent": pre["intent"],
            "gap": pre["gap"],
            "asked": pre["asked"],
            "can_search": False,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    busy = _realtime_busy()
    if busy:
        # 与实时变声互斥：不拉起模型，但仍要给能点的东西（桌宠不该「点了没反应」）。
        return {"engine": "rule", "replies": _rule_replies(messages, last_side), "note": _n(_side_warn(messages), busy)}
    ok, why_not = available()
    if not ok:
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": _n(_side_warn(messages), why_not),
        }
    try:
        port = _ensure_server()
        # 最近有图片/视频才截图（多 400~800 token + ~200ms）；没有就纯文本。
        image = chat_snapshot_b64() if _needs_look(messages) else None
        parsed, raw_bad = _ask_json(
            port, build_prompt(messages, chat, last_side), image_b64=image
        )
    except Exception as exc:  # noqa: BLE001 —— 本地推理失败一律降级
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": _n(_side_warn(messages), f"本地模型没跑起来：{exc}"),
        }
    if parsed is None:
        return {
            "engine": "rule",
            "replies": _rule_replies(messages, last_side),
            "note": _n(_side_warn(messages), "模型输出没通过校验，先给模板建议"),
            # 诊断用：坏掉的原文（截断）。前端不读；评测把它打出来，下次失败才有的查。
            "raw": (raw_bad or "")[:400],
        }
    raw_replies = parsed["replies"]
    if not raw_replies:
        # 模型明确判定「这条不用回」：别拿模板凑数 —— 用户点了按钮却收到
        # 「收到 / 哈哈哈 / 好嘞」，比看到「这条不用回」更糟（也更假）。
        return {
            "engine": "local",
            "model": MODEL_LABEL,
            "replies": [],
            "why": parsed["why"],
            "note": _n(_side_warn(messages), _voice_warn(messages),
                       parsed["why"] or "模型认为这条不用回"),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    # 逐条留档：删了什么、为什么删。没有这个，note 里的「有 1 条在替你答应」
    # 就只是一个数字 —— 2026-10-26 评测里 7/24 轮踩到，却查不出模型在编什么。
    _judged, salvaged = _judge_replies(raw_replies, messages)
    replies = [d["text"] for d in _judged if not d["reason"]]
    dropped = [d for d in _judged if d["reason"]]
    note = ""
    if not replies:
        if pre.get("quiet"):
            # 群通知语境 + 模型给的候选全被砍（实测就是「复述对方通知」）：
            # 这时候退回模板只会给「666」「[奸笑]」—— 对「请及时晚签」回 666，
            # 比安静地告诉用户「不用回」差得多。2026-10-26 真机实测原样。
            return {
                "engine": "local",
                "model": MODEL_LABEL,
                "replies": [],
                "why": "",
                "note": _n(_side_warn(messages), "群通知，这条不用特意回"),
                "intent": "notice",
                "gap": "",
                "asked": pre["asked"],
                "can_search": False,
                "dropped": dropped,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        # 全被删了 —— 宁可退回模板，也不能摆出「点了就替他答应」的选项
        replies = _rule_replies(messages, last_side)
        note = f"模型那几条不能用（{_drop_why(dropped)}），已换成安全的模板"
    elif dropped:
        # 措辞只报**真实命中的规则**。原来一律写「在替你答应」，可删除原因也可能是
        # 复述群里的原话、或用了用户从不说的「哈哈/收到」—— 报错原因会让人照着
        # 错误方向改 prompt（2026-10-26 抽 drop_reason 时才看出这一点）。
        note = f"有 {len(dropped)} 条已去掉（{_drop_why(dropped)}）"
        # 只删不补会留下「全是接梗」的残局：2026-10-26 评测实测一轮给出
        # `['666', '[奸笑]']` —— 对方在让带东西，这两条一个都没回答问题。
        # 所以删到只剩 1 条时，从模板补一条**能直接答**的（模板走的是识别问句那条路，
        # 不掺承诺）。`last_side == "right"` 时不补：那时模板是「等对方回就行」，
        # 当成候选发出去很怪。
        if len(replies) < 2 and last_side != "right":
            for cand in _rule_replies(messages, last_side):
                if cand and cand not in replies:
                    replies = [cand, *replies]
                    break
    # 判完候选再收口：`_YOU_STATE_RE` 那条要不要放行，取决于候选里有没有纯表态，
    # 所以这一次判定必须在 `drop_promises` 之后做。
    tier = classify_intent(messages, replies)
    if tier["intent"] == "need_you":
        # 候选一条都不留：这个尺寸的模型在「只有用户知道」的问题上，
        # 三条里必然至少一条在替他说具体的事（实测 3/3），留着就是留着坑。
        note = "这条只有你自己能答，没给候选" + (" · " + note if note else "")
        replies = []
    elif tier["intent"] == "need_info":
        # 公共知识（攻略/数值/排行）模型同样没有 —— 给了就是编，所以先清空。
        # 但清空之后不能就这么把面板丢给用户（原来只留一句「点查攻略」）：
        # 判成 need_info 的这一刻，检索词已经在手上（就是 `asked` 那句），
        # 直接搜一次、照着资料重写，这条链路才闭环。
        replies = []
        doc = _auto_digest(messages, chat) if _AUTO_SEARCH else None
        if doc:
            sub = suggest_from_digest(messages, doc, chat, last_side)
            items = doc.get("items") or []
            return {
                "engine": sub.get("engine", "local"),
                "model": MODEL_LABEL,
                "replies": sub.get("replies") or [],
                # 攻略要点：给用户自己看的，不进群，所以可以比 replies 长。
                "advice": sub.get("advice") or [],
                "why": sub.get("why", ""),
                "note": _n(
                    _side_warn(messages),
                    _voice_warn(messages),
                    sub.get("note") or "",
                    f"自己去查了「{doc.get('q')}」，{len(items)} 条结果",
                    note,
                ),
                "dropped": dropped + list(sub.get("dropped") or []),
                "salvaged": salvaged,
                "intent": tier["intent"],
                "gap": tier["gap"],
                "asked": tier["asked"],
                "can_search": tier["can_search"],
                # 来源摆出来，用户才知道这句话是从哪来的 —— 模型写错时能一眼对回去。
                "sources": [
                    {"title": str(it.get("title") or ""), "url": str(it.get("url") or "")}
                    for it in items[:3]
                ],
                # 整份结果一起给前端：面板上「搜攻略」那块直接填好、不用再点一次
                # （点开就看，省掉重复的 2~3 秒检索）。
                "digest": {
                    "ok": True,
                    "q": doc.get("q") or "",
                    "items": items,
                    "hint": doc.get("hint") or "",
                    "elapsed_ms": doc.get("elapsed_ms") or 0,
                },
                "searched": doc.get("q") or "",
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            }
        note = "这条得查一下 —— 点「查攻略」，搜到再让我写" + (" · " + note if note else "")
    return {
        "engine": "local",
        "model": MODEL_LABEL,
        "replies": replies,
        "why": parsed["why"],
        "note": _n(_side_warn(messages), _voice_warn(messages), note),
        # 诊断用：被删掉的候选原文 + 命中哪条规则。前端不读它也不影响渲染；
        # 评测 `tools/eval_reply_quality.py` 靠它回答「模型到底在编什么」。
        "dropped": dropped,
        # 诊断用：从「答应 + 问细节」里拆出来的半句（原文 → 实际发出去的样子）。
        "salvaged": salvaged,
        "intent": tier["intent"],
        "gap": tier["gap"],
        "asked": tier["asked"],
        "can_search": tier["can_search"],
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


#: 攻略模式在 `_SYSTEM` 后面追加的一段。刻意不动 `_SYSTEM` 本体：
#: 那段是 40 个真实回话时机上逐条调出来的，改一个字都要重新跑对照。
_DIGEST_TAIL = (
    "\n下面附的是刚在网上查到的资料（攻略站的行文，可能有广告味）。"
    "**只取一两个具体点**（名字、数量、比例、顺序），说成一句像你自己随手打的话；"
    "不要整段抄，不要写「资料显示」「攻略上说」「根据资料」，不要带网址和站名。"
    "资料里没有的照样不许编 —— 宁可只接一句「我看看」，也不要补一个数。"
)

#: 有图/有资料时用的提示：在原来的 `replies` 之外**多要一段 `advice`**。
#:
#: 为什么多这一层：用户原话是「有些东西只有我能回答，只能 AI 建议我回答什么；
#: 攻略类的搜出来给我看，我照着攻略再让他做更多事」。他要的是先**看懂**（advice），
#: 再挑一句话发出去（replies）。两者受众不同，不能混在一条字段里 ——
#: advice 进面板给本人读，可以有标点、可以说得细；replies 要能原样发进群，还是 3~10 字。
_ADVICE_TAIL = (
    "\n这条链路要输出 **advice + replies 两样**，JSON 变成："
    '{"advice":["要点1","要点2"],"replies":["回复1","回复2","回复3"]}'
    "\n两样东西的读者不同，别写混："
    "\n- `advice` 是给**用户本人**看的（不进群）：3~5 条，每条一句话，"
    "说清「做什么/什么时候/多少数量」。可以有标点、可以用数字。"
    "**只写图和资料里真有的**，凑不满就少写两条，绝不许补一条听起来像攻略的空话。"
    "\n- `replies` 是能**原样点发进群**的：每条 3~10 个字、不带标点、不用「哈哈」、"
    "不替用户答应任何事、不复述群里已有的话，三条长度要错开。"
    "\n图里是**这一期具体排了什么**（日期、活动名、数量门槛），资料是**这类活动怎么打**。"
    "两边都读，冲突时以图为准 —— 那张图才是这个群这一周真正要过的内容。"
    "\n资料和图上都没有的照样不许编：宁可只写两条 advice、说「我看看」。"
    "\n只输出 JSON，不要解释、不要代码块。"
)

#: 读图那条链路的超时与 token。比纯文本宽得多：
#: 2026-10-25 实测（RTX 5060 8G，Ornith-9B + mmproj）：一张 2315×1550 的表
#: prefill 3.5k token，整条「读图 + 消化资料 + 出 5 条建议 + 3 条回复」15.9s / 146 token；
#: 只读图 22s。给到 90s 是因为显存紧张时视觉塔会退到 CPU，那档慢 3~4 倍。
VISION_TIMEOUT = 90.0
ADVICE_MAX_TOKENS = 520



def vision_ready() -> tuple[bool, str]:
    """能不能读图。返回 (能否, 不能的人话原因)。

    条件是**有 mmproj**。文件在但模型不是多模态的就别硬上 —— llama.cpp 会直接
    报投影维度错（4B 的 2560 维接不到 9B 的 4096），那种错对用户是黑箱。
    """
    if (os.environ.get(ENV_MMPROJ) or "").strip() == "" and ENV_MMPROJ in os.environ:
        return False, "已用 VM_REPLY_MMPROJ= 关掉了看图"
    mm = _find_mmproj()
    if not mm:
        return False, "没找到视觉权重 mmproj（当前模型可能不是多模态）"
    return True, ""


def clean_advice(raw) -> list[str]:
    """把模型给的 advice 洗成「能读的几条」。

    与 `replies` 完全相反：advice 是给本人看的，**允许长、允许标点、允许数字**。
    只挡三样：空串、整段复读（同一句出现两次）、以及超过 120 字的流水账
    （那已经不是「一条要点」，是模型把整篇攻略抄下来了）。
    """
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        s = str(item or "").strip().strip('"“”').strip()
        # 去掉模型自己加的项目符号，但**只认真正的序号形态**：
        # `1.` `1、` `1)` `-` `•`。
        # ⚠️ 不能写成 `^[-*·•\d.、)\s]+` —— 那样会把「10月27日先升巴尔德」开头的
        # 「10」当序号吃掉，变成「月27日先升巴尔德」（2026-10-25 真机实测，
        # 五条建议里三条开头都少了日期）。
        s = re.sub(r"^\s*(?:[-*·•]\s*|\d{1,2}\s*[.、)）]\s*)", "", s)
        if not s or len(s) > 120 or s in out:
            continue
        out.append(s)
    return out[:5]


def suggest_from_digest(
    messages: list[dict],
    digest: dict,
    chat: str = "",
    last_side: str | None = None,
    image: str | None = None,
) -> dict:
    """有外部参考时写建议：`advice` 给用户自己看，`replies` 能直接点发。

    参考可以来自两处，**至少要有一样**：
    - `digest`：搜狗搜到的几条攻略（用户点了「搜攻略」）
    - `image`：群里那张图（用户点了「看图」）

    两样都给了就一起用 —— 这是最有用的一种组合：图说「这一期排了什么」，
    资料说「这类活动怎么打」，模型只需把它们接起来。

    为什么单独一条链路、而不是把资料塞进 `suggest()`：参考资料要用户点一下才付
    这十几秒；塞进默认链路还会让模型开始「主动查攻略」—— 它没有联网能力，
    那条路最后只会变成编造带站名的假引用。

    与 `suggest()` 唯一的分歧是多了一段参考，**风格约束一个字没松**：
    最后那句还是要真人点着发进群里。
    """
    start = time.perf_counter()
    from web_digest import digest_prompt

    items = [it for it in ((digest or {}).get("items") or []) if it.get("title") or it.get("snippet")]
    # 图可能读不了（模型不是多模态 / 文件没了）。读不了就当没图，但**要在 note 里说**，
    # 否则用户会以为模型看过图、只是没写出东西来。
    img_b64 = None
    img_note = ""
    if image:
        can, why_not = vision_ready()
        if not can:
            img_note = f"没读图：{why_not}"
        else:
            try:
                img_b64 = image_payload(image)
            except Exception as exc:  # noqa: BLE001
                img_note = f"图打不开：{type(exc).__name__}"
    if not items and not img_b64:
        return {
            "engine": "rule", "replies": [], "advice": [], "note": img_note or "没搜到可用的资料，先按你自己的话回",
            "intent": "need_info",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    ok, why_not = available()
    if not ok:
        return {
            "engine": "rule", "replies": [], "advice": [], "note": why_not, "intent": "need_info",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    try:
        port = _ensure_server()
        prompt = build_prompt(messages, chat, last_side)
        if items:
            prompt += "\n\n【查到的资料】\n" + digest_prompt(digest, messages)
        if img_b64:
            prompt += (
                "\n\n【群里那张图】用户刚点了「看图」，图附在下面。"
                "它是这个群这一周真正要过的安排，先把上面的日期/活动名/数量读准，再看资料。"
            )
        parsed, raw_bad = _ask_json(
            port,
            prompt,
            system=_SYSTEM + _ADVICE_TAIL,
            max_tokens=ADVICE_MAX_TOKENS if img_b64 or items else 110,
            image_b64=img_b64,
            timeout=VISION_TIMEOUT if img_b64 else CHAT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 —— 与 suggest 一致：失败一律降级，不抛
        return {
            "engine": "rule", "replies": [], "advice": [], "note": f"本地模型没跑起来：{exc}",
            "intent": "need_info",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    if parsed is None:
        return {
            "engine": "rule", "replies": [], "advice": [], "note": "模型输出没通过校验",
            # 诊断用：坏掉的原文（截断）。大图场景一次 20s，失败时更需要能查。
            "raw": (raw_bad or "")[:400],
            "intent": "need_info",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    advice = clean_advice(parsed.get("advice"))
    _judged, salvaged = _judge_replies(parsed["replies"], messages)
    replies = [d["text"] for d in _judged if not d["reason"]]
    dropped = [d for d in _judged if d["reason"]]
    # 抄标题是最常见的失败形态：搜索结果的标题被整句搬过来当回复
    # （「无尽冬日英雄搭配攻略」），长度和语体都跟群聊对不上。
    titles = {str(it.get("title") or "") for it in items}
    for r in list(replies):
        if len(r) > 30:
            dropped.append({"text": r, "reason": "超过 30 字（像在念稿）"})
        elif r in titles:
            dropped.append({"text": r, "reason": "整句抄了搜索结果标题"})
    replies = [r for r in replies if len(r) <= 30 and r not in titles]
    return {
        "engine": "local",
        "model": MODEL_LABEL,
        "replies": replies,
        "advice": advice,
        "why": parsed["why"],
        "dropped": dropped,
        "note": "" if (replies or advice) else "模型没写出能用的，自己说一句更稳",
        "intent": "need_info",
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def suggest_one(messages: list[dict], chat: str = "", last_side: str | None = None) -> dict | None:
    """自动托管用：**一条**可直接发送的回复；任何一步不成立都返回 None。

    为什么不像 `suggest()` 那样降级到规则模板：模板是给用户点的备选，
    自动发出去会变成复读机（「收到」「哈哈哈」），比不回更糟。
    """
    if _realtime_busy():
        # 托管是**真发**：没有模型就不发，绝不拿模板顶出去（与 suggest 的降级不同）。
        return None
    ok, _why_not = available()
    if not ok:
        return None
    try:
        port = _ensure_server()
        content = _chat(
            port,
            build_prompt(messages, chat, last_side),
            CHAT_TIMEOUT,
            system=_SYSTEM_ONE,
            max_tokens=MAX_ONE_TOKENS,
            temperature=ONE_TEMPERATURE,
            image_b64=chat_snapshot_b64() if _needs_look(messages) else None,
        )
    except Exception:  # noqa: BLE001 —— 托管侧只关心「有没有可用的一条」
        return None
    out = parse_one_json(content)
    if out is None:
        return None
    # 托管是**真的发出去**：模型一旦写了「我帮你买」，发出去就是替用户许了承诺。
    # 这里不return，直接丢弃 —— 宁可这条不回，也不能替用户答应（2026-10-24 评测）。
    # 同理，复述群友的话（echoes_context）发出去等于把别人的话认成自己的；
    # 声称「发你了」（claims_done）更是直接替用户说了一句没做过的假话。
    reply = out.get("reply") or ""
    if looks_like_promise(reply) or claims_done(reply) or echoes_context(reply, messages):
        return None
    return out


def status() -> dict:
    """诊断用：模型/可执行文件是否在位、服务是否活着。"""
    ok, why = available()
    return {
        "ok": ok,
        "note": why,
        "engine": "local",
        "model": MODEL_LABEL,
        "model_path": _find_model(),
        "server": _find_exe(),
        "running": _PROC is not None and _PROC.poll() is None,
    }


def _set_port_for_test(port: int | None) -> None:
    """测试夹具：把请求指向一个假 llama-server（不 spawn 真进程）。"""
    global _PORT_OVERRIDE
    _PORT_OVERRIDE = port
