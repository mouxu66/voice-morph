"""输字变声（TTS）链路自检单测。

与 test_send_chain.py（实时变声侧）同规格：纯函数 + 端点两层。
TTS 侧的故障面与实时变声**完全不同**（后者查声卡，这里查引擎/参考音/磁盘），
所以检查项也完全不同，不要照抄 audio 那套。

重点锁三件事：
  1. 引擎进程「未加载」必须是 warn（不是 error）—— 首次合成自动加载，不是故障
  2. 模型缺失时**不**再报进程项 —— 否则用户看到两条红，不知道该做哪个动作
  3. 没有参考音的音色（市场装的 RVC 权重）必须给出去哪条路才对的具体指引
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import tts_api as mod  # noqa: E402


def by_key(report, key):
    return next((i for i in report["items"] if i["key"] == key), None)


def build(**over):
    """默认全绿，按需覆盖单项。"""
    kw = {
        "model_ok": True,
        "model_detail": "/models/qwen3-tts-1.7b-base",
        "tokenizer_ok": True,
        "worker_alive": True,
        "voice_id": "kangaroo",
        "ref_ok": True,
        "ref_detail": "参考音就位：reference.wav",
        "out_writable": True,
        "out_detail": "/outputs（可写）",
        "tokenizer_detail": "/models/qwen3-tts-tokenizer-12hz",
    }
    kw.update(over)
    return mod.build_tts_chain_report(**kw)


def test_all_good():
    r = build()
    assert r["ok"] is True and r["all_ok"] is True
    assert len(r["items"]) == 4
    assert all(i["ok"] for i in r["items"])


def test_worker_not_loaded_is_warn_not_error():
    """未加载 = 首次使用要等 20~30 秒，属**正常现象**，不能报成故障吓退用户。"""
    r = build(worker_alive=False)
    it = by_key(r, "tts_worker")
    assert it["ok"] is False
    assert it.get("warn") is True
    assert "首次" in it["detail"]


def test_model_missing_suppresses_worker_item():
    """模型都没有时不再报进程项 —— 否则两条红项，用户不知道先做哪个。"""
    r = build(model_ok=False, model_detail="模型缺失")
    assert by_key(r, "tts_models")["ok"] is False
    assert by_key(r, "tts_worker") is None
    assert r["all_ok"] is False


def test_missing_tokenizer_also_fails_model_item():
    r = build(tokenizer_ok=False)
    it = by_key(r, "tts_models")
    assert it["ok"] is False
    assert "分词器" in it["detail"]


def test_no_voice_selected():
    r = build(voice_id="", ref_ok=False)
    it = by_key(r, "voice_ref")
    assert it["ok"] is False
    assert "音色库" in it["hint"]
    assert r["all_ok"] is False


def test_voice_without_reference_points_to_right_path():
    """市场音色（RVC 权重）没有 reference → 必须说清「只能开麦变声」并指向音色库。

    这是首页 STEP 0 最容易踩的坑：用户试听了预置音色，以为能输字合成。
    """
    r = build(voice_id="market_voice", ref_ok=False, ref_detail="音色 [market_voice] 不存在")
    it = by_key(r, "voice_ref")
    assert it["ok"] is False
    assert "实时变声" in it["hint"]
    assert "音色库" in it["hint"]


def test_out_dir_not_writable():
    r = build(out_writable=False, out_detail="/outputs 不可写：磁盘满")
    it = by_key(r, "out_dir")
    assert it["ok"] is False
    assert it.get("warn") is not True  # 这条是硬阻断，不是告警
    assert "磁盘" in it["detail"] or "磁盘" in it["hint"]


def test_items_are_frontend_compatible():
    """结构必须与 /audio/send_chain 的 items 同构，前端才能复用同一套渲染。"""
    r = build(worker_alive=False)
    for it in r["items"]:
        assert set(it) >= {"key", "ok", "label", "detail", "hint"}
        assert isinstance(it["key"], str) and isinstance(it["ok"], bool)


def test_endpoint_probes_and_passes_through(monkeypatch):
    """端点层：确认探测结果被透传（用 monkeypatch 顶掉真实磁盘/进程探测）。"""
    import config as cfg

    monkeypatch.setattr(cfg, "QWEN_MODEL_DIR", Path("/nonexistent-model"))
    monkeypatch.setattr(cfg, "QWEN_TOKENIZER_DIR", Path("/nonexistent-tok"))
    monkeypatch.setattr(mod, "_probe_out_dir", lambda: (True, "/outputs（可写）"))
    monkeypatch.setattr(mod, "selected_voice", lambda: "")
    r = mod.tts_send_chain()
    assert r["ok"] is True and r["all_ok"] is False
    assert by_key(r, "tts_models")["ok"] is False
    assert by_key(r, "voice_ref")["ok"] is False


def test_probe_out_dir_reports_status():
    """_probe_out_dir 返回 (bool, 说明) 二元组，且真写探针后不留垃圾文件。"""
    ok, detail = mod._probe_out_dir()
    assert isinstance(ok, bool) and isinstance(detail, str)
    if ok:
        assert not (mod.OUT / ".tts_write_probe").exists()
