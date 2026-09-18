"""tests/ 目录的公共隔离：任何用例都不许碰真实的 outputs/。

为什么需要（2026-09-18 事故）：`m2_server/conftest.py` 只管 import 路径和外部资源
探测，**没有隔离任何输出路径**；而 `test_play_worker.py` 会调真实的
`wechat_voice._do_send()` —— 它内部会 `_append_history()` 写
`outputs/wechat_send_history.json`，还会起 `_restore_async` 后台线程回写。

后果实测：跑一遍 `pytest tests/test_play_worker.py`（12 个用例）后，真实历史文件的
md5 就变了，里面多出一条 `tts_x.wav / 1.0s / "微信当前没在运行"`。用户打开桌宠看到
「1 分钟前 · 1s」，会以为是自己刚发的那条 —— 排查方向被彻底带偏。

对策：在这里做**全局 autouse 隔离**。放在 tests/conftest.py 而不是各测试文件里，
是因为"忘了加隔离"的代价由用户承担（真实历史被污染），autouse 让遗忘不可能发生。
需要真实路径的用例自己 monkeypatch 回来即可。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_outputs(tmp_path, monkeypatch):
    """把 OUTPUTS_DIR 与 wechat_voice.HISTORY_FILE 一律指向 tmp_path。

    HISTORY_FILE 在 wechat_voice 里是**模块级常量**（导入时由 cfg.OUTPUTS_DIR 求值），
    所以只 patch cfg 不够，必须把模块属性也换掉。
    """
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    try:
        import wechat_voice as wv
    except Exception:      # fastapi 等依赖缺失时（用例会自己 importorskip）放行
        yield tmp_path
        return
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    yield tmp_path
