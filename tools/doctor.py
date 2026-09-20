"""变声工坊 · 环境体检。

这个应用的前端只是界面，真正的推理全在本地 Python 后端与若干外部工具里。
换一台机器时最容易卡在"界面打开了但什么都用不了"，本脚本把依赖逐项查一遍，
并直接给出补齐命令 —— 目标是不用翻文档也能把环境配好。

第 5 步（插件化）之后多了两件事：
  * **torch 不再是"必需项"** —— 它归 `sound.offline-vc` / `sound.audition` /
    `sound.workshop` 的 extras。没装 torch 时 `/api/health` 会返回 `cuda: null`
    而不是 500，核心页面照常能用，所以这里的判定跟着变成"按启用集"。
  * **每个启用插件的 extras 逐项对账** —— 见最后的「插件依赖」一节，
    直接给 `setup_env.ps1` 的命令（而不是让人自己猜该装什么）。

用法：
    python tools\\doctor.py            # 人可读报告
    python tools\\doctor.py --json     # 机器可读（供安装脚本/前端消费）
退出码：0=全部就绪，1=缺必需项，2=缺可选但功能会受限（仅 --strict 时返回）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_PORT = 8000


class Check:
    def __init__(self, key: str, label: str, required: bool = True):
        self.key = key
        self.label = label
        self.required = required
        self.ok = False
        self.detail = ""
        self.fix = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
        }


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"执行超时({timeout}s)"


def _find_venv_py() -> Path | None:
    for p in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if p.exists():
            return p
    return None


def _find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_checks() -> list[Check]:
    results: list[Check] = []

    # ---------- 1. Python 主环境 ----------
    c = Check("python", "Python 主环境", required=True)
    venv_py = _find_venv_py()
    if venv_py:
        rc, out, err = _run([str(venv_py), "-c", "import sys;print(sys.version.split()[0])"])
        c.ok = rc == 0
        c.detail = f"{venv_py}（Python {out}）" if c.ok else err
    else:
        rc, out, err = _run([sys.executable, "-c", "import sys;print(sys.version.split()[0])"])
        c.ok = rc == 0
        c.detail = f"未找到项目 .venv，回退系统解释器 {sys.executable}（Python {out}）"
        c.fix = f'cd "{ROOT}" && python -m venv .venv'
    if c.ok and not venv_py:
        c.fix = f'cd "{ROOT}" && python -m venv .venv && .venv\\Scripts\\python.exe -m pip install -r requirements.txt'
    results.append(c)

    py = str(venv_py) if venv_py else sys.executable

    # ---------- 2. 后端依赖 ----------
    c = Check("backend_deps", "后端依赖（fastapi / uvicorn / pydub）", required=True)
    rc, out, err = _run([py, "-c", "import fastapi, uvicorn, pydub; print('ok')"])
    c.ok = rc == 0 and "ok" in out
    c.detail = "已安装" if c.ok else (err.splitlines()[-1] if err else "导入失败")
    if not c.ok:
        c.fix = f'"{py}" -m pip install -r "{ROOT / "requirements.txt"}"'
    results.append(c)

    # ---------- 2.5 插件 extras（按启用集） ----------
    # 第 5 步的核心验收方式：不再问"torch 装了没"，而是问
    # 「你**启用的**这些能力，各自的 extras 齐了没」。
    plan: dict = {}
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import plugin_extras  # noqa: PLC0415

        plan = plugin_extras.resolve()
        installed = plugin_extras.check_installed(py, plan["python"])
        missing_pkgs = sorted(k for k, v in installed.items() if not v)
        c = Check("plugin_extras", "插件依赖（按当前启用集）", required=False)
        c.ok = not missing_pkgs
        if missing_pkgs:
            # 说清"缺的这个包是谁要的" —— 只说包名，用户不知道该开/关哪个能力
            owners = {
                pkg: [v["name"] for v in plan["by_plugin"].values() if pkg in v["python"]]
                for pkg in missing_pkgs
            }
            detail = "；".join(f"{pkg}（{'、'.join(owners[pkg])}）" for pkg in missing_pkgs)
            c.detail = f"启用 {plan['enabled_count']} 个能力 · 缺 {len(missing_pkgs)} 个：{detail}"
            c.fix = f'"{ROOT / "tools" / "setup_env.ps1"}" -Preset {plan["preset"]}'
        else:
            c.detail = f"启用 {plan['enabled_count']} 个能力 · 所需 {len(plan['python'])} 个包全部就绪"
        results.append(c)
    except Exception as e:  # noqa: BLE001
        c = Check("plugin_extras", "插件依赖（按当前启用集）", required=False)
        c.detail = f"算不出来：{type(e).__name__}: {e}"
        c.fix = f'"{py}" "{ROOT / "tools" / "plugin_extras.py"}"'
        results.append(c)
        plan = {}

    # ---------- 3. PyTorch / CUDA ----------
    # 第 5 步起 torch 归 extras（sound.offline-vc / sound.audition / sound.workshop）：
    # 没装它不该算"必需项缺失"（`/health` 会降级成 `cuda: null`，核心功能照常）。
    # 但**缺了要说清哪几个能力会不可用**，否则用户看到"可选"就忽略了。
    torch_owners = [
        v["name"]
        for v in plan.get("by_plugin", {}).values()
        if {x.lower() for x in v["python"]} & {"torch", "torchaudio"}
    ]
    c = Check("torch", "PyTorch + CUDA", required=False)
    code = (
        "import torch;"
        "print(torch.__version__, torch.cuda.is_available(),"
        "(torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''))"
    )
    rc, out, err = _run([py, "-c", code], timeout=180)
    if rc == 0 and out:
        ver, avail, name = (out.split(" ", 2) + ["", ""])[:3]
        c.ok = avail == "True"
        c.detail = f"torch {ver} · CUDA {'可用' if c.ok else '不可用'}{(' · ' + name) if name else ''}"
        if not c.ok:
            c.fix = ("装了 torch 但 CUDA 不可用，多半是 CPU 版（会静默不用显卡）。"
                     "重装 GPU 版：pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128")
    else:
        c.ok = not torch_owners  # 没启用需要它的能力 → 不算缺
        c.detail = "未安装" + (
            "（当前启用集不需要它，不影响使用）" if not torch_owners
            else f"（{'、'.join(torch_owners)} 需要它，这些能力不可用）"
        )
        c.fix = f'"{ROOT / "tools" / "setup_env.ps1"}" -Preset {plan.get("preset", "standard")}' if torch_owners else ""
    results.append(c)

    # ---------- 4. TTS（Qwen3-TTS） ----------
    # 两套环境任一可用即可：主 venv 直接装了 qwen_tts，或配置了独立的 TTS venv。
    tt = Check("tts", "Qwen3-TTS 推理环境", required=False)
    rc, out, err = _run([py, "-c", "import qwen_tts; print('ok')"], timeout=120)
    if rc == 0:
        tt.ok = True
        tt.detail = f"主环境已安装（{py}）"
    else:
        tts_py_env = os.environ.get("VM_TTS_VENV_PY") or str(
            ROOT / "tts_trial" / "venv312" / "Scripts" / "python.exe"
        )
        if Path(tts_py_env).exists():
            # qwen_tts 导入时会往 stdout 打一坨 banner（含建议装 flash-attn 的警告），
            # 因此用带前缀的标记行取版本号，只认最后一行
            marker = "VM_TTS_VER:"
            rc2, out2, err2 = _run(
                [tts_py_env, "-c", f"import qwen_tts, torch; print('{marker}' + torch.__version__)"],
                timeout=180,
            )
            ver = next((ln[len(marker):] for ln in reversed(out2.splitlines()) if ln.startswith(marker)), "")
            tt.ok = rc2 == 0 and bool(ver)
            tt.detail = (
                f"独立环境 {tts_py_env}（torch {ver}）" if tt.ok
                else f"独立环境存在但导入失败：{(err2 or out2).splitlines()[-1] if (err2 or out2) else ''}"
            )
        else:
            tt.detail = "未找到 qwen_tts，也没有独立 TTS 环境"
        if not tt.ok:
            tt.fix = (f'"{ROOT / "tools" / "setup_env.ps1"}" -Plugins sound.tts  '
                      f'（或设置 VM_TTS_VENV_PY 指向已装好的环境）')
    results.append(tt)

    # ---------- 5. TTS 模型权重 ----------
    c = Check("tts_weights", "TTS 模型权重", required=False)
    model_dir = Path(os.environ.get("VM_QWEN_MODEL_DIR") or (ROOT / "tts_models" / "qwen3-tts-1.7b-base"))
    tok_dir = Path(os.environ.get("VM_QWEN_TOKENIZER_DIR") or (ROOT / "tts_models" / "qwen3-tts-tokenizer-12hz"))
    have_model = model_dir.exists() and any(model_dir.glob("*.safetensors"))
    have_tok = tok_dir.exists() and any(tok_dir.iterdir())
    c.ok = have_model and have_tok
    c.detail = (f"模型 {'有' if have_model else '缺'} {model_dir} · "
                f"tokenizer {'有' if have_tok else '缺'} {tok_dir}")
    if not c.ok:
        c.fix = (f'把 Qwen3-TTS-12Hz-1.7B-Base 权重放到 {model_dir}，'
                 f'tokenizer 放到 {tok_dir}（可用 VM_QWEN_MODEL_DIR / VM_QWEN_TOKENIZER_DIR 覆盖）')
    results.append(c)

    # ---------- 6. RVC 整合包 ----------
    c = Check("rvc", "RVC 整合包（实时变声）", required=False)
    rvc_root = Path(os.environ.get("VM_RVC_ROOT") or "D:/RVC")
    rvc_venv = rvc_root / ".venv" / "Scripts" / "python.exe"
    train_py = rvc_root / "train_rvc_voice.py"
    realtime_py = rvc_root / "realtime_gui.py"
    c.ok = rvc_root.exists() and rvc_venv.exists() and train_py.exists() and realtime_py.exists()
    missing = [n for n, e in (("整合包", rvc_root.exists()), ("自带环境", rvc_venv.exists()),
                              ("train_rvc_voice.py", train_py.exists()), ("realtime_gui.py", realtime_py.exists())) if not e]
    c.detail = f"{rvc_root}" + (f" · 缺少：{'、'.join(missing)}" if missing else " · 完整")
    if not c.ok:
        c.fix = f'下载 RVC 整合包并配置环境变量 VM_RVC_ROOT（当前指向 {rvc_root}）'
    results.append(c)

    # ---------- 7. 虚拟声卡 ----------
    c = Check("vb_cable", "VB-Audio CABLE 虚拟声卡", required=False)
    ps = _find_powershell()
    if ps:
        rc, out, err = _run([ps, "-NoProfile", "-Command",
                             "Get-CimInstance Win32_SoundDevice | Select-Object -ExpandProperty Name"], timeout=60)
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        hit = [n for n in names if "cable" in n.lower()]
        c.ok = bool(hit)
        c.detail = "；".join(hit) if hit else f"未检测到（共 {len(names)} 个音频设备）"
    else:
        c.detail = "未找到 PowerShell，无法枚举音频设备"
    if not c.ok:
        c.fix = "到 https://vb-audio.com/Cable/ 下载安装 VB-CABLE（实时变声依赖它做声音中转）"
    results.append(c)

    # ---------- 8. 后端是否在运行 ----------
    c = Check("backend_running", f"后端服务（端口 {BACKEND_PORT}）", required=False)
    c.ok = _port_in_use(BACKEND_PORT)
    c.detail = "正在运行" if c.ok else "未运行（启动：python m2_server\\server.py）"
    if not c.ok:
        c.fix = f'cd "{ROOT / "m2_server"}" && "{py}" server.py'
    results.append(c)

    return results


def main() -> int:
    as_json = "--json" in sys.argv
    checks = run_checks()
    missing_required = [c for c in checks if c.required and not c.ok]
    missing_optional = [c for c in checks if not c.required and not c.ok]

    if as_json:
        print(json.dumps({
            "root": str(ROOT),
            "ok": not missing_required,
            "missing_required": [c.key for c in missing_required],
            "missing_optional": [c.key for c in missing_optional],
            "checks": [c.to_dict() for c in checks],
        }, ensure_ascii=False, indent=2))
    else:
        print("")
        print("变声工坊 · 环境体检")
        print(f"项目根：{ROOT}")
        print("")
        # 标记一律用 ASCII：Windows 控制台默认 GBK，✓/✗ 会直接抛 UnicodeEncodeError
        for c in checks:
            mark = "OK" if c.ok else ("NG" if c.required else "--")
            tag = "必需" if c.required else "可选"
            print(f"  [{mark}] {c.label}  ({tag})")
            print(f"      {c.detail}")
            if not c.ok and c.fix:
                print(f"      → {c.fix}")
            print("")
        total_required = sum(1 for c in checks if c.required)
        ok_required = sum(1 for c in checks if c.required and c.ok)
        print(f"汇总：必需项 {ok_required}/{total_required} 就绪"
              + (f"，{len(missing_required)} 项缺失" if missing_required else "")
              + (f"；{len(missing_optional)} 项可选能力不可用" if missing_optional else "，可选能力全部就绪"))
        if missing_required:
            print("")
            print("必需项缺失，应用无法工作。按上面的 → 提示补齐后重跑本脚本。")
        print("")

    return 1 if missing_required else 0


if __name__ == "__main__":
    sys.exit(main())
