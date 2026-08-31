"""级联变声子进程：真麦采集 → VAD 分块 → ASR(8001) → TTS(8001 fast) → CABLE Input 播放。

运行环境：D:\\RVC\\.venv\\Scripts\\python.exe（唯一有 sounddevice 的环境，
与 offline_vc / rvc_live 同一约定）。由 cascade.py 拉起，状态写入
outputs/cascade_state.json 供 /api/cascade/status 读取。

为什么走文字中转：RVC 转换保留源说话人的口音与发音习惯；级联链路
ASR→文字→TTS 后，输出只含目标音色，与源发音完全解耦。

分块策略（宁长勿短，延迟与韵律的折中）：
    - VAD 静音判停：连续静音 ≥ silence_ms（默认 400ms）认为一句结束
    - 上限强制切：单块达 chunk_max_s（默认 6s）强制切出，防长句撑大延迟
    - 下限丢弃：有效语音 < min_chunk_s（默认 0.5s）直接丢弃（喘息/噪声）
    分块必须在 ASR 之前独立完成：whisper 的 vad_filter 是内部过滤器，
    会把短片段整段吞掉，调用方无法区分「没说话」和「被过滤器吃掉」。

文件模式测试口：--file <wav> 跳过采集与播放，把文件按同一状态机分块跑
全链路并拼接输出，用于命令行验证（不用对着麦克风喊）。

坑（均已处理，详见实施方案）：
    #2 输入固定真麦（绝不采 CABLE Output，否则回环啸叫）
    #4 启动时先用极短文本跑一次 /tts 预热 prompt 缓存
    #5 worker 已把 GPU 推理放线程池，这里串行逐块请求（坑 10）
    #9 采集 16k（ASR），播放 24k（与 TTS 输出一致，不重采样）
"""
import argparse
import io
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

try:
    import webrtcvad
except ImportError:  # RVC venv 未装 webrtcvad 时回退能量 VAD
    webrtcvad = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SR_IN = 16000    # 采集/ASR 采样率
SR_OUT = 24000   # TTS 输出/播放采样率
FRAME_MS = 30    # VAD 帧长（webrtcvad 仅支持 10/20/30ms）
FRAME_N = SR_IN * FRAME_MS // 1000

INPUT_KEYWORD = os.environ.get("VM_LIVE_INPUT_DEVICE", "麦克风阵列")
OUTPUT_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")

STATE = {
    "running": True, "stage": "init", "pid": os.getpid(),
    "last_text": "", "last_asr_s": 0.0, "last_tts_s": 0.0, "last_audio_s": 0.0,
    "last_fast": None, "chunks": 0, "dropped": 0,
    "avg_latency_s": 0.0, "last_latency_s": 0.0, "queued_s": 0.0,
    # 分阶段耗时统计（最近 30 块）：识别/合成的 avg 与 p95
    "avg_asr_s": 0.0, "p95_asr_s": 0.0, "avg_tts_s": 0.0, "p95_tts_s": 0.0,
    "input_device": "", "output_device": "",
    "error": "", "updated_at": "",
}
_latencies: deque = deque(maxlen=20)
_asr_hist: deque = deque(maxlen=30)
_tts_hist: deque = deque(maxlen=30)


def _p95(xs) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return float(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))])


def _update_stage_stats(which: str, val: float):
    """记录一次分阶段耗时并刷新 STATE 的 avg/p95（which: asr|tts）。"""
    hist = _asr_hist if which == "asr" else _tts_hist
    hist.append(val)
    STATE[f"avg_{which}_s"] = round(sum(hist) / len(hist), 2)
    STATE[f"p95_{which}_s"] = round(_p95(hist), 2)


def _write_state():
    STATE["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE["queued_s"] = round(_PLAYER.queued() / SR_OUT, 2) if _PLAYER else 0.0
    tmp = str(_STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, _STATE_PATH)


def _set_stage(stage: str):
    if STATE["stage"] != stage:
        STATE["stage"] = stage
        print(f"[cascade] stage -> {stage}", flush=True)
        _write_state()


_STATE_PATH = Path("cascade_state.json")
_PLAYER = None


# ---------------- VAD 分块状态机 ----------------

class Chunker:
    """双判据切句状态机：静音判停 / 上限强制切；IDLE 时保留 pre-roll 防吃句首。"""

    def __init__(self, silence_ms=400, chunk_max_s=6.0, min_chunk_s=0.5,
                 onset_ms=90, pre_roll_ms=240):
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.max_frames = int(chunk_max_s * 1000) // FRAME_MS
        self.min_voiced = int(min_chunk_s * 1000) // FRAME_MS
        self.onset_frames = max(1, onset_ms // FRAME_MS)
        self._pre = deque(maxlen=pre_roll_ms // FRAME_MS)
        self.reset()

    def reset(self):
        self._frames: list[np.ndarray] = []
        self._voiced = 0
        self._onset = 0
        self._silence = 0
        self._last_voiced_ts = 0.0
        self._speaking = False

    def feed(self, frame: np.ndarray, is_speech: bool, ts: float):
        """喂一帧，返回 (pcm, speech_end_ts) 或 None。

        speech_end_ts = 最后一个有声帧的时刻（延迟统计的起点，
        比「判停时刻」早约 silence_ms，更贴近用户真实说完的时刻）。
        """
        if is_speech:
            self._last_voiced_ts = ts
        if not self._speaking:
            self._pre.append(frame)
            self._onset = self._onset + 1 if is_speech else 0
            if self._onset >= self.onset_frames:
                self._speaking = True
                self._frames = list(self._pre)
                self._voiced = self._onset
                self._silence = 0
                self._pre.clear()
            return None
        self._frames.append(frame)
        if is_speech:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        hit_max = len(self._frames) >= self.max_frames
        if self._silence >= self.silence_frames or hit_max:
            pcm = np.concatenate(self._frames)
            end_ts = self._last_voiced_ts
            keep = not hit_max  # 强制切的块没有尾静音，直接进入下一块
            self.reset()
            if not keep:
                self._speaking = True  # 语音仍在继续，从头累计下一块
                self._frames = [frame]
                self._voiced = 1 if is_speech else 0
                self._last_voiced_ts = ts if is_speech else end_ts
            return pcm, end_ts
        return None


class EnergyVad:
    """webrtcvad 缺失时的回退：自适应噪声底 + RMS 阈值（近距说话足够稳）。"""

    def __init__(self, ratio=6.0):
        self.ratio = ratio
        self.floor = None

    def is_speech(self, pcm: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        if self.floor is None:
            self.floor = max(rms, 1e-3)
        hit = rms > self.floor * self.ratio
        if not hit:
            self.floor = 0.9 * self.floor + 0.1 * rms
        return hit


def make_vad_detector(aggressiveness: int):
    if webrtcvad is not None:
        vad = webrtcvad.Vad(aggressiveness)
        return lambda pcm: vad.is_speech(pcm.tobytes(), SR_IN)
    print("[cascade] webrtcvad 不可用，回退能量 VAD", flush=True)
    ev = EnergyVad()
    return ev.is_speech


# ---------------- 播放器：24k 队列 + 预缓冲 + 交叉淡化 ----------------

class Player:
    """输出流回调从队列拉数据；不足补零。enqueue 与队尾做 crossfade 防咔哒。

    prime：累计入队 ≥ prime_s 才开始出声，吸收 ASR/TTS 抖动导致的断流。
    每个块登记 (起始位置, 结束位置)，callback 越过起始位置时记录开始播放
    时刻——延迟统计终点（口径：说完 → 开始听到）。
    """

    def __init__(self, device, fade_ms=25, prime_s=0.5):
        self.fade_n = int(SR_OUT * fade_ms / 1000)
        self.prime_n = int(SR_OUT * prime_s)
        self._lock = threading.Lock()
        self._buf: deque[np.ndarray] = deque()
        self._queued = 0
        self._started = False
        self._position = 0
        self._marks: list[tuple[int, int, int]] = []  # (start_pos, end_pos, seq)
        self._starts: dict[int, float] = {}
        self._seq = 0
        self._stream = sd.OutputStream(
            device=device, samplerate=SR_OUT, channels=1, dtype="float32",
            blocksize=int(SR_OUT * 0.04), callback=self._cb)

    def start(self):
        self._stream.start()

    def stop(self):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def _cb(self, outdata, frames, time_info, status):
        with self._lock:
            if not self._started:
                if self._queued >= self.prime_n:
                    self._started = True
                else:
                    outdata.fill(0)
                    return
            out = np.zeros(frames, dtype=np.float32)
            pos, need = 0, frames
            while need > 0 and self._buf:
                head = self._buf[0]
                take = min(need, len(head))
                out[pos:pos + take] = head[:take]
                pos += take
                need -= take
                if take == len(head):
                    self._buf.popleft()
                else:
                    self._buf[0] = head[take:]
            self._queued -= frames - need
            self._position += frames  # 含补零：播放位置始终按输出帧推进
            outdata[:, 0] = out
            now = time.time()
            keep = []
            for start_pos, end_pos, seq in self._marks:
                if self._position >= start_pos and seq not in self._starts:
                    self._starts[seq] = now
                if self._position < end_pos:
                    keep.append((start_pos, end_pos, seq))
            self._marks = keep

    def enqueue(self, audio: np.ndarray) -> int:
        with self._lock:
            if len(self._buf) and self.fade_n > 0 and len(audio) > self.fade_n:
                tail = self._buf[-1]
                n = min(self.fade_n, len(tail))
                w = np.linspace(0.0, 1.0, n, dtype=np.float32)
                mixed = tail[-n:] * (1.0 - w) + audio[:n] * w
                # 融合段长度不变（tail[:-n]+mixed == len(tail)），新块只入队 [n:] 之后
                self._buf[-1] = np.concatenate([tail[:-n], mixed])
                audio = audio[n:]
            start_pos = self._position + self._queued
            self._buf.append(np.ascontiguousarray(audio, dtype=np.float32))
            self._queued += len(audio)
            self._seq += 1
            seq = self._seq
            self._marks.append((start_pos, start_pos + len(audio), seq))
            return seq

    def pop_starts(self) -> dict[int, float]:
        """收割「各块开始播放时刻」（延迟统计终点：说完→开始听到）。"""
        with self._lock:
            starts, self._starts = self._starts, {}
            return starts

    def queued(self) -> int:
        with self._lock:
            return self._queued


# ---------------- worker 客户端（8001） ----------------

class Worker:
    def __init__(self, base="http://127.0.0.1:8001"):
        self.base = base

    def health(self, timeout=3.0) -> bool:
        try:
            r = requests.get(self.base + "/health", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def wait_ready(self, timeout_s=300.0):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.health():
                return
            time.sleep(2.0)
        raise RuntimeError("等待 8001 worker 就绪超时（模型加载失败？）")

    def asr(self, path: str) -> dict:
        # fast=True：短句用 beam_size=1 + 免时间戳，短块延迟约降一半，
        # 长句识别质量差异可忽略（级联块长 0.5~6s）
        r = requests.post(self.base + "/transcribe",
                          json={"path": path, "vad_filter": False, "fast": True},
                          timeout=120)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"ASR 失败: {data['error']}")
        return data

    def tts(self, text: str, ref_audio: str, ref_text: str,
            timeout=180) -> tuple[np.ndarray, int, bool]:
        r = requests.post(self.base + "/tts", timeout=timeout, json={
            "text": text, "language": "Chinese",
            "ref_audio": ref_audio, "ref_text": ref_text, "fast": True})
        if r.status_code != 200 or not r.content:
            raise RuntimeError(f"TTS 失败 HTTP {r.status_code}")
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        if audio.ndim > 1:
            audio = audio[:, 0]
        fast = r.headers.get("X-Fast-TTS") == "1"
        return audio, sr, fast


def _resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to:
        return x
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ---------------- 设备解析 ----------------

def find_device(keyword: str, is_input: bool) -> int:
    """按关键词在 MME 主机 API 下模糊匹配设备索引。"""
    apis = sd.query_hostapis()
    mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != mme:
            continue
        ch = d["max_input_channels"] if is_input else d["max_output_channels"]
        if ch > 0 and keyword.lower() in d["name"].lower():
            return i
    kind = "输入" if is_input else "输出"
    raise RuntimeError(f"找不到{kind}设备（关键词: {keyword}）")


def resolve_devices() -> tuple[int, int, str, str]:
    dev_in = find_device(INPUT_KEYWORD, is_input=True)
    dev_out = find_device(OUTPUT_KEYWORD, is_input=False)
    name_in = sd.query_devices(dev_in)["name"]
    # 坑 #2：输入绝不能是 CABLE，否则 CABLE Output→采集→合成→CABLE Input 回环啸叫
    if "cable" in name_in.lower():
        raise RuntimeError(f"输入设备解析到了 {name_in}，会造成回环啸叫，拒绝启动")
    name_out = sd.query_devices(dev_out)["name"]
    return dev_in, dev_out, name_in, name_out


# ---------------- 全链路处理 ----------------

class Cascade:
    def __init__(self, args, worker: Worker):
        self.args = args
        self.worker = worker
        self.tmp_wav = Path(args.out_dir) / "cascade_tmp_chunk.wav"
        self.fail_streak = 0
        self.pending_lat: dict[int, float] = {}
        self.out_chunks: list[np.ndarray] = []

    def warmup(self):
        """坑 #3/#4：等 worker 就绪后预热两件事——
        1) /transcribe 一次（whisper 懒加载，不预热则第一句话 ASR 多卡 ~3s）
        2) 极短文本跑一次 /tts 建 prompt 缓存与 CUDA Graph（捕获约 2~4s）"""
        _set_stage("warming")
        self.worker.wait_ready()
        t0 = time.time()
        self.tmp_wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(self.tmp_wav), np.zeros(SR_IN, dtype=np.float32), SR_IN,
                 subtype="PCM_16")
        self.worker.asr(str(self.tmp_wav))
        print(f"[cascade] ASR 预热完成 {time.time() - t0:.1f}s", flush=True)
        t0 = time.time()
        audio, sr, fast = self.worker.tts("好", self.args.ref_audio, self.args.ref_text)
        print(f"[cascade] TTS 预热完成 {time.time() - t0:.1f}s "
              f"(fast={fast}, {len(audio) / sr:.1f}s 音频已丢弃)", flush=True)

    def process(self, pcm: np.ndarray, speech_end_ts: float | None) -> None:
        global _PLAYER
        dur = len(pcm) / SR_IN
        voiced = self._count_voiced(pcm)
        if voiced < self.args.min_chunk_s:
            STATE["dropped"] += 1
            print(f"[cascade] 丢弃短块 {dur:.2f}s (voiced {voiced:.2f}s)", flush=True)
            return
        _set_stage("asr")
        t0 = time.time()
        sf.write(str(self.tmp_wav), pcm, SR_IN, subtype="PCM_16")
        tr = self.worker.asr(str(self.tmp_wav))
        asr_s = time.time() - t0
        _update_stage_stats("asr", asr_s)
        text = (tr.get("text") or "").strip()
        STATE.update(last_asr_s=round(asr_s, 2), last_text=text)
        if not text:
            STATE["dropped"] += 1
            print(f"[cascade] ASR 空文本，丢弃 {dur:.2f}s 块", flush=True)
            _set_stage("capturing")
            return
        _set_stage("tts")
        t0 = time.time()
        audio, sr, fast = self.worker.tts(text, self.args.ref_audio, self.args.ref_text)
        tts_s = time.time() - t0
        _update_stage_stats("tts", tts_s)
        if sr != SR_OUT:
            audio = _resample(audio, sr, SR_OUT)
        STATE.update(last_tts_s=round(tts_s, 2), last_fast=fast,
                     last_audio_s=round(len(audio) / SR_OUT, 2))
        print(f"[cascade] {dur:.1f}s -> 「{text}」 asr {asr_s:.2f}s tts {tts_s:.2f}s "
              f"fast={fast} -> {len(audio) / SR_OUT:.1f}s 音频", flush=True)
        self.fail_streak = 0
        if self.args.file:
            self.out_chunks.append(audio)
            STATE["chunks"] += 1
            return
        _set_stage("playing")
        seq = _PLAYER.enqueue(audio)
        if speech_end_ts:
            self.pending_lat[seq] = speech_end_ts
        STATE["chunks"] += 1

    def _count_voiced(self, pcm: np.ndarray) -> float:
        det = self.vad_det
        n = 0
        for i in range(0, len(pcm) - FRAME_N + 1, FRAME_N):
            if det(pcm[i:i + FRAME_N]):
                n += 1
        return n * FRAME_MS / 1000

    def collect_latency(self):
        """端到端滞后 = 块开始播放时刻 - 该块最后一个有声帧时刻。

        口径对齐验收标准（≤2.5s）：用户说完一句话到开始听到目标音色，
        即 判停静音 + ASR + TTS + (积压排队)。播放本身以正常语速流出，
        不算在滞后里；积压累积会在这个口径里自然暴露（越说越慢=滞后增长）。
        """
        if not _PLAYER:
            return
        starts = _PLAYER.pop_starts()
        if not starts:
            return
        for seq, start_ts in starts.items():
            end_ts = self.pending_lat.pop(seq, None)
            if end_ts is None:
                continue
            lat = start_ts - end_ts
            _latencies.append(lat)
            STATE["last_latency_s"] = round(lat, 2)
            STATE["avg_latency_s"] = round(sum(_latencies) / len(_latencies), 2)
            print(f"[cascade] 端到端滞后 {lat:.2f}s (avg {STATE['avg_latency_s']}s)",
                  flush=True)
        _write_state()

    def handle_error(self, exc: Exception) -> None:
        self.fail_streak += 1
        msg = f"{type(exc).__name__}: {exc}"
        STATE["error"] = msg
        print(f"[cascade] 块处理失败({self.fail_streak}): {msg}", flush=True)
        _write_state()
        if self.fail_streak >= 3:
            raise SystemExit(f"连续 {self.fail_streak} 次失败，级联退出: {msg}")

    def run_frames(self, frame_iter, vad_det):
        """统一主循环：frame_iter 产出 (pcm_frame, ts)，file 与 live 模式共用。"""
        self.vad_det = vad_det
        chunker = Chunker(silence_ms=self.args.silence_ms,
                          chunk_max_s=self.args.chunk_max_s,
                          min_chunk_s=self.args.min_chunk_s)
        _set_stage("capturing")
        for frame, ts in frame_iter:
            try:
                out = chunker.feed(frame, vad_det(frame), ts)
                self.collect_latency()
                if out is not None:
                    pcm, end_ts = out
                    try:
                        self.process(pcm, end_ts)
                        _set_stage("capturing")
                    except SystemExit:
                        raise
                    except Exception as e:
                        self.handle_error(e)
            except SystemExit:
                raise
        # 输入耗尽（file 模式 / 流关闭）：处理尾部残余
        if chunker._speaking and chunker._frames:
            pcm = np.concatenate(chunker._frames)
            try:
                self.process(pcm, chunker._last_voiced_ts)
            except SystemExit:
                raise
            except Exception as e:
                self.handle_error(e)


def run_live(args):
    global _PLAYER
    dev_in, dev_out, name_in, name_out = resolve_devices()
    STATE.update(input_device=name_in, output_device=name_out)
    print(f"[cascade] 输入: {name_in} | 输出: {name_out}", flush=True)
    worker = Worker(args.worker)
    cas = Cascade(args, worker)
    cas.warmup()

    _PLAYER = Player(dev_out, prime_s=args.prime_s)
    q: queue.Queue = queue.Queue()

    def in_cb(indata, frames, time_info, status):
        # PortAudio 回调线程只入队，绝不阻塞（处理在主循环串行进行）
        q.put(indata[:, 0].copy())

    with sd.InputStream(device=dev_in, samplerate=SR_IN, channels=1,
                        dtype="int16", blocksize=FRAME_N, callback=in_cb):
        _PLAYER.start()
        try:
            _live_main_loop(args, cas, q)
        finally:
            _PLAYER.stop()


def _live_main_loop(args, cas, q):
    def gen():
        while True:
            yield q.get().astype(np.float32) / 32768.0, time.time()

    # VAD 输入统一为 float32，EnergyVad/webrtcvad 内部转 bytes
    cas.run_frames(gen(), _make_det(args))


def _make_det(args):
    det = make_vad_detector(args.vad_level)
    return lambda pcm: det((pcm * 32767).astype(np.int16))


def run_file(args):
    """文件模式：整段 wav 按同一状态机分块跑 ASR→TTS，拼接输出（无实时播放）。"""
    data, sr = sf.read(args.file, dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]
    data = _resample(data, sr, SR_IN)
    pcm16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    worker = Worker(args.worker)
    cas = Cascade(args, worker)
    cas.warmup()
    STATE.update(input_device=f"file:{args.file}", output_device="(none)")

    def gen():
        n = len(pcm16) // FRAME_N
        for i in range(n):
            yield pcm16[i * FRAME_N:(i + 1) * FRAME_N].astype(np.float32) / 32768.0, \
                i * FRAME_MS / 1000.0

    t0 = time.time()
    cas.run_frames(gen(), _make_det(args))
    if not cas.out_chunks:
        print("[cascade] 文件模式：没有可合成内容", flush=True)
        return
    merged = cas.out_chunks[0]
    fade_n = int(SR_OUT * 0.025)
    for c in cas.out_chunks[1:]:
        n = min(fade_n, len(merged), len(c))
        w = np.linspace(0, 1, n, dtype=np.float32)
        merged = np.concatenate([merged[:-n], merged[-n:] * (1 - w) + c[:n] * w, c[n:]])
    out = Path(args.out_dir) / "cascade_file_out.wav"
    sf.write(str(out), merged, SR_OUT)
    spoke = len(pcm16) / SR_IN
    gen_s = len(merged) / SR_OUT
    wall = time.time() - t0
    print(f"[cascade] 文件模式完成: 输入 {spoke:.1f}s -> 合成 {gen_s:.1f}s, "
          f"总耗时 {wall:.1f}s, 整体 RTF {spoke / max(wall, 1e-6):.2f}x", flush=True)
    print(f"[cascade] 输出: {out}", flush=True)


def run_live_asr(args):
    """asr-only 模式：真麦 → VAD 分块 → ASR(8001) → 状态文件（无 TTS/播放/声卡操作）。

    供实时变声（RVC）期间挂桌宠实时字幕用：RVC 自己占真麦与 CABLE，本模式
    只共享采集真麦（WASAPI 共享模式允许多客户端），转写结果写独立状态文件，
    由 /api/rvc/live/status 合并返回。绝不碰声卡配置，绝不采 CABLE。
    """
    dev_in = find_device(INPUT_KEYWORD, is_input=True)
    name_in = sd.query_devices(dev_in)["name"]
    if "cable" in name_in.lower():
        raise RuntimeError(f"输入设备解析到了 {name_in}，会造成回环，拒绝启动")
    STATE.update(input_device=name_in, output_device="(asr-only)")
    print(f"[live-asr] 输入: {name_in}", flush=True)

    worker = Worker(args.worker)
    _set_stage("warming")
    worker.wait_ready()
    tmp_wav = Path(args.out_dir) / "live_asr_tmp.wav"
    tmp_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(tmp_wav), np.zeros(SR_IN, dtype=np.float32), SR_IN, subtype="PCM_16")
    worker.asr(str(tmp_wav))  # whisper 懒加载预热
    print("[live-asr] ASR 预热完成", flush=True)

    vad_det = _make_det(args)
    chunker = Chunker(silence_ms=args.silence_ms, chunk_max_s=args.chunk_max_s,
                      min_chunk_s=args.min_chunk_s)
    q: queue.Queue = queue.Queue()

    def in_cb(indata, frames, time_info, status):
        q.put(indata[:, 0].copy())

    def gen():
        while True:
            yield q.get().astype(np.float32) / 32768.0, time.time()

    def transcribe(pcm: np.ndarray):
        _set_stage("transcribing")
        t0 = time.time()
        sf.write(str(tmp_wav), pcm, SR_IN, subtype="PCM_16")
        tr = worker.asr(str(tmp_wav))
        text = (tr.get("text") or "").strip()
        STATE.update(last_asr_s=round(time.time() - t0, 2))
        if text:
            STATE.update(last_text=text, error="")
            STATE["chunks"] += 1
            print(f"[live-asr] 「{text}」 ({time.time() - t0:.2f}s)", flush=True)
        _write_state()
        _set_stage("capturing")

    _set_stage("capturing")
    with sd.InputStream(device=dev_in, samplerate=SR_IN, channels=1,
                        dtype="int16", blocksize=FRAME_N, callback=in_cb):
        for frame, ts in gen():
            out = chunker.feed(frame, vad_det(frame), ts)
            if out is not None:
                pcm, _ = out
                try:
                    transcribe(pcm)
                except SystemExit:
                    raise
                except Exception as e:
                    STATE["error"] = f"{type(e).__name__}: {e}"[:1500]
                    _write_state()
                    print(f"[live-asr] 转写失败: {e}", flush=True)


def main():
    p = argparse.ArgumentParser(description="级联变声子进程（录音→ASR→TTS）")
    p.add_argument("--ref-audio", default="D:/变声/tts_models/ref/meituan_rat_002.wav")
    p.add_argument("--ref-text", default="", help="参考文字稿；空则 x-vector 声纹模式")
    p.add_argument("--chunk-max-s", type=float, default=6.0)
    p.add_argument("--silence-ms", type=int, default=400)
    p.add_argument("--min-chunk-s", type=float, default=0.5)
    p.add_argument("--prime-s", type=float, default=0.5)
    p.add_argument("--vad-level", type=int, default=2, help="webrtcvad 灵敏度 0-3")
    p.add_argument("--worker", default="http://127.0.0.1:8001")
    p.add_argument("--state-path", default="")
    p.add_argument("--out-dir", default="")
    p.add_argument("--file", default="", help="文件模式：处理该 wav 而非麦克风")
    p.add_argument("--asr-only", action="store_true",
                   help="只转写不合成不播放（实时变声期间的桌宠字幕），无声卡操作")
    args = p.parse_args()

    base = Path(__file__).resolve().parent.parent
    args.out_dir = args.out_dir or str(base / "outputs")
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    global _STATE_PATH
    _STATE_PATH = Path(args.state_path) if args.state_path \
        else Path(args.out_dir) / "cascade_state.json"

    try:
        if args.asr_only:
            run_live_asr(args)
        elif args.file:
            run_file(args)
        else:
            run_live(args)
    except SystemExit as e:
        STATE.update(running=False, stage="error", error=str(e)[:1500])
        _write_state()
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        STATE.update(running=False, stage="error", error=f"{type(e).__name__}: {e}"[:1500])
        _write_state()
        raise
    finally:
        if _PLAYER:
            _PLAYER.stop()
        STATE["running"] = False
        _write_state()


if __name__ == "__main__":
    main()
