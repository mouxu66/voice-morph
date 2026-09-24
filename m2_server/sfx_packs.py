"""音效包：成套素材的布局、发现、安装与卸载（叶子模块，**无路由**）。

为什么要有「包」这一层
----------------------
出厂 6 条随插件走（`plugins/sound.fx-board/samples/`），`/import` 收**单条**用户素材。
两者之间缺一层：**成套素材** —— 第三方/用户把十几条音效打成一份分发，
于是「装」和「卸」都该是**一次动作**，而不是让用户点十几次导入。

为什么是「资产包」而不是「插件」（2026-09-24 用户拍板 A 案）
------------------------------------------------------------
`docs/插件化设计.md` §十 记着硬阻碍：前端是**构建期静态**的
（`web/src/lib/pluginRoutes.tsx` 的 `import.meta.glob` 在 `vite build` 时就把页面
打进 bundle），**下载下来的插件带不进一个已构建好的前端**；而
`plugin_manifest._validate()` 又明令「一个不挂 router 的插件不该存在」。
音效包只需要**素材**、沿用已有的格子渲染，所以走**资产形态** ——
与音色市场（下载到 RVC logs）、桌宠皮肤市场（下载 zip 解到 pet-skins）同一类，
既够用，又不触发上面那个阻碍。

布局
----
    <media>/soundboard/packs/<pack_id>/
        pack.json          # {name, license, author?, description?, homepage?, samples?}
        *.wav              # 素材，stem 即包内素材名
        LICENSE / README…  # 随便放，原样保留（署名要跟着分发走）

包内素材的 **id 命名空间化**为 `"<pack_id>/<stem>"`，裸 stem 仍只属于出厂/导入。
不这么做的代价：后装的包把已有 id 顶掉，而**用户界面上看不出任何变化** ——
「谁覆盖谁」这种隐式规则在本仓库已经被咬过一次（见 `sfx_lib` 头注释）。

`pack.json` 的 `license` 是**必填**的
------------------------------------
不是因为流程洁癖：包会被分发到**别人**的机器上，而这份声明是那边唯一能看到的
许可信息。缺了它，用户装到一个来路不明的东西时没有任何判断依据。
（`tools/audit_licenses.py` 只管**仓库内**的资产，管不到运行时下载的东西 ——
所以这条义务只能由格式本身和界面来承担。）
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import config as cfg
import soundfile as sf

#: 装到哪（跟 media 走，与 `sfx_lib.IMPORT_DIR` 平级）
PACKS_DIR = cfg.MEDIA_DIR / "soundboard" / "packs"
PACK_JSON = "pack.json"

#: 上限（都是一次性读进内存的量，宁可早拒也不要 OOM）
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_SAMPLE_BYTES = 16 * 1024 * 1024
MAX_SAMPLES = 200
#: 单条素材最长时长（秒）：声板是"短促一击"，装进一首歌没有意义
MAX_SAMPLE_S = 60.0

#: 目录里允许出现的文件后缀（**白名单**，不是黑名单）：包会被分发到别人机器上，
#: 「夹带可执行文件」这件事不该由"我记得拦了哪几个"来保证。
#: `""` 覆盖没有后缀的名字（`LICENSE` / `README`）。
ALLOWED_SUFFIXES = frozenset({"", ".wav", ".json", ".txt", ".md"})

#: 下载域名白名单（与 `market_download.ALLOWED_HOSTS` 同一策略：只认镜像与官方）
ALLOWED_HOSTS = (
    "huggingface.co",
    "hf-mirror.com",
    "hf.co",
    "modelscope.cn",
    "raw.githubusercontent.com",
)

#: 清单地址：URL 或本地路径。留空 = 货架没配（界面据此提示，而不是报错）。
INDEX_ENV = "VM_SFX_PACK_INDEX"
#: 清单本身的大小上限（清单是文本，几 KB 量级）
MAX_INDEX_BYTES = 512 * 1024


class PackError(Exception):
    """音效包相关错误（带 HTTP 语义的 status，由调用方翻成 HTTPException）。

    刻意不 import fastapi：本模块要能被 `tools/pack_sfx.py`（纯离线打包脚本）
    与测试单独 import。
    """

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = int(status)


# --------------------------------------------------------------- 名字规则


def clean_stem(raw: str, max_len: int = 40) -> str | None:
    """把文件名收成安全 stem；给不出安全的就返回 `None`。

    ⚠️ 这是个**归一化**函数（`../x` → `x`、`a/b` → `b`），不是**校验**函数。
    要"这个 id 必须原样合法"的调用方得自己比一次（`clean_stem(v) == v`）——
    包 id / 清单条目 / 命名空间两段都这么用。
    两者混起来就是漏记：`clean_stem("../a")` 会欣然返回 `"a"`（看起来没毛病），
    而那个 `"../a"` 一旦被当成目录名拼进路径，就正好是穿越。

    规则只有一份，`sfx_lib.sanitize_stem`（抛 SfxError）也走这里 ——
    两处各写一份「哪些字符不行」迟早会漂，而漂的那一侧总是漏的那一侧。
    """
    try:
        # `Path()` 遇到内嵌 NUL 会抛 ValueError（不是返回怪值）——
        # 文件名里塞 `\x00` 是常见手法，这里必须收成 None，而不是让整条请求 500
        stem = Path(str(raw or "")).name  # 去掉任何目录成分（../x.wav → x.wav）
        stem = Path(stem).stem
    except ValueError:
        return None
    if not stem or stem in (".", ".."):
        return None
    if any(c in '\\/:*?"<>|' or ord(c) < 32 for c in stem):
        return None
    if len(stem) > max_len:
        return None
    return stem


def split_id(sample_id: str) -> tuple[str, str] | None:
    """素材 id → `(pack_id, stem)`；裸 id（出厂/导入）返回 `None`；非法返回 `("", "")`。

    只认**恰好一段** `/`。多一个字符都不放宽：这是把"用户能控制的名字"变成
    文件路径的唯一入口，宽一点就是路径穿越。
    """
    sid = str(sample_id or "")
    if "/" not in sid:
        return None
    parts = sid.split("/")
    if len(parts) != 2:
        return ("", "")
    pid, stem = parts
    if clean_stem(pid) != pid or clean_stem(stem) != stem:
        return ("", "")
    return (pid, stem)


# --------------------------------------------------------------- 目录


def pack_dir(pack_id: str) -> Path:
    """`pack_id` → 目录，并确认它仍在 `PACKS_DIR` 之内（判据，不是字符串黑名单）。"""
    pid = clean_stem(pack_id)
    if not pid or pid != str(pack_id):
        raise PackError(f"非法的音效包 id：{pack_id!r}")
    d = (PACKS_DIR / pid).resolve()
    if not d.is_relative_to(PACKS_DIR.resolve()):
        raise PackError(f"非法的音效包 id：{pack_id!r}")
    return d


def read_pack(pack_id: str) -> dict | None:
    """读包的 `pack.json`（含样本数）；没有/坏了返回 `None`（调用方决定怎么报）。"""
    try:
        d = pack_dir(pack_id)
    except PackError:
        return None
    f = d / PACK_JSON
    if not f.is_file():
        return None
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    meta = dict(meta)
    meta["id"] = d.name
    # 声明与事实分开两处：`sample_meta` 是 pack.json 里作者写的显示名/标签，
    # `samples` 是**盘上真实的** wav 名单。只留声明 → 名字写了但文件没打进去时
    # 界面上会多出一条播不出来的格子；只留事实 → 显示名退化成文件名。
    declared = meta.get("samples")
    meta["sample_meta"] = declared if isinstance(declared, dict) else {}
    meta["samples"] = sample_stems(d.name)
    meta["count"] = len(meta["samples"])
    try:
        meta["bytes"] = sum(
            (d / f"{s}.wav").stat().st_size for s in meta["samples"]
        )
    except OSError:
        meta["bytes"] = 0
    return meta


def sample_stems(pack_id: str) -> list[str]:
    """包内素材名（wav 的 stem，排序稳定）。"""
    try:
        d = pack_dir(pack_id)
    except PackError:
        return []
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.wav") if p.is_file())


def list_packs() -> list[dict]:
    """已安装的包（按 id 排序）。坏包也列出来，但带上 `broken` 原因。

    **不静默跳过坏包**：用户装过什么、现在为什么不见了，必须能在界面上问出来
    —— 「东西消失了且没人解释」是最难排查的一类。
    """
    if not PACKS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(p for p in PACKS_DIR.iterdir() if p.is_dir()):
        if d.name.startswith("."):  # 安装中间态（`.tmp-*` / `.old-*`）不是包
            continue
        meta = read_pack(d.name)
        if meta is None:
            out.append({"id": d.name, "name": d.name, "broken": "缺少或损坏 pack.json",
                        "count": 0, "samples": [], "bytes": 0})
            continue
        # 文件名单（`samples`）与 `count` 冗余，列表里去掉；但 `sample_meta` 留下 ——
        # 它是作者写的显示名/标签，`sfx_lib.list_samples()` 靠它把格子标成"金币"
        # 而不是 "coin"（卸早了就会静默退化成文件名，见那个函数的注释）。
        meta.pop("samples", None)
        out.append(meta)
    return out


def resolve_sample(pack_id: str, stem: str) -> Path | None:
    """包内素材 → 文件路径（同样用「拼完之后仍在目录内」当判据）。"""
    try:
        d = pack_dir(pack_id)
    except PackError:
        return None
    p = (d / f"{stem}.wav").resolve()
    if not p.is_relative_to(d):
        return None
    return p if p.is_file() else None


# --------------------------------------------------------------- 安装


def _member_ok(name: str) -> bool:
    """zip 单条成员名是否可接受（拒绝绝对路径、`..`、盘符、奇怪后缀）。"""
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return False
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return False
    return True


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xF000)


def _read_zip(data: bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo], str]:
    """打开 zip 并做**全部**前置校验，返回 `(zf, 成员, 包根前缀)`。

    校验与解压分开：装到一半才发现第 7 条素材是符号链接，就已经往用户目录里
    写了 6 个文件了。宁可先全扫一遍（只有几十条成员，成本可忽略）。
    """
    if len(data) > MAX_ZIP_BYTES:
        raise PackError(f"包太大了：>{MAX_ZIP_BYTES // (1024 * 1024)}MB", status=413)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise PackError(f"这不是一个有效的 zip：{e}") from e

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise PackError("包里没有文件")
    for i in infos:
        if _is_symlink(i):
            raise PackError(f"包里含符号链接，拒绝：{i.filename}")
        if not _member_ok(i.filename):
            raise PackError(f"包里有不接受的文件：{i.filename}")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNPACKED_BYTES:
        raise PackError(f"解压后过大：>{MAX_UNPACKED_BYTES // (1024 * 1024)}MB", status=413)

    # 包根：允许 pack.json 在根，也允许**唯一一个**顶层目录里（压缩工具的常见形状）
    names = [i.filename.replace("\\", "/") for i in infos]
    roots = {n.split("/")[0] for n in names if "/" in n}
    if "pack.json" in names:
        root = ""
    elif len(roots) == 1 and f"{next(iter(roots))}/pack.json" in names:
        root = f"{next(iter(roots))}/"
    else:
        raise PackError("包里找不到 pack.json（应放在根目录）")
    return zf, infos, root


def _parse_pack_json(raw: bytes, src: str) -> dict:
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PackError(f"{src} 不是合法 JSON：{e}") from e
    if not isinstance(meta, dict):
        raise PackError(f"{src} 顶层必须是对象")
    for key in ("name", "license"):
        val = str(meta.get(key) or "").strip()
        if not val:
            # 见模块头：license 必填是**分发**需求，不是流程洁癖
            raise PackError(f"{src} 缺 {key!r}（包的显示名与许可声明都必填）")
    return meta


def install_zip(data: bytes, pack_id: str | None = None, *, overwrite: bool = False) -> dict:
    """安装一个包（zip 字节）→ `{id, name, license, samples, count, replaced}`。

    原子性：先解到 `PACKS_DIR/.tmp-*`，校验通过后再整目录换进去。
    已存在的包要先 `overwrite=True` —— 覆盖别人装过的包必须是**明确选择**，
    因为那会把用户自己导入的、或是别处装来的东西删掉。
    """
    zf, infos, root = _read_zip(data)
    meta_name = "pack.json" if not root else f"{root}{PACK_JSON}"
    try:
        raw_meta = zf.read(meta_name)
    except KeyError as e:  # pragma: no cover —— _read_zip 已保证它在
        raise PackError(f"包里找不到 {PACK_JSON}") from e
    meta = _parse_pack_json(raw_meta, meta_name)

    # id 来源优先级：显式传入 > pack.json 的 id > zip 里的目录名
    explicit = str(pack_id or "").strip()
    declared = str(meta.get("id") or "").strip()
    guess = ""
    if not explicit and not declared and root:
        guess = root.rstrip("/").split("/")[-1]
    pid = clean_stem(explicit or declared or guess or "")
    if not pid:
        raise PackError("给不出合法的音效包 id（用 --id/pack_id 指定）")

    wavs: list[str] = []
    for i in infos:
        n = i.filename.replace("\\", "/")
        if root and not n.startswith(root):
            continue
        rel = n[len(root):]
        if not rel.lower().endswith(".wav"):
            continue
        stem = clean_stem(rel)
        if not stem:
            raise PackError(f"素材名不合法：{rel}")
        if i.file_size > MAX_SAMPLE_BYTES:
            raise PackError(f"单条素材过大：{rel}（>{MAX_SAMPLE_BYTES // (1024 * 1024)}MB）", status=413)
        wavs.append(stem)
    if not wavs:
        raise PackError("包里没有任何 .wav 素材")
    if len(wavs) > MAX_SAMPLES:
        raise PackError(f"素材太多了：{len(wavs)} > {MAX_SAMPLES}")
    dup = sorted({s for s in wavs if wavs.count(s) > 1})
    if dup:
        raise PackError(f"包内素材重名：{dup[0]}")

    target = pack_dir(pid)
    if target.exists() and not overwrite:
        raise PackError(f"已装过同名音效包：{pid}（要替换请显式覆盖）", status=409)

    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PACKS_DIR / f".tmp-{pid}-{os.getpid()}"
    old = PACKS_DIR / f".old-{pid}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    try:
        tmp.mkdir(parents=True)
        for i in infos:
            n = i.filename.replace("\\", "/")
            if root and not n.startswith(root):
                continue
            rel = n[len(root):] or Path(n).name
            dest = (tmp / rel).resolve()
            if not dest.is_relative_to(tmp.resolve()):  # 双保险：上面已查过成员名
                raise PackError(f"成员名越界：{i.filename}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(i) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 256 * 1024)
        # 装完再验一遍素材真的能读、时长合理 —— 「装上了但播不出来」要在安装时拒
        for stem in wavs:
            f = tmp / f"{stem}.wav"
            try:
                info = sf.info(str(f))
            except Exception as e:
                raise PackError(f"素材读不出来：{f.name}（{e}）") from e
            if info.duration > MAX_SAMPLE_S:
                raise PackError(f"素材太长：{f.name}（{info.duration:.0f}s）")
        if target.exists():
            target.rename(old)  # 同卷重命名，先挪开再换，失败还能挪回来
        try:
            tmp.rename(target)
        except OSError:
            if old.exists() and not target.exists():
                old.rename(target)  # 换失败 → 把旧的放回去，别让用户白丢一个包
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    zf.close()
    return {
        "id": pid,
        "name": str(meta.get("name") or pid),
        "license": str(meta.get("license") or ""),
        "author": str(meta.get("author") or ""),
        "samples": sorted(wavs),
        "count": len(wavs),
        "replaced": False,
    }


def uninstall(pack_id: str) -> dict:
    """卸载一个包 → `{id, ids, count}`（`ids` 是该包占过的素材 id，供上层清计数）。"""
    d = pack_dir(pack_id)  # 非法 id 直接抛
    if not d.is_dir():
        raise PackError(f"没有这个音效包：{pack_id}", status=404)
    ids = [f"{d.name}/{s}" for s in sample_stems(d.name)]
    shutil.rmtree(d)
    return {"id": d.name, "ids": ids, "count": len(ids)}


# --------------------------------------------------------------- 市场清单


def _validate_url(url: str) -> str:
    """域名白名单（精确域或其子域）。返回规范化后的 URL。"""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError as e:
        raise PackError(f"清单里的地址没法解析：{url!r}") from e
    if u.scheme not in ("http", "https"):
        raise PackError(f"清单里的地址协议不支持：{url!r}")
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise PackError(f"下载域名不在白名单: {host}")
    return url


def _fetch_bytes(url: str, limit: int, timeout: float = 30.0) -> bytes:
    """取一段字节（带大小上限）。**独立成函数**，测试与打包脚本都好打桩。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "voice-morph/sfx-packs"})
    # 显式禁代理：本仓库踩过"系统代理把 127.0.0.1 也代理走"的坑
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:  # noqa: S310 —— 已过 _validate_url
        data = r.read(limit + 1)
    if len(data) > limit:
        raise PackError(f"内容超过上限（{limit // 1024}KB）", status=413)
    return data


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _index_entries(raw: object) -> list[dict]:
    """校验清单形状：`{packs: [...]}`。**坏条目直接报，不静默丢**。

    静默丢掉一条 = 用户看着一个"少了一项"的货架，没有任何线索可查。
    """
    if not isinstance(raw, dict):
        raise PackError("清单顶层必须是对象")
    packs = raw.get("packs")
    if not isinstance(packs, list):
        raise PackError("清单缺 packs 数组")
    out: list[dict] = []
    for i, item in enumerate(packs):
        if not isinstance(item, dict):
            raise PackError(f"清单第 {i + 1} 项不是对象")
        for key in ("id", "name", "license", "url"):
            if not str(item.get(key) or "").strip():
                raise PackError(f"清单第 {i + 1} 项缺 {key!r}")
        if clean_stem(item["id"]) != item["id"]:
            # 必须**原样**合法：归一化会放过 `../a`（见 clean_stem 的警告）
            raise PackError(f"清单第 {i + 1} 项的 id 不合法：{item['id']!r}")
        _validate_url(str(item["url"]))  # 货架上的地址当场验，别等用户点了才报
        keep = dict(item)
        keep["downloads"] = int(item.get("downloads") or 0)
        out.append(keep)
    return out


def load_index() -> dict:
    """读音效包清单 → `{source, items, error, note}`。三种情形都**不是异常**：

    - 没配地址（`VM_SFX_PACK_INDEX` 空）→ `note` 说明"货架没开"，界面照常显示
      「从 zip 安装」，而不是弹一个用户无法处理的错误；
    - 配了但取不到/格式坏 → `error` 带原因（拼错的地址必须看得见）；
    - 正常 → `items`。
    """
    src = os.environ.get(INDEX_ENV, "").strip()
    if not src:
        return {
            "source": None,
            "items": [],
            "error": "",
            "note": f"还没有配置音效包清单地址（{INDEX_ENV}）—— 仍可用「从 zip 安装」",
        }
    try:
        if src.startswith(("http://", "https://")):
            data = _fetch_bytes(_validate_url(src), MAX_INDEX_BYTES)
        else:
            p = Path(src)
            if not p.is_file():
                raise PackError(f"清单文件不存在：{src}")
            if p.stat().st_size > MAX_INDEX_BYTES:
                raise PackError("清单文件过大")
            data = p.read_bytes()
        raw = json.loads(data.decode("utf-8"))
        items = _index_entries(raw)
    except PackError as e:
        return {"source": src, "items": [], "error": str(e), "note": ""}
    except (OSError, ValueError) as e:
        return {"source": src, "items": [], "error": f"{type(e).__name__}: {e}", "note": ""}
    installed = {p["id"] for p in list_packs()}
    for it in items:
        it["installed"] = it["id"] in installed
    return {"source": src, "items": items, "error": "", "note": ""}


def install_from_index(pack_id: str, *, overwrite: bool = False) -> dict:
    """从清单里的地址下载并安装（市场那条路）。

    ★ 清单里有 `sha256` 就**必须**校验：市场条目里带的哈希是"我下到的东西
    是不是你承诺的那份"的唯一判据，跳过它等于把清单当成不可验证的传说。
    清单没给哈希时不假装校验过（返回里如实说 `sha256_verified: False`）。
    """
    idx = load_index()
    if idx.get("error"):
        raise PackError(f"音效包清单不可用：{idx['error']}", status=502)
    item = next((i for i in idx["items"] if i["id"] == pack_id), None)
    if item is None:
        raise PackError(f"清单里没有这个音效包：{pack_id}", status=404)
    data = _fetch_bytes(_validate_url(str(item["url"])), MAX_ZIP_BYTES)
    want = str(item.get("sha256") or "").strip().lower()
    if want and sha256_of(data) != want:
        raise PackError("下载到的包与清单里的 sha256 不一致，已拒绝安装", status=502)
    res = install_zip(data, pack_id, overwrite=overwrite)
    res["source"] = str(item["url"])
    res["sha256_verified"] = bool(want)
    return res
