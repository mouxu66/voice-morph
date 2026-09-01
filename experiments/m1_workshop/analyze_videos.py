#!/usr/bin/env python3
# 快速"看"视频：抽音 + ASR 识别语言/内容 + 估算 BGM/人声占比
import os, subprocess, sys, json, tempfile
import numpy as np, soundfile as sf

VIDEOS = [
    ("D:/Users/mouxu/Downloads/老板的胆子真是肥嘟嘟的_哔哩哔哩_bilibili.mp4", "老板的胆子..."),
    ("D:/Users/mouxu/Downloads/神人の外卖（5）_哔哩哔哩_bilibili-20260827-ne4zlou33r.mp4", "神人の外卖5"),
    ("D:/Users/mouxu/Downloads/video_260828_110637.mp4", "video_110637"),
    ("D:/Users/mouxu/Downloads/《美团袋鼠视频合集》_哔哩哔哩_bilibili.mp4", "袋鼠合集"),
    ("D:/Users/mouxu/Downloads/video_260828_105338.mp4", "video_105338"),
]
TMP = "D:/变声/m1_workshop/_tmp_audio"
os.makedirs(TMP, exist_ok=True)

def extract(vp):
    out = os.path.join(TMP, os.path.basename(vp).rsplit(".",1)[0] + ".wav")
    if not os.path.exists(out):
        subprocess.run(["ffmpeg","-y","-i",vp,"-vn","-acodec","pcm_s16le","-ar","16000","-ac","1",out],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out

def bgm_heuristic(wav):
    a,sr = sf.read(wav)
    if a.ndim>1: a=a[:,0]
    a=a.astype(float)
    # 低频频段(<=200Hz)能量占比：BGM/伴奏常有持续低频床
    from numpy.fft import rfft, rfftfreq
    n=len(a); win=np.hanning(min(n, sr*2))
    seg=min(n, sr*2)
    x=a[:seg]; x=x-np.mean(x)
    sp=np.abs(rfft(x*win)); fr=rfftfreq(len(x),1/sr)
    low=(fr<=200).sum()
    low_e=sp[:low].sum(); tot=sp.sum()
    low_ratio=low_e/tot if tot>0 else 0
    rms=np.sqrt(np.mean(a**2))
    return low_ratio, rms

def main():
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    for vp, tag in VIDEOS:
        wav = extract(vp)
        a,sr = sf.read(wav); dur=len(a)/sr
        segs, info = model.transcribe(wav, beam_size=5, vad_filter=True)
        segs=list(segs)
        speech=sum((s.end-s.start) for s in segs)
        text="".join(s.text for s in segs)
        low_ratio, rms = bgm_heuristic(wav)
        lang=info.language
        sr_ratio = speech/dur if dur>0 else 0
        # 关键词
        kw=[]
        for k in ["袋鼠","美团","外卖"," kangaroo","kangaroo"]:
            if k.lower() in text.lower(): kw.append(k)
        print(f"### {tag} | {dur:.0f}s | lang={lang} | speech_ratio={sr_ratio:.2f} | lowfreq(BGM?)={low_ratio:.2f} | rms={rms:.3f}")
        print(f"    文本片段: {text[:120].strip()}")
        if kw: print(f"    关键词: {kw}")
        print()

if __name__=="__main__":
    main()
