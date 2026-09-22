"""`weights_only=True` 必须留着的守护 —— 用真 torch 验，故不在 FAST_TESTS 里。

背景（2026-09-21）
------------------
本机 `auto_rb` 音色报「PyTorch 2.6 weights_only」，被记成「PyTorch 版本问题」。
实测真因是**文件本身不是权重**：它正好是 `test_market_search_install.py` 的夹具
`b"\\x80\\x02" + os.urandom(512*1024-2)`。

危险的地方不在于错一次，而在于**这个错误修法看起来很有道理**：
torch 的报错原文写着 "Re-running `torch.load` with `weights_only` set to `False`
will likely succeed"，而关掉它确实能让代码"跑起来"（对这个文件也不会 succeed，
但很多人会先关掉再说）。代价是拿掉了一层真实的安全边界 ——
音色市场的权重来自公网，第三方 ckpt 的 `__reduce__` 载荷可以触发任意代码执行，
`weights_only=True` 正是挡这个的。

所以本文件**钉住那个决策的依据**：真实形状的 RVC 权重在 `weights_only=True` 下
本来就加载得了 —— 严格模式不挡任何正常模型，只挡「文件根本不是权重」。
哪天有人想关掉它，这里会先红，逼他正面回答「你确定要拿掉这层保护？」。

为什么不在 `test_offline_vc_infer.py` 里
----------------------------------------
那个文件在 `check.py` 的 `FAST_TESTS` 里（pre-commit 会跑），而 import torch
实测 2.5s，会挤掉提交预算。这里用 `importorskip`，没有 torch 的机器（CI）直接跳过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

torch = pytest.importorskip("torch", reason="本组守护需要真实 torch")

import offline_vc_infer  # noqa: E402


def _rvc_root() -> Path | None:
    """RVC 根目录：优先 env，其次 config.py 的默认值。不存在则整组跳过。"""
    env = os.environ.get("VM_RVC_ROOT")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    try:
        import config

        p = Path(config.RVC_ROOT)
        return p if p.is_dir() else None
    except Exception:  # noqa: BLE001 —— 配置读不到就当没有，跳过而不是报错
        return None


def test_strict_load_accepts_a_real_shaped_rvc_checkpoint(tmp_path):
    """★ 决策依据：正常形状的 RVC 权重（dict + weight/config）在严格模式下能加载。

    用 torch.save 造一个**真实格式**的文件（torch 1.6+ 默认 zip 容器），
    形状照抄本机真实 ckpt 的键：`weight` / `config` / `f0` / `version`。
    这条机器无关，任何装了 torch 的环境都能跑 —— 所以它是那个决策的**常规守卫**。
    """
    p = tmp_path / "real_shape.pth"
    payload = {
        "weight": {"emb_g.weight": torch.zeros(2, 4)},
        "config": [4, 32, 192, 8, 2, 8],
        "f0": 1,
        "version": "v2",
        "info": "synthetic",
    }
    torch.save(payload, p)

    got = offline_vc_infer.load_checkpoint(str(p), torch)
    assert isinstance(got, dict)
    assert set(got) >= {"weight", "config"}
    assert torch.equal(got["weight"]["emb_g.weight"], payload["weight"]["emb_g.weight"])

    # 顺带确认结构判读不会误伤正常文件
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "不是权重文件" not in msg, f"判读误伤了正常存档：{msg}"
    assert "既不是 zip(PK) 也不是 pickle" not in msg, f"判读误伤了正常存档：{msg}"


def test_strict_load_is_actually_used(tmp_path):
    """★ 直接钉住调用参数：`weights_only=True` 不能被悄悄改成 False。

    上一条用例只能证明「严格模式下正常文件能过」，但有人把参数改成 False
    它照样是绿的。所以这里拦一层：记录 `load` 收到的 kwargs，断言确实是 True。
    """
    p = tmp_path / "real_shape.pth"
    torch.save({"weight": {"w": torch.zeros(1)}, "config": [1], "f0": 1, "version": "v2"}, p)

    seen: dict = {}
    real_load = torch.load

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_load(*args, **kwargs)

    class _Wrapper:
        load = staticmethod(spy)

    offline_vc_infer.load_checkpoint(str(p), _Wrapper)
    assert seen.get("weights_only") is True, (
        f"load_checkpoint 必须用 weights_only=True（实际 {seen.get('weights_only')!r}）—— "
        f"见本文件 docstring：音色市场权重来自公网，这层是真实的安全边界。"
    )


def _voice_checkpoints(root: Path) -> list[Path]:
    """真实的**音色**权重（`load_vc` 真正消费的那种）。

    ⚠️ 不要拿 `assets/pretrained/*.pth` 当样本：那是**训练底模**
    （形状 `{model, iteration, learning_rate}`），由 RVC 训练脚本加载，
    不走 `load_vc` —— 实测（2026-09-21）第一版就是拿它们当样本，结果被
    形状校验正确拦下，暴露的是**测试选错了样本**而不是代码有问题。
    """
    out = [p for p in sorted((root / "logs").glob("*/*.pth")) if p.is_file()]
    out += [p for p in sorted((root / "assets" / "weights").glob("*.pth")) if p.is_file()]
    return out


def test_real_voice_weights_load_under_strict_mode():
    """真实音色权重（`logs/*/*.pth`）要能在严格模式下加载。

    这才是那个决策的**机器级证据**：`weights_only=True` 不挡任何正常音色。
    机器/CI 上没装 RVC → 跳过。

    ★ 只验 **1 个** 就够（2026-09-22 由 3 个改成 1 个）
    -------------------------------------------------
    本用例是**偶发假红**的来源，实测（3 次全量：2 次红 / 1 次绿；
    单跑本文件、只跑到本文件的 40 个文件范围、人为占 1.5GB 内存 —— 全绿）。

    机制：本机可用内存只有 ~4.3GB（其余被常驻程序占掉），而 pytest 进程
    在 **collect 阶段**就因本模块顶层的 `pytest.importorskip("torch")`
    常驻 ~600MB；再连续 `torch.load` 三个 57MB 权重（每个解包后的瞬时占用
    远超文件体积，且前一个要到下一轮赋值才释放）会顶到上限 →
    `torch.load` 抛 MemoryError → 被 `load_checkpoint` 包成 RuntimeError →
    **三个全 skip** → `checked == 0` → 报红。而它红的时候报的是
    「严格模式可能真的挡了正常模型」—— **完全误导**，真因只是内存不够。

    三处改动对症：
      1. 只验 1 个（本用例的目的是"证明严格模式不挡正常模型"，
         1 个正常模型即可成立；"真实格式能被严格模式接受"另有
         `test_strict_load_accepts_a_real_shaped_rvc_checkpoint` 覆盖）
      2. 每加载一个就 `del`，不把多个权重的峰值叠在一起
      3. **区分"内存不足"与"文件坏了"** —— 前者是环境问题，skip；
         后者才是产品问题，照常报红
    """
    root = _rvc_root()
    if root is None:
        pytest.skip("本机没有 RVC 整合包（VM_RVC_ROOT 未设且默认路径不存在）")
    cands = _voice_checkpoints(root)
    if not cands:
        pytest.skip(f"{root} 下没有音色权重（logs/*/*.pth）")

    checked = 0
    skipped: list[str] = []
    oom: list[str] = []
    for pth in cands:
        try:
            got = offline_vc_infer.load_checkpoint(str(pth), torch)
        except RuntimeError as exc:
            msg = str(exc)
            # 内存不足 → 环境问题（见 docstring），不算"严格模式挡了正常模型"
            if "MemoryError" in msg or "out of memory" in msg or "DefaultCPUAllocator" in msg:
                oom.append(pth.name)
            else:
                lines = msg.splitlines()
                skipped.append(f"{pth.name}: {lines[1].strip() if len(lines) > 1 else msg[:120]}")
            continue
        try:
            assert isinstance(got, dict) and got, f"{pth.name} 加载结果不是非空 dict"
            assert "weight" in got and "config" in got, f"{pth.name} 形状不对（{list(got)[:6]}）"
        finally:
            del got     # 立刻释放，别把多个权重的峰值叠在一起
        checked += 1
        break           # 1 个正常模型即足以证明严格模式不挡正常模型

    if checked == 0 and oom and not skipped:
        pytest.skip(
            f"{len(oom)} 个音色加载时**内存不足**（如 {oom[:3]}）—— 这是**环境问题**"
            f"（本机可用内存偏低时全量跑会偶发），不是产品问题；单独跑本文件即为绿。"
            f"见本用例 docstring。"
        )
    assert checked > 0, (
        f"连一个正常音色都没能加载 —— 严格模式可能真的挡了正常模型，"
        f"那才该考虑放宽（这是必须正面回答的问题，别直接改测试）。\n"
        f"被跳过的：{skipped}\n"
        f"内存不足的：{oom}"
    )


# ---------------------------------------------------------------- 偶发假红守卫


def _fake_weights_dir(tmp_path, n=3) -> Path:
    """造一个「有 n 个音色权重」的假 RVC 根（形状与 `_voice_checkpoints` 对得上）。"""
    root = tmp_path / "rvc"
    for i in range(n):
        d = root / "logs" / f"v{i}"
        d.mkdir(parents=True)
        (d / f"v{i}.pth").write_bytes(b"x")
    (root / "assets" / "weights").mkdir(parents=True)
    return root


def _run_inner() -> BaseException:
    """跑内层用例，返回它抛出的异常（**不判断类型**）。

    为什么不直接 `pytest.raises(期望类型)`：拿到**别的**异常时它会放行那个异常，
    而若那个异常恰好是 `skip`，本用例就变成"被跳过" —— 在 CI 里**不算失败**，
    守卫静默失效（2026-09-22 变异验证时实测踩到：变异后显示 `s` 而非 `F`，
    退出码仍是 0）。所以这里一律先抓成 `BaseException`，再显式断言类型。
    """
    with pytest.raises(BaseException) as ei:  # noqa: PT011 —— 故意抓全部，见上
        test_real_voice_weights_load_under_strict_mode()
    return ei.value


def _assert_exc_is(exc: BaseException, want: type) -> None:
    assert isinstance(exc, want), (
        f"内层抛的是 {type(exc).__name__}：{exc}\n期望 {want.__name__} —— "
        f"异常类型不对说明走错了分支（尤其当它是 skip 时，会被 pytest 静默当作「跳过」）。"
    )


def test_oom_only_skips_instead_of_red(monkeypatch, tmp_path):
    """★ 失败**全部**因内存不足 → skip（环境问题），而不是报红。

    这是 2026-09-22 那次偶发假红的对症守卫：全量跑时可用内存偏低 →
    连续 `torch.load` 三个 57MB 权重全部 MemoryError → `checked == 0` →
    原实现报的是**误导性**的「严格模式可能真的挡了正常模型」。

    把 `if checked == 0 and oom and not skipped: pytest.skip(...)`
    删掉（即回退成直接 assert）本用例会红。
    """
    monkeypatch.setattr(sys.modules[__name__], "_rvc_root", lambda: _fake_weights_dir(tmp_path))

    def boom(_pth, _torch):
        raise RuntimeError(
            "RVC 权重加载失败：x\n  结构判读：...\n"
            "  原始错误：MemoryError: DefaultCPUAllocator: not enough memory\n"
        )

    monkeypatch.setattr(offline_vc_infer, "load_checkpoint", boom)
    exc = _run_inner()
    _assert_exc_is(exc, pytest.skip.Exception)
    assert "内存不足" in str(exc)


def test_broken_file_still_reports_red(monkeypatch, tmp_path):
    """★ 对照：**文件坏了**（不是内存）仍必须报红 —— skip 不能掩盖真问题。

    没有这条，"内存不足就 skip"会退化成"什么失败都 skip"，
    那才是真的把守护做废了。把 OOM 判定放宽成"所有 RuntimeError 都算内存不足"
    本用例会红（实测：会变成 `s`，故本用例显式断言异常类型，见 `_run_inner`）。
    """
    monkeypatch.setattr(sys.modules[__name__], "_rvc_root", lambda: _fake_weights_dir(tmp_path))

    def boom(_pth, _torch):
        raise RuntimeError(
            "RVC 权重加载失败：x\n  结构判读：不是权重文件\n  原始错误：UnpicklingError: junk"
        )

    monkeypatch.setattr(offline_vc_infer, "load_checkpoint", boom)
    exc = _run_inner()
    _assert_exc_is(exc, AssertionError)
    assert "严格模式可能真的挡了正常模型" in str(exc)
