"""生成内置肤装包 assets/pet-skins/furina/（芙宁娜 MIT 素材，随安装包分发）。

把 web/electron/pet/svg/*.webp（横向 spritesheet，帧 150×150）整理成
标准皮肤包结构：skin.json + 每状态一个 sheet + preview.png + LICENSE。
皮肤包会随桌面端(web/electron/pet)一起分发；后端启动时物化到 outputs/pet-skins。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # tools/ -> m2_server/ -> D:\变声
SRC = ROOT / "web" / "electron" / "pet" / "svg"
DST = ROOT / "m2_server" / "assets" / "pet-skins" / "furina"

# 状态 → (源文件, 帧数, 周期秒)
STATES = {
    "idle": ("idle.webp", 37, 3.0),
    "listen": ("idle-look.webp", 37, 3.0),
    "think": ("thinking.webp", 37, 3.0),
    "play": ("conducting.webp", 22, 1.8),
    "build": ("building.webp", 16, 1.4),
    "error": ("error.webp", 37, 3.0),
}


def find_ffmpeg():
    import sys as s

    s.path.insert(0, str(ROOT / "m2_server"))
    from common import find_ffmpeg as _ff

    return _ff()


def main():
    DST.mkdir(parents=True, exist_ok=True)
    states = {}
    for state, (fname, frames, dur) in STATES.items():
        src = SRC / fname
        if not src.exists():
            sys.exit(f"缺素材: {src}")
        shutil.copy2(src, DST / f"{state}.webp")
        states[state] = {"sheet": f"{state}.webp", "frames": frames, "dur": dur}
    skin = {
        "id": "furina",
        "name": "芙宁娜（水神）",
        "category": "二次元",
        "license": "MIT",
        "attribution": "Ice-teapop/desktop-pet（原创 SVG, MIT）",
        "frameW": 150,
        "frameH": 150,
        "states": states,
    }
    (DST / "skin.json").write_text(json.dumps(skin, ensure_ascii=False, indent=2), "utf-8")
    (DST / "LICENSE").write_text(
        "来源: Ice-teapop/desktop-pet（原创 SVG）\n许可: MIT\n\n"
        "本皮肤素材来自第三方开源项目，按 MIT 许可使用与再分发。\n"
        "随应用分发时保留本文件与 skin.json 中的 attribution/license 字段。\n",
        "utf-8",
    )
    # 预览图：取 idle strip 首帧 150×150
    subprocess.run(
        [
            find_ffmpeg(),
            "-y",
            "-i",
            str(DST / "idle.webp"),
            "-vf",
            "crop=150:150:0:0",
            "-frames:v",
            "1",
            str(DST / "preview.png"),
        ],
        capture_output=True,
    )
    print(f"OK: {DST} 已生成 {len(states)} 状态 + skin.json + preview.png")


if __name__ == "__main__":
    main()
