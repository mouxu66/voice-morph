"""会话助理：把「群里的那句话」变成一张**待你点头的提案**，而不是直接发消息。

设计边界（改之前先读这一节）
--------------------------
1. **观察与执行彻底分开**。`extract`/`search` 只产出 `Proposal`；只有 `executor.run()`
   会碰 TTS / RVC / 微信。而 `executor.run()` 的入口只有一个：`/assistant/proposals/{id}/confirm`。
   没有「置信度高就自动发」这种开关 —— 用户要的是"让我来同意"，不是"让我来得及撤销"。
2. **动作是白名单**。`schema.KINDS` 里没有的 kind 一律拒绝（和 `ai_actions` 同一口径）。
   新增能力必须在这里声明 required 字段，解析层与执行层都按它校验。
3. **不造第二套功能 API**。执行器只转调既有能力（`ytdlp_fetch` / `qwen3_tts` /
   `rvc_convert` / `wechat_voice`），判据、护栏、降级全部复用原处。本仓吃过
   「两处各抄一份判据、漂掉一个」的亏（见 `docs/犯错档案-工程.md`）。
4. **解析失败要留白**。抽不出歌名就给 `needs_more`，不要拿整句话当歌名塞进搜索 ——
   搜错了还发出去，比不搜更糟。
"""

from __future__ import annotations

__all__ = ["schema", "store", "extract", "search", "audio", "executor"]
