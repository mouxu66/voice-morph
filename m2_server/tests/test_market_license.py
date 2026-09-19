"""市场音色许可回读（G4）测试。

这组测试要钉住的核心不是"能不能取到许可"，而是**"取不到时说的是哪句话"**：

  - 上游**没标**许可（`unlabeled`）
  - 我们**没读通**（`unreachable`）

这两件事在法务含义上完全不同（前者=默认保留所有权利，后者=未知要再查），
但都表现为"没有许可名"。一旦在代码里塌缩成同一个值，`source.json` 就会开始撒谎 ——
而它正是别人日后复查时唯一的依据。所以下面每个分支都有独立用例。

夹具里两段 payload 的**形状来自 2026-09-14 的真实响应**（已记进
`THIRD_PARTY_NOTICES.md` §5 G4）：两个上游仓库都**没有**标注许可。
"""

from __future__ import annotations

import market_license as ml
import pytest

#: 真实响应形状：hf-mirror 返回 200，但没有 cardData，tags 只有 region:us
HF_UNLABELED = {
    "id": "chaye741/RVC-Voice-Models",
    "tags": ["region:us"],
    "downloads": 0,
    "likes": 8,
    "siblings": [{"rfilename": "weights/x.pth"}],
}

#: 真实响应形状：魔搭返回 200，Data 里三个许可字段都是空串
MS_UNLABELED = {
    "Code": 200,
    "Success": True,
    "Data": {
        "Name": "hudddd/Retrieval-based-Voice",
        "License": "",
        "LicenseName": "",
        "LicenseLink": "",
    },
}


def _entry(platform: str = "hf", repo: str = "owner/repo") -> dict:
    return {"platform": platform, "repo": repo, "license": "兜底文案"}


def _get(payload):
    """构造一个注入用的取数函数，并记录被请求的 URL。"""
    calls: list[str] = []

    def _fn(url: str, timeout: float):  # noqa: ARG001
        calls.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _fn, calls


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize(
    "value,expect",
    [
        ("mit", "mit"),
        ("  apache-2.0  ", "apache-2.0"),
        (["cc-by-nc-4.0"], "cc-by-nc-4.0"),  # HF 的 cardData.license 见过 list
        (["mit", "cc0-1.0"], "mit, cc0-1.0"),
        ([], ""),
        (None, ""),
        ("", ""),
    ],
)
def test_parse_hf_license_handles_shapes(value, expect):
    """`cardData.license` 的类型不固定，写死成 str 会在某些仓库静默变空。"""
    assert ml.parse_hf_license({"cardData": {"license": value}}) == expect


def test_parse_hf_license_falls_back_to_tags():
    """模型卡没写但 tags 有 `license:xxx` 时也要认。"""
    assert ml.parse_hf_license({"tags": ["region:us", "license:mit"]}) == "mit"


def test_parse_hf_license_returns_empty_when_upstream_says_nothing():
    """**真实情形**：chaye741/RVC-Voice-Models 就是这样的响应。"""
    assert ml.parse_hf_license(HF_UNLABELED) == ""


def test_parse_ms_license_reads_data_block():
    assert ml.parse_ms_license({"Data": {"License": "Apache-2.0"}}) == "Apache-2.0"
    assert ml.parse_ms_license({"Data": {"LicenseName": "MIT"}}) == "MIT"


def test_parse_ms_license_returns_empty_when_all_fields_blank():
    """**真实情形**：hudddd/Retrieval-based-Voice 三个字段都是空串。"""
    assert ml.parse_ms_license(MS_UNLABELED) == ""


# ------------------------------------------------------------------ probe 三态
def test_probe_hf_reads_model_card():
    get, calls = _get({"cardData": {"license": "mit"}})
    out = ml.probe(_entry("hf"), get=get)
    assert out["license"] == "mit"
    assert out["license_source"] == ml.SOURCE_MODEL_CARD
    assert out["license_endpoint"] == "https://hf-mirror.com/api/models/owner/repo"
    assert calls == [out["license_endpoint"]]
    assert out["license_checked_at"]


def test_probe_modelscope_reads_data_block():
    get, calls = _get({"Data": {"License": "Apache-2.0"}})
    out = ml.probe(_entry("modelscope"), get=get)
    assert out["license"] == "Apache-2.0"
    assert out["license_source"] == ml.SOURCE_MODEL_CARD
    assert calls[0].startswith("https://modelscope.cn/api/v1/models/")


def test_probe_distinguishes_unlabeled_from_unreachable():
    """**本组最重要的用例**：没标 ≠ 没读通。

    `unlabeled` 说明"我们查过了，上游确实什么都没写"——这才是有价值的信息
    （默认保留所有权利）。塌缩成 `unreachable` 会让人以为只是没查成，
    日后可能有人"再查一次就好了"，实际上是白费功夫。
    """
    get, _ = _get(HF_UNLABELED)
    assert ml.probe(_entry("hf"), get=get)["license_source"] == ml.SOURCE_UNLABELED

    get, _ = _get(RuntimeError("connection reset"))
    assert ml.probe(_entry("hf"), get=get)["license_source"] == ml.SOURCE_UNREACHABLE


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("timeout"),  # 网络挂了
        ValueError("bad json"),  # 响应不是 JSON
        ["不是对象"],  # 响应是数组
        None,  # 空响应
    ],
)
def test_probe_never_raises_and_keeps_fallback(payload):
    """探测是安装收尾步骤，任何异常都必须被吸收 —— 否则网络抖动会让用户装不上音色。"""
    get, _ = _get(payload)
    out = ml.probe(_entry("hf"), get=get)
    assert out["license"] == ""
    assert out["license_source"] == ml.SOURCE_UNREACHABLE


def test_probe_unknown_platform_is_honest():
    """将来加新源却没写 parser 时，宁可说"没读"也不猜一个。"""
    get, calls = _get({"cardData": {"license": "mit"}})
    out = ml.probe(_entry("kaggle"), get=get)
    assert out["license_source"] == ml.SOURCE_UNREACHABLE
    assert out["license_endpoint"] == ""
    assert calls == []  # 不该乱请求


def test_probe_missing_repo_is_honest():
    get, calls = _get({"cardData": {"license": "mit"}})
    out = ml.probe({"platform": "hf", "repo": ""}, get=get)
    assert out["license_source"] == ml.SOURCE_UNREACHABLE
    assert calls == []


# ------------------------------------------------------------------ 文案
def test_describe_spells_out_where_the_license_came_from():
    """三态在 UI 上必须说人话，否则"上游未标注"会被读成"已确认可用"。"""
    fallback = "仅供个人学习研究，勿商用"
    assert "读自模型卡" in ml.describe(
        {"license": "mit", "license_source": ml.SOURCE_MODEL_CARD}, fallback
    )
    text = ml.describe({"license": "", "license_source": ml.SOURCE_UNLABELED}, fallback)
    assert "上游未标注许可" in text and "保留所有权利" in text
    assert "未能读取" in ml.describe(
        {"license": "", "license_source": ml.SOURCE_UNREACHABLE}, fallback
    )


# ------------------------------------------------------------------ 离线保证
def test_default_fetch_is_stubbed_in_tests():
    """conftest 打了 `_default_get` 的桩：不传 get 时必须落 unreachable 而不是发请求。

    这条守的是"测试不依赖外网"这件事本身。它红了说明 conftest 的离线夹具被删了，
    于是**每一条跑安装的用例**都会偷偷发真实 HTTP 请求（表现为 CI 变慢/偶发超时）。
    """
    out = ml.probe(_entry("hf"))
    assert out["license_source"] == ml.SOURCE_UNREACHABLE


# ------------------------------------------------------------------ 落盘与展示
def test_source_json_records_license_once_installed(tmp_path, monkeypatch):
    """`write_source` 要把三态一起落盘（这是日后复查的唯一依据）。"""
    import market_install

    monkeypatch.setattr(market_install.cfg, "RVC_ROOT", tmp_path)
    mgr = market_install.InstallManager.__new__(market_install.InstallManager)
    mgr.write_source(
        "v1",
        "demo/001",
        "演示音色",
        {
            "license": "",
            "license_source": ml.SOURCE_UNLABELED,
            "license_repo": "chaye741/RVC-Voice-Models",
            "license_endpoint": "https://hf-mirror.com/api/models/chaye741/RVC-Voice-Models",
            "license_checked_at": "2026-09-14 21:00:00",
        },
    )
    import json

    data = json.loads((tmp_path / "logs" / "v1" / "source.json").read_text("utf-8"))
    assert data["source"] == "market"
    assert data["license_source"] == "unlabeled"
    assert data["license_repo"] == "chaye741/RVC-Voice-Models"


def test_write_source_without_license_fields_still_works(tmp_path, monkeypatch):
    """探测失败（返回 None）时不能把 source.json 写坏 —— 溯源标记比许可信息更重要。"""
    import json

    import market_install

    monkeypatch.setattr(market_install.cfg, "RVC_ROOT", tmp_path)
    mgr = market_install.InstallManager.__new__(market_install.InstallManager)
    mgr.write_source("v2", "demo/002", "另一音色")
    data = json.loads((tmp_path / "logs" / "v2" / "source.json").read_text("utf-8"))
    assert data["source"] == "market"
    assert "license_source" not in data


def test_exp_license_exposes_only_market_installs(tmp_path, monkeypatch):
    """自训音色没有 source.json → 不该冒出一个空的许可行给前端。"""
    import json

    import rvc_common

    monkeypatch.setattr(rvc_common.cfg, "RVC_ROOT", tmp_path)
    logs = tmp_path / "logs"

    (logs / "self_trained").mkdir(parents=True)
    (logs / "self_trained" / "meta.json").write_text('{"display_name": "自训"}', encoding="utf-8")
    assert rvc_common.exp_license("self_trained") == {}

    (logs / "from_market").mkdir(parents=True)
    (logs / "from_market" / "source.json").write_text(
        json.dumps(
            {
                "source": "market",
                "display_name": "市场",
                "license": "",
                "license_source": "unlabeled",
                "license_checked_at": "2026-09-14 21:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    got = rvc_common.exp_license("from_market")
    assert got["license_source"] == "unlabeled"
    assert got["source_license"] == ""
    assert got["license_checked_at"] == "2026-09-14 21:00:00"
