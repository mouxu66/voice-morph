"""出厂音效合成器：程序化生成 `sound.fx-board` 的 6 条出厂素材（2026-09-23）。

为什么自己合成而不是打包 Kenney 的 CC0 包
------------------------------------------
设计稿（`docs/特效声板设计.md` §六）原计划 P1 用 Kenney 的 CC0 素材。实际落地时改成
**全部程序化合成**，理由有三条，且都比"素材好不好听"重要：

1. **版权链最短**：合成产物是本仓库自己的作品，不存在"这个包到底是 CC0 还是 CC-BY"的
   核对成本，也不需要往 `THIRD_PARTY_NOTICES.md` 里加署名条目（`tools/audit_licenses.py`
   会为每个第三方产物要求一条来源）；
2. **可复现、可调**：`python tools/gen_sfx.py` 一条命令重出全部素材，改音色就是改几行参数；
   二进制素材进了 git 就再也说不清"它当初是按什么参数裁的"；
3. **体积**：6 条 × ≤1.6s × 48kHz 单声道 16bit ≈ 600KB，比任何整包都小。

素材本身是「综艺感」用途 —— 微信语音里叠一声爆炸、掌声、警报，不是影视拟音，
所以合成器够用。真要好莱坞级素材，那是 P2 素材包（market 下载）的事。

用法
----
    python tools/gen_sfx.py             # 写入插件 samples/ 目录
    python tools/gen_sfx.py --out DIR   # 写到别处（自检用）

确定性：全部用 `np.random.default_rng(seed)`，同一条音效每次生成**逐字节一致** ——
所以 `samples/` 进了仓库也不会因为"谁重跑了一次"而漂移。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48000

#: 输出峰值 —— 声板侧还会乘 gain（默认 0.9），这里留足余量避免叠加人声时削波。
PEAK = 0.9


def _norm(x: np.ndarray, peak: float = PEAK) -> np.ndarray:
    m = float(np.max(np.abs(x))) if len(x) else 0.0
    return x if m == 0 else (x * (peak / m)).astype(np.float32)


def _env(n: int, attack_s: float, decay_s: float) -> np.ndarray:
    """快起 + 指数衰减包络（打击类音效的骨架）。"""
    t = np.arange(n) / SR
    att = np.clip(t / max(attack_s, 1e-4), 0.0, 1.0)
    return (att * np.exp(-t / max(decay_s, 1e-4))).astype(np.float64)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """一阶 IIR 低通（够用且零依赖；scipy 不必为了这几条素材引进生成脚本）。"""
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def _highpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    return x - _lowpass(x, cutoff)


def _compress(x: np.ndarray, drive: float = 6.0) -> np.ndarray:
    """软压缩（tanh）——只用来拉平**可感响度**，不是音色处理。

    峰值归一化对瞬态密集的素材（掌声：一堆短脉冲）会算出"峰高但听起来轻"的结果：
    实测掌声 RMS 0.055 vs 报警 0.585（约 20dB 差距）。压一下把 RMS/峰值的比抬起来，
    六条音效在耳朵里才是同一个档次。
    """
    return np.tanh(x * drive) / np.tanh(drive)


# --------------------------------------------------------------- 6 条音效


def fx_boom(rng) -> np.ndarray:
    """爆炸：噪声爆点 + 下扫低频 thump + 长尾——"轰"的记忆点在低频冲击。"""
    n = int(1.4 * SR)
    t = np.arange(n) / SR
    # 噪声层：宽带噪声经低通，截止随时间下扫（爆炸的"闷"来自高频先没）
    noise = rng.standard_normal(n)
    decay = np.exp(-t / 0.35)
    body = _lowpass(noise * decay, 900)
    # 低频冲击：60→28Hz 下扫正弦
    sweep = np.cumsum(np.linspace(60, 28, n) / SR)
    thump = np.sin(2 * np.pi * sweep) * _env(n, 0.004, 0.28) * 1.5
    # 软削波给一点"炸"的失真
    x = np.tanh((body * 1.8 + thump) * 1.3)
    return _norm(x[: int(1.4 * SR)])


def fx_applause(rng) -> np.ndarray:
    """掌声：~140 个随机时刻的短噪声脉冲 + 密度包络 + 尾巴。"""
    n = int(1.6 * SR)
    out = np.zeros(n)
    # 密度：先快速起势，中段最密，尾部渐稀
    for _ in range(140):
        pos = int(rng.beta(1.6, 2.2) * n * 0.92)
        clap_len = int(0.02 * SR)
        if pos + clap_len >= n:
            continue
        burst = rng.standard_normal(clap_len) * np.exp(-np.arange(clap_len) / (0.004 * SR))
        out[pos : pos + clap_len] += _highpass(burst, 1200) * rng.uniform(0.5, 1.0)
    out = _lowpass(out, 6000) * np.exp(-np.arange(n) / (0.5 * SR))
    return _norm(_compress(out, 8.0))


def fx_alarm(rng) -> np.ndarray:
    """警报：两音交替（800/620Hz）+ 电话带通，3 个周期。"""
    n = int(1.2 * SR)
    t = np.arange(n) / SR
    period = 0.4
    gate = ((t % period) < period / 2).astype(np.float64)
    freq = 800.0 * gate + 620.0 * (1 - gate)
    x = np.sin(2 * np.pi * np.cumsum(freq) / SR)
    # 电话感：300~3400Hz 带通近似（高通 + 低通）
    x = _lowpass(_highpass(x, 300), 3400)
    return _norm(np.tanh(x * 1.6) * _env(n, 0.02, 3.0))


def fx_riser(rng) -> np.ndarray:
    """升调：频率与噪声截止同步上扫 + 幅度渐强——"要出事"的提示音。"""
    n = int(1.2 * SR)
    t = np.arange(n) / SR
    sweep = np.cumsum(np.linspace(200, 1900, n) / SR)
    tone = np.sin(2 * np.pi * sweep) * 0.7
    # 噪声层用固定低通近似"越来越亮"（生成脚本不必为此上二阶滤波）
    air = rng.standard_normal(n) * 0.4
    gain = (t / t[-1]) ** 1.5  # 渐强
    return _norm((tone + _highpass(air, 2000)) * gain)


def fx_ding(rng) -> np.ndarray:
    """叮：钟体分音（含非谐分音）+ 各自指数衰减——比纯正弦"叮"更真实。"""
    n = int(1.0 * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for partial, decay, amp in ((1046, 0.5, 1.0), (2093, 0.35, 0.6), (2760, 0.22, 0.35), (4700, 0.12, 0.18)):
        x += np.sin(2 * np.pi * partial * t) * np.exp(-t / decay) * amp
    return _norm(x)


def fx_weird(rng) -> np.ndarray:
    """鬼畜：颤音 + 环形调制的"啵哟"感，纯喜剧用途。"""
    n = int(1.0 * SR)
    t = np.arange(n) / SR
    vib = 220 + 90 * np.sin(2 * np.pi * 7 * t)  # 7Hz 颤音
    tone = np.sin(2 * np.pi * np.cumsum(vib) / SR)
    ring = np.sign(np.sin(2 * np.pi * 24 * t))  # 方波载波 → 机械/鬼畜
    x = (tone * 0.7 + tone * ring * 0.5) * _env(n, 0.01, 0.6)
    return _norm(x)


#: 文件名 → (显示名, 合成函数, 标签)。文件名即素材 id（catalog 直接用它）。
ITEMS = {
    "boom": ("爆炸", fx_boom, ["综艺", "提示"]),
    "applause": ("掌声", fx_applause, ["综艺"]),
    "alarm": ("警报", fx_alarm, ["综艺", "提示"]),
    "riser": ("升调", fx_riser, ["转场"]),
    "ding": ("叮", fx_ding, ["提示"]),
    "weird": ("鬼畜", fx_weird, ["搞笑"]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "m2_server" / "plugins" / "sound.fx-board" / "samples"),
        help="输出目录（默认插件 samples/）",
    )
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    manifest: dict[str, dict] = {}
    for i, (name, (label, fn, tags)) in enumerate(ITEMS.items()):
        # 固定种子：同一条音效每次生成逐字节一致（重跑不改 git 里的字节）
        data = fn(np.random.default_rng(20260923 + i))
        path = out / f"{name}.wav"
        sf.write(str(path), data, SR, subtype="PCM_16")
        manifest[name] = {"name": label, "tags": tags}
        size = path.stat().st_size
        total += size
        print(f"  {label:<4} {path.name:<12} {len(data)/SR:.2f}s  {size/1024:6.1f}KB")

    # 显示名/标签也落成 JSON：后端只认这份清单，避免"名字写在两处然后漂了"。
    # （`soundboard.py` 的 catalog 读它；`test_soundboard.py` 会断言它与 wav 一一对应。）
    (out / "manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"合计 {total/1024:.0f}KB + manifest.json → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
