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

from pathlib import Path
from urllib.parse import quote

from market_images import image_url

# 精选条目配图：优先远程图库缓存（VM_MARKET_IMG_REPO 自动同步），回退打包图
# assets/market_imgs/<voice_id>.<ext>。角色向音色的配图为自生成的原创卡通插画
# （2026-09-14 起，取代此前无授权链的网络搜集图）。
# 无图条目前端按分类配色首字母占位。解析逻辑统一在 market_images 模块。
#
# 注：此前这里写过"人设向用 OpenMoji 主题图标（CC BY-SA 4.0）"——**那句是假的**，
# 图标从未落地到任何资产：`git log --all --diff-filter=A -- '*openmoji*'` 为空，
# market_imgs 全是自制插画。它曾让 THIRD_PARTY_NOTICES 挂上一条凭空捏造的署名义务
# （G2，2026-09-14 撤销）。注释描述意图可以，但**别把意图写成事实** ——
# 断言会被下游当成依据。真引入图标时，tools/audit_licenses.py 的 ASSET_TRIGGERS
# 会扫到并要求补署名，不靠这句注释。
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "market_imgs"


def _attach_image(item: dict) -> dict:
    """有本地配图则附加 image 字段（相对 /api 路径，前端过 mediaUrl 转绝对）。"""
    url = image_url(item["voice_id"])
    if url:
        item["image"] = url
    return item


HF_BASE = "https://hf-mirror.com"  # 主源（本机可达；镜像=HF 官方）
HF_OFFICIAL = "https://huggingface.co"  # mirror_url 用官方（远端可用，本机不可达）
MS_BASE = "https://modelscope.cn"

CHAYE_REPO = "chaye741/RVC-Voice-Models"
HUDDD_REPO = "hudddd/Retrieval-based-Voice"


def _hf_resolve(repo: str, path: str, base: str) -> str:
    """HF 直链：{base}/{repo}/resolve/main/{path}（path 做 URL 编码）。"""
    return f"{base}/{quote(repo, safe='/')}/resolve/main/{quote(path, safe='/')}"


def _ms_resolve(model_id: str, path: str) -> str:
    """魔搭直链（官方 SDK 模板）：/api/v1/models/{id}/repo?Revision=master&FilePath=.."""
    return (
        f"{MS_BASE}/api/v1/models/{quote(model_id, safe='/')}"
        f"/repo?Revision=master&FilePath={quote(path, safe='/')}"
    )


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


def _ms_entry(
    voice_id: str,
    display: str,
    path: str,
    has_index: bool = False,
    desc: str = "",
    category: str = "跨源·魔搭",
) -> dict:
    entry = {
        "id": f"ms-{voice_id}",
        "voice_id": voice_id,
        "name": display,
        "platform": "modelscope",
        "repo": HUDDD_REPO,
        "category": category,
        "desc": desc or f"魔搭下载源音色「{display}」（hudddd 精选集合 15+ 款）",
        "size_hint_mb": 55 if voice_id != "kiki" else 110,
        "license": "未标注·仅供个人学习研究，勿商用",
        "download": {"url": _ms_resolve(HUDDD_REPO, f"models/{path}/{path}.pth")},
    }
    if has_index:
        entry["index"] = {"url": _ms_resolve(HUDDD_REPO, f"models/{path}/{path}.index")}
    return entry


def _hf_entry(
    item_id: str, voice_id: str, display: str, category: str, desc: str, size_hint_mb: int = 72
) -> dict:
    """HF 精选条目（weights/<名>.pth + indices/<名>.index 成对，均已实测存在）。"""
    return {
        "id": item_id,
        "voice_id": voice_id,
        "name": display,
        "platform": "hf",
        "repo": CHAYE_REPO,
        "category": category,
        "desc": desc,
        "size_hint_mb": size_hint_mb,
        "license": "社区自训·仅供个人使用，勿商用",
        **_hf_pair(display, display),
    }


# 精选清单（2026-09-07 扩充：7 → 18 条）：
# 选品红线：不收真人政治人物/真人网红名条目（仓库里的奥巴马/特朗普/丁真/卢本伟一律不上）。
# HF 源 chaye741/RVC-Voice-Models：33 对 pth+index 实测齐全，精选 13 款（卡通/女声/男声）。
# 魔搭源 hudddd/Retrieval-based-Voice：46 款，精选 5 款（lanyangyang 双源对照 / kiki 带 index /
#   sunwukong·paidaxing 角色向 / guaiguai 甜系）。
MANIFEST: list[dict] = [
    _hf_entry(
        "katoong-lanyangyang",
        "katoong_lanyangyang",
        "卡通·懒羊羊",
        "卡通",
        "《喜羊羊》懒羊羊经典懒散腔，同款音色也上架魔搭源可切换下载",
    ),
    _hf_entry(
        "katoong-manbo",
        "katoong_manbo",
        "卡通·曼波",
        "卡通",
        "网络热梗“曼波”卡通音色，活泼奶音，适合娱乐玩梗",
    ),
    _hf_entry(
        "nv-oi",
        "nv_oi",
        "女声·OI",
        "女声",
        "元气少女声线，日常/游戏/直播通用款",
    ),
    _hf_entry(
        "nv-yuner",
        "nv_yuner",
        "女声·云儿青春",
        "女声",
        "清亮青春系女声，吐字清晰，适合朗诵/播报向内容",
    ),
    _hf_entry(
        "nv-peipei",
        "nv_peipei",
        "女声·佩佩",
        "女声",
        "俏皮活泼女声，尾音上扬有灵气，游戏开黑/日常整活",
    ),
    _hf_entry(
        "nv-beijixing",
        "nv_beijixing",
        "女声·北极星",
        "女声",
        "清冷疏离系女声，气质向，适合氛围感内容与角色配音",
    ),
    _hf_entry(
        "nv-caomei",
        "nv_caomei",
        "女声·草莓",
        "女声",
        "甜系软糯女声，甜度拉满，适合卖萌系主播与甜妹人设",
    ),
    _hf_entry(
        "nv-yalin-yujie",
        "nv_yalin_yujie",
        "女声·雅琳御姐",
        "女声",
        "成熟御姐音，气场足，适合女王人设/解说/播报",
    ),
    _hf_entry(
        "nv-xuejie",
        "nv_xuejie",
        "女声·学姐",
        "女声",
        "温柔学姐音，亲切耐听，适合讲书/陪伴类内容",
    ),
    _hf_entry(
        "male-yansang",
        "male_yansang",
        "男声·烟嗓",
        "男声",
        "低沉磁性烟嗓，适合故事/电台/低沉人设",
    ),
    _hf_entry(
        "male-nanshen",
        "male_nanshen",
        "男声·男神",
        "男声",
        "清亮通透男声，标准发音，适合播音/解说/正式场合",
    ),
    _hf_entry(
        "male-qingnian",
        "male_qingnian",
        "男声·青年",
        "男声",
        "日常青年男声，自然口语，游戏/直播/日常通用",
    ),
    _hf_entry(
        "male-qingnian-shouruo",
        "male_qingnian_shouruo",
        "男声·青年瘦弱",
        "男声",
        "偏瘦弱少年感男声，适合少年角色/弱势人设配音",
    ),
    _ms_entry(
        "lanyangyang",
        "懒羊羊（魔搭源）",
        "lanyangyang",
        has_index=False,
        desc="魔搭下载源音色「懒羊羊」，与 HF 源同款可对照下载速度",
    ),
    _ms_entry(
        "kiki",
        "Kiki（魔搭源）",
        "kiki",
        has_index=True,
        desc="魔搭源女声，带配套 index，特征检索更贴，可实时变声",
    ),
    _ms_entry(
        "sunwukong",
        "孙悟空（魔搭源）",
        "sunwukong",
        has_index=False,
        desc="西游角色向音色，戏腔感足，整活/角色扮演",
        category="角色",
    ),
    _ms_entry(
        "paidaxing",
        "派大星（魔搭源）",
        "paidaxing",
        has_index=False,
        desc="海绵宝宝角色向卡通音色，憨憨奶音，玩梗整活",
        category="角色",
    ),
    _ms_entry(
        "guaiguai",
        "乖乖（魔搭源）",
        "guaiguai",
        has_index=False,
        desc="甜系女声，温顺软和，陪伴/哄睡向内容",
        category="女声",
    ),
    # ---- 魔搭源扩容（2026-09-07，文件实测 ~52M；除 sisi 外均无 index：试听/离线可用，
    #      实时变声缺 index 检索会弱。命名按拼音可读化，音质未逐个人耳验收）----
    _ms_entry(
        "sisi",
        "丝丝（魔搭源）",
        "sisi",
        has_index=True,
        desc="魔搭源女声，带配套 index，可用于实时变声",
        category="女声",
    ),
    _ms_entry(
        "yujie2",
        "御姐（魔搭源）",
        "yujie2",
        desc="魔搭源成熟御姐声线，气场足，适合女王人设/解说",
        category="女声",
    ),
    _ms_entry(
        "tianmei",
        "甜美女声（魔搭源）",
        "tianmei",
        desc="魔搭源甜美女声，适合日常聊天/游戏开黑",
        category="女声",
    ),
    _ms_entry(
        "keruanshaonv",
        "可软少女（魔搭源）",
        "keruanshaonv",
        desc="魔搭源软萌少女声线，直播/陪玩向",
        category="女声",
    ),
    _ms_entry(
        "guanguanv1",
        "关关（魔搭源）",
        "guanguanV1",
        desc="魔搭源音色「关关」V1 版",
        category="女声",
    ),
    _ms_entry(
        "tangguo", "糖果（魔搭源）", "tangguo", desc="魔搭源甜系音色「糖果」", category="女声"
    ),
    _ms_entry("yueyue", "月月（魔搭源）", "yueyue", desc="魔搭源音色「月月」", category="女声"),
    _ms_entry(
        "xiaohuanhuan",
        "小欢欢（魔搭源）",
        "xiaohuanhuan",
        desc="魔搭源活泼音色「小欢欢」",
        category="女声",
    ),
    _ms_entry("lili", "莉莉（魔搭源）", "lili", desc="魔搭源音色「莉莉」", category="女声"),
    _ms_entry("qiqi", "奇奇（魔搭源）", "qiqi", desc="魔搭源音色「奇奇」", category="女声"),
    _ms_entry(
        "diyin",
        "低音（魔搭源）",
        "diyin",
        desc="魔搭源低音声线，适合低沉人设/电台",
        category="男声",
    ),
    _ms_entry("lulu", "露露（魔搭源）", "lulu", desc="魔搭源音色「露露」", category="女声"),
]


def get_manifest() -> list[dict]:
    """返回清单副本（避免调用方改到模块级常量），并自动挂配图。"""
    return [_attach_image(dict(item)) for item in MANIFEST]


def find_manifest_item(item_id: str) -> dict | None:
    """按条目标识（id 或 voice_id）查找清单项。"""
    for item in MANIFEST:
        if item.get("id") == item_id or item.get("voice_id") == item_id:
            return _attach_image(dict(item))
    return None
