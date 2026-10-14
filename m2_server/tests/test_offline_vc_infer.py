"""offline_vc_infer.postprocess_audio 回归测试。

历史坑：旧实现结尾 np.clip(y, -0.99, 0.99) 硬削波，RVC 瞬态 + IIR 低通过冲
后 ~0.17% 样本被削平，听感刺耳（NatScore 掉到负值区）。重构为「只衰减」的
峰值归一（-1.5 dBFS 目标）后，用例钉死：不削波、响度锚点保持、静音安全。
"""

import os
import random
import sys

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")

import offline_vc_infer  # noqa: E402

TARGET = 10 ** (-1.5 / 20)


def _speech_like(
    n: int = 48000,
    sr: int = 48000,
    sine_amp: float = 0.3,
    n_impulse: int = 20,
    impulse_amp: float = 6.0,
) -> np.ndarray:
    """正弦 + 稀疏大幅瞬态：高波峰因子，模拟 RVC 输出的过冲尖峰。
    瞬态只放后半段，保证前半段是纯正弦（供波形形状断言用）。"""
    t = np.arange(n) / sr
    y = sine_amp * np.sin(2 * np.pi * 220 * t)
    rng = np.random.default_rng(7)
    idx = rng.integers(n // 2, n - 100, size=n_impulse)
    y[idx] += impulse_amp
    return y.astype(np.float32)


def test_loud_input_no_hard_clip_peak_at_target():
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    assert out.dtype == np.float32
    peak = float(np.abs(out).max())
    assert peak == pytest.approx(TARGET, rel=1e-3)  # 峰值衰减到 -1.5 dBFS
    assert peak < 0.99  # 不再顶硬限幅沿
    # 无平顶削波：贴着目标的样本占比必须远低于旧 clip 实现
    assert (np.abs(out) >= TARGET * 0.999).mean() < 0.005


def test_no_flat_top_clipping():
    """等比缩放不产生平顶：贴峰的长连续段是硬削波特征，等比缩放没有。"""
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    peak = float(np.abs(out).max())
    at_peak = np.abs(np.abs(out) - peak) < 1e-4 * peak
    idx = np.where(at_peak)[0]
    assert len(idx) > 0
    segments = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
    max_run = max(len(s) for s in segments)
    assert max_run <= 3  # 硬削波会形成远超 3 个样本的连续平顶


def test_normal_signal_rms_anchor():
    """峰值安全的正常信号：RMS 应锚定在 -18 dBFS，不被再放大。"""
    t = np.arange(48000) / 48000
    y = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = offline_vc_infer.postprocess_audio(y, 48000)
    rms = float(np.sqrt((out**2).mean()))
    assert rms == pytest.approx(10 ** (-18 / 20), rel=0.05)
    assert float(np.abs(out).max()) < TARGET  # 未触发峰值衰减


def test_low_sr_filter_guard():
    """22.05k 模型输出：低通截止自动让出 Nyquist（min(11k, 0.45·sr)），不报错。"""
    y = _speech_like(n=22050, sr=22050)
    out = offline_vc_infer.postprocess_audio(y, 22050)
    assert np.isfinite(out).all()
    assert float(np.abs(out).max()) <= TARGET + 1e-6


def test_silence_and_dc_safe():
    out = offline_vc_infer.postprocess_audio(np.zeros(4800, dtype=np.float32), 48000)
    assert out.shape == (4800,) and np.isfinite(out).all() and float(np.abs(out).max()) == 0.0
    out2 = offline_vc_infer.postprocess_audio(np.full(4800, 0.5, dtype=np.float32), 48000)
    assert np.isfinite(out2).all() and float(np.abs(out2).max()) <= TARGET + 1e-6


# ============== 后处理旋钮的默认值（2026-10-14 加） ==============
#
# 由来：为了让「同一个输入、只换一个旋钮」成为可测实验（`experiments/cover_knobs.py`），
# 把写死在那段 15 个位置参数调用里的 `0.33` / `0.25` / `11000.0` 提成了模块级常量 + 关键字参数。
# 纯搬运，但它带来一个新的失败方式：**默认值被顺手改掉** —— 那时三条链路（CLI /
# 常驻 worker / 级联实时）的输出都变了，而每一条都还是“能出声”，没有任何测试会变色。
#
# 所以这里钉三件事：
#   ① 三个常量就是字面量 0.33 / 0.25 / 11000.0（改它必须来改这里）；
#   ② `convert_audio` 的**签名默认值** = 那三个常量（防“提了参数但默认值写歪”）；
#   ③ 旋钮真的走进了 `pipeline()` 的**对应位置** —— 那个调用有 15 个位置参数，
#      错一位就会把 rms_mix_rate 当成 version 传进去，而且不会报错。


def test_knob_constants_are_pinned():
    assert offline_vc_infer.PROTECT == 0.33
    assert offline_vc_infer.RMS_MIX_RATE == 0.25
    assert offline_vc_infer.POST_LOWPASS_HZ == 11000.0


def test_convert_audio_defaults_are_the_constants():
    import inspect

    sig = inspect.signature(offline_vc_infer.convert_audio)
    assert sig.parameters["protect"].default == offline_vc_infer.PROTECT
    assert sig.parameters["rms_mix_rate"].default == offline_vc_infer.RMS_MIX_RATE
    assert sig.parameters["lowpass_hz"].default == offline_vc_infer.POST_LOWPASS_HZ


def test_postprocess_default_lowpass_is_unchanged():
    """提参数前后，默认调用必须逐样本一致（不是"差不多"）。"""
    y = _speech_like()
    a = offline_vc_infer.postprocess_audio(y, 48000)
    b = offline_vc_infer.postprocess_audio(y, 48000, lowpass_hz=11000.0)
    assert np.array_equal(a, b)


def _two_tone(n: int = 48000, sr: int = 48000, hi_hz: float = 15000.0) -> np.ndarray:
    """1kHz + 15kHz 等幅双音：低通是否生效看两者的**幅度比**（对整体缩放不敏感）。"""
    t = np.arange(n) / sr
    y = 0.2 * np.sin(2 * np.pi * 1000 * t) + 0.2 * np.sin(2 * np.pi * hi_hz * t)
    return y.astype(np.float32)


def _tone_ratio(y: np.ndarray, sr: int, lo_hz: float = 1000.0, hi_hz: float = 15000.0) -> float:
    n = min(len(y), sr)
    seg = y[:n] * np.hanning(n)
    Y = np.abs(np.fft.rfft(seg))
    fr = np.fft.rfftfreq(n, 1 / sr)
    lo = Y[np.argmin(np.abs(fr - lo_hz))]
    hi = Y[np.argmin(np.abs(fr - hi_hz))]
    return float(hi / max(lo, 1e-12))


def test_lowpass_attenuates_11k_above_and_zero_means_off():
    """两件事一起查：默认低通**确实**压 11k 以上；而传 0 是**真的不低通**。

    两者缺一条，`cover_knobs` 里那个「去掉低通」的对照就成了假对照 ——
    若 `0` 被当成“用默认值”，两臂会完全相同（而脚本不会报错，只是听不出区别）。
    实测（本机）：默认 0.28、传 0 时 1.00 —— 所以断言取 0.5 / 0.8 的宽裕位置。
    """
    y = _two_tone()
    assert _tone_ratio(offline_vc_infer.postprocess_audio(y, 48000), 48000) < 0.5
    off = offline_vc_infer.postprocess_audio(y, 48000, lowpass_hz=0)
    assert _tone_ratio(off, 48000) > 0.8


class _RecordingPipeline:
    """把 `pipeline()` 的位置参数原样记下来 —— 位置漂移只能这么查。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def pipeline(self, *args, **_kwargs):
        self.calls.append(args)
        return np.zeros(4096, dtype=np.float32)


def _stub_engine(monkeypatch):
    """桩引擎：不 import torch，也不碰真模型。

    `apply_infer_seed` 必须替换 —— 它内部 `import torch`，而本文件在 FAST_TESTS 里
    （该文件顶部已说明：不 import torch，2.5s 会挤掉 pre-commit 预算）。
    """
    import types

    monkeypatch.setattr(offline_vc_infer, "apply_infer_seed", lambda seed=0: seed)
    pipe = _RecordingPipeline()
    vc = types.SimpleNamespace(pipeline=pipe, hubert_model=None, net_g=None)
    # index 给非空值：`convert_audio` 里写的是 `index_rate if engine.index else 0.0`
    # —— 空 index 会把 rate 归零（无检索可用），那样就查不出"rate 有没有传错位置"。
    engine = offline_vc_infer.RvcEngine(
        vc=vc, tgt_sr=48000, index="added_stub.index", if_f0=1, version="v2", pth="stub"
    )
    return engine, pipe


def test_knobs_reach_the_pipeline_in_the_right_positions(monkeypatch):
    engine, pipe = _stub_engine(monkeypatch)
    offline_vc_infer.convert_audio(
        engine, np.zeros(16000, dtype=np.float32), 0, 0.5, protect=0.5, rms_mix_rate=0.7
    )
    args = pipe.calls[-1]
    assert args[8] == 0.5  # index_rate
    assert args[12] == 0.7  # rms_mix_rate
    assert args[14] == 0.5  # protect


def test_defaults_reach_the_pipeline_unchanged(monkeypatch):
    engine, pipe = _stub_engine(monkeypatch)
    offline_vc_infer.convert_audio(engine, np.zeros(16000, dtype=np.float32), 0, 0.5)
    args = pipe.calls[-1]
    assert args[12] == 0.25 and args[14] == 0.33


# ============== 权重加载失败的判读（2026-09-21 补） ==============
#
# 由来：本机 `auto_rb` 音色报「PyTorch 2.6 weights_only」，于是被记成「PyTorch 版本问题」。
# 实测真因是**文件本身不是权重** —— 它正好是 test_market_search_install.py 的夹具
# `b"\x80\x02" + os.urandom(512*1024-2)`（512KiB 随机数据 + 两个假魔数字节）。
#
# 这个坑值钱的地方在于**torch 的报错主动指错方向**：它写着
# "Re-running torch.load with weights_only set to False will likely succeed" ——
# 而对这个文件并不会 succeed，照它做只会白改一遍代码、还把第三方 ckpt 的
# `__reduce__` 载荷风险重新引进来（音色市场的权重来自公网，正是威胁模型）。
#
# 所以下面两组用例钉两件事：
#   1. `diagnose_pth` 能**只读结构**说清「这文件为什么不可用」（不反序列化，对不可信文件安全）；
#   2. `load_checkpoint` 的报错必须**明确否掉**「改用 weights_only=False」这个错误修法，
#      并且必须带上结构判读 —— 否则下一个人还会照 torch 的文案去改。
#
# 本文件在 check.py 的 FAST_TESTS 里，所以这里**不 import torch**
# （实测 2.5s，会挤掉 pre-commit 预算）：改用桩 torch 只测错误路径与形状校验。
# 真实 torch + 真实权重的守护在 test_offline_vc_infer_pth_guard.py（全量跑）。


class _FakeTorchRaises:
    """桩 torch：`load` 抛指定的异常，用于测错误路径而不装真 torch。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def load(self, *_a, **_kw):
        raise self._exc


class _FakeTorchReturns:
    def __init__(self, value):
        self._value = value

    def load(self, *_a, **_kw):
        return self._value


def _seeded_junk(n: int, seed: int = 20260927) -> bytes:
    """生成的"垃圾载荷"用**固定种子**，不用 `os.urandom`。

    ★ 为什么必须固定种子：`diagnose_pth` 对 `\\x80` 开头的文件有两个分支 ——

        · "早期就非法"    —— pickletools 走前缀时抛错
        · "前缀合法（损坏可能在深处）" —— 走满 2000 个 op 都没抛

    而随机字节**两支都可能落**：2026-09-27 实测 `os.urandom(4096-2)` 有
    **0.5%**（400 次里 2 次）落到"前缀合法"那一支，但用例永远只断言另一支
    → 大约每跑 200 次全量就红一次，而且极难复现（全量那次还会被 safe-delete
    守卫在进程退出时掐断，连失败详情与汇总行都打不出来，只能 `-x` 重跑才定位到）。

    固定种子既保留"假魔数 + 随机垃圾"的形状（与真出事那个夹具同形），
    又让分支确定。两个分支各有一条用例显式覆盖 —— 见
    `test_diagnose_reports_legal_prefix_but_deep_corruption`。
    """
    return random.Random(seed).randbytes(n)


def test_diagnose_rejects_random_bytes_with_fake_magic(tmp_path):
    """★ 核心用例：`\x80\x02` + 随机数据（就是那个夹具的形状）必须被判为「不是权重文件」。

    只查前两字节的魔数校验会放它过去（`market_download._torch_header_ok` 就是那么做的，
    且那是**刻意的**廉价校验）—— 所以判读必须真的走一遍 pickle 结构。
    """
    p = tmp_path / "junk.pth"
    p.write_bytes(b"\x80\x02" + _seeded_junk(512 * 1024 - 2))
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "pickle 流在**早期就非法**" in msg, f"没识破假魔数文件：{msg}"
    assert "524,288 字节" in msg, f"应报出文件大小（便于判断是否下载不完整）：{msg}"


def test_diagnose_reports_legal_prefix_but_deep_corruption(tmp_path):
    """另一个分支：前缀能走满 2000 个 op 时，判读会保留"损坏可能在深处"的说法。

    这条是上面那条的**补集**。之前缺的就是它 —— 于是随机载荷偶尔掉进这一支时，
    上一条会红，而看报错的人会以为是产品代码坏了。
    载荷构造：`\\x80\\x02`（PROTO 2）后跟 2500 个 `N`（NONE），每个都是合法 op，
    必然走满 2000 个才返回，因此分支完全确定。
    """
    p = tmp_path / "deep.pth"
    p.write_bytes(b"\x80\x02" + b"N" * 2500)
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "前缀合法" in msg, msg
    assert "损坏可能在深处" in msg, msg


def test_diagnose_rejects_html(tmp_path):
    """下到错误页面（404 HTML）是最常见的「不是权重」形态。"""
    p = tmp_path / "page.pth"
    p.write_text("<!DOCTYPE html><html><body>404</body></html>", encoding="utf-8")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "既不是 zip(PK) 也不是 pickle" in msg, msg
    assert "错误页面" in msg, msg


def test_diagnose_reports_broken_zip(tmp_path):
    """PK 开头但 zip 已损坏 —— 典型的下半截文件。"""
    p = tmp_path / "half.pth"
    p.write_bytes(b"PK\x03\x04" + os.urandom(200))
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "zip 容器已损坏" in msg, msg


def test_diagnose_reports_zip_without_data_pkl(tmp_path):
    """zip 能开但不含 data.pkl → 不是 torch 存档。"""
    import zipfile

    p = tmp_path / "nope.pth"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "hello")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "没有 data.pkl" in msg, msg


def test_diagnose_accepts_genuine_zip_shape(tmp_path):
    """像样的 zip 存档（含 data.pkl）不该被报成「不是权重文件」。"""
    import zipfile

    p = tmp_path / "ok.pth"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("archive/data.pkl", b"\x80\x04N.")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "含 data.pkl" in msg, msg
    assert "不是权重文件" not in msg, f"不该否定一个形状正确的存档：{msg}"


def test_diagnose_handles_missing_file(tmp_path):
    """路径不存在要给人话，而不是抛 FileNotFoundError 糊到调用方脸上。"""
    msg = offline_vc_infer.diagnose_pth(str(tmp_path / "gone.pth"))
    assert "读不到" in msg, msg


def test_load_checkpoint_error_denies_the_weights_only_fix(tmp_path):
    """★ 报错必须**明确否掉** torch 文案建议的那个修法。

    这是本组用例的真正目的：错误信息如果不点破，下一个 agent 看到
    "Re-running with weights_only=False will likely succeed" 就会去关掉保护。

    载荷同样用固定种子（见 `_seeded_junk`）—— 这里原本是 `os.urandom(4096-2)`，
    0.5% 的概率会落进"前缀合法"分支，让这条断言随机变红。
    """
    p = tmp_path / "junk.pth"
    p.write_bytes(b"\x80\x02" + _seeded_junk(4096 - 2))
    torch = _FakeTorchRaises(
        RuntimeError(
            "Weights only load failed. In PyTorch 2.6, we changed the default value of the "
            "`weights_only` argument in `torch.load` from `False` to `True`. Re-running "
            "`torch.load` with `weights_only` set to `False` will likely succeed."
        )
    )
    with pytest.raises(RuntimeError) as ei:
        offline_vc_infer.load_checkpoint(str(p), torch)
    msg = str(ei.value)
    assert "不是 PyTorch 版本问题" in msg, f"没点破误诊方向：{msg}"
    assert "weights_only=False" in msg, f"没警告那个错误修法：{msg}"
    assert "结构判读" in msg, f"没带结构判读，下一个人仍无从判断：{msg}"
    assert "pickle 流在**早期就非法**" in msg, f"判读结论没进来：{msg}"


def test_load_checkpoint_rejects_wrong_shape(tmp_path):
    """能反序列化但不是 RVC ckpt（缺 weight/config）也要拦，且说清实际拿到了什么。"""
    p = tmp_path / "notrvc.pth"
    p.write_bytes(b"\x80\x04N.")
    torch = _FakeTorchReturns({"model": 1, "iteration": 2})  # 这是预训练底模的形状
    with pytest.raises(RuntimeError) as ei:
        offline_vc_infer.load_checkpoint(str(p), torch)
    msg = str(ei.value)
    assert "格式不对" in msg, msg
    assert "weight/config" in msg, msg
    assert "['model', 'iteration']" in msg, f"应报出实际键（好判断这是哪种文件）：{msg}"


def test_load_checkpoint_accepts_genuine_shape(tmp_path):
    """正常形状要通过，且**原样返回**（不能顺手改内容）。"""
    p = tmp_path / "ok.pth"
    p.write_bytes(b"\x80\x04N.")
    payload = {"weight": {"a": 1}, "config": [1, 2, 3], "f0": 1, "version": "v2"}
    torch = _FakeTorchReturns(payload)
    assert offline_vc_infer.load_checkpoint(str(p), torch) is payload


# ============== 推理随机性必须被播种（2026-10-13 补） ==============
#
# 由来：RVC v2 的 NSF 声源里有两个**未播种**的随机项（`infer/module/models.py`）：
#   · `SineGen._f02sine`: `rand_ini = torch.rand(1, 1, dim)`        —— 谐波初始相位
#   · `SineGen.forward` : `noise = noise_amp * torch.randn_like(..)` —— 噪声激励
# 所以「同一输入 / 同一权重 / 同一参数」每次转换都不同：实测同一命令跑两次，
# CAM++ 差 **0.0212**，比当时那张 index_rate 扫描表的**全部极差 0.0039** 还大
# —— 那张表的差异根本读不出来。播种后两次输出逐字节一致（md5 相同），
# 「只改一个变量跑一次」的对照才可信。
#
# 本文件在 FAST_TESTS 里（约定不 import torch，见上方说明），故这里用**桩 torch**。


class _FakeTorchSeed:
    """桩 torch：只记录 manual_seed / cuda.manual_seed_all 收到的值。"""

    def __init__(self, cuda_available=True):
        self.seeds = {}
        outer = self

        class _Cuda:
            @staticmethod
            def is_available():
                return cuda_available

            @staticmethod
            def manual_seed_all(s):
                outer.seeds["cuda"] = s

        self.cuda = _Cuda()

    def manual_seed(self, s):
        self.seeds["cpu"] = s


def test_apply_infer_seed_seeds_cpu_and_cuda_with_the_constant(monkeypatch):
    fake = _FakeTorchSeed()
    monkeypatch.setitem(sys.modules, "torch", fake)
    assert offline_vc_infer.apply_infer_seed() == offline_vc_infer.INFER_SEED
    assert fake.seeds == {"cpu": offline_vc_infer.INFER_SEED, "cuda": offline_vc_infer.INFER_SEED}


def test_apply_infer_seed_respects_explicit_seed(monkeypatch):
    """想平均多次采样时显式换 seed 必须能透传（默认值不是写死的唯一选项）。"""
    fake = _FakeTorchSeed()
    monkeypatch.setitem(sys.modules, "torch", fake)
    assert offline_vc_infer.apply_infer_seed(7) == 7
    assert fake.seeds == {"cpu": 7, "cuda": 7}


def test_apply_infer_seed_skips_cuda_when_unavailable(monkeypatch):
    """无 GPU 环境（CI）不得去调 cuda API 而崩掉。"""
    fake = _FakeTorchSeed(cuda_available=False)
    monkeypatch.setitem(sys.modules, "torch", fake)
    offline_vc_infer.apply_infer_seed()
    assert "cuda" not in fake.seeds
    assert fake.seeds["cpu"] == offline_vc_infer.INFER_SEED


class _FakePipe:
    """桩 Pipeline：不加载模型，只回一小段波形（本条只测「播种有没有发生」）。"""

    def pipeline(self, *_a, **_kw):
        return np.zeros(4000, dtype=np.float32)


class _FakeVC:
    def __init__(self):
        self.pipeline = _FakePipe()
        self.hubert_model = None
        self.net_g = None


def _fake_engine():
    return offline_vc_infer.RvcEngine(
        vc=_FakeVC(), tgt_sr=48000, index="", if_f0=1, version="v2", pth="fake.pth"
    )


def test_convert_audio_seeds_before_running(monkeypatch):
    """★ 三条链路只在 convert_audio 里播种一次：钉死它确实被调、默认值与透传都对。"""
    calls = []
    monkeypatch.setattr(
        offline_vc_infer,
        "apply_infer_seed",
        lambda seed=offline_vc_infer.INFER_SEED: calls.append(seed) or seed,
    )
    offline_vc_infer.convert_audio(_fake_engine(), np.zeros(16000, dtype=np.float32))
    offline_vc_infer.convert_audio(_fake_engine(), np.zeros(16000, dtype=np.float32), seed=99)
    assert calls == [offline_vc_infer.INFER_SEED, 99]
