"""wechat_uia 模块单测（不碰真实微信进程，全程 fake）。

覆盖：PE 解析、RVA 缓存、热激活状态机、配置开关、控件查询的安全降级。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wechat_uia as u

# ---------------- 假控件 ----------------


class FakeRect:
    def __init__(self, box):
        self.left, self.top, self.right, self.bottom = box


class FakeCtrl:
    def __init__(self, box=None, name="", class_name="", children=(), exists=True):
        self._box = box
        self.Name = name
        self.ClassName = class_name
        self._children = list(children)
        self._exists = exists

    @property
    def BoundingRectangle(self):
        if self._box is None:
            raise RuntimeError("no rect")
        return FakeRect(self._box)

    def Exists(self, timeout=0, waitTime=0):
        return self._exists

    def GetChildren(self):
        return self._children

    def Click(self):
        self.clicked = True


class FakeRoot(FakeCtrl):
    def Control(self, **kw):
        return self._next


@pytest.fixture(autouse=True)
def _clear_state():
    u.reset_state()
    yield
    u.reset_state()


# ---------------- 纯函数 ----------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ('语音15"秒', 15.0),
        ('语音1"秒', 1.0),
        ('语音60"秒', 60.0),
        ('语音 8" 秒', 8.0),
        ("语音7″秒", 7.0),
        (None, None),
        ("", None),
        ("没有秒数", None),
    ],
)
def test_duration_from_message(name, expected):
    assert u.duration_from_message(name) == expected


def test_rect_of_and_center():
    c = FakeCtrl(box=(10, 20, 110, 220))
    assert u.rect_of(c) == (10, 20, 110, 220)
    assert u.center_of(c) == (60, 120)


def test_rect_of_degenerate_returns_none():
    assert u.rect_of(FakeCtrl(box=(10, 20, 10, 20))) is None  # 零面积
    assert u.rect_of(FakeCtrl(box=None)) is None  # 抛异常


def test_pe_sections_rejects_garbage():
    assert u._pe_sections(b"not a pe file") == []
    assert u._pe_sections(b"") == []


def test_section_lookup():
    secs = [
        {
            "name": ".text",
            "rva": 0x1000,
            "vsize": 0x2000,
            "raw_size": 0x2000,
            "raw_ptr": 0x400,
            "chars": u.IMAGE_SCN_MEM_EXECUTE,
        }
    ]
    assert u._section_for_rva(secs, 0x1500)["name"] == ".text"
    assert u._section_for_rva(secs, 0x9000) is None
    assert u._offset_to_rva(secs, 0x500) == 0x1100
    assert u._offset_to_rva(secs, 0x10) is None


def test_scan_gate_rva_missing_file(tmp_path):
    rva, cands = u.scan_gate_rva(tmp_path / "nope.dll")
    assert rva is None and cands == []


# ---------------- RVA 缓存 ----------------


def test_rva_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "_rva_cache_path", tmp_path / "gate.json")
    dll = Path("C:/x/Weixin.dll")
    assert u._load_cached_rva(dll, 100, 5.0) is None  # 未写过
    u._save_cached_rva(dll, 100, 5.0, 0xAD19668)
    assert u._load_cached_rva(dll, 100, 5.0) == 0xAD19668
    assert u._load_cached_rva(dll, 101, 5.0) is None  # size 变了
    assert u._load_cached_rva(dll, 100, 99.0) is None  # mtime 变了（升级微信）
    assert u._load_cached_rva(dll, 100, 5.0) == 0xAD19668  # 原记录没被破坏


def test_rva_cache_survives_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "gate.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(u, "_rva_cache_path", p)
    assert u._load_cached_rva(Path("d.dll"), 1, 1.0) is None
    u._save_cached_rva(Path("d.dll"), 1, 1.0, 0x10)
    assert u._load_cached_rva(Path("d.dll"), 1, 1.0) == 0x10


# ---------------- 热激活状态机 ----------------


def test_ensure_active_disabled_by_env(monkeypatch):
    monkeypatch.setattr(u, "UIA_ENABLED", False)
    assert u.ensure_active() is False


def test_ensure_active_no_wechat(monkeypatch):
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (None, None))
    assert u.ensure_active() is False
    assert "未运行" in u.status()["reason"]


def test_ensure_active_no_weixin_dll(monkeypatch):
    """微信 3.9（WeChat.exe）没有 Weixin.dll → 直接退化。"""
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (1234, 4321))
    monkeypatch.setattr(u, "_weixin_dll", lambda pid: None)
    assert u.ensure_active() is False
    assert "Weixin.dll" in u.status()["reason"]


def test_ensure_active_writes_byte_and_succeeds(monkeypatch, tmp_path):
    """byte=0 → 写 1 → 树物化为 mmui:: → True；缓存命中后不重复扫描。"""
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (1234, 4321))
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"x")
    monkeypatch.setattr(u, "_weixin_dll", lambda pid: (0x140000000, 1, str(dll)))
    monkeypatch.setattr(u, "_rva_cache_path", tmp_path / "gate.json")
    monkeypatch.setattr(u.time, "sleep", lambda s: None)

    scans = {"n": 0}

    def fake_scan(path):
        scans["n"] += 1
        return 0xAD19668, [(1, 0xAD19668)]

    monkeypatch.setattr(u, "scan_gate_rva", fake_scan)
    mem = {"v": 0}
    monkeypatch.setattr(u, "read_byte", lambda pid, addr: mem["v"])
    writes = []
    monkeypatch.setattr(
        u,
        "write_byte",
        lambda pid, addr, v: (writes.append((addr, v)), mem.__setitem__("v", v), True)[-1],
    )
    monkeypatch.setattr(u, "_root_class", lambda hwnd: u.CLASS_MAIN)

    assert u.ensure_active() is True
    assert writes == [(0x140000000 + 0xAD19668, 1)]
    assert scans["n"] == 1
    assert u.status()["rva"] == "0xad19668"

    # 第二次：PID 未变且已就绪 → 直接命中，不再扫描/写内存
    assert u.ensure_active() is True
    assert scans["n"] == 1 and len(writes) == 1
    # 缓存文件已落盘（跨进程复用）
    assert u._load_cached_rva(dll, *u._dll_mtime(dll)) == 0xAD19668


def test_ensure_active_scan_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (1, 2))
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"x")
    monkeypatch.setattr(u, "_weixin_dll", lambda pid: (0x1000, 1, str(dll)))
    monkeypatch.setattr(u, "_rva_cache_path", tmp_path / "gate.json")
    monkeypatch.setattr(u, "scan_gate_rva", lambda p: (None, []))
    assert u.ensure_active() is False
    assert "gate" in u.status()["reason"]


def test_ensure_active_read_failure(monkeypatch, tmp_path):
    """OpenProcess 失败（权限不足）→ False，不抛异常。"""
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (1, 2))
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"x")
    monkeypatch.setattr(u, "_weixin_dll", lambda pid: (0x1000, 1, str(dll)))
    monkeypatch.setattr(u, "_rva_cache_path", tmp_path / "gate.json")
    monkeypatch.setattr(u, "scan_gate_rva", lambda p: (0x20, []))
    monkeypatch.setattr(u, "read_byte", lambda pid, addr: None)
    assert u.ensure_active() is False
    assert "权限" in u.status()["reason"]


def test_ensure_active_tree_not_materialized(monkeypatch, tmp_path):
    """byte 已是 1 但树还是 Qt 空壳 → 不能算就绪（上层要回退像素）。"""
    monkeypatch.setattr(u, "UIA_ENABLED", True)
    monkeypatch.setattr(u, "_wechat_hwnd_pid", lambda: (1, 2))
    dll = tmp_path / "Weixin.dll"
    dll.write_bytes(b"x")
    monkeypatch.setattr(u, "_weixin_dll", lambda pid: (0x1000, 1, str(dll)))
    monkeypatch.setattr(u, "_rva_cache_path", tmp_path / "gate.json")
    monkeypatch.setattr(u, "scan_gate_rva", lambda p: (0x20, []))
    monkeypatch.setattr(u, "read_byte", lambda pid, addr: 1)
    monkeypatch.setattr(u, "_root_class", lambda hwnd: "Qt51514QWindowIcon")
    assert u.ensure_active() is False
    assert "未物化" in u.status()["reason"]


# ---------------- 控件查询的降级行为 ----------------


def test_queries_return_none_without_root(monkeypatch):
    monkeypatch.setattr(u, "_root", lambda: None)
    assert u.input_field_rect() is None
    assert u.voice_button_rect() is None
    assert u.overlay_rect() is None
    assert u.overlay_exists() is False
    assert u.send_button_rect() is None
    assert u.cancel_button_rect() is None
    assert u.voice_messages() == []
    assert u.latest_voice_message() is None
    assert u.click_voice_button() is False


def test_find_uses_short_timeout(monkeypatch):
    """_find 必须把 timeout 透传给 Exists，否则轮询会白等满默认超时。"""
    seen = []

    class C(FakeCtrl):
        def Exists(self, timeout=0, waitTime=0):
            seen.append(timeout)
            return False

    root = FakeRoot()
    root._next = C()
    monkeypatch.setattr(u, "_root", lambda: root)
    u._find(timeout=0.25, ClassName="mmui::ChatVoiceRecordView")
    assert seen == [0.25]


def test_voice_button_rect_reads_box(monkeypatch):
    root = FakeRoot()
    root._next = FakeCtrl(box=(1113, 1525, 1155, 1567), name=u.NAME_VOICE_BTN)
    monkeypatch.setattr(u, "_root", lambda: root)
    assert u.voice_button_rect() == (1113, 1525, 1155, 1567)
    assert u.center_of(FakeCtrl(box=(1113, 1525, 1155, 1567))) == (1134, 1546)


def test_voice_messages_walks_tree(monkeypatch):
    leaf = FakeCtrl(class_name=u.CLASS_VOICE_MSG, name='语音15"秒')
    mid = FakeCtrl(children=[leaf])
    root = FakeCtrl(children=[mid])
    monkeypatch.setattr(u, "_root", lambda: root)
    assert u.voice_messages() == ['语音15"秒']
    assert u.latest_voice_message() == '语音15"秒'


def test_latest_voice_message_picks_lowest(monkeypatch):
    """多条语音时取最靠下（最新）的那条，而不是树序最后一条。"""
    a = FakeCtrl(box=(0, 100, 10, 110), class_name=u.CLASS_VOICE_MSG, name='语音60"秒')
    b = FakeCtrl(box=(0, 900, 10, 910), class_name=u.CLASS_VOICE_MSG, name='语音6"秒')
    root = FakeCtrl(children=[b, a])  # 树序故意把新的放前面
    monkeypatch.setattr(u, "_root", lambda: root)
    assert u.latest_voice_message() == '语音6"秒'


def test_status_snapshot_shape():
    st = u.status()
    assert set(st) == {"enabled", "ready", "pid", "rva", "dll", "reason"}
