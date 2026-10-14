# -*- coding: utf-8 -*-
"""把两个 seed 的 checkpoint 扫描结果并排打出来（结论表，供文档引用）。"""
import json
import pathlib

PAIRS = [
    ("media/covers/ckpt/metrics.json", "seed 1234"),
    ("media/covers/ckpt_s4321/metrics.json", "seed 4321"),
]


def load(p):
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))["files"]


def main() -> int:
    runs = [(label, load(p)) for p, label in PAIRS]
    keys = [k for k in runs[0][1] if not k.startswith("source")]
    labels = [label for label, _ in runs]

    print("== 对你的声纹（CAM++，越高越像你）==")
    hdr = "权重".ljust(34) + "".join(lab.ljust(14) for lab in labels)
    print(hdr)
    for k in keys:
        row = k.ljust(34)
        for _, f in runs:
            v = f.get(k, {}).get("secs_vs_your_ref")
            row += (f"{v:.4f}" if v is not None else "—").ljust(14)
        print(row)

    print()
    print("== 对原唱这一段（越高=越像掺了原唱）==")
    print(hdr)
    for k in keys:
        row = k.ljust(34)
        for _, f in runs:
            v = f.get(k, {}).get("secs_vs_orig_singer")
            row += (f"{v:.4f}" if v is not None else "—").ljust(14)
        print(row)

    print()
    print("== 质心 Hz（越高越亮）==")
    print(hdr)
    for k in keys:
        row = k.ljust(34)
        for _, f in runs:
            v = f.get(k, {}).get("centroid_hz")
            row += (str(v) if v is not None else "—").ljust(14)
        print(row)

    print()
    print("== 4-8k 相对 dB（越低越闷）==")
    print(hdr)
    for k in keys:
        row = k.ljust(34)
        for _, f in runs:
            v = f.get(k, {}).get("band_db", {}).get("4-8k")
            row += (f"{v}" if v is not None else "—").ljust(14)
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
