"""素材 → 切片 → 质检 → 建音色 → 训练集 无人值守流水线（自动化④）。

与各页面/接口的分工：

    前端素材页   人看着跑，逐步点，出问题人判断
    本脚本       无人值守：一条命令把「新素材 → 质检合格的音色/训练集」跑到底，
                 退出码即结论，可直接进计划任务/CI 冒烟。

链路（全部复用 8000 服务的现有接口，不重造逻辑）：

    1) 素材就绪检查        GET /raw_videos
    2) 流水线（提轨→去BGM→切片）POST /pipeline/run → 轮询 /pipeline/status
    3) 说话人分离（可选）    POST /clips/diarize?file=<素材>
    4) 切片质检             POST /clips/qc?file=<素材>  → A/B/C/D 落盘缓存
    5) 建音色（自动优选）    POST /voicebank?voice_id=..&auto=1&target_s=..&enhance=..
    6) 训练集导出           POST /rvc/dataset/generate + /export（TTS 语料路线）
       --skip-tts-corpus    跳过 TTS 语料，仅导出**真实 A/B 切片**作训练集
                            （袋鼠铁律 4：RVC 是 voice-to-voice，不需要文字标注）
    7) （可选）触发训练     POST /ft/train（--train，无人值守默认不自动开训）

设计约定：
  - 不动 m2_server 代码：只编排现有接口；接口挂了=流水线失败，退出码非零。
  - 每阶段产物都有断言（切片数、A/B 可用条数、picked 数、导出数），
    任何一条不满足即失败——无人值守最忌「跑完了但结果是空的」。
  - --dry-run 只做第 1 步并打印将执行的动作清单，零写入。
  - 报告落盘 outputs/auto_pipeline.json，便于历史对比与计划任务留存。

用法（主 .venv，8000 服务已在跑）：

    .venv/Scripts/python.exe tools/auto_pipeline.py                       # 全部素材
    .venv/Scripts/python.exe tools/auto_pipeline.py --file xxx.mp4        # 指定素材
    .venv/Scripts/python.exe tools/auto_pipeline.py --voice-id kangaroo   # 指定音色 ID
    .venv/Scripts/python.exe tools/auto_pipeline.py --dry-run             # 只看会做什么
    .venv/Scripts/python.exe tools/auto_pipeline.py --skip-tts-corpus     # 只导出真实切片

前置：服务已启动（桌面端开着即可）；有素材在 media/raw_videos/；
demucs 首跑需联网下模型。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import config as cfg

# 与 tools/cascade_acceptance.py 同口径
API = "http://127.0.0.1:8000/api"
REPORT = ROOT / "outputs" / "auto_pipeline.json"

# 轮询预算：demucs 首跑要下模型，长素材去 BGM 可能十几分钟
PIPELINE_TIMEOUT_S = 3600        # 流水线单轮上限
TTS_CORPUS_TIMEOUT_S = 1200      # 20 句语料生成上限
TRAIN_TIMEOUT_S = 7200           # 微调训练等待上限
POLL_INTERVAL_S = 5.0
MAX_POLLS = 720


class PipelineError(RuntimeError):
    """任何阶段断言失败都收敛到这里——退出码 1，报告里带阶段名。"""


def _get(path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(API + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(path: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        API + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post_form(path: str, fields: dict, timeout: int = 600) -> dict:
    """voicebank 建库走 form（clips=name1&clips=name2&auto=1...）。"""
    data = urllib.parse.urlencode(fields, doseq=True).encode()
    req = urllib.request.Request(
        API + path, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _say(msg: str) -> None:
    print(msg, flush=True)


def _log(stage: str, msg: str) -> None:
    _say(f"[{stage}] {msg}")


def _require(cond: bool, stage: str, msg: str) -> None:
    if not cond:
        raise PipelineError(f"[{stage}] {msg}")


# ---------------- 各阶段 ----------------

def list_materials(client_get, wanted: list[str] | None) -> list[str]:
    """要处理的素材清单：指定 --file 时逐一校验存在；否则取全部素材。"""
    data = client_get("/raw_videos")
    names = [v["name"] for v in data.get("videos", [])]
    if wanted:
        missing = [w for w in wanted if w not in names]
        if missing:
            raise PipelineError(f"素材不存在于 media/raw_videos/：{missing}")
        return list(wanted)
    return names


def wait_pipeline(poll, deadline_s: int) -> dict:
    """轮询 /pipeline/status 至 done/error/cancelled；进度变化才打印，不刷屏。"""
    last = ""
    polls = 0
    t0 = time.monotonic()
    while True:
        st = poll()
        status = st.get("status")
        if status == "done":
            return st
        if status in ("error", "cancelled"):
            raise PipelineError(
                f"流水线失败：{st.get('error') or st.get('message') or status}")
        if time.monotonic() - t0 > deadline_s:
            raise PipelineError(f"流水线超时（>{deadline_s}s）")
        msg = st.get("message", "")
        if msg and msg != last:
            _log("pipeline", f"{st.get('percent', 0):>3}%  {msg}")
            last = msg
        polls += 1
        if polls > MAX_POLLS:
            raise PipelineError(f"轮询次数超限（{MAX_POLLS} 次）")
        time.sleep(POLL_INTERVAL_S)


def stage_pipeline(client_get, client_post, names: list[str],
                   timeout_s: int) -> dict:
    """阶段 2：跑 提轨→去BGM→切片，轮询至终态，断言切出切片。

    client_post(path, payload) —— pipeline/run 的 file 参数走 JSON body。
    """
    _log("pipeline", f"启动流水线：{len(names)} 个素材 {names}")
    r = client_post("/pipeline/run", {"file": names})
    _require(r.get("started"), "pipeline", f"流水线未启动：{r}")
    st = wait_pipeline(lambda: client_get("/pipeline/status"),
                       deadline_s=timeout_s)
    _require(st.get("clips", 0) > 0, "pipeline",
             f"流水线跑完但切出 0 条切片：{st.get('message', '')}")
    _log("pipeline", f"完成：{st.get('clips')} 条切片")
    return st


def stage_qc(client_post, names: list[str], with_spk: bool) -> dict:
    """阶段 3+4：说话人分离（可选）→ 切片质检，返回等级累计分布。"""
    grades_total = {"A": 0, "B": 0, "C": 0, "D": 0}
    for name in names:
        q = urllib.parse.quote(name)
        if with_spk:
            try:
                client_post(f"/clips/diarize?file={q}", {})
            except Exception as exc:  # noqa: BLE001 —— 分离失败不阻断：质检降级为无 spk 维度
                _log("qc", f"说话人分离失败（降级为无声纹维度继续）：{exc}")
        r = client_post(f"/clips/qc?file={q}&spk=true&force=true", {})
        g = r.get("grades") or {}
        for k in grades_total:
            grades_total[k] += int(g.get(k) or 0)
        _log("qc", f"{name}: A {g.get('A', 0)} / B {g.get('B', 0)} / "
                   f"C {g.get('C', 0)} / D {g.get('D', 0)}")
    return grades_total


def stage_voicebank(client_post_form, voice_id: str, target_s: float,
                    enhance: bool) -> dict:
    """阶段 5：建音色（自动优选 A/B 切片拼 reference.wav）。"""
    fields: dict = {"auto": "1", "target_s": str(int(target_s))}
    if enhance:
        fields["enhance"] = "1"
    r = client_post_form(f"/voicebank?voice_id={urllib.parse.quote(voice_id)}",
                         fields)
    _require(r.get("ok") and r.get("picked", 0) > 0, "voicebank",
             f"建库未选中任何切片：{r}")
    _log("voicebank", f"建库完成：{voice_id}，picked {r.get('picked')} 条，"
                      f"reference.wav {r.get('duration_s')}s")
    return r


def stage_tts_corpus(client_post, client_get, voice_id: str) -> dict:
    """阶段 6a：TTS 语料生成（20 句）→ 导出 RVC 整合包训练集。"""
    r = client_post("/rvc/dataset/generate", {"voice_id": voice_id})
    _require(r.get("started"), "tts_corpus", f"语料生成未启动：{r}")
    t0 = time.monotonic()
    while True:
        st = client_get("/rvc/dataset/status")
        if not st.get("running"):
            break
        if time.monotonic() - t0 > TTS_CORPUS_TIMEOUT_S:
            raise PipelineError(f"TTS 语料生成超时（>{TTS_CORPUS_TIMEOUT_S}s）")
        time.sleep(3)
    _require(not st.get("error"), "tts_corpus", f"语料生成失败：{st.get('error')}")
    _require(st.get("done", 0) >= st.get("total", 20), "tts_corpus",
             f"语料生成不完整：done={st.get('done')}/{st.get('total')}")
    _log("tts_corpus", f"生成 {st.get('done')} 句")
    r2 = client_post(f"/rvc/dataset/export?voice_id={urllib.parse.quote(voice_id)}")
    _require(r2.get("copied", 0) > 0, "tts_corpus", f"导出失败：{r2}")
    _log("tts_corpus", f"导出 {r2.get('copied')} 条 → {r2.get('dest')}")
    return {"generated": st.get("done"), "exported": r2.get("copied"),
            "dest": r2.get("dest")}


def stage_export_real_clips(client_get, exp_name: str) -> dict:
    """阶段 6b（--skip-tts-corpus）：把真实 A/B 切片拷成 RVC 训练集目录。

    袋鼠铁律 4：RVC 重训是 voice-to-voice，不需要 TTS 语料。从 /clips 拿质检
    等级，把 A/B 级切片复制到 dataset_raw/<exp>/（与 rvc_dataset_api.export
    的 dataset_raw 口径一致，放 RVC 整合包可选训练集位置）。
    """
    import shutil

    data = client_get("/clips")
    clips = data.get("clips") or []
    keep = [c for c in clips
            if (c.get("qc") or {}).get("grade") in ("A", "B")]
    _require(keep, "export_real", "没有 A/B 级切片可导出（先跑质检）")

    dest_root = cfg.RVC_EXPORT_DIR.parent / exp_name
    dest_root.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in keep:
        src = cfg.MEDIA_DIR / "clips" / f"{c['name']}.wav"
        if not src.exists():
            continue
        shutil.copy2(src, dest_root / f"{c['name']}.wav")
        n += 1
    _require(n > 0, "export_real", "A/B 切片一个都没拷成（文件被占用?）")
    _log("export_real", f"导出 {n} 条 A/B 切片 → {dest_root}")
    return {"exported": n, "dest": str(dest_root)}


def stage_train(client_get, client_post, voice_id: str, epochs: int) -> dict:
    """阶段 7（--train）：触发微调训练并等待终态（ft/train 是后台线程）。"""
    r = client_post(f"/ft/train?voice_id={urllib.parse.quote(voice_id)}"
                    f"&epochs={int(epochs)}")
    _require(r.get("ok"), "train", f"训练未启动：{r}")
    _log("train", f"已启动 epochs={epochs}；qc_warning={r.get('qc_warning') or '无'}")
    t0 = time.monotonic()
    while True:
        st = client_get(f"/ft/train_status?voice_id={urllib.parse.quote(voice_id)}")
        if st.get("done"):
            _log("train", "训练完成（[DONE]）")
            return {"started": True, "done": True, "rc": st.get("rc")}
        if st.get("rc") is not None and not st.get("running"):
            raw_rc = st.get("rc")
            rc = int(raw_rc) if raw_rc is not None else 1   # 0 是合法成功值，不能用 `or 1`
            if rc == 0:
                return {"started": True, "done": True, "rc": 0}
            raise PipelineError(f"训练失败 rc={rc}：详见 ft 状态页日志")
        if time.monotonic() - t0 > TRAIN_TIMEOUT_S:
            raise PipelineError(f"训练等待超时（>{TRAIN_TIMEOUT_S}s）")
        time.sleep(30)


def run_pipeline_tool(args: dict, client_get, client_post, client_post_form) -> dict:
    """主流程（client 依赖注入，便于单测）。失败抛 PipelineError。"""
    report: dict = {"materials": [], "stages": {}}

    names = list_materials(client_get, args.get("file"))
    report["materials"] = names
    _require(names, "materials",
             "没有待处理素材：media/raw_videos/ 为空或 --file 未匹配")
    _log("materials", f"{len(names)} 个素材：{names}")

    if args.get("dry_run"):
        dest_exp = args.get("exp_name") or args.get("voice_id") or "rvc_dataset"
        plan = [
            "POST /pipeline/run + 轮询 /pipeline/status",
            "POST /clips/diarize + /clips/qc（每素材）",
            (f"POST /voicebank?voice_id={args.get('voice_id')}&auto=1"
             f"&target_s={args.get('target_s')}"),
            ("POST /rvc/dataset/generate + /export（TTS 语料路线）"
             if not args.get("skip_tts_corpus")
             else f"导出真实 A/B 切片 → dataset_raw/{dest_exp}/"),
            (f"POST /ft/train?voice_id={args.get('voice_id')}"
             if args.get("train") else None),
        ]
        _say("dry-run 计划：")
        for item in plan:
            if item:
                _say(f"  - {item}")
        report["dry_run"] = True
        return report

    report["stages"]["pipeline"] = stage_pipeline(
        client_get, client_post, names, args.get("timeout_s") or PIPELINE_TIMEOUT_S)

    grades = stage_qc(client_post, names, args.get("with_spk", True))
    report["stages"]["qc"] = grades
    _require(grades["A"] + grades["B"] > 0, "qc",
             f"无可用切片（A/B=0）：{grades}——素材人声不清晰或说话人太杂")

    vid = args.get("voice_id") or "auto_voice"
    report["stages"]["voicebank"] = stage_voicebank(
        client_post_form, vid, args.get("target_s") or 30.0,
        args.get("enhance", False))

    if args.get("skip_tts_corpus"):
        report["stages"]["export_real"] = stage_export_real_clips(
            client_get, args.get("exp_name") or vid)
    else:
        report["stages"]["tts_corpus"] = stage_tts_corpus(
            client_post, client_get, vid)

    if args.get("train"):
        report["stages"]["train"] = stage_train(
            client_get, client_post, vid, args.get("epochs") or 12)

    return report


def main(argv: list[str] | None = None) -> int:
    # 输出编码不是装饰：Windows 下 stdout 被重定向（管道/文件）时按 ANSI(cp936) 编码，
    # 而流水线进度里的 `✓`/`⚠️` 在 GBK 之外 → UnicodeEncodeError + 进度断掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="素材→切片→质检→建音色→训练集 无人值守流水线")
    ap.add_argument("--file", action="append", default=[],
                    help="素材名（可重复）；缺省处理 raw_videos 全部")
    ap.add_argument("--voice-id", default="auto_voice",
                    help="音色 ID（默认 auto_voice）")
    ap.add_argument("--target-s", type=float, default=30.0,
                    help="建库目标秒数（默认 30；精细档建议 60）")
    ap.add_argument("--enhance", action="store_true",
                    help="建库时用 DeepFilterNet 增强切片（更慢更干净）")
    ap.add_argument("--no-spk", dest="with_spk", action="store_false",
                    default=True, help="跳过说话人分离（更快，质检缺声纹维度）")
    ap.add_argument("--skip-tts-corpus", action="store_true",
                    help="只导出真实 A/B 切片，不生成 TTS 语料（袋鼠路线）")
    ap.add_argument("--exp-name", default="",
                    help="真实切片导出的实验名（默认随 voice_id）")
    ap.add_argument("--train", action="store_true",
                    help="最后触发微调训练并等待完成")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--timeout-s", type=int, default=PIPELINE_TIMEOUT_S,
                    help="流水线阶段超时（默认 3600s）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")
    ns = ap.parse_args(argv)
    args = vars(ns)

    t0 = time.time()
    report: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "api": API,
                    "args": {k: v for k, v in args.items()
                             if v not in (None, "", False)}}
    try:
        result = run_pipeline_tool(args, _get, _post, _post_form)
        report.update(result)
        report["ok"] = True
        report["elapsed_s"] = round(time.time() - t0, 1)
        _say(f"\n✅ 全链路完成（{report['elapsed_s']}s）")
        _say(json.dumps(report["stages"], ensure_ascii=False, indent=2))
        rc = 0
    except PipelineError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        report["elapsed_s"] = round(time.time() - t0, 1)
        print(f"\n❌ 失败：{exc}", file=sys.stderr)
        rc = 1
    except urllib.error.URLError as exc:
        report["ok"] = False
        report["error"] = f"服务不可达（8000 没开?）：{exc}"
        report["elapsed_s"] = round(time.time() - t0, 1)
        print(f"\n❌ 失败：服务不可达：{exc}", file=sys.stderr)
        rc = 1

    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        _say(f"报告：{REPORT}")
    except OSError as exc:
        _say(f"报告落盘失败（不影响退出码）：{exc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
