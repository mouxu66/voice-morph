"""`vision_grab`（看图：双击缩略图 → 截预览窗 → 关窗）的**产物落点**。

为什么值得单测：这张图一次有 2.0 MB 上下（实测 2315×1550 的 PNG），而它原先写在
`outputs/eval-3864/shots/grabbed.png` —— 一个 **eval 证据目录**：既不在"退出即删"的
会话目录里，也没有任何按年龄的清理会收它，等于永久留档（而且每次覆盖同名文件，
看着"只有一张"，实际是用户磁盘上长期躺着一张几 MB 的图）。用户 2026-10-27 原话：

    我想就是随用随删就是一天内他就删掉

两个判据：
    1. 默认落 `outputs/.session/`（随启动清空 / 退出即删，且有 24h TTL 兜底）；
    2. 写之前顺手扫掉过期的旧产物（`session_out.prune_expired`）。

`out_path` 显式指定的那份仍然听调用方的 —— `tools/` 里的探针都靠这条。
整条链路用替身换掉：不碰微信、不点鼠标、不发按键、不截真屏。
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import config as cfg  # noqa: E402
import session_out  # noqa: E402

#: 与 `test_chat_radar` 同一套合成图节奏：字高 18、行高 22（间隙 4 < ROW_GAP=5，
#: 多行会被合并成一块 —— 正好是"一条够高的左侧消息"）。
_LINE_H = 18
_LINE_STEP = 22


@pytest.fixture()
def outputs(tmp_path, monkeypatch):
    """把 `cfg.OUTPUTS_DIR` 指到临时目录（`session_out` 每次现读，立刻生效）。"""
    d = tmp_path / "outputs"
    d.mkdir()
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", d)
    session_out._SAVED.clear()
    return d


class _Win:
    """最小 WindowInfo 替身。`rect` 是 `(left, top, width, height)`，不是右下角。"""

    rect = (100, 100, 800, 400)


class _Frame:
    def __init__(self, image, roi):
        self.image, self.roi, self.window = image, roi, _Win()


def _chat_image() -> np.ndarray:
    """一块够高的左侧消息（`_biggest_left_block` 只要 `height >= 140` 的 left 块）。"""
    img = np.full((400, 800, 3), 250, dtype=np.uint8)
    y = 40
    while y < 240 - 2:
        img[y : y + _LINE_H, 20:200:2] = 60
        y += _LINE_STEP
    return img


@pytest.fixture()
def grabber(monkeypatch):
    """装配好的 `vision_grab`，整条桌面链路已换成替身。"""
    from PIL import Image

    from chat_radar import capture as cap

    import vision_grab as vg

    class _WV:
        """`wechat_voice` 替身：只需要两个鼠标动作。"""

        moved: list = []

        @staticmethod
        def _mouse_move_abs(x, y):
            _WV.moved.append((x, y))

        @staticmethod
        def _mouse_left(down):
            _WV.moved.append(bool(down))

    monkeypatch.setitem(sys.modules, "wechat_voice", _WV)  # 挡在 import 之前，别真加载它
    monkeypatch.setattr(cap, "enable_dpi_awareness", lambda: None)
    monkeypatch.setattr(
        cap,
        "capture_frame",
        lambda: _Frame(_chat_image(), cap.Roi(left=0, top=0, right=800, bottom=400)),
    )
    monkeypatch.setattr(vg, "_wechat_pid", lambda: 4242)
    monkeypatch.setattr(vg, "_top_level_windows", lambda pid: {})
    monkeypatch.setattr(vg, "_press_esc", lambda: None)
    monkeypatch.setattr(vg, "OPEN_WAIT", 0.0)
    monkeypatch.setattr(vg, "CLOSE_WAIT", 0.0)
    monkeypatch.setattr(
        "PIL.ImageGrab.grab",
        lambda *a, **kw: Image.new("RGB", (600, 400), (210, 210, 210)),
    )
    return vg


def _age(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_grab_shot_lands_in_session_dir_and_sweeps_old(outputs, grabber, monkeypatch):
    """★ 默认落点必须是会话目录，且写之前顺手扫掉过期旧产物。"""
    old = session_out.ensure() / "wxshot_old.png"
    old.write_bytes(b"x" * 1024)
    _age(old, 30 * 3600)
    monkeypatch.setattr(session_out, "_LAST_PRUNE", 0.0)

    path, note = grabber.grab_biggest_image()

    assert path, f"没截出图：{note}"
    p = Path(path)
    assert session_out.is_session(p), f"截图没落在会话目录，落在：{p}"
    assert p.suffix == ".png" and p.stat().st_size > 0
    assert not old.exists(), "写新图的时候没有顺手扫掉过期旧产物"


def test_grab_respects_explicit_out_path(outputs, grabber, tmp_path):
    """显式给了路径就听调用方的（`tools/` 里的探针靠这条）。"""
    dst = tmp_path / "explicit.png"

    path, note = grabber.grab_biggest_image(dst)

    assert path == str(dst), note
    assert dst.is_file()
    assert not session_out.is_session(dst)
