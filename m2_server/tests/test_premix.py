"""预混模式单测：`sfx_lib.mix_into`（混音语义）+ `/api/soundboard/premix`（端点契约）。

为什么这批用例要单独存在
------------------------
预混是**发送前置**动作：它产出的 wav 会被当成微信语音发出去。所以这里的错不是
"界面上某个数字不对"，而是"发出去的语音里少了用户要的那声响，而界面显示一切正常"。
因此重点不在覆盖率，而在钉住三件事：

1. **三种混淆位置的语义**（叠加 / 开头 / 结尾）—— 它们各自意味着完全不同的听感；
2. **不劫持"最近一条 TTS 产物"**：输出名用 `sfxmix_` 前缀，发送链路的默认口径
   （`glob("tts_*.wav")`）不能被混过的文件悄悄顶替（顶替了 = 用户点"发送"发的是旧内容）；
3. **坏输入当场失败**（不是静默发一条没混的语音）—— 与效果链"尽力而为"的约定相反，
   理由写在 `soundboard.soundboard_premix` 的注释里。

纯 DSP 部分（`mix_into`）不碰 HTTP：它同时服务效果链里的「插入音效」，两处共用一份
实现，所以语义要在这一层钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import config as cfg  # noqa: E402
import sfx_lib  # noqa: E402
import soundboard  # noqa: E402

SR = 48000


# --------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """素材目录与计数文件按用例隔离（绝不写进用户的真 media/outputs）。"""
    monkeypatch.setattr(sfx_lib, "IMPORT_DIR", tmp_path / "imported")
    monkeypatch.setattr(soundboard, "STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(soundboard, "_proc", None)
    yield


@pytest.fixture()
def blip(tmp_path, monkeypatch) -> np.ndarray:
    """一条**自己造的**素材：44.1kHz、0.1s 线性斜坡。

    用自造素材而不是出厂爆炸声，是为了让每条断言都有精确的期望值（出厂素材是
    合成出来的复杂瞬态，只能做"响了没有"这类弱断言）。44100 还顺带覆盖重采样：
    素材与目标音频采样率不同是常态（CABLE 默认 44100，`gen_sfx` 产出 48000）。
    """
    d = tmp_path / "imported"
    d.mkdir(parents=True, exist_ok=True)
    pcm = np.linspace(0.0, 0.8, int(44100 * 0.1), dtype=np.float32)
    sf.write(str(d / "blip.wav"), pcm, 44100, subtype="PCM_16")
    return pcm


def _silence(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(sr * seconds), dtype=np.float32)


def _tone(seconds: float, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


# --------------------------------------------------------------- mix_into：位置语义


def test_prepend_puts_the_sfx_before_the_voice(blip):
    """拼在开头：长度 = 音效 + 人声，且前半段**就是**那条音效。"""
    voice = _silence(0.2)
    out, notes = sfx_lib.mix_into(voice, SR, [{"sample": "blip", "mode": "prepend", "gain": 1.0}])
    assert notes == []
    n = int(round(len(blip) * SR / 44100))  # 重采样后的长度
    assert out.size == n + voice.size
    # 斜坡 + 线性插值：容差给 2e-3（PCM16 量化 + 插值误差）
    assert np.allclose(out[:n], np.interp(
        np.arange(n) / SR, np.arange(len(blip)) / 44100, blip
    ), atol=2e-3)
    assert np.allclose(out[n:], 0.0)


def test_append_puts_the_sfx_after_the_voice(blip):
    voice = _silence(0.2)
    out, _ = sfx_lib.mix_into(voice, SR, [{"sample": "blip", "mode": "append"}])
    n = int(round(len(blip) * SR / 44100))
    assert out.size == voice.size + n
    assert np.allclose(out[: voice.size], 0.0)
    assert np.abs(out[voice.size :]).max() > 0.1


def test_layer_keeps_length_and_only_adds_energy_at_the_offset(blip):
    """叠加：长度不变，只在 `at_s` 处多出音效 —— 这正是实时声板的等价物。"""
    voice = _silence(0.5)
    out, notes = sfx_lib.mix_into(
        voice, SR, [{"sample": "blip", "mode": "layer", "at_s": 0.2, "gain": 1.0}]
    )
    assert notes == []
    assert out.size == voice.size, "叠加不该改变长度"
    n = int(round(len(blip) * SR / 44100))
    off = int(0.2 * SR)
    assert np.abs(out[off : off + n]).max() > 0.1
    assert np.allclose(out[:off], 0.0), "起点之前不该有声"
    assert np.allclose(out[off + n :], 0.0), "音效之后不该有声"


def test_layer_default_offset_is_zero(blip):
    """不给 `at_s` 就从 0 开始 —— 与实时声板"点一下就在当下响"的心智一致。"""
    out, _ = sfx_lib.mix_into(_silence(0.3), SR, [{"sample": "blip"}])
    assert np.abs(out[: int(0.05 * SR)]).max() > 0.1


def test_multiple_prepends_keep_the_user_order(blip):
    """勾了两条"放在开头"的音效：按勾选顺序排，不是字典序也不是随机。"""
    out, _ = sfx_lib.mix_into(
        _silence(0.1),
        SR,
        [
            {"sample": "blip", "mode": "prepend", "gain": 1.0},
            {"sample": "blip", "mode": "prepend", "gain": 0.25},
        ],
    )
    n = int(round(len(blip) * SR / 44100))
    # 第一条（响的）在前，第二条（轻的）在后
    assert np.abs(out[:n]).max() > np.abs(out[n : 2 * n]).max() + 0.1


# --------------------------------------------------------------- mix_into：宽容与上限


def test_bad_inputs_are_skipped_with_a_note_not_an_exception(blip):
    """坏素材/坏位置只记一条说明 —— 一条音效读不出来不该让整条语音发不出去。"""
    voice = _tone(0.2)
    out, notes = sfx_lib.mix_into(
        voice,
        SR,
        [
            {"sample": "nope"},  # 不存在
            {"sample": "blip", "mode": "sideways"},  # 未知位置
            {"sample": ""},  # 没指定
            "不是字典",  # 类型都不对
        ],
    )
    assert len(notes) == 3
    assert any("nope" in n for n in notes)
    assert np.allclose(out, voice), "一条都没混进去时应当原样返回"


def test_gain_and_offset_are_clamped_instead_of_breaking(blip):
    """越界/非数值参数 clamp 到合法区间 —— 前端滑杆、手改 JSON 都会经过这里。"""
    voice = _silence(0.5)
    out, notes = sfx_lib.mix_into(
        voice, SR, [{"sample": "blip", "gain": 99, "at_s": "not-a-number"}]
    )
    assert notes == []
    assert np.abs(out).max() <= 0.97 + 1e-6, "gain 99 被 clamp 到 1.5，再整体防削波"


def test_layer_offset_beyond_the_end_lands_at_the_end_without_a_silent_tail(blip):
    """`at_s` 超出音频长度：贴到末尾，而不是在中间留一段静音（静音会变成微信里的空白）。"""
    voice = _silence(0.2)
    out, _ = sfx_lib.mix_into(
        voice, SR, [{"sample": "blip", "mode": "layer", "at_s": 9.0, "gain": 1.0}]
    )
    n = int(round(len(blip) * SR / 44100))
    assert out.size == voice.size + n  # 只长出音效那一段，没有 9 秒静音
    assert np.abs(out[-n:]).max() > 0.1


def test_result_never_clips_when_layering_loud_sfx_on_a_loud_voice(blip):
    """叠加必然抬高峰值：必须防削波（削波在微信录完之后，用户听不出原因）。"""
    out, _ = sfx_lib.mix_into(
        _tone(0.5, amp=0.9),
        SR,
        [{"sample": "blip", "gain": 1.5}, {"sample": "blip", "gain": 1.5}],
    )
    assert np.all(np.isfinite(out))
    assert np.abs(out).max() <= 0.97 + 1e-6


def test_mix_into_does_not_mutate_the_callers_array(blip):
    """叠加用的是就地 `+=`：忘了 copy 就会把调用方的缓冲改掉（效果链里是本链的输入）。"""
    voice = _tone(0.2)
    before = voice.copy()
    sfx_lib.mix_into(voice, SR, [{"sample": "blip", "mode": "layer"}])
    assert np.array_equal(voice, before)


def test_load_pcm_is_cached_and_invalidated_by_rewrite(blip, tmp_path):
    """读了就缓存（连点格子/反复试混不该反复解码）；素材被改写后缓存要失效。"""
    p = sfx_lib.IMPORT_DIR / "blip.wav"
    got1, sr1 = sfx_lib.load_pcm("blip")
    got2, _ = sfx_lib.load_pcm("blip")
    assert got1 is got2, "同一素材第二次读取应当命中缓存（同一对象）"
    assert sr1 == 44100
    # 改写（模拟用户导入同名新素材）→ mtime/size 变了 → 必须重新读
    sf.write(str(p), np.zeros(1000, dtype=np.float32), 16000, subtype="PCM_16")
    got3, sr3 = sfx_lib.load_pcm("blip")
    assert sr3 == 16000 and got3.size == 1000


# --------------------------------------------------------------- /premix 端点


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(soundboard.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def tts_wav() -> str:
    """造一条"合成产物"落进 outputs/（文件名模拟真链路：`tts_*.wav`）。"""
    name = "tts_premix_unit.wav"
    sf.write(str(cfg.OUTPUTS_DIR / name), _tone(0.5), SR, subtype="PCM_16")
    return name


def _premix(client, **kw):
    body = {"inserts": [{"sample": "blip", "mode": "prepend", "gain": 1.0}]}
    body.update(kw)
    return client.post("/api/soundboard/premix", json=body)


def test_premix_writes_a_new_wav_next_to_the_source(client, blip, tts_wav):
    r = _premix(client, wav=tts_wav)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["wav"] == "sfxmix_tts_premix_unit.wav"
    out = cfg.OUTPUTS_DIR / j["wav"]
    assert out.is_file()
    # 拼在开头 → 比原文件长（原 0.5s + 0.1s 音效）
    assert j["seconds"] > 0.55
    assert j["inserts"] == 1 and j["skipped"] == []
    # 原文件**不能被改**（发送链路的"最近一条合成产物"还指着它）
    assert abs(sfx_lib.duration_s(cfg.OUTPUTS_DIR / tts_wav) - 0.5) < 0.02


def test_premixed_file_does_not_hijack_the_latest_tts_default(client, blip, tts_wav):
    """★ 核心不变量：混过的文件不算"最近一条 TTS 产物"。

    发送链路的默认口径是 `glob("tts_*.wav")` 取最新。如果预混输出叫 `tts_*.wav`，
    它就会成为默认目标 —— 于是"再合成一条、点发送"会发出**上一次混过的旧内容**，
    而且完全没有报错。这条用例就是钉住这个。
    """
    _premix(client, wav=tts_wav)
    mixed = sorted(p.name for p in cfg.OUTPUTS_DIR.glob("tts_*.wav"))
    assert mixed == [tts_wav], f"混音产物混进了 tts_* 命名空间：{mixed}"

    # 不给 wav 时仍然落在原 tts 上（第二次预混的结果应当与第一次同源同长）
    r2 = _premix(client)
    assert r2.status_code == 200
    assert r2.json()["wav"] == "sfxmix_tts_premix_unit.wav"


def test_premix_is_idempotent_about_file_names(client, blip, tts_wav):
    """反复试混不堆垃圾：同名覆盖（前端每改一次勾选就要重混一次）。"""
    for _ in range(3):
        assert _premix(client, wav=tts_wav).status_code == 200
    assert len(list(cfg.OUTPUTS_DIR.glob("sfxmix_*.wav"))) == 1
    # 不留半截临时文件（原子替换用 .tmp 中转）
    assert not list(cfg.OUTPUTS_DIR.glob("*.tmp"))


def test_premix_counts_as_usage_in_the_catalog(client, blip, tts_wav):
    _premix(client, wav=tts_wav)
    counts = {i["id"]: i["count"] for i in client.get("/api/soundboard/catalog").json()["items"]}
    assert counts["blip"] == 1


def test_premix_rejects_unknown_sample_instead_of_skipping_it(client, tts_wav):
    """★ 预混与效果链的失败语义**相反**：这里是明确的用户动作，差一条就该当场报错。

    静默跳过的话，用户听到的是一条"没混进去"的语音，而界面上没有任何异常提示 ——
    那正是最难被发现的一类失败。
    """
    r = _premix(
        client,
        wav=tts_wav,
        inserts=[{"sample": "blip"}, {"sample": "ghost"}],
    )
    assert r.status_code == 404
    assert "没有这个音效" in r.json()["detail"]
    assert not list(cfg.OUTPUTS_DIR.glob("sfxmix_*.wav")), "报错了就不该留下产物"


def test_premix_rejects_bad_mode_empty_and_too_many(client, blip, tts_wav):
    assert _premix(client, wav=tts_wav, inserts=[{"sample": "blip", "mode": "diagonal"}]).status_code == 400
    assert _premix(client, wav=tts_wav, inserts=[]).status_code == 400
    many = [{"sample": "blip"} for _ in range(sfx_lib.MAX_INSERTS + 1)]
    assert _premix(client, wav=tts_wav, inserts=many).status_code == 400


@pytest.mark.parametrize("bad", ["../boom", "..\\boom", "sub/x.wav", "D:/x.wav", ".", "..", ""])
def test_premix_rejects_paths_outside_outputs(client, bad):
    """请求体里的"音频名"只允许是 outputs/ 里的裸文件名。

    与 `wechat_voice._resolve_wav` 的差异是**刻意**的：那边放行绝对路径（它要处理
    发送链路内部生成的临时文件），这里只服务一件事，所以直接拒 —— 外部请求体
    不该能指向 outputs 之外。
    """
    r = _premix(client, wav=bad)
    assert r.status_code in (400, 404)


def test_premix_reports_missing_file_with_404(client):
    assert _premix(client, wav="tts_does_not_exist.wav").status_code == 404


def test_premix_without_any_tts_product_says_what_to_do(client, monkeypatch, tmp_path):
    """一条合成产物都没有时：给可执行的指引，不是 500。"""
    empty = tmp_path / "empty_outputs"
    empty.mkdir()
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", empty)
    assert _premix(client).status_code == 404


def test_premix_is_reachable_on_the_real_app(blip, tts_wav):
    """★ 用 TestClient 打**真 server.app**：挂载是清单驱动的，漏了就是线上 404。

    只测自己 `include_router` 的小 app 不够 —— `soundboard` 挂上了不代表
    **新端点**在一个真启动的进程里可用（路由是在 `mount_plan()` 里装的）。
    与 `test_soundboard.py::test_endpoint_is_reachable_on_the_real_app` 同一手法。
    """
    import server
    from fastapi.testclient import TestClient

    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post(
        "/api/soundboard/premix",
        json={"wav": tts_wav, "inserts": [{"sample": "blip", "mode": "append"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["wav"].startswith("sfxmix_")


def test_premix_does_not_need_the_effects_plugin(client, blip, tts_wav, monkeypatch):
    """★ 归属不变量：预混属 `sound.fx-board`，**不能**依赖 `sound.effects`。

    关掉 `sound.effects` 时 `effects` 的 router 会卸载，但用户面板上的"预混"按钮
    还在（它属声板）。如果预混偷偷走了效果链的端点，那个按钮就会 404 —— 正是
    `docs/犯错指南.md` 速查表 74 那一类"宿主开着、被托管插件关着"的缺口。
    """
    import ast

    tree = ast.parse(Path(soundboard.__file__).read_text(encoding="utf-8"))
    imported = {
        (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
    } | {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "effects" not in imported, "声板不许 import 效果器模块（否则关掉它预混就废了）"
    # 端点确实能用（不依赖 effects 的 router 被挂载）
    assert _premix(client, wav=tts_wav).status_code == 200
