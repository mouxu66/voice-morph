"""人偶皮肤市场 —— 市场核心：内置清单 / 下载安装 / 应用 / 卸载。

与音色市场（market_download 下载器 + market_install 编排）解耦，主题改为
「桌宠皮肤」：
  - 皮肤包格式见 pet_skin_build.py（skin.json + per-state webp spritesheet）
  - 内置 bundle 皮肤直接随安装包（assets/pet-skins/<id>/），启动时自动物化
    到 outputs/pet-skins/<id>/，免下载；
  - 远端皮肤按 manifest 的 source_urls 从素材源 GitHub/OpenGameArt 直链下载，
    经转换器（atlas-8x9 / gif-multi）生成皮肤包，全过程只允许白名单域名。

安全护栏（与 market_download 同款思路，域白名单 + 手动跟随重定向）：
  - 下载域名白名单：github.com / raw.githubusercontent.com / codeload.github.com
    / githubusercontent.com / opengameart.org
  - 单文件 ≤ 50MB；zip 解压强制 resolve() 后位于目标皮肤目录内（防 zip slip）
  - skin.json 字段白名单校验（状态 key / 帧数 / id），非法即失败回滚
"""
from __future__ import annotations

import json
import shutil
import threading
import urllib.parse
import zipfile
from pathlib import Path

import requests

from pet_skin_build import (
    SKIN_STATE_KEYS,
    SkinBuildError,
    build_skin,
    is_valid_skin_id,
    load_skin,
    validate_skin_files,
)
from runtime import OUT

# ---- 路径与常量 ----
PET_SKINS_DIR = OUT / "pet-skins"
BUNDLE_SKINS_DIR = Path(__file__).resolve().parent / "assets" / "pet-skins"
STATE_FILE = OUT / "pet-skin-state.json"
DEFAULT_SKIN = "furina"

CHUNK_SIZE = 256 * 1024
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
MAX_SOURCE_BYTES = 50 * 1024 * 1024   # 单源文件上限 50MB
MAX_REDIRECTS = 5

# 皮肤素材源域名白名单（重定向每一跳都要过）
PET_ALLOWED_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "githubusercontent.com",   # 用户头像/直链
    "opengameart.org",
    "www.opengameart.org",
}

# 当前应用皮肤的持久化（userData 侧另存 pet.json.skin；这里做后端权威副本）
_APPLIED_LOCK = threading.Lock()

# ---- 外置清单（ext）：GitHub 扫描器「扫描即上线」的落盘清单 ----
EXT_FILE = OUT / "pet-scan-ext.json"          # [{id,name,category,license,...discovery}, ...]
_EXT_LOCK = threading.Lock()


def get_ext_items() -> list[dict]:
    """读外置清单（扫描器发现且试转通过的候选皮肤）；文件缺失/损坏返回 []。"""
    try:
        return json.loads(EXT_FILE.read_text("utf-8"))
    except Exception:
        return []


def add_ext_item(item: dict) -> dict:
    """写入/更新一条外置清单条目（按 id 去重，重复则覆盖）。

    「扫描即上线」入口：扫描器试转通过后调用，市场 install/apply/search/detail
    立即能看到该皮肤（find_manifest_item 会查到这里）。"""
    skin_id = str(item.get("id") or "")
    if not is_valid_skin_id(skin_id):
        raise PetMarketError("外置皮肤 id 非法")
    if any(m["id"] == skin_id for m in get_manifest()):
        raise PetMarketError(f"皮肤「{skin_id}」已存在于内置清单，id 冲突")
    with _EXT_LOCK:
        items = get_ext_items()
        items = [it for it in items if it.get("id") != skin_id]
        items.append({**item, "id": skin_id})
        EXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXT_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
    return item


def remove_ext_item(skin_id: str) -> bool:
    """从外置清单移除条目；存在则返回 True。不物理删除皮肤目录。"""
    with _EXT_LOCK:
        items = get_ext_items()
        rest = [it for it in items if it.get("id") != skin_id]
        if len(rest) == len(items):
            return False
        EXT_FILE.write_text(json.dumps(rest, ensure_ascii=False, indent=2), "utf-8")
    return True

# ---- 多任务安装队列（≤MAX_CONCURRENT 并发下载/转换，其余排队，可取消） ----
MAX_CONCURRENT_INSTALLS = 2            # 同时下载/转换数
_TASK_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}           # skin_id -> 任务状态（含 cancel 标记）
_ALL_IDS: list[str] = []               # 创建顺序（旧→新，托盘展示顺序）
_QUEUE: list[str] = []                 # 排队中的 skin_id（FIFO）
_RUNNING = 0                           # 当前实际运行的 worker 数

_ACTIVE_STATUSES = {"downloading", "installing"}
_NON_TERMINAL = {"queued", "downloading", "installing"}

EMPTY_TASK = {"skin_id": "", "status": "idle", "phase": "", "message": "", "percent": 0,
              "error": "", "cancel": False}


class PetMarketError(Exception):
    """市场业务错误（API 捕获后映射 404/409/400）。"""


# ---------------- manifest（素材均许可干净可分发） ----------------
# pixel-* 系列：CanFlyhang/Desktop-Pixel-Pet（MIT）程序化像素宠物 JSON，
# 经 pixel-json 适配器（pet_skin_build.py）转换成皮肤包。
_PIX_URL = ("https://raw.githubusercontent.com/CanFlyhang/Desktop-Pixel-Pet/main/assets/pets/{}")


def _pixel_item(sid: str, name: str, desc: str, src: str) -> dict:
    return {
        "id": sid,
        "name": name,
        "category": "像素萌宠",
        "license": "MIT",
        "attribution": "CanFlyhang/Desktop-Pixel-Pet (MIT)",
        "description": desc,
        "source_type": "pixel-json",
        "bundle": False,
        "source_urls": [_PIX_URL.format(src)],
        "meta": {"frameW": 0, "frameH": 0},   # 转换时按 JSON size 回填
    }


def get_manifest() -> list[dict]:
    return [
        {
            "id": "furina",
            "name": "芙宁娜（水神）",
            "category": "二次元",
            "license": "MIT",
            "attribution": "Ice-teapop/desktop-pet（原创 SVG, MIT）",
            "description": "原神水神芙宁娜主题桌宠，原创二创 sprite（MIT 全开放可商用），随安装包内置。",
            "source_type": "bundle",
            "bundle": True,
            "meta": {"frameW": 150, "frameH": 150},
        },
        {
            "id": "gel-slime",
            "name": "史莱姆 Gel",
            "category": "卡通",
            "license": "Apache-2.0",
            "attribution": "anjiemo/IdeDesktopPetSprite",
            "description": "原创卡通史莱姆（Gel），IDE 桌宠默认角色，8×9 atlas 逐状态动画，非二次元风格。",
            "source_type": "atlas-8x9",
            "bundle": False,
            "source_urls": [
                "https://raw.githubusercontent.com/anjiemo/IdeDesktopPetSprite/master/src/main/resources/pets/gel-slime.png",
            ],
            "meta": {
                "frameW": 192, "frameH": 208, "cols": 8,
                "row_map": {"idle": 0, "listen": 1, "play": 2, "build": 2, "think": 4, "error": 3},
            },
        },
        {
            "id": "mika",
            "name": "Mika 学妹",
            "category": "二次元",
            "license": "MIT",
            "attribution": "ROTl24/pet-github",
            "description": "chibi 长发 JK 学妹（Codex 桌宠格式），灰色校服 + 耳机，8×9 atlas。",
            "source_type": "atlas-8x9",
            "bundle": False,
            "source_urls": [
                "https://raw.githubusercontent.com/ROTl24/pet-github/main/pet/spritesheet.webp",
            ],
            "meta": {
                "frameW": 192, "frameH": 208, "cols": 8,
                "row_map": {"idle": 0, "listen": 1, "play": 2, "build": 2, "think": 4, "error": 3},
            },
        },
        {
            "id": "pixel-cat",
            "name": "像素小猫",
            "category": "像素萌宠",
            "license": "CC0",
            "attribution": "Shepardskin (OpenGameArt, CC0)",
            "description": "CC0 公共领域像素猫，行走/奔跑 gif，任意项目可用、免署名。",
            "source_type": "gif-multi",
            "bundle": False,
            "source_urls": [
                "https://opengameart.org/sites/default/files/cat%20sprite.zip",
            ],
            "meta": {
                "gif_map": {"idle": "cat idle.gif", "play": "cat walking.gif", "build": "cat walking.gif",
                            "listen": "cat idle.gif", "think": "cat idle.gif", "error": "cat walking.gif"},
                "frameW": 0, "frameH": 0,   # 下载后按实际帧数推算
            },
        },
        _pixel_item("pixel-capybara", "卡皮巴拉", "CC 像素卡皮巴拉（水豚），二次元以外的治愈系像素宠物，轻微呼吸动画。",
                    "pixel_capybara.json"),
        _pixel_item("pixel-bubble-slime", "泡泡史莱姆", "半透明泡泡质感史莱姆宠，像素呼吸动画。", "pixel_bubble_slime.json"),
        _pixel_item("pixel-matcha-bear", "抹茶小熊", "抹茶配色小熊，绿色治愈系像素宠物。", "pixel_matcha_bear.json"),
        _pixel_item("pixel-ice-penguin", "小企鹅", "蓝白配色小企鹅，南极治愈系像素宠物。", "pixel_ice_penguin.json"),
        _pixel_item("pixel-energetic-duck", "元气小鸭", "鹅黄色元气小鸭，活泼的像素宠物。", "pixel_energetic_duck.json"),
        _pixel_item("pixel-cyber-cat", "赛博猫", "霓虹赛博风格像素猫，工业感配色。", "pixel_cyber_cat.json"),
        _pixel_item("pixel-lucky-koi", "幸运锦鲤", "红白锦鲤像素宠物，寓意好运。", "pixel_lucky_koi.json"),
        _pixel_item("pixel-neon-fox", "霓虹狐", "紫色霓虹系小狐狸，夜间氛围像素宠物。", "pixel_neon_fox.json"),
    ]


def find_manifest_item(skin_id: str) -> dict | None:
    """同时查内置清单与外置清单（扫描器「扫描即上线」的皮肤也能被安装/应用）。"""
    for item in get_manifest():
        if item["id"] == skin_id:
            return item
    for item in get_ext_items():
        if item.get("id") == skin_id:
            return item
    return None


# ---------------- 状态与应用 ----------------


def load_applied() -> str:
    """当前应用的皮肤 ID（缺省 furina；文件损坏回退默认）。"""
    try:
        return json.loads(STATE_FILE.read_text("utf-8")).get("applied") or DEFAULT_SKIN
    except Exception:
        return DEFAULT_SKIN


def save_applied(skin_id: str) -> None:
    with _APPLIED_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"applied": skin_id}), "utf-8")


def current_applied() -> str:
    return load_applied()


def ensure_bundle(skin: dict) -> Path:
    """把内置 bundle 皮肤从 assets/pet-skins/<id> 物化到 outputs/pet-skins/<id>。"""
    src = BUNDLE_SKINS_DIR / skin["id"]
    dst = PET_SKINS_DIR / skin["id"]
    if dst.joinpath("skin.json").exists():
        return dst
    if not src.joinpath("skin.json").exists():
        raise PetMarketError(f"内置皮肤资源缺失: {skin['id']}")
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    return dst


def installed_skins() -> list[dict]:
    """已安装皮肤列表（读已完成物化的皮肤包 skin.json）。"""
    if not PET_SKINS_DIR.is_dir():
        return []
    applied = load_applied()
    out = []
    for d in sorted(PET_SKINS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            skin = load_skin(d)
        except Exception:
            continue
        out.append({
            "id": d.name, "name": skin.get("name") or d.name,
            "category": skin.get("category") or "其他",
            "license": skin.get("license") or "",
            "attribution": skin.get("attribution") or "",
            "applied": d.name == applied,
            "preview": f"/api/pet-market/image/{d.name}",
        })
    return out


def skin_dir(skin_id: str) -> Path:
    """皮肤目录（校验合法 id，防路径穿越；不存在抛 404 语义错误）。"""
    if not is_valid_skin_id(skin_id):
        raise PetMarketError("皮肤 id 非法")
    d = PET_SKINS_DIR / skin_id
    if not d.joinpath("skin.json").exists():
        raise PetMarketError("皮肤未安装")
    return d


def applied_skin() -> dict:
    """当前应用皮肤配置（渲染器换肤用）：{id, frameW, frameH, states}。

    states.{state} = {sheet, frames, dur}；应用态指向未安装皮肤时回退默认。
    """
    sid = load_applied()
    try:
        d = skin_dir(sid)
    except PetMarketError:
        # 应用态指向未安装皮肤（或默认 bundle 尚未物化）：回退默认并尝试物化
        sid = DEFAULT_SKIN
        save_applied(sid)
        item = find_manifest_item(sid)
        if item is not None and item.get("bundle"):
            ensure_bundle(item)
        d = skin_dir(sid)
    skin = load_skin(d)
    return {"id": d.name, "frameW": int(skin["frameW"]), "frameH": int(skin["frameH"]),
            "states": skin.get("states") or {}}


def sheet_file(skin_id: str, name: str) -> Path:
    """皮肤 spritesheet 文件（name 必须出现在该皮肤 skin.json 状态表中）。"""
    d = skin_dir(skin_id)
    skin = load_skin(d)
    allowed = {s["sheet"] for s in (skin.get("states") or {}).values() if s.get("sheet")}
    if name not in allowed:
        raise PetMarketError("皮肤资源不存在")
    return d / name


# ---------------- 下载（轻量，白名单直链） ----------------


def _validate_url(url: str) -> str:
    """白名单校验（精确域或子域），非法抛 PetMarketError。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError as exc:
        raise PetMarketError(f"非法 URL: {url}") from exc
    for allowed in PET_ALLOWED_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return host
    raise PetMarketError(f"下载域名不在白名单: {host}")


def _download_to(url: str, dst: Path, box: dict) -> None:
    """流式下载（手动跟随重定向，每跳过白名单），大小实时拦截。"""
    redirects = 0
    headers = {}
    while True:
        _validate_url(url)
        resp = requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                            allow_redirects=False, headers=headers)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise PetMarketError(f"重定向缺少 Location: {url}")
            url = urllib.parse.urljoin(url, loc)
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise PetMarketError(f"重定向次数过多（>{MAX_REDIRECTS}）：{url}")
            continue
        break
    try:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0) or None
        if total and total > MAX_SOURCE_BYTES:
            raise PetMarketError(f"源文件超过上限 {MAX_SOURCE_BYTES} 字节")
        dst.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with dst.open("wb") as f:
            for chunk in resp.iter_content(CHUNK_SIZE):
                if box.get("cancel"):
                    resp.close()
                    raise PetMarketError("已取消")
                if not chunk:
                    continue
                f.write(chunk)
                received += len(chunk)
                if received > MAX_SOURCE_BYTES:
                    raise PetMarketError(f"源文件超过上限 {MAX_SOURCE_BYTES} 字节")
        box["bytes"] = received
    finally:
        resp.close()


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压 zip 到 dest，强制所有项位于 dest 内（防 zip slip）。"""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest.resolve() not in target.parents:
                raise PetMarketError(f"zip 内含越界路径: {info.filename}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def _make_preview(skin_dir: Path) -> None:
    """从 idle strip 首帧生成 preview.png（市场卡片图）。失败静默。"""
    try:
        skin = load_skin(skin_dir)
        idle = skin.get("states", {}).get("idle")
        if not idle:
            return
        fw, fh = int(skin["frameW"]), int(skin["frameH"])
        from common import find_ffmpeg
        import subprocess
        subprocess.run(
            [find_ffmpeg(), "-y", "-i", str(skin_dir / idle["sheet"]),
             "-vf", f"crop={fw}:{fh}:0:0", "-frames:v", "1",
             str(skin_dir / "preview.png")],
            capture_output=True, timeout=120)
    except Exception:
        pass


# ---------------- 安装编排（多任务队列） ----------------


def _set_task(skin_id: str, **kw) -> None:
    """线程安全更新某皮肤的任务状态（不存在则忽略）。"""
    with _TASK_LOCK:
        t = _TASKS.get(skin_id)
        if t is not None:
            t.update(kw)


def progress() -> dict:
    """所有安装任务 + 队列概览（前端托盘轮询）。

    返回 {"items": [...], "active": n, "queued": n}；items 按 进行中→排队→终结 排序，
    同档内按创建顺序。
    """
    with _TASK_LOCK:
        prio = {"downloading": 0, "installing": 0, "queued": 1, "done": 2,
                "cancelled": 2, "failed": 2}
        items = []
        for k in _ALL_IDS:
            t = _TASKS.get(k)
            if t is not None:
                d = dict(t)
                d.pop("cancel", None)
                items.append(d)
        items.sort(key=lambda t: prio.get(t.get("status", "idle"), 9))
        return {
            "items": items,
            "active": sum(1 for t in items if t["status"] in _ACTIVE_STATUSES),
            "queued": sum(1 for t in items if t["status"] == "queued"),
        }


def is_busy() -> bool:
    """是否有任何任务在下载/转换中（卸载等互斥用）。"""
    with _TASK_LOCK:
        return any(t["status"] in _ACTIVE_STATUSES for t in _TASKS.values())


def _kick() -> None:
    """从队列弹出任务启动 worker，直到并发上限；被取消/已终结的排队项跳过。"""
    global _RUNNING
    with _TASK_LOCK:
        while _RUNNING < MAX_CONCURRENT_INSTALLS and _QUEUE:
            sid = _QUEUE.pop(0)
            t = _TASKS.get(sid)
            if t is None or t.get("cancel") or t["status"] != "queued":
                continue
            _RUNNING += 1
            threading.Thread(target=_install_worker, args=(sid,), daemon=True).start()


def _install_worker(skin_id: str) -> None:
    global _RUNNING
    task = _TASKS.get(skin_id) or EMPTY_TASK
    try:
        item = find_manifest_item(skin_id)
        if item is None:
            raise PetMarketError("清单中无此皮肤")
        task.update(status="downloading", phase="下载素材", message=f"正在下载 {item['name']}",
                    percent=5, error="")
        tmp = PET_SKINS_DIR / f".tmp-{skin_id}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        src_dir = tmp / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        if item.get("bundle"):
            task.update(status="installing", phase="物化内置皮肤", message="正在准备内置皮肤",
                        percent=60, error="")
            ensure_bundle(item)
        else:
            urls = item.get("source_urls") or []
            if not urls:
                raise PetMarketError("该皮肤缺少下载源")
            saved: list[Path] = []
            for i, url in enumerate(urls):
                if task.get("cancel"):
                    raise PetMarketError("已取消")
                name = Path(urllib.parse.urlparse(url).path).name or f"src{i}"
                dst = src_dir / name
                task.update(percent=5 + i * 30 // max(1, len(urls)))
                _download_to(url, dst, task)   # box=task：取消标记实时生效
                saved.append(dst)
            task.update(status="installing", phase="生成皮肤包", message="转换动画为皮肤包",
                        percent=75, error="")

            meta = dict(item.get("meta") or {})
            meta.update({"id": skin_id, "name": item.get("name") or skin_id,
                         "category": item.get("category") or "其他",
                         "license": item.get("license") or "unknown",
                         "attribution": item.get("attribution") or ""})
            if item["source_type"] == "gif-multi":
                # 解 zip 后把解出文件作为 gif 源
                zips = [p for p in saved if p.suffix.lower() == ".zip"]
                if zips:
                    iz = src_dir / "unzip"
                    _safe_extract(zips[0], iz)
                    saved = [p for p in iz.iterdir() if p.is_file()] or saved
            dst_skin = PET_SKINS_DIR / skin_id
            if dst_skin.exists():
                shutil.rmtree(dst_skin, ignore_errors=True)
            try:
                build_skin(item["source_type"], saved, dst_skin, meta)
            except SkinBuildError as exc:
                shutil.rmtree(dst_skin, ignore_errors=True)
                raise PetMarketError(str(exc)) from exc
            warns = validate_skin_files(dst_skin)
            if warns:
                shutil.rmtree(dst_skin, ignore_errors=True)
                raise PetMarketError("皮肤包校验失败: " + "; ".join(warns))
            if task.get("cancel"):
                shutil.rmtree(dst_skin, ignore_errors=True)
                raise PetMarketError("已取消")
            _make_preview(dst_skin)

        task.update(status="done", phase="完成", message="", percent=100, error="")
    except PetMarketError as exc:
        if task.get("cancel"):
            task.update(status="cancelled", phase="已取消", message="", percent=0, error="")
        else:
            task.update(status="failed", phase="失败", message="", percent=0, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 兜底，避免安装线程崩溃
        task.update(status="failed", phase="失败", message="", percent=0,
                    error=f"安装异常: {exc}")
    finally:
        tmp = PET_SKINS_DIR / f".tmp-{skin_id}"
        shutil.rmtree(tmp, ignore_errors=True)
        with _TASK_LOCK:
            _RUNNING -= 1
        _kick()


def install(skin_id: str) -> dict:
    """加入安装队列并立即调度（≤MAX_CONCURRENT 并发，其余排队）。
    同 id 已在队列/进行中时 409。返回该任务当前状态（通常 queued）。"""
    if not is_valid_skin_id(skin_id):
        raise PetMarketError("皮肤 id 非法")
    if find_manifest_item(skin_id) is None:
        raise PetMarketError("清单中无此皮肤")
    with _TASK_LOCK:
        cur = _TASKS.get(skin_id)
        if cur and cur["status"] in _NON_TERMINAL:
            raise PetMarketError(f"「{skin_id}」已在任务中（排队或进行中）")
        task = {"skin_id": skin_id, "status": "queued", "phase": "排队", "message": "",
                "percent": 0, "error": "", "cancel": False}
        _TASKS[skin_id] = task
        _ALL_IDS.append(skin_id)
        _QUEUE.append(skin_id)
    _kick()
    with _TASK_LOCK:
        return dict(_TASKS[skin_id])


def cancel(skin_id: str) -> dict:
    """取消排队或进行中的安装任务。
    排队 → 直接出队标记取消；进行中 → 置 cancel 标记，worker 在下载/转换间隙停下。"""
    with _TASK_LOCK:
        task = _TASKS.get(skin_id)
        if task is None:
            raise PetMarketError("无此安装任务")
        if task["status"] in ("done", "cancelled", "failed"):
            raise PetMarketError("任务已结束，无法取消")
        if task["status"] == "queued":
            task.update(status="cancelled", phase="已取消", message="", percent=0, error="")
            try:
                _QUEUE.remove(skin_id)
            except ValueError:
                pass
            return dict(task)
        # downloading/installing
        task["cancel"] = True
        task.update(phase="取消中", message="正在取消…")
        return dict(task)


# ---------------- 搜索与详情 ----------------


def search(query: str = "", category: str = "") -> list[dict]:
    """本地模糊搜索：名称/描述/分类/作者/许可/ID 命中即出，附带 installed/applied 标记。"""
    q = (query or "").strip().lower()
    cat = (category or "").strip()
    installed_ids = {i["id"] for i in installed_skins()}
    applied = load_applied()
    out = []
    for m in get_manifest():
        if cat and (m.get("category") or "其他") != cat:
            continue
        if q:
            hay = " ".join(
                str(m.get(k) or "") for k in
                ("id", "name", "description", "category", "attribution", "license")
            ).lower()
            if q not in hay:
                continue
        out.append({**m, "installed": m["id"] in installed_ids, "applied": m["id"] == applied})
    return out


def detail(skin_id: str) -> dict:
    """皮肤详情：清单信息 + 源链接 + 已装状态 + 帧尺寸 + 状态动画表 + 许可全文。

    bundle 皮肤未物化时先物化再读；未安装的远端皮肤 states 为空、license_text 为空。
    """
    item = find_manifest_item(skin_id)
    if item is None:
        raise PetMarketError("清单中无此皮肤")
    d: Path | None = None
    try:
        d = skin_dir(skin_id)
    except PetMarketError:
        if item.get("bundle"):
            try:
                d = ensure_bundle(item)
            except PetMarketError:
                d = None
    states: dict = {}
    frameW = frameH = 0
    license_text = ""
    if d is not None:
        skin = load_skin(d)
        states = skin.get("states") or {}
        frameW = int(skin.get("frameW") or 0)
        frameH = int(skin.get("frameH") or 0)
        lic = d / "LICENSE"
        if lic.exists():
            license_text = lic.read_text("utf-8", errors="replace")[:8000]
    return {
        "item": {k: item.get(k) for k in
                 ("id", "name", "category", "license", "attribution", "description",
                  "source_type", "bundle")},
        "source_urls": item.get("source_urls") or [],
        "installed": d is not None,
        "applied": load_applied() == skin_id,
        "frameW": frameW, "frameH": frameH,
        "states": states,
        "license_text": license_text,
    }


def apply(skin_id: str) -> dict:
    """应用皮肤：把 skin.json 落位到 outputs（bundle 先物化），并写应用状态。"""
    item = find_manifest_item(skin_id)
    if item is None:
        raise PetMarketError("清单中无此皮肤")
    try:
        d = skin_dir(skin_id)
    except PetMarketError:
        if item.get("bundle"):
            d = ensure_bundle(item)
        else:
            raise
    load_skin(d)   # 校验 skin.json 合法
    save_applied(skin_id)
    return {"applied": skin_id}


def uninstall(skin_id: str) -> dict:
    """卸载皮肤：bundle 皮肤拒绝卸载（内置，仅复位应用态）；远端皮肤删目录。
    当前应用皮肤被我卸载时，应用态回到默认 furina。"""
    if not is_valid_skin_id(skin_id):
        raise PetMarketError("皮肤 id 非法")
    item = find_manifest_item(skin_id)
    if is_busy():
        raise PetMarketError("已有皮肤任务在进行中")
    if item and item.get("bundle"):
        # 内置皮肤不物理删除，只复位应用态
        if load_applied() == skin_id:
            save_applied(DEFAULT_SKIN)
        return {"uninstalled": skin_id, "reset_applied": True}
    d = PET_SKINS_DIR / skin_id
    if d.joinpath("skin.json").exists():
        shutil.rmtree(d, ignore_errors=True)
    if load_applied() == skin_id:
        save_applied(DEFAULT_SKIN)
    return {"uninstalled": skin_id}


# 启动物化：确保默认 bundle 皮肤就位（幂等，失败不阻塞服务）
def ensure_default_bundle() -> None:
    item = find_manifest_item(DEFAULT_SKIN)
    if item is None:
        return
    try:
        ensure_bundle(item)
    except Exception:  # noqa: BLE001 - 启动兜底
        pass