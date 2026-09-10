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
_ACTIVE_LOCK = threading.Lock()
_ACTIVE = {"skin_id": "", "status": "idle", "phase": "", "message": "", "percent": 0,
           "error": ""}


class PetMarketError(Exception):
    """市场业务错误（API 捕获后映射 404/409/400）。"""


# ---------------- manifest（第一期，素材均许可干净可分发） ----------------
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
    ]


def find_manifest_item(skin_id: str) -> dict | None:
    for item in get_manifest():
        if item["id"] == skin_id:
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


# ---------------- 安装编排 ----------------


def progress() -> dict:
    with _ACTIVE_LOCK:
        return dict(_ACTIVE)


def is_busy() -> bool:
    with _ACTIVE_LOCK:
        return _ACTIVE["status"] in ("downloading", "installing")


def _set_active(**kw) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.update(kw)


def _install_worker(skin_id: str) -> None:
    try:
        item = find_manifest_item(skin_id)
        if item is None:
            raise PetMarketError("清单中无此皮肤")
        _set_active(status="downloading", phase="下载素材", message=f"正在下载 {item['name']}",
                    percent=5, error="")
        tmp = PET_SKINS_DIR / f".tmp-{skin_id}"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        src_dir = tmp / "src"
        src_dir.mkdir(parents=True, exist_ok=True)

        if item.get("bundle"):
            _set_active(status="installing", phase="物化内置皮肤", message="正在准备内置皮肤",
                        percent=60, error="")
            ensure_bundle(item)
        else:
            urls = item.get("source_urls") or []
            if not urls:
                raise PetMarketError("该皮肤缺少下载源")
            box: dict = {}
            saved: list[Path] = []
            for i, url in enumerate(urls):
                name = Path(urllib.parse.urlparse(url).path).name or f"src{i}"
                dst = src_dir / name
                _set_active(percent=5 + i * 30 // max(1, len(urls)))
                _download_to(url, dst, box)
                saved.append(dst)
            _set_active(status="installing", phase="生成皮肤包", message="转换动画为皮肤包",
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
            _make_preview(dst_skin)

        _set_active(status="done", phase="完成", message="", percent=100, error="")
    except PetMarketError as exc:
        _set_active(status="failed", phase="失败", message="", percent=0, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 兜底，避免安装线程崩溃
        _set_active(status="failed", phase="失败", message="", percent=0,
                    error=f"安装异常: {exc}")
    finally:
        _set_active(status="done" if _ACTIVE["status"] == "done" else _ACTIVE["status"])
        tmp = PET_SKINS_DIR / f".tmp-{skin_id}"
        shutil.rmtree(tmp, ignore_errors=True)


def install(skin_id: str) -> dict:
    """启动安装（下载源→转换→物化）。同 id 进行中/忙碌时 409。"""
    if not is_valid_skin_id(skin_id):
        raise PetMarketError("皮肤 id 非法")
    if find_manifest_item(skin_id) is None:
        raise PetMarketError("清单中无此皮肤")
    if is_busy():
        raise PetMarketError("已有皮肤任务在进行中")
    _set_active(status="downloading", phase="排队", message="", percent=0, error="",
                skin_id=skin_id)
    t = threading.Thread(target=_install_worker, args=(skin_id,), daemon=True)
    t.start()
    return progress()


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