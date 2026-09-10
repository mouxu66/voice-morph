# -*- coding: utf-8 -*-
"""微信播放常驻 worker（延迟优化，2026-09-10）。

为什么需要
----------
`_do_send` 以前每条语音都 `subprocess.run(RVC_VENV_PY, -c <inline play script>)`：
那个内联脚本一启动就要 import numpy / sounddevice / soundfile，冷导入 ~2.5-3s，
且**这段导入必然落在「点录音之前」**（否则会被微信录成语音开头的空白）。
这就是微信段 ~12s 固有开销里最大的一块。

本脚本做成常驻进程：启动只 import 一次（启动时由 warmup 预热），之后从 stdin
逐行收 `{"wav","lead","tail"}` 命令，播放时往 stdout 打 `{"type":"playing"}` /
`{"type":"done"}` / `{"type":"error",...}` 行协议。每条语音只付「推理+sd.play」，
不再付冷导入 —— 单条省 ~2.5-3s（与 RVC 常驻 worker 同一思路）。

行协议（stdout，每行一个 JSON，flush）：
    {"type":"ready"}                      启动就绪（import 已完成）
    {"type":"playing"}                    已开播（静音头开始播）
    {"type":"done"}                       播完
    {"type":"error","msg":"..."}          失败
stdin：每行一个 JSON 命令 `{"wav":"/abs/path","lead":0.8,"tail":0.3}`，EOF 退出。
"""
import json
import sys

import numpy as np
import sounddevice as sd
import soundfile as sf

KEYWORD = (sys.argv[1] if len(sys.argv) > 1 else "CABLE Input").lower()


def _resolve_device():
    apis = sd.query_hostapis()
    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] == mme and d["max_output_channels"] > 0 and KEYWORD in d["name"].lower():
            return i
    return None


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    idx = _resolve_device()
    if idx is None:
        _emit({"type": "error", "msg": f"device_not_found:{KEYWORD}"})
        return
    _emit({"type": "ready"})          # import 已付过，通知父进程
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
        except Exception:
            continue
        wav = cmd.get("wav")
        lead = float(cmd.get("lead", 0.8))
        tail = float(cmd.get("tail", 0.3))
        if not wav:
            _emit({"type": "error", "msg": "missing wav"})
            continue
        try:
            data, sr = sf.read(wav, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            ld = np.zeros(int(sr * max(0.0, lead)), dtype="float32")
            tl = np.zeros(int(sr * max(0.0, tail)), dtype="float32")
            _emit({"type": "playing"})
            sd.play(np.concatenate([ld, data, tl]), sr, device=idx)
            sd.wait()
            _emit({"type": "done"})
        except Exception as e:
            _emit({"type": "error", "msg": str(e)[:300]})


if __name__ == "__main__":
    main()
