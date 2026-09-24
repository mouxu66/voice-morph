"""打一个音效包（zip）并打印可直接粘进市场清单的条目。

音效包是**成套素材**的分发单位：装一次 / 卸一次（格式与理由见 `sfx_packs.py` 头注释）。
本脚本是它的**生产端** —— 应用只负责装，不负责造。

用法
----
    # ① 把自己录/收集的一目录 wav 打成包
    python tools/pack_sfx.py --from 我的音效/ --id mypack --name 我的音效 \
        --license "自录，仅自用" --author 我

    # ② 生成一份**演示包**（程序化合成，零第三方版权，用来验链路）
    python tools/pack_sfx.py --demo arcade -o outputs/sfx-packs/arcade.zip

    # ③ 打完之后：把 zip 传到你自己的托管位置（HF / 魔搭 / 仓库 raw），
    #    把打印出来的条目粘进清单 JSON，再让后端指向它（VM_SFX_PACK_INDEX）。

清单长这样（`{"packs": [...]}`，逐条校验见 `sfx_packs._index_entries`）：

    {
      "id": "arcade", "name": "街机音效", "license": "CC0-1.0", "author": "…",
      "url": "https://hf-mirror.com/<你的仓库>/resolve/main/sfx/arcade.zip",
      "sha256": "<脚本打印的那个>", "bytes": 123456, "downloads": 0
    }

为什么 `license` 必填
--------------------
包会被装到**别人**的机器上，而这行声明是那边唯一能看到的许可信息
（`tools/audit_licenses.py` 只管仓库内的资产，管不到运行时下载的东西）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "tools"))

import sfx_packs  # noqa: E402
import gen_sfx  # noqa: E402  —— 合成原语（_norm/_env/_lowpass/_highpass/_compress）复用同一份

SR = gen_sfx.SR


# --------------------------------------------------------------- 演示音效（街机风）

def _sweep(f0: float, f1: float, sec: float, attack: float = 0.005) -> np.ndarray:
    """扫频（激光/跳跃那种"登—"或"呜—"）：相位用频率的**累加**，不是 f(t)·t。"""
    n = int(SR * sec)
    f = np.linspace(f0, f1, n)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return gen_sfx._norm(np.sin(phase) * gen_sfx._env(n, attack, sec - attack))


def _blip(freq: float, sec: float) -> np.ndarray:
    n = int(SR * sec)
    t = np.arange(n) / SR
    return gen_sfx._norm(np.sin(2 * np.pi * freq * t) * gen_sfx._env(n, 0.002, sec - 0.002))


def _arp(freqs: list[float], each: float) -> np.ndarray:
    return gen_sfx._norm(np.concatenate([_blip(f, each) for f in freqs]))


def _noise_hit(sec: float = 0.18, cutoff: float = 900.0) -> np.ndarray:
    rng = np.random.default_rng(20260924)
    n = int(SR * sec)
    x = rng.standard_normal(n).astype(np.float32)
    x = gen_sfx._lowpass(x, cutoff) * gen_sfx._env(n, 0.001, sec - 0.001)
    return gen_sfx._norm(gen_sfx._compress(x, 8.0))


#: 演示包内容：名字 → (显示名, 生成函数, 标签)
DEMO: dict[str, tuple[str, object, list[str]]] = {
    "coin": ("金币", lambda: _arp([988.0, 1319.0], 0.06), ["游戏", "提示"]),
    "laser": ("激光", lambda: _sweep(2200.0, 320.0, 0.22), ["游戏"]),
    "jump": ("跳跃", lambda: _sweep(320.0, 1200.0, 0.16), ["游戏"]),
    "hit": ("击中", lambda: _noise_hit(), ["游戏"]),
    "powerup": ("升级", lambda: _arp([523.0, 659.0, 784.0, 1046.0], 0.08), ["游戏"]),
    "gameover": ("失败", lambda: _arp([523.0, 415.0, 330.0], 0.18), ["游戏"]),
}


def _write_demo(out_dir: Path) -> tuple[Path, dict[str, dict]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, dict] = {}
    for name, (label, fn, tags) in DEMO.items():
        data = fn()
        # 固定种子/确定性合成：同一份脚本每次吐出逐字节一致的包（重跑不改清单里的哈希）
        sf.write(str(out_dir / f"{name}.wav"), data, SR, subtype="PCM_16")
        meta[name] = {"name": label, "tags": tags}
    return out_dir, meta


# --------------------------------------------------------------- 打包


def _sample_meta_from_dir(src: Path) -> dict[str, dict]:
    """从目录里的 `manifest.json`（若有）取显示名/标签；没有就用文件名。"""
    m = src / "manifest.json"
    if not m.is_file():
        return {}
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def build(src: Path, pack_id: str, name: str, license_: str, author: str,
          desc: str, out: Path, *, meta: dict[str, dict] | None = None) -> tuple[Path, list[str]]:
    wavs = sorted(p for p in src.glob("*.wav") if p.is_file())
    if not wavs:
        raise SystemExit(f"{src} 下没有 .wav")
    if sfx_packs.clean_stem(pack_id) != pack_id:
        raise SystemExit(f"id 不合法（限 40 字符、只留常规字符）：{pack_id!r}")
    if not license_.strip():
        raise SystemExit("--license 必填：包会被装到别人机器上，那行声明是那边唯一的依据")
    declared = dict(meta or {})
    samples = {}
    for f in wavs:
        info = declared.get(f.stem) if isinstance(declared.get(f.stem), dict) else {}
        samples[f.stem] = {"name": info.get("name") or f.stem, "tags": info.get("tags") or []}
    manifest = {
        "name": name or pack_id,
        "license": license_,
        "samples": {k: v for k, v in samples.items() if v["name"] != k or v["tags"]},
    }
    if author:
        manifest["author"] = author
    if desc:
        manifest["description"] = desc

    out.parent.mkdir(parents=True, exist_ok=True)
    # 逐字节一致的 zip：固定时间戳/顺序，重跑不会让清单里的 sha256 变来变去
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for f in wavs:
            zi = zipfile.ZipInfo(f"{f.stem}.wav", date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, f.read_bytes())
    return out, sorted(samples)


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个音效包（zip）+ 打印市场清单条目")
    ap.add_argument("--from", dest="src", help="素材目录（*.wav）")
    ap.add_argument("--demo", metavar="ID", help="生成演示包（程序化合成，ID 作为包 id）")
    ap.add_argument("--id", help="包 id（默认取素材目录名）")
    ap.add_argument("--name", default="", help="显示名")
    ap.add_argument("--license", dest="license_", default="", help="许可声明（必填）")
    ap.add_argument("--author", default="", help="作者/来源")
    ap.add_argument("--desc", default="", help="一句话说明")
    ap.add_argument("--host", default="https://hf-mirror.com/<你的仓库>/resolve/main/sfx",
                    help="zip 上传后的所在目录（只用于打印清单条目里的 url）")
    ap.add_argument("-o", "--out", help="输出 zip 路径")
    args = ap.parse_args()

    if args.demo:
        pack_id = sfx_packs.clean_stem(args.demo) or ""
        work = ROOT / "outputs" / "sfx-packs" / f"{pack_id}-src"
        src, meta = _write_demo(work)
        name = args.name or "街机音效"
        license_ = args.license_ or "CC0-1.0（本仓库程序化合成，无第三方素材）"
    elif args.src:
        src = Path(args.src).resolve()
        if not src.is_dir():
            raise SystemExit(f"不是目录：{src}")
        pack_id = sfx_packs.clean_stem(args.id or src.name) or ""
        meta = _sample_meta_from_dir(src)
        name = args.name or pack_id
        license_ = args.license_
    else:
        ap.error("要给 --from <目录> 或 --demo <id>")
    if not pack_id:
        raise SystemExit("给不出合法的包 id")

    out = Path(args.out).resolve() if args.out else ROOT / "outputs" / "sfx-packs" / f"{pack_id}.zip"
    out, stems = build(src, pack_id, name, license_, args.author, args.desc, out, meta=meta)
    data = out.read_bytes()
    entry = {
        "id": pack_id,
        "name": name,
        "license": license_,
        "url": f"{args.host.rstrip('/')}/{out.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "downloads": 0,
    }
    if args.author:
        entry["author"] = args.author
    print(f"包 id    : {pack_id}")
    print(f"素材     : {len(stems)} 条（{'、'.join(stems)}）")
    print(f"输出     : {out}  {len(data) / 1024:.1f}KB")
    print("\n把它传到你自己的托管位置，再把下一条粘进清单的 packs 数组里：\n")
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
