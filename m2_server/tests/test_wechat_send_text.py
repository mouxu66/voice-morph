"""一键发送 `/api/wechat/send_text`（文字 → TTS → RVC → 自动发微信）单测。

背景：桌宠「合成并发送」原本走 `/api/tts` + `play_to_cable`（半自动，还得用户自己按住 Alt），
且**整条链路没有 RVC** —— 袋鼠音色的唯一来源被漏掉了。这里锁定新链路的组装行为。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import common  # noqa: E402
import config as cfg  # noqa: E402
import runtime  # noqa: E402
import rvc_convert  # noqa: E402
import tts_api  # noqa: E402
import wechat_voice as wv  # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    # 音色库也指向临时目录：send_text 现在会看「这个音色有没有参考音」（_voice_has_reference
    # → runtime.VOICEBANK），不隔离就会去读开发机的 D:/变声/media/voicebank ——
    # 开发机装了 kangaroo、CI 没装，同一条用例两边结论不同。
    # 默认只放一个有参考音的 kangaroo：现有用例大多用它，且都不该依赖「没有参考音」。
    vb = tmp_path / "voicebank"
    (vb / "kangaroo").mkdir(parents=True)
    (vb / "kangaroo" / "reference.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(runtime, "VOICEBANK", vb)
    # 当前选中音色同理（selected_voice.json 也是本机状态）；需要它的用例自行覆盖
    monkeypatch.setattr(common, "selected_voice", lambda: "")
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    # 绝不能让单测真去点微信 / 真跑 RVC / 真加载 TTS
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)
    # 2026-09-18：send_text 现在会在锁内做只读预检（_send_preflight），
    # conftest 把 VM_WECHAT_RESTART 强制成 0 → 预检会真去枚举微信窗口。
    # 测试机微信开不开都会让结果抖动，这里统一打桩成「微信就绪」；
    # 预检本身的分因行为在 test_wechat_restart.py 里专门测。
    monkeypatch.setattr(
        wv.wproc,
        "list_wechat_processes",
        lambda: [{"pid": 1, "name": "Weixin.exe", "exe": "C:/wx/Weixin.exe"}],
    )
    monkeypatch.setattr(
        wv.wproc,
        "enum_wechat_windows",
        lambda: [{"hwnd": 11, "pid": 1, "area": 2_000_000, "exe": "C:/wx/Weixin.exe"}],
    )
    # 切卡预热（_PendingApply）在 _do_send 之前的后台线程就跑 _run_audio，
    # 而本机 audio_config.ps1 真实存在 → 不打桩就会真起 PowerShell 动声卡。
    # 这里默认打桩，需要记录调用的测试自行覆盖。
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    yield tmp_path


def _add_ref_voice(tmp_path: Path, *ids: str) -> Path:
    """在隔离出的音色库里造几个「有参考音」的音色（给借用参考音的用例用）。"""
    vb = tmp_path / "voicebank"
    for i in ids:
        (vb / i).mkdir(parents=True, exist_ok=True)
        (vb / i / "reference.wav").write_bytes(b"RIFF")
    return vb


def _fake_send(tmp_path, monkeypatch, ret=None):
    """打桩 TTS / RVC / _do_send，返回记录用的 dict。"""
    calls = {}

    def _synth(text, voice_id="", *a, **kw):
        calls["synth"] = (text, voice_id)
        f = tmp_path / "tts_fake.wav"
        f.write_bytes(b"RIFF")
        return f, 3.0, voice_id or "kangaroo"

    def _rvc(wav, voice, pitch=0, index_rate=0.5):
        calls["rvc"] = (Path(wav).name, voice, pitch, index_rate)
        out = tmp_path / f"{Path(wav).stem}_{voice}.wav"
        out.write_bytes(b"RIFF")
        return out

    def _do(req, pre_apply=None):
        calls["send"] = req.wav
        # 模拟真 _do_send 的契约：领了 pre_apply 就必须消费（消费后 send_text 不再 abandon）
        if pre_apply is not None:
            pre_apply.result()
            calls["pre_apply_consumed"] = True
        return {"ok": True, "outcome": "ok", "steps": ["已发送"], "restored": True}

    monkeypatch.setattr(tts_api, "synth_wav", _synth)
    monkeypatch.setattr(rvc_convert, "rvc_convert", _rvc)
    monkeypatch.setattr(rvc_convert, "resolve_model", lambda v: (Path("x.pth"), None))
    monkeypatch.setattr(wv, "_do_send", _do)
    return calls


# ---------------- resolve_rvc_voice：voicebank id → RVC 实验名 ----------------


def test_resolve_rvc_voice_prefers_v2(monkeypatch):
    """kangaroo → kangaroo_v2（现役主力），不是 kangaroo（voicebank 目录名不同）。"""
    seen = []

    def _resolve_model(voice):
        seen.append(voice)
        if voice != "kangaroo_v2":
            raise rvc_convert.RvcError("nope")
        return (Path("x.pth"), None)

    monkeypatch.setattr(rvc_convert, "resolve_model", _resolve_model)
    assert rvc_convert.resolve_rvc_voice("kangaroo") == "kangaroo_v2"
    assert seen[0] == "kangaroo_v2"  # 主力优先，先试它


def test_resolve_rvc_voice_falls_back_to_40k(monkeypatch):
    """v2 没有时退 40k（实际目录名是 kangaroo_v2_40k，不是 kangaroo_40k）。"""
    monkeypatch.setattr(
        rvc_convert,
        "resolve_model",
        lambda v: (
            (Path("x.pth"), None)
            if v == "kangaroo_v2_40k"
            else (_ for _ in ()).throw(rvc_convert.RvcError("nope"))
        ),
    )
    assert rvc_convert.resolve_rvc_voice("kangaroo") == "kangaroo_v2_40k"


def test_resolve_rvc_voice_none_when_missing(monkeypatch):
    monkeypatch.setattr(
        rvc_convert, "resolve_model", lambda v: (_ for _ in ()).throw(rvc_convert.RvcError("nope"))
    )
    assert rvc_convert.resolve_rvc_voice("nosuch") is None
    assert rvc_convert.resolve_rvc_voice("") is None


# ---------------- /api/wechat/send_text ----------------


# -------- 无参考音的音色（市场装的）也能发：借一段参考音做语气，音色交给 RVC --------
#
# 由来（2026-09-22 用户实测，原话「只能选择两个音色」）：用户在「音色市场」装了几个
# 卡通音色，桌宠面板的下拉里一个都看不到，永远只有自训的那两个。根因不在下拉的
# 过滤写法，而在**链路缺了半段** —— 市场装的是 `logs/<id>/<id>.pth` 推理权重，
# 没有 reference.wav，而 `voice_ref()` 对这类音色直接抛错，整条 send_text 会 400。
# 但 send_text 末尾必过 RVC、音色由 RVC 决定，所以语气借谁的都行。
#
# 下面两条是**不能借**的护栏，比“能不能借”更重要：
#   · 没有 RVC 兜底时不借（否则会拿别人的声音冒充用户选的音色）；
#   · 借不到时不编，原样传下去让 synth_wav 抛它本该抛的错。


def test_tts_ref_for_keeps_voice_with_reference(tmp_path, monkeypatch):
    """有参考音的音色不动 —— 借用只在"缺参考音"时才发生。"""
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "other")
    assert wv._tts_ref_for("kangaroo", "kangaroo_v2") == ("kangaroo", "")


def test_tts_ref_for_borrows_selected_voice(tmp_path, monkeypatch):
    """市场音色（无参考音）→ 借主界面当前选中的那个音色做语气。"""
    _add_ref_voice(tmp_path, "kangaroo", "aaa_first")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    assert wv._tts_ref_for("lazy_sheep", "lazy_sheep") == ("kangaroo", "kangaroo")


def test_tts_ref_for_falls_back_to_first_bank_voice(tmp_path, monkeypatch):
    """没有选中的音色 → 退到音色库里第一个有参考音的（取目录名升序，结果可复现）。"""
    _add_ref_voice(tmp_path, "aaa_first", "kangaroo")
    assert wv._tts_ref_for("lazy_sheep", "lazy_sheep") == ("aaa_first", "aaa_first")


def test_tts_ref_for_skips_self(tmp_path, monkeypatch):
    """选中项就是那个缺参考音的音色 → 绝不能"借它自己"（借不到等于没变）。"""
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "lazy_sheep")
    assert wv._tts_ref_for("lazy_sheep", "lazy_sheep") == ("kangaroo", "kangaroo")


def test_tts_ref_for_never_borrows_without_rvc(tmp_path, monkeypatch):
    """★ 没有 RVC 兜底时绝不借：借来的参考音会直接决定输出音色。

    那是在拿别人的声音冒充用户选的音色 —— 比"直接报错"更难发现、更误导。
    所以"声音不像"要比"发出去了但是别人的声音"好。
    """
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    assert wv._tts_ref_for("lazy_sheep", "") == ("lazy_sheep", "")


def test_tts_ref_for_returns_want_when_nothing_to_borrow(tmp_path, monkeypatch):
    """借不到就不编：原样返回，让 synth_wav 抛它本来就该抛的 400/404。"""
    monkeypatch.setattr(runtime, "VOICEBANK", tmp_path / "empty_bank")
    assert wv._tts_ref_for("lazy_sheep", "lazy_sheep") == ("lazy_sheep", "")


def test_send_text_full_chain(tmp_path, monkeypatch):
    """文字 → 合成 → RVC 换声 → 发送，steps 里要说清楚换了哪个音色。

    顺带锁死切卡预热契约：_do_send 拿到的 pre_apply 必须被消费。
    """
    calls = _fake_send(tmp_path, monkeypatch)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo"))
    assert res["ok"] is True
    assert calls["synth"][0] == "你好"
    assert calls["rvc"][1] == "kangaroo_v2"  # 自动推出 RVC 实验名
    assert calls["send"] == "tts_fake_kangaroo_v2.wav"  # 发的是**换声后**的文件
    assert calls.get("pre_apply_consumed") is True  # 切卡任务被 _do_send 消费
    assert any("RVC 换声" in s for s in res["steps"])


def test_send_text_honors_explicit_rvc_voice(tmp_path, monkeypatch):
    calls = _fake_send(tmp_path, monkeypatch)
    wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo", rvc_voice="katoong_manbo"))
    assert calls["rvc"][1] == "katoong_manbo"


def test_send_text_borrows_reference_for_market_voice(tmp_path, monkeypatch):
    """市场音色端到端：语气借自 kangaroo，换声换成的还是用户选的那个。

    这就是用户在面板里选「懒羊羊（市场）」时走的那条路。
    """
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    calls = _fake_send(tmp_path, monkeypatch)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="lazy_sheep"))
    assert res["ok"] is True
    assert calls["synth"][1] == "kangaroo"      # 语气借来的
    assert calls["rvc"][1] == "lazy_sheep_v2"   # ★ 音色还是用户选的那个
    assert any("语气借自" in s for s in res["steps"]), "借了参考音就得在 steps 里说清楚"


def test_send_text_empty_voice_uses_selected_for_both(tmp_path, monkeypatch):
    """`voice_id` 留空 = 面板选的「主界面选中」：TTS 与 RVC 必须用**同一个**音色。

    ⚠️ 护栏：把 want 与「TTS 实际用的音色」拆开之后很容易写错成
    `resolve_rvc_voice("")` → 推不到 → 静默退回"不换声"（⾳色会明显不像），
    而且失败得很安静，不像报错那样有人发现。
    """
    calls = _fake_send(tmp_path, monkeypatch)
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    res = wv.send_text(wv.SendTextReq(text="你好"))
    assert calls["synth"][1] == "kangaroo"
    assert calls["rvc"][1] == "kangaroo_v2"
    assert not any("语气借自" in s for s in res["steps"]), "这个音色自带参考音，不该走借用"


def test_send_text_warns_when_no_rvc(tmp_path, monkeypatch):
    """找不到 RVC 音色时照发，但必须显式警告——否则又是一条"不像袋鼠"的语音。"""
    calls = _fake_send(tmp_path, monkeypatch)
    monkeypatch.setattr(rvc_convert, "resolve_rvc_voice", lambda v: None)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="nosuch"))
    assert "rvc" not in calls  # 没换声
    assert any("未换声" in s for s in res["steps"])
    assert calls["send"] == "tts_fake.wav"  # 发的是原始 TTS 产物


def test_send_text_rejects_empty_text():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        wv.send_text(wv.SendTextReq(text="   "))
    assert e.value.status_code == 400


def test_send_text_route_registered():
    """桌宠调的是这个路由，别哪天改名了没人发现。

    include_router 是惰性挂载，枚举 app.routes 看不到子路由（见 test_server.py），
    故用真实请求验证：空 text 应返回 400 而不是 404。
    """
    import server
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.post("/api/wechat/send_text", json={"text": "  "})
    assert resp.status_code == 400  # 路由在，被参数校验拦下（404 才是没注册）


# ---------------- 切卡与 TTS 并行（_PendingApply，2026-09-10 延迟优化） ----------------


class _FakeProc:
    """假播放子进程（同 test_wechat_retry）：绝不能让单测真的启动 RVC venv 播音频。"""

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _make_wav_like_retry(tmp_path, name="tts_x.wav"):
    """真 wav 字节（_wav_duration 被打桩时不读内容，但 _do_send 仍要求文件存在）。"""
    import io

    import numpy as np
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600, dtype=np.float32), 16000, format="WAV")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


def test_pending_apply_result_returns_run_audio_result(monkeypatch):
    """result() 返回后台 _run_audio('apply') 的结果，且只调一次。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True, "action": a})
    t = wv._PendingApply()
    res = t.result()
    assert res == {"ok": True, "action": "apply"}
    assert calls == ["apply"]


def test_pending_apply_propagates_error(monkeypatch):
    """后台 apply 失败 → result() 原样抛出，交给 _do_send 的异常路径。"""
    monkeypatch.setattr(
        wv, "_run_audio", lambda a: (_ for _ in ()).throw(RuntimeError("apply boom"))
    )
    t = wv._PendingApply()
    with pytest.raises(RuntimeError, match="apply boom"):
        t.result()


def test_pending_apply_abandon_restores_after_success(monkeypatch):
    """abandon()（未消费早退）：必须还原声卡，绝不把默认麦留在 CABLE 上。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    t = wv._PendingApply()
    t.abandon()
    assert calls == ["apply", "restore"]


def test_pending_apply_abandon_restores_after_failure(monkeypatch):
    """apply 本身失败后 abandon：restore 可能同样失败，不能让异常冒出 abandon。"""
    calls = []

    def fake(a):
        calls.append(a)
        raise RuntimeError("no device")

    monkeypatch.setattr(wv, "_run_audio", fake)
    t = wv._PendingApply()
    t.abandon()  # 不应抛
    assert calls == ["apply", "restore", "reset"]  # restore 失败 → _safe_restore 用 reset 兑底


def test_pending_apply_abandon_noop_after_consume(monkeypatch):
    """result() 消费后 abandon 是空操作：还原已由 _do_send 自己的路径负责。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    t = wv._PendingApply()
    t.result()
    t.abandon()
    assert calls == ["apply"]


def test_do_send_uses_pre_apply_instead_of_second_sync_apply(tmp_path, monkeypatch):
    """传了 pre_apply：_do_send 只等尾差，绝不能再同步跑第二次 apply（否则白省）。"""
    _make_wav_like_retry(tmp_path)
    calls = []

    def fake_run(a):
        calls.append(a)
        return {"ok": True}

    monkeypatch.setattr(wv, "_run_audio", fake_run)

    class FakeTask:
        def __init__(self):
            self.consumed = False

        def result(self):
            self.consumed = True
            return {"ok": True}

        def abandon(self):
            pass

    task = FakeTask()
    monkeypatch.setattr(wv, "_PendingApply", FakeTask)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"), pre_apply=task)
    assert res["outcome"] == "ok"
    assert task.consumed is True
    assert calls == []  # 同步 apply 一次都没跑
    assert any("并行" in s for s in res["steps"])


def test_do_send_without_pre_apply_keeps_sync_apply(tmp_path, monkeypatch):
    """不传 pre_apply（/send_voice、tools 直连）：保持原地同步切卡，行为不变。"""
    _make_wav_like_retry(tmp_path)
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"
    assert calls == ["apply"]  # 原地同步 apply，和优化前一致


def test_send_text_starts_pending_apply_and_frees_it_on_tts_failure(tmp_path, monkeypatch):
    """send_text 一拿锁就起预热任务；TTS 失败走 abandon 兜底还原声卡。"""
    calls = []

    def fake_run(a):
        calls.append(a)
        return {"ok": True}

    monkeypatch.setattr(wv, "_run_audio", fake_run)
    monkeypatch.setattr(
        tts_api, "synth_wav", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts boom"))
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        wv.send_text(wv.SendTextReq(text="你好"))
    assert e.value.status_code == 500
    assert "tts boom" in e.value.detail
    assert calls == ["apply", "restore"]  # 预热被 abandon()：切了卡又还原，无残留


# -------- 渲染侧只传 voice_id，不自己复制「voicebank → RVC 实验名」的约定 --------
#
# 由来（2026-09-21 复核）：`web/electron/pet-actions.cjs` 的 send_text 载荷里
# `rvc_voice: ""` 被记成「桌宠面板单音色写死」。实际是**刻意的委托** ——
# 后端在 `rvc_voice` 为空时按 `voice_id` 推（见上面 resolve_rvc_voice 那组用例）。
# 复核结论：
#   · 桌宠面板**有**音色下拉（`pet.html` 的 `#voiceSel`，从 `/api/voices` 灌、
#     留 `has_reference` **或** `pth_exists` 的、选中项存 localStorage），不是单音色；
#   · `rvc_voice: ""` → 后端推 `kangaroo → kangaroo_v2`，实测可用。
#
# 那为什么还要钉？因为「留空」看起来太像漏填了，已经被误报两次。这条用例的价值
# 不是防功能回归（功能没坏），而是**防有人把它"补成"一个写死的音色名** ——
# 那会让所有用户被锁到同一个音色上，而且改的人会以为自己在修 bug。
#
# 同理，下面也钉住「面板必须保留音色选择入口」：真要是有人把下拉删了、
# 退回单音色，那才是这个条目描述的那个 bug。

# -------- /preview_text：不发送的 TTS+RVC 试听（市场装的音色也能听到真声音） --------
#
# 由来（2026-09-22）：面板以前把「试听」**禁用**给"没有参考音的音色"，因为试听走
# `/api/tts`，而那条链要求音色自带参考音 → 市场装的音色点了只会 404。
# 新端点补上 RVC 那一步（语气借参考音、音色由 RVC 决定），并在合成不出可用声音时
# 退回 market_preview 的固定样板句。
#
# 这一组里最值钱的不是"能试听"，而是 **"试听绝不碰微信与声卡"** ——
# 一旦它顺手调了 _do_send / _prepare_recording_env，用户点一下「试听」就会莫名其妙
# 发出去一条语音。这条用"所有微信侧入口都被打桩 + 断言一个都没被调"来守。


@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    """试听链路的打桩：TTS / RVC / 质量关全过，并记录微信侧有没有被碰到。"""
    touched: dict = {"wechat": []}

    def _synth(text, voice_id="", *a, **kw):
        touched["synth"] = (text, voice_id)
        f = tmp_path / "tts_preview.wav"
        f.write_bytes(b"RIFF")
        return f, 3.0, voice_id

    def _rvc(wav, voice, pitch=0, index_rate=0.5):
        touched["rvc"] = (Path(wav).name, voice, pitch, index_rate)
        out = tmp_path / f"{Path(wav).stem}_{voice}.wav"
        out.write_bytes(b"RIFF")
        return out

    import market_preview

    monkeypatch.setattr(tts_api, "synth_wav", _synth)
    monkeypatch.setattr(rvc_convert, "rvc_convert", _rvc)
    monkeypatch.setattr(rvc_convert, "resolve_model", lambda v: (Path("x.pth"), None))
    # ⚠️ 必须打桩：真实 _quality_ok 会去解码 "RIFF" 这类假文件，解不开就判不合格
    # → 每条用例都意外走进样板句兜底，测出来的东西完全不是想测的。
    monkeypatch.setattr(market_preview, "_quality_ok", lambda p: (True, ""))
    # 微信侧：任何一处被碰到都记下来，下面用"必须为空"守"不发送"
    monkeypatch.setattr(wv, "_do_send", lambda *a, **k: touched["wechat"].append("_do_send"))
    monkeypatch.setattr(
        wv, "_prepare_recording_env", lambda: touched["wechat"].append("_prepare_recording_env")
    )
    monkeypatch.setattr(
        wv, "_PendingRecordingEnv", lambda: touched["wechat"].append("_PendingRecordingEnv")
    )
    monkeypatch.setattr(wv, "_run_audio", lambda a: touched["wechat"].append(f"_run_audio:{a}"))
    return touched


def test_preview_text_tts_then_rvc(tmp_path, monkeypatch, preview_env):
    """市场音色（无参考音）试听：语气借自 kangaroo，换声换成用户选的那个。"""
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    r = wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="lazy_sheep"))
    assert r["ok"] is True
    assert r["source"] == "text"
    assert preview_env["synth"] == ("你好", "kangaroo")  # 语气借来的
    assert preview_env["rvc"][1] == "lazy_sheep_v2"  # ★ 音色还是用户选的那个
    assert r["borrowed"] == "kangaroo"
    assert r["url"].startswith("/api/media/outputs/")


def test_preview_text_never_touches_wechat(tmp_path, monkeypatch, preview_env):
    """★ 试听绝不能碰微信与声卡：不发送、不切卡、不占发送锁。

    这是用户对这个端点的唯一要求（"不发送的试听"）—— 一旦失效，点「试听」会直接
    发出去一条语音，而且失败得很安静。
    """
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="lazy_sheep"))
    assert preview_env["wechat"] == [], f"试听碰到了微信/声卡：{preview_env['wechat']}"
    assert not wv._send_lock.locked(), "试听不该占发送锁（会把用户真正的发送堵住）"


def test_preview_text_keeps_tts_when_rvc_fails(tmp_path, monkeypatch, preview_env):
    """音色**自带**参考音而换声炸了 → 保留 TTS 结果（＝今天的 /api/tts 行为），不报错。

    这里保留是对的：参考音本身就是这个音色，所以 TTS 结果的音色没跑。
    """
    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(
        rvc_convert, "rvc_convert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rvc boom"))
    )
    r = wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="kangaroo"))
    assert r["ok"] is True and r["source"] == "text"
    assert r["borrowed"] == ""
    assert any("RVC 换声失败" in s for s in r["steps"])


def test_preview_text_never_hands_over_the_borrowed_voice(tmp_path, monkeypatch, preview_env):
    """★ 借了参考音又没换成声 → 必须退样板句，绝不能把借来的嗓音当成「这个音色」。

    这是本条链路上唯一会"拿别人的声音冒充用户选的音色"的口子，而且很隐蔽：音频能播、
    时长正常、steps 里也只是个 ⚠。用户对这个端点的要求就是"听到它真正的声音，而不是
    听到借来的嗓音"—— 一旦这里改回"保留 TTS 结果"，他听到的恰好是袋鼠的嗓音。
    """
    import market_preview

    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    monkeypatch.setattr(
        rvc_convert, "rvc_convert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rvc boom"))
    )
    monkeypatch.setattr(
        market_preview,
        "generate",
        lambda vid, download=None: {"status": "ready", "url": "/api/media/outputs/market/x.wav"},
    )
    r = wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="lazy_sheep"))
    assert r["source"] == "sample", "借来的嗓音被当成这个音色交出去了"
    assert any("不是「lazy_sheep」的声音" in s for s in r["steps"])
    # 样板句不是用户打的字 → 面板靠这个字段拒绝把它当待发产物
    assert "样板句" in r["note"]


def test_preview_text_falls_back_to_sample_when_silent(tmp_path, monkeypatch, preview_env):
    """TTS 吐静音（市场/卡通音色最容易出的一种）→ 退回样板句，绝不播一段空白。"""
    import market_preview

    _add_ref_voice(tmp_path, "kangaroo")
    monkeypatch.setattr(common, "selected_voice", lambda: "kangaroo")
    monkeypatch.setattr(market_preview, "_quality_ok", lambda p: (False, "静音（RMS 过低）"))
    monkeypatch.setattr(
        market_preview,
        "generate",
        lambda vid, download=None: {
            "status": "ready",
            "url": "/api/media/outputs/market/x_preview.wav",
        },
    )
    r = wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="lazy_sheep"))
    assert r["ok"] is True and r["source"] == "sample"
    assert "样板句" in r["note"]
    assert any("改用样板句试听" in s for s in r["steps"])


def test_preview_text_falls_back_to_sample_when_tts_raises(monkeypatch, preview_env):
    """TTS 直接抛错 → 同样退样板句，而且因为是异步生成要如实报 generating。"""
    import market_preview

    monkeypatch.setattr(
        tts_api, "synth_wav", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts boom"))
    )
    monkeypatch.setattr(market_preview, "generate", lambda vid, download=None: {"status": "generating"})
    r = wv.preview_text(wv.PreviewTextReq(text="你好", voice_id="lazy_sheep"))
    assert r["ok"] is False and r["source"] == "sample"
    assert r["status"] == "generating"
    assert "再点一次" in r["error"]


def test_preview_text_rejects_empty_text():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        wv.preview_text(wv.PreviewTextReq(text="   "))
    assert e.value.status_code == 400


def test_preview_text_route_registered():
    """面板调的是这个路由，改名了得有人发现（同 send_text 的写法）。"""
    import server
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.post("/api/wechat/preview_text", json={"text": "  "})
    assert resp.status_code == 400  # 路由在，被参数校验拦下（404 才是没注册）


_ELECTRON = _ROOT.parent / "web" / "electron"


def test_pet_actions_delegates_rvc_voice_to_backend():
    """★ `rvc_voice` 必须留空（交给后端推），不能被写成具体音色名。"""
    src = (_ELECTRON / "pet-actions.cjs").read_text(encoding="utf-8")

    assert 'rvc_voice: ""' in src, (
        "pet-actions.cjs 不再把 rvc_voice 留空 —— 若你把它改成了具体音色名，"
        "请先读 tool 端注释：那会让所有用户锁到同一音色（后端本来会按 voice_id 推）。"
    )
    # 反例：不许出现 `rvc_voice: "xxx"` 这种写死
    import re

    hardcoded = re.findall(r'rvc_voice:\s*"([^"]+)"', src)
    assert hardcoded == [], f"rvc_voice 被写死成了 {hardcoded} —— 应留空交给后端按 voice_id 推"

    # 载荷必须把面板选的音色带上，否则后端无从推
    assert "voice_id: voiceId" in src, "send_text 载荷没带上面板选的 voice_id"


def test_pet_panel_keeps_a_voice_selector():
    """★ 桌宠面板必须保留音色选择入口（真正的「单音色写死」是这样的）。"""
    html = (_ELECTRON / "pet" / "pet.html").read_text(encoding="utf-8")

    assert 'id="voiceSel"' in html, "桌宠面板的音色下拉没了 —— 这才是真的单音色写死"
    assert "/api/voices" in html, "音色下拉没有从 /api/voices 灌数据"
    assert "localStorage" in html, "选中的音色应持久化，否则每次重开都要重选"


def test_pet_panel_lists_market_voices_too():
    """★ 下拉必须连「只有 RVC 权重、没有参考音」的市场音色一起列出来。

    由来（2026-09-22 用户实测，原话「只能选择两个音色」）：这里以前过滤成
    `v.has_reference`，理由是“没参考的 RVC 模型合不了 TTS”。局部结论没错，
    但把整条路堵死了 —— 用户在音色市场装的音色一个都选不到，下拉里永远只剩自训的
    那两个。后端 `_tts_ref_for` 补上了缺的那半段后，筛选条件应该改成
    “这个音色能不能用”（有参考音 **或** 有可推理权重）。

    钉住两条：① 用 OR 而不是只留 has_reference；② 选项文案带上来源，
    否则用户分不清哪个是市场的、哪个是自己训的。
    """
    html = (_ELECTRON / "pet" / "pet.html").read_text(encoding="utf-8")

    assert "v.has_reference || v.pth_exists" in html, (
        "音色下拉又只列 has_reference 的音色了 —— 市场装的（只有 .pth）会整批消失"
    )
    # 选项文案：`${v.display_name}（${tag}）`，tag 来自 source 字段（与主界面 voiceLabel 同一约定）
    assert 'v.source === "market" ? "市场" : "自训"' in html, (
        "下拉选项没标来源 —— 用户分不清哪个是市场装的、哪个是自己训的"
    )
    assert "（${tag}）" in html, "来源标记没有落到选项文案上"


def test_pet_preview_uses_the_non_sending_endpoint():
    """★ 面板「试听」必须走不发送的 preview_text，而不是 /api/tts。

    `/api/tts` 里没有 RVC，市场装的音色（只有 .pth）会 404 —— 这正是当初把「试听」
    禁用掉的原因。同时必须回传 `sendable`：后端退回样板句时，面板不能把主按钮切成
    「发送试听」，否则发出去的是固定样板句，不是用户打的字。
    """
    src = (_ELECTRON / "pet-actions.cjs").read_text(encoding="utf-8")
    body = src[src.index("function previewWechatTextFromPet") :]
    body = body[: body.index("\n}\n") + 3]

    assert '"/api/wechat/preview_text"' in body, "试听没走不发送的 preview_text 端点"
    assert '"/api/tts"' not in body, "试听又走回 /api/tts 了 —— 市场音色会 404"
    assert "sendable" in body, "没回传 sendable —— 样板句会被当成本次待发产物"


def test_pet_panel_does_not_send_the_sample_sentence():
    """★ 退回样板句时**不能**把它设成待发产物。

    面板的「试听 → 发送」是两步：试听产物会被当作要发的内容。样板句不是用户打的字，
    一旦进了 lastPreview，主按钮变成「发送试听」，发出去的就不是人话了。
    """
    html = (_ELECTRON / "pet" / "pet.html").read_text(encoding="utf-8")
    assert "r.sendable === false" in html, "面板没区分样板句与用户的文字"
    # 样板句分支必须把 lastPreview 清掉（而不是设进去）
    gate = html[html.index("r.sendable === false") :]
    gate = gate[: gate.index("previewAudio")]
    assert "lastPreview = null" in gate, "样板句被设进了 lastPreview —— 主按钮会变成「发送试听」"


def test_pet_panel_passes_selected_voice():
    """面板点「发送」时要把下拉里选的音色传下去（不能传空常量）。"""
    html = (_ELECTRON / "pet" / "pet.html").read_text(encoding="utf-8")
    assert "window.pet.sendText(text, voiceId)" in html, (
        "面板发送时没用上选中的音色 —— 传空/常量就等于单音色"
    )
    assert "const voiceId = voiceSel.value" in html, "voiceId 应取自下拉当前值"
