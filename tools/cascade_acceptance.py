"""级联变声验收脚本（方案第九节验收标准的自动化部分）。

用法（跑在主 .venv，与 8000 服务同环境）：
    # 1) 文件模式全链路：RTF / 内容回听比对 / 声纹相似度（无需对着麦克风说话）
    python tools/cascade_acceptance.py file <输入wav> [--ref <参考wav>]

    # 2) 实时延迟统计：先在前端启动级联，对着麦克风说 10+ 句话，
    #    脚本轮询 status 收集每块滞后，Ctrl+C 结束输出统计
    python tools/cascade_acceptance.py latency

    # 3) 声卡还原检查：停止级联后跑，验证备份文件消失且默认设备回到真麦
    python tools/cascade_acceptance.py restore

人工项（无法自动化）：播放连续性听感、回环啸叫实测（status 的
input_device 含 CABLE 即回环，脚本会带 code-review 式检查）。
"""
import argparse
import json
import subprocess
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

API = "http://127.0.0.1:8000/api"
WORKER = "http://127.0.0.1:8001"
# 与 config.py 同一约定：整合包位置用 VM_RVC_ROOT 覆盖（默认是作者本机的 D:/RVC）
RVC_ROOT = Path(os.environ.get("VM_RVC_ROOT", "D:/RVC"))
RVC_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
STREAM_PY = ROOT / "m2_server" / "cascade_stream.py"


def _post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _emb(path: str):
    return _post_json(WORKER + "/emb", {"path": path}, timeout=300).get("emb")


def _audio_status() -> dict:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "m2_server" / "audio_config.ps1"), "-action", "status"],
        capture_output=True, text=True, timeout=60, encoding="utf-8",
        errors="replace").stdout
    return json.loads(out)


def check_file(args):
    """文件模式全链路：RTF + 内容回听 + 声纹。"""
    src = Path(args.file)
    ref = Path(args.ref)
    if not src.exists():
        sys.exit(f"输入文件不存在: {src}")
    if not ref.exists():
        sys.exit(f"参考音频不存在: {ref}")

    print("[1/4] 文件模式全链路（VAD→ASR→TTS，跑在 RVC venv）…")
    r = subprocess.run(
        [str(RVC_PY), str(STREAM_PY), "--file", str(src),
         "--ref-audio", str(ref)],
        capture_output=True, text=True, timeout=1200, encoding="utf-8",
        errors="replace", cwd=str(ROOT))
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        sys.exit("级联子进程执行失败")

    out = ROOT / "outputs" / "cascade_file_out.wav"
    if not out.exists():
        sys.exit("没有产出 outputs/cascade_file_out.wav")

    print("[2/4] RTF 检查（处理快于播放才算流式可行，阈值 >1.0）…")
    rtf = None
    for line in r.stdout.splitlines():
        if "整体 RTF" in line:
            rtf = float(line.split("RTF")[1].strip().rstrip("x"))
    ok_rtf = rtf is not None and rtf > 1.0
    print(f"  整体 RTF = {rtf}x  ->  {'PASS' if ok_rtf else 'FAIL'}")

    print("[3/4] 内容正确性（ASR 回听合成音频，与链路输入文本对比）…")
    tr_out = _post_json(WORKER + "/transcribe",
                        {"path": str(out), "vad_filter": False}, timeout=300)
    tr_in = _post_json(WORKER + "/transcribe",
                       {"path": str(src), "vad_filter": False}, timeout=300)
    print(f"  源转写: {tr_in.get('text')}")
    print(f"  输出转写: {tr_out.get('text')}")
    a = set((tr_in.get("text") or "").replace("，", "").replace(",", ""))
    b = set((tr_out.get("text") or "").replace("，", "").replace(",", ""))
    overlap = len(a & b) / max(len(a | b), 1)
    ok_text = overlap >= 0.6  # whisper 逐次转写有噪声，字符重合度作近似判据
    print(f"  字符重合度 {overlap:.2f}  ->  {'PASS' if ok_text else 'FAIL'}")

    print("[4/4] 音色正确性（声纹相似度，阈值 ≥0.95；原版基线 0.983）…")
    import numpy as np
    e1, e2 = _emb(str(out)), _emb(str(ref))
    if not e1 or not e2:
        sys.exit("声纹提取失败（worker /emb 返回空）")
    v1, v2 = np.asarray(e1), np.asarray(e2)
    sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
    ok_sim = sim >= 0.95
    print(f"  相似度 {sim:.4f}  ->  {'PASS' if ok_sim else 'FAIL'}")

    print("\n=== 文件模式验收汇总 ===")
    print(f"RTF>1.0: {'PASS' if ok_rtf else 'FAIL'} | "
          f"内容一致: {'PASS' if ok_text else 'FAIL'} | "
          f"声纹≥0.95: {'PASS' if ok_sim else 'FAIL'}")


def check_latency():
    """轮询 /api/cascade/status 收集块滞后（口径：说完→开始听到，阈值 ≤2.5s）。"""
    print("轮询级联状态中（对麦克风说 10 句以上，Ctrl+C 结束）…")
    seen = []
    last = None
    try:
        while True:
            try:
                s = _get_json(API + "/cascade/status")
                lat = s.get("last_latency_s") or 0
                if lat and lat != last:
                    seen.append(lat)
                    last = lat
                    print(f"  块 {len(seen)}: 滞后 {lat}s (avg {s.get('avg_latency_s')}s, "
                          f"队列 {s.get('queued_s')}s, 丢弃 {s.get('dropped')})")
            except Exception:
                pass
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    if not seen:
        sys.exit("没有采集到任何块，先在前端启动级联并说话")
    seen.sort()
    n = len(seen)
    median = seen[n // 2]
    print(f"\n=== 实时延迟验收（{n} 块）===")
    print(f"中位数 {median}s | 最小 {seen[0]}s | 最大 {seen[-1]}s")
    print(f"阈值 ≤2.5s: {'PASS' if median <= 2.5 else 'FAIL'}")
    # 累积检测：前后半段中位数对比（增长 >0.5s 说明处理跟不上说话）
    if n >= 6:
        half = n // 2
        early = sorted(seen[:half])[half // 2]
        late = sorted(seen[half:])[(n - half) // 2]
        grew = late - early
        print(f"前半段中位 {early}s vs 后半段中位 {late}s（增长 {grew:+.2f}s）")
        print(f"滞后不累积（|增长|≤0.5s）: {'PASS' if abs(grew) <= 0.5 else 'FAIL'}")


def check_restore():
    """停止级联后的声卡还原与回环静态检查。"""
    print("[1/2] 声卡还原检查…")
    st = _audio_status()
    cap = st.get("capture", {})
    cap_names = set(str(v) for v in cap.values())
    backup = Path.home() / "AppData" / "Local" / "rvc_audio_backup.txt"
    ok_restore = (not backup.exists()) and cap_names and \
        all("CABLE" not in c for c in cap_names)
    print(f"  当前录音设备: {cap_names}")
    print(f"  备份文件存在: {backup.exists()}  ->  {'PASS' if ok_restore else 'FAIL'}")

    print("[2/2] 回环静态检查（级联运行时输入必须是真麦）…")
    try:
        s = _get_json(API + "/cascade/status")
        inp = s.get("input_device", "")
        ok_loop = (not s.get("running")) or ("cable" not in inp.lower())
        print(f"  级联输入设备: {inp or '(未运行)'}  ->  {'PASS' if ok_loop else 'FAIL'}")
    except Exception as e:
        print(f"  状态查询失败（8000 服务未启动?）: {e}  ->  SKIP")


def main():
    p = argparse.ArgumentParser(description="级联变声验收")
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("file", help="文件模式全链路验收")
    pf.add_argument("file")
    pf.add_argument("--ref", default=str(ROOT / "tts_models" / "ref" / "meituan_rat_002.wav"))
    sub.add_parser("latency", help="实时延迟统计（需先启动级联并说话）")
    sub.add_parser("restore", help="声卡还原检查")
    args = p.parse_args()
    if args.cmd == "file":
        check_file(args)
    elif args.cmd == "latency":
        check_latency()
    else:
        check_restore()


if __name__ == "__main__":
    main()
