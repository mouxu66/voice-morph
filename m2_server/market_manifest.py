"""音色市场 —— 内置精选清单（推荐 Tab 数据源）。

两源条目均经仓库文件 API 实测验证（2026-09-05）：
  - HF 源 chaye741/RVC-Voice-Models：weights/<名>.pth + indices/<名>.index 33 对
  - 魔搭源 hudddd/Retrieval-based-Voice：models/<名>/<名>.pth（55MB 推理权重），
    其中 kiki / keruanv2 / sisi 带配套 .index；lanyangyang 与 HF 源同音色，
    便于演示"同一音色切换下载源"。

voice_id 用 ASCII（RVC logs/assets 目录名要求安全字符），display_name 显示中文。
下载 URL 全部落在域名白名单（hf-mirror.com / huggingface.co / modelscope.cn）。
版权提示：社区自训音色多为"仅供学习研究"，这里统一标注 license，前端展示提醒。
"""
from urllib.parse import quote

HF_BASE = "https://hf-mirror.com"          # 主源（本机可达；镜像=HF 官方）
HF_OFFICIAL = "https://huggingface.co"     # mirror_url 用官方（远端可用，本机不可达）
MS_BASE = "https://modelscope.cn"

CHAYE_REPO = "chaye741/RVC-Voice-Models"
HUDDD_REPO = "hudddd/Retrieval-based-Voice"


def _hf_resolve(repo: str, path: str, base: str) -> str:
    """HF 直链：{base}/{repo}/resolve/main/{path}（path 做 URL 编码）。"""
    return f"{base}/{quote(repo, safe='/')}/resolve/main/{quote(path, safe='/')}"


def _ms_resolve(model_id: str, path: str) -> str:
    """魔搭直链（官方 SDK 模板）：/api/v1/models/{id}/repo?Revision=master&FilePath=.."""
    return (f"{MS_BASE}/api/v1/models/{quote(model_id, safe='/')}"
            f"/repo?Revision=master&FilePath={quote(path, safe='/')}")


def _hf_pair(display: str, path: str) -> dict:
    """构造 HF 条目 download/index（主源 hf-mirror，镜像=官方）。"""
    pth_path = f"weights/{path}.pth"
    idx_path = f"indices/{path}.index"
    return {
        "download": {
            "url": _hf_resolve(CHAYE_REPO, pth_path, HF_BASE),
            "mirror_url": _hf_resolve(CHAYE_REPO, pth_path, HF_OFFICIAL),
        },
        "index": {
            "url": _hf_resolve(CHAYE_REPO, idx_path, HF_BASE),
            "mirror_url": _hf_resolve(CHAYE_REPO, idx_path, HF_OFFICIAL),
        },
    }


def _ms_entry(voice_id: str, display: str, path: str, has_index: bool) -> dict:
    entry = {
        "id": f"ms-{voice_id}",
        "voice_id": voice_id,
        "name": display,
        "platform": "modelscope",
        "repo": HUDDD_REPO,
        "category": "跨源·魔搭",
        "desc": f"魔搭下载源音色「{display}」（hudddd 精选集合 15+ 款）",
        "size_hint_mb": 55 if voice_id != "kiki" else 110,
        "license": "未标注·仅供个人学习研究，勿商用",
        "download": {"url": _ms_resolve(HUDDD_REPO, f"models/{path}/{path}.pth")},
    }
    if has_index:
        entry["index"] = {"url": _ms_resolve(HUDDD_REPO, f"models/{path}/{path}.index")}
    return entry


# 推荐清单：HF 源 5 款（卡通/女声/男声，避开真人真人姓名/名人类目）
# + 魔搭源 2 款（lanyangyang 与 HF 源同音色做双源对照；kiki 带 index 可实时变声）
MANIFEST: list[dict] = [
    {
        "id": "katoong-lanyangyang",
        "voice_id": "katoong_lanyangyang",
        "name": "卡通·懒羊羊",
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": "卡通",
        "desc": "《喜羊羊》懒羊羊经典懒散腔，同款音色也上架魔搭源可切换下载",
        "size_hint_mb": 72,
        "license": "社区自训·仅供个人使用，勿商用（卡通角色 IP 亦有风险）",
        "demo": "https://huggingface.co/chaye741/RVC-Voice-Models/resolve/main/weights/卡通-懒羊羊.pth",
        **_hf_pair("卡通-懒羊羊", "卡通-懒羊羊"),
    },
    {
        "id": "katoong-manbo",
        "voice_id": "katoong_manbo",
        "name": "卡通·曼波",
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": "卡通",
        "desc": "网络热梗“曼波”卡通音色，活泼奶音，适合娱乐玩梗",
        "size_hint_mb": 72,
        "license": "社区自训·仅供个人使用，勿商用",
        **_hf_pair("卡通-曼波", "卡通-曼波"),
    },
    {
        "id": "nv-oi",
        "voice_id": "nv_oi",
        "name": "女声·OI",
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": "女声",
        "desc": "元气少女声线，日常/游戏/直播通用款",
        "size_hint_mb": 72,
        "license": "社区自训·仅供个人使用，勿商用",
        **_hf_pair("女声-OI", "女声-OI"),
    },
    {
        "id": "nv-yuner",
        "voice_id": "nv_yuner",
        "name": "女声·云儿青春",
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": "女声",
        "desc": "清亮青春系女声，吐字清晰，适合朗诵/播报向内容",
        "size_hint_mb": 72,
        "license": "社区自训·仅供个人使用，勿商用",
        **_hf_pair("女声-云儿青春", "女声-云儿青春"),
    },
    {
        "id": "male-yansang",
        "voice_id": "male_yansang",
        "name": "男声·烟嗓",
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": "男声",
        "desc": "低沉磁性烟嗓，适合故事/电台/低沉人设",
        "size_hint_mb": 72,
        "license": "社区自训·仅供个人使用，勿商用",
        **_hf_pair("男声-烟嗓", "男声-烟嗓"),
    },
    _ms_entry("lanyangyang", "懒羊羊（魔搭源）", "lanyangyang", has_index=False),
    _ms_entry("kiki", "Kiki（魔搭源）", "kiki", has_index=True),
]


def get_manifest() -> list[dict]:
    """返回清单副本（避免调用方改到模块级常量）。"""
    return [dict(item) for item in MANIFEST]


def find_manifest_item(item_id: str) -> dict | None:
    """按条目标识（id 或 voice_id）查找清单项。"""
    for item in MANIFEST:
        if item.get("id") == item_id or item.get("voice_id") == item_id:
            return dict(item)
    return None