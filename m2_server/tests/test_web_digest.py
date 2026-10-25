"""web_digest 单测：解析 / 检索词构造 / 相关性判定。

**不联网**：`search()` 用一个假 requests 顶掉；HTML 结构照 2026-10-25 从搜狗
真实响应里抄下来的两种摘要形态（带 `cacheresult_summary_*` id 与只带
`class="fz-mid space-txt"`）各一条。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_digest as wd  # noqa: E402

#: 两条结果：第一条摘要挂 id，第二条挂 class —— 只认 id 的话第二条会被砍掉。
HTML = """
<html><body>
<div class="vrwrap" id="sogou_vr_1_wrap_1"><h3 class="vr-title"><a href="/link?url=x"
  data-x="1"><em>无尽冬日</em>英雄搭配攻略:最强阵容推荐</a></h3>
<div class="fz-mid space-txt base-ellipsis clamp2" id="cacheresult_summary_1">
  第一个推荐的是赫罗尼莫、谢尔盖、茉莉的组合，持续输出很强。
</div>
<a class="citeLinkClass" href="/link?url=x"><span>搜狐网</span><span>https://www.sohu.com/a...</span><span>2024-12-31</span></a>
<div data-url="https://www.sohu.com/a/843790318_121850900"></div></div>

<div class="vrwrap" id="sogou_vr_2_wrap_2"><h3 class="vr-title"><em>无尽冬日</em>打熊阵容怎么搭</h3>
<div class="fz-mid space-txt base-ellipsis clamp2">盾矛弓按 4:1:5 配，前排承伤后排集火。</div>
<a class="citeLinkClass" href="/link?url=y"><span>九游</span><span>https://a.9game.cn/...</span><span>2025-01-02</span></a>
<div data-url="https://a.9game.cn/news/1.html"></div></div>

<div class="vrwrap"><h3 class="vr-title">搜狗相关搜索</h3><div class="fz-mid space-txt">不该出现</div></div>
</body></html>
"""


def test_parse_reads_both_summary_shapes():
    """两种摘要挂法都要认出来 —— 只认 id 会把 9 条砍成 2 条。"""
    items = wd.parse_sogou(HTML, "无尽冬日 英雄 搭配")
    assert len(items) == 2
    assert items[0]["title"] == "无尽冬日英雄搭配攻略:最强阵容推荐"
    assert items[0]["source"] == "搜狐网"
    assert items[0]["date"] == "2024-12-31"
    assert items[0]["url"] == "https://www.sohu.com/a/843790318_121850900"
    assert "盾矛弓" in items[1]["snippet"]
    assert items[1]["url"] == "https://a.9game.cn/news/1.html"


def test_parse_drops_noise_and_hits_counts():
    items = wd.parse_sogou(HTML, "无尽冬日 英雄 搭配")
    assert all("相关搜索" not in it["title"] for it in items)
    # 两个 token（无尽冬日、英雄、搭配 三段里）都出现在标题里
    assert items[0]["hits"] >= 2


def test_build_query_skips_numeric_group_names():
    """「117」这种纯数字群名当实体去搜，会命中一堆带这个数字的无关页面。

    2026-10-25 用真实 117 群跑出来的：q 变成「117 攻略」，结果全无关却判 ok=True。
    """
    q = wd.build_query("117 群")
    assert q == ""            # 没有实体也没有片段 → 明确说「不知道搜什么」


def test_build_query_takes_first_segment_of_group_name():
    assert wd.build_query("无尽冬日3864区ALL天地联盟") == "无尽冬日 攻略"


def test_build_query_adds_short_fragment():
    q = wd.build_query("无尽冬日3864区ALL天地联盟", extra="打熊阵容怎么配")
    assert q.startswith("无尽冬日 ")
    assert "打熊阵容" in q
    assert q.endswith("攻略")


def test_clean_frag_strips_at_and_emoji():
    assert wd.clean_frag("@瑜清 打熊[奸笑]怎么配") == "打熊 怎么配"
    assert len(wd.clean_frag("一二三四五六七八九十十一十二")) == 10


class _FakeResp:
    status_code = 200

    def __init__(self, body):
        self.text = body
        self.encoding = "utf-8"


def _fake_requests(monkeypatch, body, status=200, raise_exc=None):
    class R:
        @staticmethod
        def get(url, **kw):
            if raise_exc:
                raise raise_exc
            resp = _FakeResp(body)
            resp.status_code = status
            return resp

    monkeypatch.setitem(sys.modules, "requests", R)


def test_search_rejects_irrelevant_results(monkeypatch):
    """HTTP 200 不等于查到了 —— 结果跟问题对不上时必须说「没搜到」。

    必应 RSS 那条路就是这么坑的：搜游戏攻略返回 DeepSeek 官网，XML 结构还完好。
    """
    _fake_requests(monkeypatch, HTML)
    out = wd.search("汽水音乐怎么下载")
    assert out["ok"] is False
    assert out["items"] == []
    assert out["weak"] is True          # 查到了页面，但内容对不上


def test_search_accepts_relevant_results(monkeypatch):
    """门槛是「过半 token 命中」：3 个词要中 2 个。

    fixture 里第二条只中了「无尽冬日」，会被砍掉 —— 这是**故意的**：
    宁可少给一条，也不要给一条答非所问的（真机上 8 条全是 hits=3，不受影响）。
    """
    _fake_requests(monkeypatch, HTML)
    out = wd.search("无尽冬日 英雄 搭配", limit=2)
    assert out["ok"] is True
    assert len(out["items"]) == 1
    assert "英雄搭配" in out["items"][0]["title"]
    assert out["weak"] is False


def test_search_handles_network_failure(monkeypatch):
    _fake_requests(monkeypatch, "", raise_exc=TimeoutError("boom"))
    out = wd.search("无尽冬日")
    assert out["ok"] is False
    assert "没连上" in out["note"]


def test_search_handles_captcha_page(monkeypatch):
    _fake_requests(monkeypatch, "<html>请输入验证码</html>")
    out = wd.search("无尽冬日")
    assert out["ok"] is False
    assert "验证码" in out["note"]


def test_search_empty_query_is_not_sent(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("空 query 不该发请求")

    class R:
        get = staticmethod(boom)

    monkeypatch.setitem(sys.modules, "requests", R)
    out = wd.search("")
    assert out["ok"] is False
    assert "不知道搜什么" in out["note"]


# ---- 检索词守卫：群名给不出实体时，宁可说「不知道搜什么」 -------------------

def test_useful_frag_rejects_bare_numbers():
    """对方报「2562」，这不是检索词。

    2026-10-25 真实 117 群跑出来的坑：拿 2562 去搜，搜狗返回 4 条分属 4 个
    不同游戏的页面，而相关性校验只看「有没有出现 2562」，于是一屏垃圾被当成了
    「查到了」。宁可让用户自己打一个游戏名。
    """
    assert wd.useful_frag("2562") is False
    assert wd.useful_frag("117") is False
    assert wd.useful_frag("1") is False


def test_useful_frag_rejects_interjections():
    assert wd.useful_frag("嗯嗯") is False
    assert wd.useful_frag("好的") is False
    assert wd.useful_frag("哈哈哈") is False


def test_useful_frag_accepts_real_terms():
    assert wd.useful_frag("打熊") is True
    assert wd.useful_frag("打熊怎么配") is True
    assert wd.useful_frag("godzilla") is True


def test_build_query_empty_when_group_name_is_number():
    """「117」这种群名 + 对方报数 = 拼不出检索词，必须返回空串。"""
    assert wd.build_query("117", None, "2562") == ""
    assert wd.build_query("117", None, "") == ""


def test_build_query_keeps_number_when_group_has_entity():
    """群名有实体时，「无尽冬日 2562 攻略」是能搜的 —— 数字有关卡含义。"""
    assert wd.build_query("无尽冬日3864区ALL天地联盟", None, "2562") == "无尽冬日 2562 攻略"


def test_search_without_query_says_type_a_word():
    out = wd.search("")
    assert out["ok"] is False
    assert out["items"] == []
    assert "说个词" in out["note"]
