# -*- coding: utf-8 -*-
"""盲听发放包：把 `listen/` 的材料打成一个**可发给听者、且不泄题**的自包含目录。

为什么需要这个模块
    `listen` 生成的 `playlist.txt` 里，每行是「盲号 + **真实样本路径**」——
    而真实路径长这样：`samples/SSB0033/30s/long.wav`。
    名字里同时写着**说话人**、**时长档**、**文本条件**。
    听者只要看一眼播放器的标题栏（或文件列表），条件就直接报给他了 ——
    **盲听不盲**，而且翻车时没人会报错，只会得到一组"看起来有差异"的漂亮数据。

    所以「把音频拷出来换个名字」不是整理文件，是**盲法的实现部分**：
    真正发出去的每一个文件名都只能是盲号，真实性只活在研究者的 `key.csv` 里。

设计约束（沿用项目惯例，都不是随手选的）
    1. **不复制 `key.csv` ≠ 足够**：还要扫一遍**所有发出去的文件**，确认里面
       没有任何说话人 id / 条件名 / 时长档 / 真实路径。这条是 `_assert_no_leak`，
       它在打包时就跑，发现泄漏直接拒绝产出 —— 不生成一个"看起来能发"的包。
    2. **缺音频直接拒绝**，不静默少拷几段：听者按下播放键却没有声音，
       他会以为是自己操作错了，而现场没人知道是包少了文件。
       （与 `listen()` 的取舍不同：那边是筛评分表，这边是发出去的东西。）
    3. **评分表只留 `mos` 列**：本轮 `truth/` 为空 → ABX 评不了。
       发一张带 `abx` 空列的表格，等于让听者去猜那一列填什么，
       猜出来的值会以"非法值"被丢掉或被误填 —— 不如不发这一列。
    4. **不覆盖已有发放包**（除非 `--force`）：听者交回来的表格就放在
       `sheets/` 里，重跑一次打包会把已回收的数据冲掉。这条是防"手一抖"。
    5. **零新依赖**：只做文件拷贝与文本生成。

用法
    python tools/voicebank_pack.py --exp pilotC --out D:/听测包
    python tools/voicebank_pack.py --exp pilotC --sheets 8 --out D:/听测包

产出（`<out>/` 下）
    clips/S-0001.wav …        盲号命名的音频（**文件名不带任何条件信息**）
    playlist.txt              按盲号的播放顺序（相对路径，可直接照放）
    sheets/listener_01.csv …  预命名的评分表，一人一份
    README-听者.md            发给听者看的说明（评分口径 + 不许讨论）
    MANIFEST.json             完整性对账（盲号 + sha256），**研究者自己留**

发出去之后收回来：
    python tools/voicebank_eval.py merge-listen --exp pilotC \\
        --sheets 1.csv,2.csv,3.csv,4.csv,5.csv,6.csv
    python tools/voicebank_eval.py analyze --exp pilotC --subjective outputs/voicebank/pilotC/subjective.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from voicebank_eval import _default_root  # noqa: E402  （共用一份落盘根推导，别写第二份）

#: 发放包里的子目录
CLIPS_DIR = "clips"
SHEETS_DIR = "sheets"
MANIFEST_FILE = "MANIFEST.json"
PLAYLIST_FILE = "playlist.txt"
READER_NOTE = "README-听者.md"

#: 评分表的列。★ 只留 `mos`：发一张填不了的 `abx` 空列，等于让听者去猜。
#: （`merge-listen` 认得的就是 `blind_id` + `mos`/`abx` 里至少一列。）
SHEET_COLUMNS = ("blind_id", "mos", "note")

#: 传给听者的评分表抬头 —— 口径与 `docs/减负实测协议.md` 那类量表保持一致：
#: 不写"像不像本人"（听者不知道本人是谁），只评**自然度**。
MOS_ANCHORS = (
    "5 = 和真人说话没有区别",
    "4 = 比较自然，仔细听才能挑出问题",
    "3 = 一般，能听出是机器合成但不难受",
    "2 = 明显不自然（机械/发闷/断续）",
    "1 = 完全不像人声或无法忍受",
)


# ---------------- 内部工具 ----------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_key(key_path: Path) -> list[dict]:
    """读揭盲表；只需要 `blind_id` 与 `sample` 两列（其余列用来做泄漏扫描）。"""
    rows: list[dict] = []
    with open(key_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            bid = (r.get("blind_id") or "").strip()
            sample = (r.get("sample") or "").strip()
            if bid and sample:
                rows.append(dict(r, blind_id=bid, sample=sample))
    return rows


def _identity_tokens(rows: list[dict]) -> set[str]:
    """从揭盲表里抽出**一旦出现在发给听者的文件里就说明泄题**的字符串。

    就是三个身份列：说话人 / 时长档 / 条件。**刻意不把整条样本路径丢进来**：
    真实路径里还含着实验名与固定目录名（`…/voicebank/pilotC/samples/…`），
    而实验名是要写在发放包说明里的（"盲听听测说明（pilotC）"）——
    把实验名当身份 token，会让 guard 在**每一个**正常包上报警。
    而一个总在报警的 guard 只会被人加 `--skip` 绕过，比没有更糟。

    ★ 只取长度 ≥ 2 的 token：单字符（如条件被写成 "a"）会在正常中文里假命中。
    """
    tokens: set[str] = set()
    for r in rows:
        for col in ("speaker", "duration_tag", "condition"):
            v = (r.get(col) or "").strip()
            if len(v) >= 2:
                tokens.add(v)
    return tokens


def _assert_no_leak(files: list[Path], tokens: set[str]) -> list[dict]:
    """扫所有**发出去**的文本文件，发现任何身份 token 就报出来。

    返回命中列表（空 = 干净）。调用方拿到非空必须**拒绝产出**，不许降级成警告：
    一个"稍微泄了点题"的盲听包，出的数据比没有数据更糟 —— 它看起来是盲的。
    """
    hits: list[dict] = []
    for p in files:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        for tok in sorted(tokens):
            if tok.lower() in low:
                hits.append({"file": p.name, "token": tok})
    return hits


# ---------------- 主流程 ----------------

def pack(exp: str, *, root: Path | None = None, out: Path,
         n_sheets: int = 6, sheet_prefix: str = "listener",
         force: bool = False) -> dict:
    """把 `<root>/<exp>/listen/` 打成可发放的盲听包。"""
    base = (Path(root) if root else _default_root()) / exp
    out = Path(out)
    res: dict = {
        "exp": exp, "root": str(base), "out": str(out),
        "error": None, "error_stage": None,
        "notes": [], "warnings": [],
        "n_clips": 0, "n_sheets": 0, "clips": str(out / CLIPS_DIR),
        "leaks": [],
    }

    key_path = base / "listen" / "key.csv"
    if not key_path.exists():
        res["error"] = f"没有揭盲表: {key_path}（先跑 listen）"
        res["error_stage"] = "揭盲表"
        return res

    try:
        rows = _read_key(key_path)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"揭盲表读不动: {type(e).__name__}: {e}"
        res["error_stage"] = "揭盲表"
        return res

    if not rows:
        res["error"] = f"揭盲表为空或缺少 blind_id/sample 列: {key_path}"
        res["error_stage"] = "揭盲表"
        return res

    bids = [r["blind_id"] for r in rows]
    if len(set(bids)) != len(bids):
        res["error"] = "揭盲表里有重复盲号 —— 盲号必须一一对应，否则汇总时张冠李戴"
        res["error_stage"] = "揭盲表"
        return res

    # 目的目录非空且没给 --force：拒绝。理由是"听者交回来的表格就住在那儿"
    if out.exists() and any(out.iterdir()) and not force:
        res["error"] = (f"目标目录非空: {out} —— 已回收的评分表可能就在里面。"
                        f"确认要覆盖就加 --force")
        res["error_stage"] = "目标目录"
        return res

    # ---- 音频：先全部核一遍再动手，缺一个就整体拒绝 ----
    missing = [r for r in rows if not _resolve_sample(base, r["sample"])]
    if missing:
        res["error"] = (f"{len(missing)}/{len(rows)} 个样本的音频不存在"
                        f"（如 {missing[0]['blind_id']} → {missing[0]['sample']}）"
                        f" —— 缺文件的包到了现场才发现，整轮听测作废")
        res["error_stage"] = "音频"
        res["missing"] = [{"blind_id": r["blind_id"], "sample": r["sample"]}
                          for r in missing]
        return res

    clips_dir = out / CLIPS_DIR
    sheets_dir = out / SHEETS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    for r in rows:
        src = _resolve_sample(base, r["sample"])
        dst = clips_dir / f"{r['blind_id']}{src.suffix.lower() or '.wav'}"
        shutil.copyfile(src, dst)
        # ★ 只记盲号 / 哈希 / 字节数：**不记** sample 路径，也不记时长
        #   （时长就是自变量，写进 MANIFEST 等于把 RQ1 的答案夹在包里）
        manifest.append({"blind_id": r["blind_id"], "file": f"{CLIPS_DIR}/{dst.name}",
                         "bytes": dst.stat().st_size, "sha256": _sha256(dst)})
    res["n_clips"] = len(manifest)

    # ---- 播放清单：相对路径 + 盲号，好让听者照着放 ----
    lines = [f"# 盲听播放清单 · 实验 {exp}",
             f"# 共 {len(manifest)} 条，按下面的顺序播放",
             "# ★ 别自己翻目录、别按文件名排序 —— 顺序已随机化，换顺序就把位置效应掺进来了",
             "# ★ 每条听完立刻在评分表对应行填 mos（1–5），别回头改",
             ""]
    for m in manifest:
        lines.append(f"{m['blind_id']}\t{m['file']}")
    (out / PLAYLIST_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 评分表：一人一份，预命名 ----
    width = max(2, len(str(n_sheets)))
    for i in range(1, n_sheets + 1):
        sp = sheets_dir / f"{sheet_prefix}_{i:0{width}d}.csv"
        with open(sp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(list(SHEET_COLUMNS))
            for m in manifest:
                w.writerow([m["blind_id"], "", ""])
    res["n_sheets"] = n_sheets

    # ---- 给听者的说明 ----
    (out / READER_NOTE).write_text(_reader_note(exp, len(manifest)), encoding="utf-8")

    # ---- 泄漏守卫：扫所有发出去的文件 ----
    tokens = _identity_tokens(rows)
    leaks = _assert_no_leak(
        [out / PLAYLIST_FILE, out / READER_NOTE,
         *sorted(sheets_dir.glob("*.csv"))], tokens)
    if leaks:
        # 清掉刚拷出来的东西：留一个"看着能用但不安全"的包比没有包更危险
        shutil.rmtree(clips_dir, ignore_errors=True)
        res["error"] = (f"发放包里发现了身份信息（{len(leaks)} 处，如 "
                        f"{leaks[0]['file']} 里的 \"{leaks[0]['token']}\"）—— "
                        f"盲听包已回滚，请先修文本再重跑")
        res["error_stage"] = "泄漏守卫"
        res["leaks"] = leaks
        return res

    (out / MANIFEST_FILE).write_text(json.dumps({
        "exp": exp, "n_clips": len(manifest), "n_sheets": n_sheets,
        "blinded": True,
        "note": "盲号与 sha256 对账用；身份映射留在实验目录的 listen/key.csv，"
                "本文件不含说话人/条件/时长档 —— 所以它也不构成泄题。",
        "clips": manifest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    res["ok"] = True
    res["warnings"].append(
        f"{len(manifest)} 条音频**只按盲号命名**；播放器仍会显示音频长度，"
        f"所以「时长档」这一自变量对听者**无法盲**（3s 和 60s 一听就知道）—— "
        f"写报告时要说明 RQ1 的听测存在这一先验，别当成严格盲测")
    return res


def _resolve_sample(base: Path, sample: str) -> Path | None:
    """样本路径可能是相对的（相对实验目录）或绝对的；空串必须判为"没有"。"""
    s = str(sample or "").strip()
    if not s:
        return None
    p = Path(s)
    for cand in (base / s, p):
        if cand.is_file():
            return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="打盲听发放包（盲号命名 + 防泄题 + 预命名评分表）")
    ap.add_argument("--exp", required=True, help="实验名（如 pilotC）")
    ap.add_argument("--root", default=None, help="实验根目录（默认 outputs/voicebank）")
    ap.add_argument("--out", required=True, help="发放包落盘目录（会拷 48 个音频进去）")
    ap.add_argument("--sheets", type=int, default=6, help="预命名几份评分表（默认 6）")
    ap.add_argument("--prefix", default="listener", help="评分表文件名前缀")
    ap.add_argument("--force", action="store_true",
                    help="目标目录非空也覆盖 —— ★ 会用掉已回收的评分表，慎用")
    args = ap.parse_args()

    res = pack(args.exp, root=Path(args.root) if args.root else None,
               out=Path(args.out), n_sheets=args.sheets,
               sheet_prefix=args.prefix, force=args.force)
    if res.get("error"):
        print(f"[pack] 失败（{res.get('error_stage')}）: {res['error']}")
        for m in res.get("missing", [])[:5]:
            print(f"[pack]   缺: {m['blind_id']} → {m['sample']}")
        for lk in res.get("leaks", [])[:5]:
            print(f"[pack]   泄漏: {lk['file']} ← 「{lk['token']}」")
        return 1
    for w in res.get("warnings", []):
        print(f"[pack] 警告: {w}")
    print(f"[pack] {res['n_clips']} 条音频 → {res['clips']}")
    print(f"[pack] {res['n_sheets']} 份评分表 → {Path(res['out']) / SHEETS_DIR}")
    print(f"[pack] 播放清单 → {Path(res['out']) / PLAYLIST_FILE}")
    print(f"[pack] 发放包: {res['out']}")
    print("[pack] 下发前自己再看一眼：包内**不该**出现任何说话人/条件/时长档字样的文件名")
    return 0


def _reader_note(exp: str, n_items: int) -> str:
    """发给听者看的说明。★ 措辞里不能出现任何条件名（泄漏守卫会拒）。"""
    anchors = "\n".join(f"- {a}" for a in MOS_ANCHORS)
    return f"""# 盲听听测说明（{exp}）

谢谢你帮忙。**全程约 10 分钟**：音频本身只有约 3 分钟，剩下的是打分。

## 你要做什么

1. 打开 `playlist.txt`，**按它的顺序**播放 `clips/` 里的音频（共 {n_items} 条）。
   **别自己按文件名排序** —— 顺序是随机打乱的，换顺序会让"越听越累"混进结果。
2. 拿到你自己那份 `sheets/listener_XX.csv`（一人一份，别共用、别改名）。
3. 每听完一条，就在对应 `blind_id` 那一行的 `mos` 里填一个 **1–5 的整数**：
   这条听起来**有多自然**（多像真人说话）。

## 打分口径

{anchors}

`note` 列可以留空；如果某条特别刺耳（爆音/电流声/完全听不清），写一句。

## 三条要求（这三条不做，数据就不能用）

- **戴耳机、音量固定**，别中途调音量、别外放。
- **独立评，不要互相讨论**，也别看别人的表 —— 讨论过的评分会互相靠拢，
  那就不是多个听者，而是一个听者被复读了几遍。
- **凭第一感觉打**，不要反复回听同一条、不要改已经填过的行。

## 边界

- 这里**没有标准答案**，也不需要你猜哪条是真人 —— 只评"自然不自然"。
- 音频里是**公开许可语料**的说话人，不是我们团队的人。
- 请**不要转发**这些音频（许可允许我们本地研究使用，不等于可以再分发）。
"""


if __name__ == "__main__":
    sys.exit(main())
