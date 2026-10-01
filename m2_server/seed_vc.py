"""Seed-VC 高表现力离线变声：录音/音频文件 → （可选降噪）→ Seed-VC V2 零样本变声 → 导出 wav。

与 offline_vc.py（RVC 实时音色路径）并列：RVC 保音高但易压平韵律；Seed-VC V2 在换音色
的同时保留（V1）/ 转换（--convert-style）源音频的语气、节奏、情绪——补 RVC 缺的「表达力」。

接口：
    POST /api/seedvc/run      上传源音频 + 目标参考（voicebank 音色 或 上传参考音）+ 表达力旋钮，后台转换
    GET  /api/seedvc/status   轮询进度与结果

设计：
    - 推理在 seed_vc_repo/ 下用主 .venv 子进程跑 inference_v2.py（与生产 torch 同一环境，
      权重已缓存在 seed_vc_repo/checkpoints 与 HF 缓存，无需重下）。
    - 目标音色二选一：target_voice_id（复用 voicebank 的 reference.wav，零样本无需训练）
      或上传 target 参考音频。
    - 同一时刻只允许一个转换任务，且实时变声/级联运行中会拒绝（避免抢 GPU）。
"""

import contextlib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import config as cfg
import session_out
from common import MAX_UPLOAD_BYTES, find_ffmpeg, voice_ref
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

LOG = logging.getLogger(__name__)


def _live_running() -> bool:
    """实时变声是否在运行。延迟 import：`rvc_live` 属可关插件 sound.rvc-live，
    模块级引用会让「关掉」失效并把本模块拴在它的加载成败上（步 1 防的
    「一个 import 失败拖垮一片」的变种）。失败按未运行处理 —— 互斥放行。"""
    try:
        from rvc_live import _live_proc_alive
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[seedvc] 无法确认实时变声状态（%s），按未运行处理", exc)
        return False
    return bool(_live_proc_alive())


def _cascade_running() -> bool:
    """级联变声是否在运行。延迟 import：`cascade` 与 rvc_live 同属可关的
    sound.rvc-live，模块级引用同样会把被关掉/坏掉的能力重新拉进来。"""
    try:
        from cascade import _cascade_alive
    except Exception as exc:  # noqa: BLE001
        LOG.warning("[seedvc] 无法确认级联状态（%s），按未运行处理", exc)
        return False
    return bool(_cascade_alive())

# 产物一律落会话目录（退出即删），路径每次现读 —— 不许在这里早绑定 cfg.OUTPUTS_DIR。
SEEDVC_REPO = cfg.ROOT / "seed_vc_repo"

# ★ 两套**互不相通**的入口，是两条不同的模型链（2026-09-28 实测查清）：
#   v2 —— ASTRAL v2：config 两处 `f0_condition: false`，`convert_voice_with_streaming()`
#         签名里根本没有 f0 → **结构上只能念白**（用户听感原话「像读出来的」）。
#   f0 —— 带 `--f0-condition` / `--semi-tone-shift`，配 UViT base f0 44k 权重 + RMVPE，
#         才是歌声正解（音高偏差 +0.10 半音 vs v2 的 −15.90 半音）。
# `inference.py` 只认 `--inference-cfg-rate`，**没有** `--similarity-cfg-rate` /
# `--top-p` / `--temperature` / `--cfm-checkpoint-path`；两者参数名不通用，必须分开拼。
SEEDVC_INFER_V2 = SEEDVC_REPO / "inference_v2.py"
SEEDVC_INFER_F0 = SEEDVC_REPO / "inference.py"
#: 旧引用（既有代码/文档/测试里提到 `SEEDVC_INFER`）默认仍指 v2，行为不变。
SEEDVC_INFER = SEEDVC_INFER_V2
SEEDVC_VENV_PY = cfg.ROOT / ".venv" / "Scripts" / "python.exe"

#: 唱歌模式（f0 链路）预设 —— **A 档**：2026-09-27 用户盲听 A~E 五档后选定
#: （A = 音色最高的一档；E 音高最准但音色最低，被排除）。
#: 来源：`experiments/seedvc_shift_sweep.py` + `docs/翻唱歌声路线-实验快照-2026-09-28.md`。
#: ⚠️ 这几个旋钮任何一个掉了都**不会报错**，只会静默退化成念白/降质，所以集中在这里
#: 由 `test_seedvc_f0.py` 钉死，别散到调用点。
SINGING_PRESET: dict = {
    "diffusion_steps": 80,      # 50→80 是「免费收益」：音高不动、音色全线提升
    "inference_cfg_rate": 1.2,
    "auto_f0_adjust": True,     # 把源 F0 平移到参考音音域（音色的证据所在）
    "semi_tone_shift": 11,      # ★ A 档
    "length_adjust": 1.0,
}

# 走国内 HF 镜像下载/加载权重（首次已缓存，后续直接用）
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

# 目标音色 → 微调 run 目录（不存在该目录或目录里无 CFM_*.pth 时静默回落零样本）。
# 2026-09-05：kangaroo 用 73 条自录切片（video_260828_110637 + video_260828_105338）微调 CFM 100 步，
# 像度 CAM++ 0.71 → 0.806、漏源更低、F0 表达力更强（对照 experiments/seedvc_ft_eval.py）。
SEEDVC_FT_RUNS: dict[str, Path] = {
    "kangaroo": SEEDVC_REPO / "runs" / "kangaroo_ft_100",
}
SEEDVC_FT_MAX_RUNS = 3  # 自定义微调最多支持 N 个音色，避免误填膨胀
if len(SEEDVC_FT_RUNS) > SEEDVC_FT_MAX_RUNS:
    # 配置超过上限时仅警告不阻断：提示裁剪多余音色，避免微调目录莫名膨胀
    LOG.warning(
        "SEEDVC_FT_RUNS 共 %d 条，超过上限 %d，请裁剪音色数",
        len(SEEDVC_FT_RUNS),
        SEEDVC_FT_MAX_RUNS,
    )


def _ft_ckpt(voice_id: str) -> Path | None:
    """查目标音色对应的最新微调 CFM 检查点；无则返回 None（零样本）。"""
    run_dir = SEEDVC_FT_RUNS.get(voice_id)
    if not run_dir or not run_dir.is_dir():
        return None
    ckpts = sorted(run_dir.glob("CFM_*.pth"))
    return ckpts[-1] if ckpts else None


def resolve_singing_params(
    *,
    diffusion_steps: int | None = None,
    inference_cfg_rate: float | None = None,
    auto_f0_adjust: bool | None = None,
    semi_tone_shift: int | None = None,
    length_adjust: float | None = None,
) -> dict:
    """唱歌模式的最终参数：**None = 用 A 档标定值**，显式值原样透传。

    为什么要有这一层：这 4 个旋钮都是「不传就静默走另一条分支」的类型
    （掉 `auto_f0_adjust` → 音高被压平；掉 `semi_tone_shift` → 降八度；
    步数回落到 10 → 音色明显糊）。散在各调用点迟早漂移，所以集中一处解析，
    再由 `test_seedvc_f0.py` 把默认值钉死。
    """
    out = dict(SINGING_PRESET)
    for key, val in (
        ("diffusion_steps", diffusion_steps),
        ("inference_cfg_rate", inference_cfg_rate),
        ("auto_f0_adjust", auto_f0_adjust),
        ("semi_tone_shift", semi_tone_shift),
        ("length_adjust", length_adjust),
    ):
        if val is not None:
            out[key] = val
    return out


router = APIRouter(prefix="/api")

SEEDVC_STATE: dict = {
    "running": False,
    "status": "idle",  # idle | running | done | error
    "message": "",
    "target": "",
    "url": "",
    "duration_s": 0.0,
    "error": "",
}
_seedvc_lock = threading.Lock()


@router.post("/seedvc/run")
async def seedvc_run(
    file: UploadFile = File(...),
    target: UploadFile = File(None),
    target_voice_id: str = Form(""),
    mode: str = Form("expressive"),
    convert_style: bool = Form(False),
    similarity_cfg_rate: float = Form(0.5),
    top_p: float = Form(0.9),
    temperature: float = Form(1.0),
    diffusion_steps: int | None = Form(None),
    length_adjust: float | None = Form(None),
    denoise: bool = Form(False),
    semi_tone_shift: int | None = Form(None),
    auto_f0_adjust: bool | None = Form(None),
    inference_cfg_rate: float | None = Form(None),
):
    """提交 Seed-VC 变声任务。

    target_voice_id 与上传 target 参考音频二选一；convert_style=True 开启情绪/口音转换。

    `mode` 决定走哪条**互不相通**的链路（见 `build_cmd` 的表格）：
      - `expressive`（默认）→ `inference_v2.py`，说话/旁白的表达力转换，行为与改动前一致；
      - `singing` → f0 版 `inference.py`，**唱歌**用。此时 `semi_tone_shift` /
        `auto_f0_adjust` / `inference_cfg_rate` / `diffusion_steps` 生效，
        不传就取 `SINGING_PRESET`（A 档标定值），v2 专属旋钮（similarity_cfg_rate 等）忽略。
    """
    if mode not in ("expressive", "singing"):
        raise HTTPException(status_code=400, detail=f"未知 mode：{mode}（可选 expressive / singing）")
    live_entry = SEEDVC_INFER_F0 if mode == "singing" else SEEDVC_INFER_V2
    with _seedvc_lock:
        if SEEDVC_STATE["running"]:
            raise HTTPException(status_code=409, detail="已有转换任务在跑，请稍候")
        if not target_voice_id and (target is None or not target.filename):
            raise HTTPException(status_code=400, detail="请选择 voicebank 音色或上传目标参考音频")
        if not SEEDVC_VENV_PY.exists():
            raise HTTPException(status_code=500, detail="Seed-VC 运行环境缺失（主 .venv）")
        if not live_entry.exists():
            raise HTTPException(
                status_code=500, detail=f"Seed-VC 推理脚本缺失（seed_vc_repo/{live_entry.name}）"
            )
        if _live_running():
            raise HTTPException(
                status_code=409, detail="实时变声正在运行，请先停止后再转换（避免争抢显卡）"
            )
        if _cascade_running():
            raise HTTPException(
                status_code=409, detail="级联变声正在运行，请先停止后再转换（避免争抢显卡）"
            )

        # 解析目标参考音频
        target_label = target_voice_id or "uploaded"
        if target_voice_id:
            ref_path, _ = voice_ref(target_voice_id)  # 合法性/存在性校验，缺失抛 400/404
        else:
            ref_path = None  # 上传态：字节在路由内读完（见下），worker 内落盘

        SEEDVC_STATE.update(
            running=True,
            status="running",
            message="已提交",
            target=target_label,
            url="",
            duration_s=0.0,
            error="",
        )

    stamp = int(time.time() * 1000)
    raw_path = session_out.new_path(
        f"seedvc_src_{stamp}", Path(file.filename or "a.wav").suffix or ".wav"
    )
    if (file.size or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换"
        )
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换"
        )
    raw_path.write_bytes(raw)

    # ★ 上传的参考音必须在**路由内**读全：FastAPI 0.141 起在端点返回后自动关闭表单
    #   文件句柄（routing.py 的 file_stack.push_async_callback(body.close)），
    #   后台线程里再读 target.file 就是「I/O operation on closed file」——
    #   端到端测试实测抓到（此前从未有测试走到这条分支）。主文件上面的 await read()
    #   一直是这个口径，参考音此前漏了同样的处理。
    tgt_bytes = b""
    if ref_path is None:
        tgt_bytes = await target.read()
        if len(tgt_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"参考音频过大：>{MAX_UPLOAD_BYTES // (1024 * 1024)}MB 拒绝转换",
            )

    threading.Thread(
        target=_seedvc_worker,
        kwargs={
            "raw_path": raw_path,
            "target_bytes": tgt_bytes,
            "target_suffix": (Path(target.filename or "a.wav").suffix or ".wav")
            if target is not None
            else ".wav",
            "ref_path": ref_path,
            "target_label": target_label,
            "mode": mode,
            "convert_style": convert_style,
            "similarity_cfg_rate": similarity_cfg_rate,
            "top_p": top_p,
            "temperature": temperature,
            "diffusion_steps": diffusion_steps,
            "length_adjust": length_adjust,
            "denoise": denoise,
            "stamp": stamp,
            "semi_tone_shift": semi_tone_shift,
            "auto_f0_adjust": auto_f0_adjust,
            "inference_cfg_rate": inference_cfg_rate,
        },
        daemon=True,
    ).start()
    return {"ok": True, "target": target_label, "mode": mode}


def _preprocess(src: Path, dst: Path, denoise: bool) -> None:
    """统一转 16k 单声道 wav；denoise 时加 afftdn 降噪。"""
    af = "afftdn=nf=-25," if denoise else ""
    cmd = [
        find_ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-af",
        af + "aresample=16000",
        "-ac",
        "1",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 预处理失败: {r.stderr.strip()[:1500]}")


def build_cmd(
    in_src: Path,
    in_tgt: Path,
    out_dir: Path,
    *,
    convert_style: bool = False,
    similarity_cfg_rate: float = 0.5,
    top_p: float = 0.9,
    temperature: float = 1.0,
    diffusion_steps: int = 10,
    length_adjust: float = 1.0,
    cfm_checkpoint_path: Path | None = None,
    f0_condition: bool = False,
    auto_f0_adjust: bool = False,
    semi_tone_shift: int = 0,
    inference_cfg_rate: float = 0.7,
) -> list[str]:
    """拼一次 Seed-VC 子进程命令行（**纯函数**，便于把两条分支的旗标钉死）。

    `f0_condition=True` 走 **f0 版 `inference.py`**（歌声链路）；否则走 `inference_v2.py`
    （现役表达力链路，行为与本次改动之前完全一致）。两条链参数名**不通用**：

    | | v2（念白/表达力） | f0（歌声） |
    |---|---|---|
    | 入口 | `inference_v2.py` | `inference.py` |
    | 相似度/理解 cfg | `--similarity-cfg-rate` | `--inference-cfg-rate` |
    | f0 旋钮 | 无 | `--f0-condition` / `--auto-f0-adjust` / `--semi-tone-shift` |
    | 微调 CFM | `--cfm-checkpoint-path` | **不支持**（f0 版的 `--checkpoint` 是 f0 DiT 权重） |

    ⚠️ f0 分支里 `--f0-condition` 是**恒定 True**（否则根本不该进这条分支），
    而 `--auto-f0-adjust` 由调用方决定 —— 缺了它音高会被压平，而进程**不会报错**，
    只会安静地给出一段念白。`test_seedvc_f0.py` 守这两条。
    """
    if f0_condition and cfm_checkpoint_path is not None:
        # 静默忽略等于「用户以为在用自己的微调音色、其实跑的是底模」—— 宁可显式拒绝。
        raise ValueError(
            "f0 歌声链路不支持 cfm_checkpoint_path（该参数只存在于 inference_v2.py）"
        )
    if f0_condition:
        return [
            str(SEEDVC_VENV_PY),
            str(SEEDVC_INFER_F0),
            "--source", str(in_src),
            "--target", str(in_tgt),
            "--output", str(out_dir),
            "--diffusion-steps", str(diffusion_steps),
            "--inference-cfg-rate", str(inference_cfg_rate),
            "--length-adjust", str(length_adjust),
            "--f0-condition", "True",
            "--auto-f0-adjust", "True" if auto_f0_adjust else "False",
            "--semi-tone-shift", str(semi_tone_shift),
            "--fp16", "True",
        ]
    cmd = [
        str(SEEDVC_VENV_PY),
        str(SEEDVC_INFER_V2),
        "--source", str(in_src),
        "--target", str(in_tgt),
        "--output", str(out_dir),
        "--diffusion-steps", str(diffusion_steps),
        "--convert-style", "true" if convert_style else "false",
        "--similarity-cfg-rate", str(similarity_cfg_rate),
        "--top-p", str(top_p),
        "--temperature", str(temperature),
        "--length-adjust", str(length_adjust),
    ]
    if cfm_checkpoint_path is not None:
        cmd += ["--cfm-checkpoint-path", str(cfm_checkpoint_path)]
    return cmd


def run_conversion(
    in_src: Path,
    in_tgt: Path,
    out_dir: Path,
    *,
    convert_style: bool = False,
    similarity_cfg_rate: float = 0.5,
    top_p: float = 0.9,
    temperature: float = 1.0,
    diffusion_steps: int = 10,
    length_adjust: float = 1.0,
    cfm_checkpoint_path: Path | None = None,
    f0_condition: bool = False,
    auto_f0_adjust: bool = False,
    semi_tone_shift: int = 0,
    inference_cfg_rate: float = 0.7,
) -> Path:
    """跑一次 Seed-VC 子进程，返回生成的 wav 路径（调用方负责搬移/改名）。

    供本模块 /seedvc、offline_vc（RVC 后处理补情绪）与 ab_chain 复用。
    权重已缓存在 seed_vc_repo/checkpoints 与 HF 缓存，单次约几十秒。
    cfm_checkpoint_path 非空时用自定义微调 CFM 权重（替代零样本底模，**仅 v2 链路**）。
    f0_condition=True 时走 f0 版 `inference.py`（歌声链路），此时 v2 专属参数被忽略
    —— 该传什么由 `resolve_singing_params()` 决定，别在这里重设默认值。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(
        in_src,
        in_tgt,
        out_dir,
        convert_style=convert_style,
        similarity_cfg_rate=similarity_cfg_rate,
        top_p=top_p,
        temperature=temperature,
        diffusion_steps=diffusion_steps,
        length_adjust=length_adjust,
        cfm_checkpoint_path=cfm_checkpoint_path,
        f0_condition=f0_condition,
        auto_f0_adjust=auto_f0_adjust,
        semi_tone_shift=semi_tone_shift,
        inference_cfg_rate=inference_cfg_rate,
    )
    env = dict(os.environ)
    env["HF_ENDPOINT"] = HF_ENDPOINT
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    if f0_condition:
        # f0 链路权重（820MB DiT + RMVPE 181MB + 44kHz BigVGAN 489MB）已全部就位；
        # 不置离线的话每次启动都要联网 HEAD 探测，会白卡几十秒。
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=1800,
        encoding="utf-8",
        errors="replace",
        cwd=str(SEEDVC_REPO),
        env=env,
    )
    wavs = list(out_dir.glob("*.wav"))
    if r.returncode != 0 or not wavs:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError("Seed-VC 推理失败: " + " | ".join(tail)[-500:])
    return wavs[0]


def _seedvc_worker(
    raw_path: Path,
    target_bytes: bytes = b"",
    target_suffix: str = ".wav",
    ref_path: Path | None = None,
    target_label: str = "",
    convert_style: bool = False,
    similarity_cfg_rate: float = 0.5,
    top_p: float = 0.9,
    temperature: float = 1.0,
    diffusion_steps: int | None = None,
    length_adjust: float | None = None,
    denoise: bool = False,
    stamp: int = 0,
    cfm_checkpoint_path: Path | None = None,
    mode: str = "expressive",
    semi_tone_shift: int | None = None,
    auto_f0_adjust: bool | None = None,
    inference_cfg_rate: float | None = None,
):
    import soundfile as sf

    in_src = session_out.new_path(f"seedvc_in_src_{stamp}")
    in_tgt = session_out.new_path(f"seedvc_in_tgt_{stamp}")
    out_dir = session_out.new_path(f"seedvc_tmp_{stamp}", suffix="")
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = session_out.new_path(f"seedvc_{stamp}")
    try:
        SEEDVC_STATE.update(message="音频预处理中…")
        _preprocess(raw_path, in_src, denoise)

        # 目标参考：voicebank 直接复用；上传则落盘后预处理
        # （字节已由路由读完传入 —— 线程里不许碰 UploadFile，句柄已关）
        if ref_path is not None and ref_path.exists():
            tgt_for_vc = ref_path
        else:
            tgt_raw = session_out.new_path(f"seedvc_tgt_{stamp}", target_suffix)
            tgt_raw.write_bytes(target_bytes)
            _preprocess(tgt_raw, in_tgt, False)
            tgt_for_vc = in_tgt

        if mode == "singing":
            # ★ 唱歌链路：f0 版 inference.py。旋钮一律经 resolve_singing_params ——
            #   它的 None→A 档语义是这条链路「不传也能得到标定结果」的唯一保证。
            sp = resolve_singing_params(
                diffusion_steps=diffusion_steps,
                inference_cfg_rate=inference_cfg_rate,
                auto_f0_adjust=auto_f0_adjust,
                semi_tone_shift=semi_tone_shift,
                length_adjust=length_adjust,
            )
            SEEDVC_STATE.update(
                message=(
                    f"Seed-VC 歌声转换中…（f0 链路 · shift {sp['semi_tone_shift']:+d} · "
                    f"{sp['diffusion_steps']} 步 · 约 1~3 分钟）"
                )
            )
            produced = run_conversion(
                in_src,
                tgt_for_vc,
                out_dir,
                f0_condition=True,
                auto_f0_adjust=sp["auto_f0_adjust"],
                semi_tone_shift=sp["semi_tone_shift"],
                inference_cfg_rate=sp["inference_cfg_rate"],
                diffusion_steps=sp["diffusion_steps"],
                length_adjust=sp["length_adjust"],
            )
        else:
            SEEDVC_STATE.update(message="Seed-VC 推理中…（权重已缓存，约几十秒）")
            produced = run_conversion(
                in_src,
                tgt_for_vc,
                out_dir,
                convert_style=convert_style,
                similarity_cfg_rate=similarity_cfg_rate,
                top_p=top_p,
                temperature=temperature,
                # v2 分支的默认值：表单不传时回到改动前的 10 步 / 1.0 倍速
                diffusion_steps=10 if diffusion_steps is None else diffusion_steps,
                length_adjust=1.0 if length_adjust is None else length_adjust,
                cfm_checkpoint_path=cfm_checkpoint_path,
            )
        import shutil

        shutil.move(str(produced), str(final_path))

        d, sr = sf.read(str(final_path))
        duration_s = round(len(d) / sr, 1)
        # 不登记历史：产物只是本会话的试听结果，用户点「保存」时才进作品库
        # （默认不记，保存才留 —— 见 session_out 模块注释）。
        SEEDVC_STATE.update(
            running=False,
            status="done",
            message="完成",
            url=f"/api/media/outputs/{session_out.rel_url(final_path.name)}",
            duration_s=duration_s,
            error="",
        )
    except Exception as e:
        SEEDVC_STATE.update(running=False, status="error", message="", error=str(e))
    finally:
        for p in (raw_path, in_src, in_tgt):
            with contextlib.suppress(Exception):
                p.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            out_dir.rmdir()


@router.get("/seedvc/status")
def seedvc_status():
    return SEEDVC_STATE
