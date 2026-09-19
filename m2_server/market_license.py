"""市场音色的**许可回读**：把"兜底文案"换成"上游到底标了什么"。

背景（`THIRD_PARTY_NOTICES.md` §5 缺口 G4）
------------------------------------------
市场条目的 `license` 字段此前是一句**兜底文案**（"社区自训·仅供个人使用，勿商用"），
写明它是兜底而不是读来的事实。G4 的要求是"安装时抓 HF/魔搭 `license` 字段写入
`source.json`"。

2026-09-14 实测结果**比预期更值得记**——两个上游**都没有声明许可**：

| 源 | 请求 | 结果 |
|---|---|---|
| HF `chaye741/RVC-Voice-Models` | `GET hf-mirror.com/api/models/…` → 200 | 无 `cardData`；`tags` 只有 `region:us`，无 `license:*` |
| 魔搭 `hudddd/Retrieval-based-Voice` | `GET modelscope.cn/api/v1/models/…` → 200 | `Data.License` / `LicenseName` / `LicenseLink` **三个字段都是空串** |

于是这件事的真相比"补一个许可名"更硬：
**上游未标注许可，默认就是"保留所有权利"** —— 不是"没写所以随便用"。
所以那句"仅供个人学习研究，勿商用"**不是占位符，而是当前法律状态下唯一正确的表述**，
G4 的修法因此是"**把回读做成自动的，并把'上游未标注'这个事实记录下来**"，
而不是"读到许可名就收工"。

设计约束
--------
- **绝不因为探测失败就编一个许可**：分不清"读到空"和"没读通"的话，这条记录就是假的。
  所以结果里区分 `model-card`（读到了）/ `unlabeled`（读通了但上游没标）/
  `unreachable`（没读通，保留兜底文案）。三者语义不同，写进 `source.json` 供人复查。
- **不抛异常**：探测是安装的收尾步骤，网络问题不该让用户装不上音色。
- `get` 可注入，单测不碰网络。
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: 与 market_manifest 的 base 保持一致：本机可达的是 hf-mirror，官方站不可达。
#: 这里**不复用** market_manifest 的常量而是各自声明 —— 那两个是"下载直链"的基址，
#: 这是"API 基址"，将来任一方换域不该连坐。
HF_API_BASE = "https://hf-mirror.com"
MS_API_BASE = "https://modelscope.cn"

#: 探测结果的三态。语义必须分清，否则"没读通"会被当成"上游没标"而写进溯源文件。
SOURCE_MODEL_CARD = "model-card"  # 读到了许可字段（哪怕值特殊）
SOURCE_UNLABELED = "unlabeled"  # 读通了，但上游确实没标
SOURCE_UNREACHABLE = "unreachable"  # 没读通（网络/接口变化），沿用兜底文案


def _as_license(value) -> str:
    """许可字段可能是 str / list（HF 的 `cardData.license` 两种都见过）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_as_license(v) for v in value]
        return ", ".join(p for p in parts if p)
    return ""


def parse_hf_license(data: dict) -> str:
    """从 HF `/api/models/<repo>` 响应里提许可。

    优先 `cardData.license`（模型卡元数据），退化到 `tags` 里的 `license:xxx`。
    **实测**：`chaye741/RVC-Voice-Models` 两者都没有 → 返回空串（= 上游未标注）。
    """
    got = _as_license((data.get("cardData") or {}).get("license"))
    if got:
        return got
    for tag in data.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.split(":", 1)[1].strip()
    return ""


def parse_ms_license(data: dict) -> str:
    """从魔搭 `/api/v1/models/<id>` 响应里提许可。

    **实测**：`hudddd/Retrieval-based-Voice` 的 `Data.License` /
    `Data.LicenseName` / `Data.LicenseLink` 都是空串 → 返回空串。
    """
    payload = data.get("Data") if isinstance(data.get("Data"), dict) else data
    for key in ("License", "LicenseName"):
        got = _as_license((payload or {}).get(key))
        if got:
            return got
    return ""


def _default_get(url: str, timeout: float) -> dict:
    """默认取数：httpx（项目已有依赖），只接受 JSON 且要求 2xx。"""
    import httpx

    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def probe(
    entry: dict,
    *,
    timeout: float = 8.0,
    get: Callable[[str, float], dict] | None = None,
) -> dict:
    """探测一个市场条目的上游许可，返回可写进 `source.json` 的字段。

    返回 dict 的键：`license`（空串 = 上游未标注）、`license_source`（三态之一）、
    `license_repo`、`license_endpoint`、`license_checked_at`。

    **永不抛异常** —— 网络/接口问题落成 `unreachable`，调用方照常用兜底文案。
    """
    platform = str(entry.get("platform") or "")
    repo = str(entry.get("repo") or "")
    fetcher = get or _default_get

    if platform == "hf" and repo:
        url, parser = f"{HF_API_BASE}/api/models/{repo}", parse_hf_license
    elif platform == "modelscope" and repo:
        url, parser = f"{MS_API_BASE}/api/v1/models/{repo}", parse_ms_license
    else:
        # 平台未知（将来加源）—— 老实说"没读"，不猜
        return {
            "license": "",
            "license_source": SOURCE_UNREACHABLE,
            "license_repo": repo,
            "license_endpoint": "",
            "license_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    try:
        data = fetcher(url, timeout)
        # 只接受 JSON 对象：`None` / 数组都说明"这个响应没读懂"，
        # 不能当成"上游没标"——那是两种法务含义（见模块 docstring）。
        if not isinstance(data, dict):
            raise TypeError(f"期望 JSON 对象，得到 {type(data).__name__}")
        found = parser(data)
        return {
            "license": found,
            "license_source": SOURCE_MODEL_CARD if found else SOURCE_UNLABELED,
            "license_repo": repo,
            "license_endpoint": url,
            "license_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:  # noqa: BLE001 —— 探测失败不影响安装，落 unreachable
        return {
            "license": "",
            "license_source": SOURCE_UNREACHABLE,
            "license_repo": repo,
            "license_endpoint": url,
            "license_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def describe(fields: dict, fallback: str) -> str:
    """给前端/日志用的一句话：许可 + 它的来路（免得"上游未标注"被误读成"已确认可用"）。"""
    lic = str(fields.get("license") or "")
    kind = str(fields.get("license_source") or "")
    if kind == SOURCE_MODEL_CARD and lic:
        return f"{lic}（读自模型卡）"
    if kind == SOURCE_UNLABELED:
        return f"{fallback}（上游未标注许可 —— 未标注即默认保留所有权利）"
    return f"{fallback}（未能读取上游许可信息）"
