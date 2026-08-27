# -*- coding: utf-8 -*-
"""搬运新素材：Downloads 的两个文件 -> media/raw_videos；清理旧素材。结果写 move_out.txt。"""
import os
import shutil

SRC_DIR = r"C:\Users\mouxu\Downloads\我胆子就是肥嘟嘟的"
RAW = r"D:\变声\media\raw_videos"
CLIPS = r"D:\变声\media\clips"
OUT = r"D:\变声\tts_trial\move_out.txt"


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()
try:
    names = os.listdir(SRC_DIR)
    log("[src] " + repr(names))
    for n in names:
        src = os.path.join(SRC_DIR, n)
        dst = os.path.join(RAW, n)
        if os.path.exists(dst):
            os.remove(dst)
            log(f"[del-existing] {n}")
        shutil.move(src, dst)
        log(f"[moved] {n} ({os.path.getsize(dst)} bytes)")
    # 源文件夹已空则删除
    if not os.listdir(SRC_DIR):
        os.rmdir(SRC_DIR)
        log("[rmdir] Downloads source folder removed")
    # 清理被否决的旧素材：旧视频与 55 个旧切片
    removed = []
    for n in os.listdir(RAW):
        if "神人の外卖" in n:
            os.remove(os.path.join(RAW, n))
            removed.append(n)
    if os.path.isdir(CLIPS):
        for n in os.listdir(CLIPS):
            if n.endswith(".wav"):
                os.remove(os.path.join(CLIPS, n))
                removed.append("clip:" + n)
    log(f"[cleaned-old] {len(removed)} items removed")
    log("[end]")
except Exception as e:
    import traceback
    log(f"[FAIL] {e!r}")
    log(traceback.format_exc())
