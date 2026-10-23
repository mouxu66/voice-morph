"""提案的数据结构与动作白名单 —— 读侧（前端列表）与写侧（执行器）共用的唯一真相源。

为什么单独一个模块、而不是散在 api/executor 里
----------------------------------------------
`api.py` 依赖 FastAPI，`executor.py` 依赖 TTS/RVC/微信（重且容易失败）。而本模块
只有 dataclass 和字符串常量，于是**测试可以只 import 它**，不需要拉起任何重依赖。
这跟 `ai_actions.py` 把动作表从写侧拆出来是同一个理由。

kind 的语义（三种，刻意只有三种）
--------------------------------
- `song.send`   ：有人说「发首 X」→ 搜歌 → 截一段 → 发语音条
- `voice.say`   ：你要「用 X 的声音说 Y」→ TTS(+RVC) → 发语音条
- `reply.draft` ：分析完最后几条消息 → 给一句回复草稿（**永远不自动发**）

`reply.draft` 没有执行副作用：它只把文本回给前端，发不发由人在微信里自己按。
把它做成一个「执行会发送」的动作是越界的 —— 用户原话里"分析"和"发"是两件事。

加字段的规矩
------------
`required` 里的字段缺失 → 提案根本不生成（解析层返回 needs_more）。
`optional` 里的字段缺失 → 用默认值，不许在执行期抛 KeyError。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field

SCHEMA = 1

#: 提案状态机。`running` 之后不可再被 confirm（防重复点击导致发两条）。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
_TERMINAL = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)


@dataclass(frozen=True)
class KindSpec:
    """一种动作的声明。`fields` 只用于校验与前端渲染，不含业务逻辑。"""

    label: str
    summary: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    #: 执行时会不会碰微信。前端据此在按钮上写"确认发送"还是"确认生成"。
    sends: bool


KINDS: dict[str, KindSpec] = {
    "song.send": KindSpec(
        label="发一首歌",
        summary="搜歌 → 取回音频 → 截一段 ≤ 微信语音条上限 → 发送",
        required=("query",),
        optional=("url", "voice", "start_s", "clip_s"),
        sends=True,
    ),
    "voice.say": KindSpec(
        label="发一句语音",
        summary="文字 → TTS（可选 RVC 换声）→ 发送语音条",
        required=("text",),
        optional=("voice", "rvc_voice", "dialect"),
        sends=True,
    ),
    "reply.draft": KindSpec(
        label="拟一句回复",
        summary="根据最近的对话给草稿，只返回文本，不发送",
        required=("text",),
        optional=("tips",),
        sends=False,
    ),
}


def kind_spec(kind: str) -> KindSpec:
    """取动作声明；未知 kind 直接炸（调用方应先在入口校验）。"""
    try:
        return KINDS[kind]
    except KeyError as exc:
        raise KeyError(f"没有这个动作：{kind}") from exc


def validate_params(kind: str, params: dict) -> tuple[dict, list[str]]:
    """按声明校验并归一化参数。

    返回 `(clean, missing)`：`missing` 非空时调用方**不该**生成可执行的提案，
    应当把它变成一条 `needs_more`（"还需要你说清歌名"），而不是带着空参数去搜索。
    """
    spec = kind_spec(kind)
    clean: dict = {}
    missing: list[str] = []
    for f in spec.required:
        val = params.get(f)
        if isinstance(val, str) and val.strip():
            clean[f] = val.strip()
        elif val not in (None, ""):
            clean[f] = val
        else:
            missing.append(f)
    for f in spec.optional:
        if f in params and params[f] not in (None, ""):
            clean[f] = params[f]
    return clean, missing


@dataclass
class Proposal:
    """一条等待用户点头的动作提案。

    `evidence` 是"机器可读的下一步看哪"（如 `{"matched": "发首晴天", "from": "chat"}`），
    `detail` 才是给人看的一句话 —— 反过来会让消费方去解析长文本。
    """

    kind: str
    title: str
    detail: str = ""
    params: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    source: str = "manual"
    status: str = STATUS_PENDING
    result: dict | None = None
    error: str = ""
    id: str = ""
    created: float = 0.0
    updated: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created:
            self.created = time.time()
        if not self.updated:
            self.updated = self.created

    def to_record(self) -> dict:
        """**落盘用**。只含 dataclass 自己的字段，`Proposal(**rec)` 能原样还原。

        和 `to_dict()` 分开是必须的：后者给前端看，多带了 `label`/`sends` 两个派生字段，
        拿它落盘的话下次启动 `Proposal(**rec)` 会因未知关键字抛 TypeError，
        于是整盘提案被当成脏数据跳过 —— 用户会看到"待办全没了"。
        """
        return asdict(self)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = KINDS[self.kind].label if self.kind in KINDS else self.kind
        d["sends"] = KINDS[self.kind].sends if self.kind in KINDS else True
        return d

    def touch(self, status: str) -> None:
        self.status = status
        self.updated = time.time()

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def usable(self) -> tuple[bool, str]:
        """能不能被 confirm。返回 `(ok, 拒绝原因)` —— 原因直接给人看。"""
        if self.status == STATUS_RUNNING:
            return False, "这条已经在执行了"
        if self.status in _TERMINAL:
            return False, f"这条已经{_STATUS_CN.get(self.status, self.status)}"
        return True, ""


_STATUS_CN = {
    STATUS_PENDING: "待确认",
    STATUS_RUNNING: "执行中",
    STATUS_DONE: "完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "取消",
}
