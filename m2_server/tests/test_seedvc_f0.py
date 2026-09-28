"""Seed-VC **f0 歌声链路**的守卫测试。

产品里 Seed-VC 有两套**互不相通**的入口，`mode` 决定走哪条：

| | `expressive`（默认） | `singing` |
|---|---|---|
| 入口 | `inference_v2.py` | `inference.py` |
| config | 两处 `f0_condition: false` | 命令行 `--f0-condition True` |
| 能否唱歌 | **不能**（音高被压平 → 念白） | 能（2026-09-27 实测 +0.10 半音） |

为什么这四条退化都值得专门守（**全都不会报错，只会安静地给出错结果**）：

1. **入口选错**：f0 分支被改回 v2，照样跑完、照样出 wav —— 只是变成念白
   （用户听感原话「像读出来的」）。
2. **旗标掉了**：`--f0-condition` / `--auto-f0-adjust` / `--semi-tone-shift` 任一缺失，
   产物音高被压平或整体降八度，而判据 CAM++ 只会给出另一个数，**不报错**。
3. **参数名串台**：两条链参数名不通用（f0 版只认 `--inference-cfg-rate`，
   没有 `--similarity-cfg-rate`）。串台会在子进程里崩掉，只留一份 stderr。
4. **默认值漂移**：A 档（`shift=11`）是用户盲听 A~E 五档后**选定**的，
   落在 `SINGING_PRESET` 里；被顺手改小会静默降质。

测试策略：只碰纯函数与签名，不起子进程、不加载模型、不碰声卡。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

import seed_vc


def _cmd(**kw):
    """以最省事的默认值调 build_cmd，只覆盖关心的键。"""
    base = dict(in_src=Path("s.wav"), in_tgt=Path("t.wav"), out_dir=Path("o"))
    base.update(kw)
    return seed_vc.build_cmd(**base)


def _val(cmd: list[str], flag: str) -> str | None:
    """取 `--flag value` 里的 value；flag 不存在返回 None。"""
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _form_default(func, name):
    """取 FastAPI Form() 参数的实际默认值（Form 对象 → .default）。"""
    p = inspect.signature(func).parameters[name]
    return getattr(p.default, "default", p.default)


# --------------------------------------------------------- 入口选对没

def test_f0_branch_uses_f0_entrypoint():
    """★ f0 分支必须调 inference.py —— 调 v2 的话产物是念白，且不报错。"""
    assert _cmd(f0_condition=True)[1] == str(seed_vc.SEEDVC_INFER_F0)
    assert seed_vc.SEEDVC_INFER_F0.name == "inference.py"


def test_v2_branch_uses_v2_entrypoint():
    """默认（expressive）仍走 v2 —— offline_vc / ab_chain 的行为不能被我改动带偏。"""
    assert _cmd()[1] == str(seed_vc.SEEDVC_INFER_V2)
    assert seed_vc.SEEDVC_INFER_V2.name == "inference_v2.py"


def test_both_entrypoints_exist_side_by_side():
    """两个入口都得真的在 —— 否则上面的断言可能是同义反复。"""
    assert seed_vc.SEEDVC_INFER_F0 != seed_vc.SEEDVC_INFER_V2
    assert seed_vc.SEEDVC_INFER_F0.exists(), "f0 入口不在预期位置，前面的断言失去意义"
    assert seed_vc.SEEDVC_INFER_V2.exists()


def test_legacy_seedvc_infer_name_still_resolves_to_v2():
    """旧名 `SEEDVC_INFER` 保持指向 v2（文档与既有代码仍引用它）。"""
    assert seed_vc.SEEDVC_INFER == seed_vc.SEEDVC_INFER_V2


# ------------------------------------------------------- 旗标齐全没

def test_f0_cmd_carries_all_three_f0_flags():
    """f0 条件、auto 调整、半音移位 —— 缺任一个就静默变念白/降八度。"""
    cmd = _cmd(f0_condition=True, auto_f0_adjust=True, semi_tone_shift=11)
    assert _val(cmd, "--f0-condition") == "True"
    assert _val(cmd, "--auto-f0-adjust") == "True"
    assert _val(cmd, "--semi-tone-shift") == "11"


def test_f0_cmd_always_forces_f0_condition_true():
    """进了 f0 分支就必须 `--f0-condition True`：这条分支存在的唯一理由就是它。"""
    assert _val(_cmd(f0_condition=True, auto_f0_adjust=False), "--f0-condition") == "True"


def test_f0_cmd_never_passes_v2_only_flags():
    """★ 参数名串台：f0 版没有这些旗标，传了会 unrecognized argument 直接崩。"""
    cmd = _cmd(f0_condition=True, auto_f0_adjust=True, semi_tone_shift=11)
    for flag in ("--similarity-cfg-rate", "--top-p", "--temperature", "--convert-style"):
        assert flag not in cmd, f"f0 分支混入了 v2 专属旗标 {flag}"


def test_v2_cmd_never_passes_f0_flags():
    """反向也要守：v2 版没有 f0 旗标，混进去同样会崩。"""
    cmd = _cmd()
    for flag in ("--f0-condition", "--auto-f0-adjust", "--semi-tone-shift",
                 "--inference-cfg-rate", "--fp16"):
        assert flag not in cmd, f"v2 分支混入了 f0 专属旗标 {flag}"


def test_f0_cmd_uses_inference_cfg_rate_not_similarity():
    """两条链的 cfg 旗标不同名，别互相顶替。"""
    cmd = _cmd(f0_condition=True, auto_f0_adjust=True, inference_cfg_rate=1.2)
    assert _val(cmd, "--inference-cfg-rate") == "1.2"


def test_every_shift_gets_a_distinct_f0_cmd():
    """不同 shift 必须给出不同命令行 —— 否则改的是个不生效的旋钮。"""
    cmds = {"|".join(_cmd(f0_condition=True, auto_f0_adjust=True, semi_tone_shift=s))
            for s in (0, 5, 11, 15)}
    assert len(cmds) == 4


# ------------------------------------------------ 微调权重的静默陷阱

def test_f0_rejects_cfm_checkpoint_loudly():
    """★ f0 链路不支持微调 CFM 权重（那边的 `--checkpoint` 是 f0 DiT 权重）。

    静默忽略 = 用户以为在用自己的微调音色、实际跑的是底模 —— 必须显式抛。
    """
    with pytest.raises(ValueError, match="cfm_checkpoint_path"):
        _cmd(f0_condition=True, cfm_checkpoint_path=Path("fake.pth"))


def test_v2_still_accepts_cfm_checkpoint():
    """v2 分支行为不变：仍然接受微调权重。"""
    cmd = _cmd(cfm_checkpoint_path=Path("fake.pth"))
    assert _val(cmd, "--cfm-checkpoint-path") == "fake.pth"


# -------------------------------------------------------- A 档预设

def test_singing_preset_is_the_calibrated_a_tier():
    """★ A 档四个值 = 2026-09-27 标定值，被顺手改会静默降质。"""
    assert seed_vc.SINGING_PRESET["diffusion_steps"] == 80      # 50→80 是免费收益
    assert seed_vc.SINGING_PRESET["inference_cfg_rate"] == 1.2
    assert seed_vc.SINGING_PRESET["auto_f0_adjust"] is True
    assert seed_vc.SINGING_PRESET["length_adjust"] == 1.0


def test_singing_preset_shift_is_the_user_choice():
    """用户盲听 A~E 选定 A（shift=11）—— 这是**用户决策**，不是可随手调的常数。"""
    assert seed_vc.SINGING_PRESET["semi_tone_shift"] == 11


def test_singing_preset_shift_within_scanned_range():
    """shift 必须落在扫过的 0~15 内：超出范围就是没人验证过的外推。"""
    assert 0 <= seed_vc.SINGING_PRESET["semi_tone_shift"] <= 15


# --------------------------------------------------- None = A 档

def test_resolve_defaults_to_preset_when_all_none():
    got = seed_vc.resolve_singing_params()
    assert got == seed_vc.SINGING_PRESET
    assert got is not seed_vc.SINGING_PRESET, "必须返回副本，别让调用方改坏全局预设"


def test_resolve_keeps_explicit_false():
    """★ `auto_f0_adjust=False` 是**合法显式值**，不能被 `if val` 当 falsy 吞掉。"""
    assert seed_vc.resolve_singing_params(auto_f0_adjust=False)["auto_f0_adjust"] is False


def test_resolve_keeps_zero_shift():
    """同理：`semi_tone_shift=0`（音色最高的档）也是合法值，不能被吞成默认 11。"""
    assert seed_vc.resolve_singing_params(semi_tone_shift=0)["semi_tone_shift"] == 0


def test_resolve_overrides_only_the_given_keys():
    got = seed_vc.resolve_singing_params(semi_tone_shift=3)
    assert got["semi_tone_shift"] == 3
    assert got["diffusion_steps"] == seed_vc.SINGING_PRESET["diffusion_steps"]
    assert got["auto_f0_adjust"] is True


# ------------------------------------------------------- API 层接线

def test_api_mode_defaults_to_expressive():
    """★ 默认必须是 expressive：改动前的调用方（前端/offline_vc）行为一个字节不变。"""
    assert _form_default(seed_vc.seedvc_run, "mode") == "expressive"


def test_api_singing_knobs_default_to_none():
    """唱歌旋钮默认 None（= 交给 A 档预设），不是硬编码 11/80 —— 否则 v2 路径会被污染。"""
    for name in ("semi_tone_shift", "auto_f0_adjust", "inference_cfg_rate"):
        assert _form_default(seed_vc.seedvc_run, name) is None


def test_api_diffusion_steps_default_is_none_so_v2_keeps_10():
    """steps 默认 None：v2 分支回落到原来的 10 步，唱歌分支落到预设的 80 步。

    这里用源码断言而不是行为断言，是**刻意**的：真跑 v2 分支要 monkeypatch 掉
    ffmpeg 预处理 + 子进程 + soundfile 读回，成本远高于这条要守的东西
    （「None 别直接透传给子进程」——`str(None)` 会变成字面量 "None" 崩在 argparse）。
    """
    assert _form_default(seed_vc.seedvc_run, "diffusion_steps") is None
    assert "10 if diffusion_steps is None else diffusion_steps" in inspect.getsource(
        seed_vc._seedvc_worker
    )
    assert "1.0 if length_adjust is None else length_adjust" in inspect.getsource(
        seed_vc._seedvc_worker
    )


def test_api_rejects_unknown_mode():
    """未知 mode 直接 400，不能静默当成 expressive 跑掉（那是另一条模型链）。"""
    with pytest.raises(HTTPException) as ei:
        asyncio.run(seed_vc.seedvc_run(file=None, target=None, mode="sing"))
    assert ei.value.status_code == 400
    assert "sing" in ei.value.detail


def test_worker_accepts_singing_kwargs():
    """worker 必须收得下 API 传的唱歌参数（kwargs 接线，漏一个就是没生效）。"""
    params = inspect.signature(seed_vc._seedvc_worker).parameters
    for name in ("mode", "semi_tone_shift", "auto_f0_adjust", "inference_cfg_rate",
                 "cfm_checkpoint_path"):
        assert name in params, f"_seedvc_worker 缺参数 {name}"
