"""P2-1 失败可诊断：把「跑完了但结果不对」翻译成「为什么 + 下一步做什么」。

为什么要单独一个模块（2026-09-25）：
    `pipeline_api._pipeline_error_hint()` 已经处理了**抛异常**的那一类失败
    （ffmpeg 挂了、demucs 没下到模型）。但主线上更常见的失败是**不抛异常的**：

        · 素材是纯 BGM，切出 0 条 —— 流水线报 done，用户看着空白页面不知道哪错了；
        · 切出 40 条但 34 条是 D 级 —— 界面只说「质检可用 6 条」，不说为什么；
        · 音色建好了却「怎么调都不像」—— 真正的原因是参考音只有 3 秒，
          而界面上找不到任何地方说这件事。

    `ft_corpus_qc._advice()` 已经为**微调**那条链路做了同类的事（等级分布 + Top 原因
    + 建议），但它只服务 `media/ft/<id>/clips` 这一种语料，且输出是给已懂的人看的
    内部结构（`grades` / `top_reasons` / `advice` 三段）。

    本模块把同样的判定提升成**全链路通用**的 `build_quality_verdict()`：
    输入是流水线的 `summary`（`clip_qc.score_prefixes` 产出）与任务状态，
    输出是一个**纯数据**的结论对象，三个字段各司其职：

        verdict  str   机器判定（ok / too_short / wrong_speaker / noisy / too_loud / empty）
        title    str   一句话结论（「这段素材是纯伴奏」）
        detail   str   人话解释，带具体数字（「40 条切片里 34 条只听到伴奏」）
        actions  list  可执行的下一步，每条都指向界面上的一个具体位置

设计约定（与 clip_qc 同口径）：
    · **纯函数**：不读盘、不起线程、不 import torch。所有输入由调用方给全，
      于是可以用造出来的 summary 把每条判定分支都钉死（见 test_quality_verdict.py）。
    · **绝不抛**：输入残缺时退化成「信息不足」而不是异常 —— 诊断器自己崩掉
      比没有诊断器更糟（用户会以为整个后端挂了）。
    · **判定按「能否挽救」排序**：先说必须换素材的（wrong_speaker），
      再说要调参数的（too_loud），最后才是能补录解决的（too_short）。
      「换素材」比「调参数」贵，所以前者优先级更高 —— 早说早止损。
"""

from __future__ import annotations

# 判定码。★ 前端 `lib/qualityVerdict.ts` 的 VERDICT_META 必须覆盖这里每一个值 ——
# 漏掉一个不会报错，只会显示成默认中性色（"说好的红叉呢"），故由
# `test_quality_verdict.py::test_every_verdict_code_has_frontend_meta` 对账。
VERDICT_OK = "ok"
VERDICT_TOO_SHORT = "too_short"
VERDICT_WRONG_SPEAKER = "wrong_speaker"
VERDICT_NOISY = "noisy"
VERDICT_TOO_LOUD = "too_loud"
VERDICT_EMPTY = "empty"
VERDICT_UNKNOWN = "unknown"

# clip_qc 的判废原因（`score_clip` 的 reasons 文案）→ 判定码。
# 用**前缀**匹配而不是全等：原因里带具体数字（`有效语音仅 12%`），
# 全等匹配必然漏（`ft_corpus_qc._reason_key` 已经踩过一次，见其实现）。
# 顺序即优先级：同一条切片命中多条时，取**更靠前**的那条 —— 靠前的更严重
# （「主说话人不一致」能一票否决，「时长不符」只是不够长）。
_REASON_RULES: tuple[tuple[str, str], ...] = (
    ("声纹与主说话人不一致", VERDICT_WRONG_SPEAKER),
    ("中段静音", VERDICT_NOISY),
    ("信噪比", VERDICT_NOISY),
    ("有效语音仅", VERDICT_NOISY),
    ("削波", VERDICT_TOO_LOUD),
    ("响度", VERDICT_TOO_LOUD),
    ("时长 ", VERDICT_TOO_SHORT),
)

#: 可用（A+B）占比低于此值 → 认为该素材整体不可用。
#: 0.35 是拍出来的：实测 73 条袋鼠切片里干净的那批可用率 ~0.8，
#: 而"混了他人声"的素材可用率在 0.2~0.3 —— 0.35 落在两簇中间偏安全的一侧。
MIN_OK_RATIO = 0.35

#: 参考音低于此秒数 → 零样本克隆质量明显下降（ICL 需要足够音素覆盖）。
#: `mine_api.mine_save` 的 REF_CAP_MS=20000 是**上限**，这里说的是**下限**。
MIN_REFERENCE_S = 10.0

#: 主说话人占比低于此值 → 素材以他人声为主，音色会被带偏。
MIN_MAIN_RATIO = 0.5


def _ratio(part: int, whole: int) -> float:
    """part/whole，whole 为 0 时返回 0.0（不抛 ZeroDivisionError）。"""
    try:
        w = int(whole or 0)
        return float(part or 0) / w if w > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def dominant_reason(reasons: dict | list | None) -> str:
    """把「原因 → 次数」聚合成最值得说的那一条判定码。

    接受两种输入：
        dict  {"声纹与主说话人不一致（0.31），疑似他人声或伴奏残留": 12, ...}
        list  ["信噪比 8dB 偏低（底噪或伴奏残留）", ...]
    —— `clip_qc.score_material` 落的是 dict（已聚合），逐条分析时手上有 list。

    聚合口径与 `ft_corpus_qc._reason_key` 一致：去掉数字再比，否则
    「信噪比 8dB」与「信噪比 9dB」会被当成两个不同的原因，谁都不占多数。
    """
    counts: dict[str, int] = {}
    if isinstance(reasons, dict):
        items = reasons.items()
    elif isinstance(reasons, list):
        items = ((r, 1) for r in reasons)
    else:
        return ""

    for raw, n in items:
        text = str(raw)
        # 命中最靠前（最严重）的规则即定；都不命中归到「其它」，在这里丢弃 ——
        # 诊断要说的是**能行动**的原因，说不出科目名的原因没有行动价值。
        for prefix, code in _REASON_RULES:
            if prefix in text:
                counts[code] = counts.get(code, 0) + int(n or 1)
                break
    if not counts:
        return ""
    return max(counts, key=lambda k: counts[k])


def build_quality_verdict(
    summary: dict | None = None,
    *,
    speaker: dict | None = None,
    reference_s: float | None = None,
    status: str = "",
    error: str = "",
) -> dict:
    """把质检汇总 + 上下文，转成「结论 + 解释 + 下一步」的纯数据对象。

    参数（全部可选，缺谁就少一条判据，绝不抛）：
        summary       `clip_qc.score_prefixes()` 的产出：
                      {"total", "ok", "grades": {A,B,C,D}, "materials", "top_reasons"}
        speaker       `speaker_sep` 的返回（用 `speakers[main].ratio` 判是否以他人声为主）
        reference_s   目标音色参考音频时长（秒），用于提示「参考音太短」
        status        流水线状态（"done"/"error"/"cancelled"），用于区分「跑失败」与「跑完了但结果差」
        error         流水线抛出的错误原文（有它就直接转述，不再做质检推断）

    返回：
        {"verdict", "title", "detail", "actions", "ok_ratio", "total", "ok",
         "grades", "reason"}  —— 前端只认 verdict 决定配色，其余都是文案素材。
    """
    summary = summary if isinstance(summary, dict) else {}
    grades = summary.get("grades") if isinstance(summary.get("grades"), dict) else {}
    try:
        total = int(summary.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        ok = int(summary.get("ok") or 0)
    except (TypeError, ValueError):
        ok = 0
    ratio = _ratio(ok, total)
    reason = dominant_reason(summary.get("top_reasons"))

    base = {
        "ok_ratio": round(ratio, 3),
        "total": total,
        "ok": ok,
        "grades": {k: int(grades.get(k) or 0) for k in ("A", "B", "C", "D")},
        "reason": reason,
    }

    # ① 跑失败了：直接把后端原文摊开。这里**不加工**错误文案 ——
    #    `pipeline_api._pipeline_error_hint` 已经翻译过一轮了，再翻译一次
    #    只会把它给的具体建议（"检查网络后重试"）磨成泛泛而谈。
    if status == "error":
        return {
            **base,
            "verdict": VERDICT_EMPTY,
            "title": "这段素材没处理成功",
            "detail": error or "流水线中断了，但没有留下具体原因。",
            "actions": [
                "按上面的信息排查；网络类错误直接重试一次（demucs 首次要下模型）",
                "反复失败就换一段素材 —— 别在一段坏素材上耗",
            ],
        }

    # ② 一条都没切出来：从源头就说清是什么素材，而不是让用户去看空列表
    if total == 0:
        return {
            **base,
            "verdict": VERDICT_EMPTY,
            "title": "这段素材里没有人声",
            "detail": (
                "整段都没检测到有效语音。最常见的原因是：素材本身就是纯音乐/纯伴奏、"
                "全程环境噪音，或者后段是静音的空白录制。"
            ),
            "actions": [
                "换一段有人说话的素材 —— 这是唯一能解决的办法",
                "如果是「有画面但没声音」的视频，先用播放器确认它真的有音轨",
            ],
        }

    # ③ 说话人维度：比质检分数更根本 —— 分数再高，唱的不是本人也没用
    if isinstance(speaker, dict) and speaker.get("n_speakers", 0) and speaker.get("speakers"):
        speakers = [s for s in speaker["speakers"] if isinstance(s, dict)]
        main = next((s for s in speakers if s.get("is_main")), None)
        main_ratio = float((main or {}).get("ratio") or 0.0)
        if len(speakers) > 1 and main_ratio < MIN_MAIN_RATIO:
            n = len(speakers)
            return {
                **base,
                "verdict": VERDICT_WRONG_SPEAKER,
                "title": "这段素材以别人的声音为主",
                "detail": (
                    f"识别出 {n} 个人在说话，而你想要的那位只占了 "
                    f"{round(main_ratio * 100)}% —— 剩下的是别人。"
                    f"用这种素材训练，音色会被别人的声音带偏（表现是「像但不像你」）。"
                ),
                "actions": [
                    "在切片列表里按说话人筛选，只勾选「主说话人」那批再建库",
                    "或者换一段只有你一个人说话的素材 —— 更省事，效果也更好",
                    "如果确实需要这段素材，先点「说话人分离」让它把切片按人分好",
                ],
            }

    # ④ 有切片但基本都不可用：按**占多数的那类原因**给对策
    if ratio < MIN_OK_RATIO:
        if reason == VERDICT_NOISY:
            return {
                **base,
                "verdict": VERDICT_NOISY,
                "title": "切片里的伴奏/噪声压过了人声",
                "detail": (
                    f"{total} 条切片里只有 {ok} 条可用，多数是"
                    f"「信噪比低 / 有效语音少 / 中段静音」—— 人声被伴奏盖住了。"
                    f"这类切片会让模型学到的音色发糊。"
                ),
                "actions": [
                    "用更干净的素材：录音时离麦克风近一点、关掉背景音乐",
                    "如果原素材本身带 BGM，运行流水线时确保「去除背景音乐」这一步没有跳过",
                    "去细看 D 级切片的判废原因，确认是不是同一类问题",
                ],
            }
        if reason == VERDICT_TOO_LOUD:
            return {
                **base,
                "verdict": VERDICT_TOO_LOUD,
                "title": "素材有爆音（削波）",
                "detail": (
                    f"{total} 条切片里只有 {ok} 条可用，多数被判「响度异常 / 削波」。"
                    f"录进来的声音顶到了上限被压平，这种波形里的音色信息已经丢了，"
                    f"补不回来。"
                ),
                "actions": [
                    "换素材 —— 削波是录音时就发生的事，后期修不回来",
                    "下次录音前把系统输入音量调到 70% 左右，别贴太近",
                ],
            }
        if reason == VERDICT_TOO_SHORT:
            return {
                **base,
                "verdict": VERDICT_TOO_SHORT,
                "title": "人声太碎，凑不够训练用量",
                "detail": (
                    f"{total} 条切片里只有 {ok} 条可用，多数因为「时长不达标」被判废 —— "
                    f"素材里的有效说话被切得很零碎。"
                ),
                "actions": [
                    "换一段连续说话更长的素材（比如独白、讲解，而不是一问一答的对话）",
                    "再补录一段，可用切片累计到 30 秒以上音色才稳",
                ],
            }
        # 说不出具体科目：诚实地说"大部分不能用"，不给假具体
        return {
            **base,
            "verdict": VERDICT_NOISY,
            "title": "大部分切片不可用",
            "detail": (
                f"{total} 条切片里只有 {ok} 条可用"
                f"（A {base['grades']['A']} / B {base['grades']['B']} / "
                f"C {base['grades']['C']} / D {base['grades']['D']}），"
                f"但不合格的原因比较分散，没有单一主导问题。"
            ),
            "actions": [
                "在切片列表里按「可用等级」筛出 A/B 级，先只用这批建库",
                "换一段人声更突出、环境更安静的素材，是提高可用率最直接的办法",
            ],
        }

    # ⑤ 切片可用，但参考音太短（只在调用方给了参考音时长时提示）
    if reference_s is not None and float(reference_s or 0) < MIN_REFERENCE_S:
        return {
            **base,
            "verdict": VERDICT_TOO_SHORT,
            "title": "参考音有点短，音色会不稳",
            "detail": (
                f"切片本身没问题（{total} 条里 {ok} 条可用），但当前参考音只有 "
                f"{round(float(reference_s), 1)} 秒。零样本克隆要覆盖足够多的音素，"
                f"参考太短时，念到参考音里没出现过的字就会「飘」。"
            ),
            "actions": [
                "再补录 10 秒以上，或从现有切片里多勾几条一起聚合",
                "聚合后在音色库试听一句新话（不是参考音里的原话），听它飘不飘",
            ],
        }

    # ⑥ 一切正常 —— 也要给一句「接下来做什么」，否则用户会在这一步发呆
    return {
        **base,
        "verdict": VERDICT_OK,
        "title": "素材质量没问题",
        "detail": (
            f"{total} 条切片里 {ok} 条可用"
            f"（A {base['grades']['A']} / B {base['grades']['B']}），"
            f"这个比例足以建出一个稳定的音色。"
        ),
        "actions": [
            "把 A/B 级切片导出成训练集，下一步去实时变声页训练",
            "训练完试听一句参考音里没有的话，确认音色泛化得动",
        ],
    }
