"""微调续训（C2）单测：ft_train 的 init_from 解析与透传。

回归背景：finetune.py::_train_job() 组装的训练命令此前没有 --init_model_path，
每次加素材都从 base 全量重训。sft_8gb.py 本身支持 --init_model_path（加载任意
完整模型目录），ckpt/ft_model 保存逻辑对续训自洽（硬链接 init 文件、anchor
embedding 每次覆盖写入 codec_embedding[3000]）。

覆盖：
  - _resolve_init_from：空串→base；非法名→400；不存在→404；
    已发布 ft_model 优先；无 ft_model 回退最新 ckpt
  - ft_train 路由：合法 init_from 透传给训练线程 + status 落盘 + 返回体可见
  - _train_job cmd：init_model 非空时命令含 --init_model_path
"""
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import finetune as ft  # noqa: E402

SRC = "srckoi"
DST = "dstkoi"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 media 目录：voicebank + ft_output + 一个可训练的 dst 语料。"""
    media = tmp_path / "media"
    (media / "ft" / DST / "clips").mkdir(parents=True)
    vb = media / "voicebank"
    fo = tmp_path / "tts_trial" / "ft_output"
    monkeypatch.setattr(ft, "FT_DIR", media / "ft")
    monkeypatch.setattr(ft, "VOICEBANK", vb)
    monkeypatch.setattr(ft, "ROOT", tmp_path)
    # dst 语料：status.json + jsonl，满足 ft_train 的状态与样本数校验
    (ft.FT_DIR / DST / "status.json").write_text(
        json.dumps({"voice_id": DST, "stage": "ready", "clips": 12}), "utf-8")
    (ft.FT_DIR / DST / "train_raw.jsonl").write_text('{"audio": "x.wav"}\n', "utf-8")
    (ft.FT_DIR / DST / "train_codes.jsonl").write_text('{"audio": "x.wav"}\n', "utf-8")
    # src 模型：已发布 ft_model
    src_model = vb / SRC / "ft_model"
    src_model.mkdir(parents=True)
    (src_model / "config.json").write_text("{}", "utf-8")
    (src_model / "model.safetensors").write_bytes(b"weights")
    monkeypatch.setattr(ft.ftq, "build_report", lambda *a, **k: {"count": 12, "grades": {"A": 12, "B": 0, "C": 0, "D": 0}, "ok_count": 12, "avg_score": 90.0, "updated_at": ""})
    monkeypatch.setattr(ft, "_find_train_pid", lambda vid: None)
    ft._TRAIN.clear()
    return {"vb": vb, "fo": fo, "src_model": src_model}


# ---------------- _resolve_init_from ----------------

def test_init_from_empty_means_base(env):
    assert ft._resolve_init_from("") == ""
    assert ft._resolve_init_from("   ") == ""


def test_init_from_rejects_invalid_name(env):
    with pytest.raises(HTTPException) as ei:
        ft._resolve_init_from("../escape")
    assert ei.value.status_code == 400


def test_init_from_unknown_voice_404(env):
    with pytest.raises(HTTPException) as ei:
        ft._resolve_init_from("nosuchvoice")
    assert ei.value.status_code == 404


def test_init_from_prefers_published_model(env):
    got = ft._resolve_init_from(SRC)
    assert Path(got) == env["src_model"]


def test_init_from_falls_back_to_latest_ckpt(env, monkeypatch):
    """没发布过：回退 ft_output/<src>/ 最新 checkpoint（_published_model 语义）。"""
    import shutil
    shutil.rmtree(env["vb"] / SRC)
    ck_root = env["fo"] / SRC
    for n in (0, 1):
        d = ck_root / f"checkpoint-epoch-{n}"
        d.mkdir(parents=True)
        (d / "model.safetensors").write_bytes(b"w")
        (d / "config.json").write_text("{}", "utf-8")
    monkeypatch.setattr(ft, "ROOT", env["fo"].parent.parent)  # 无关紧要，路径已隔离
    monkeypatch.setattr(ft, "VOICEBANK", env["vb"])
    got = ft._resolve_init_from(SRC)
    assert Path(got).name == "checkpoint-epoch-1"


# ---------------- ft_train 路由透传 ----------------

def test_ft_train_passes_init_model_to_thread(env, monkeypatch):
    captured = {}

    def fake_thread(target, args=(), daemon=False):
        captured["target"] = target
        captured["args"] = args
        class _T:
            def start(self):
                pass
        return _T()

    monkeypatch.setattr(ft.threading, "Thread", fake_thread)
    res = ft.ft_train(DST, epochs=8, init_from=SRC)
    assert res["ok"] is True
    assert Path(res["init_from"]) == env["src_model"]
    assert Path(captured["args"][2]) == env["src_model"]   # (voice_id, epochs, init_model)
    # status 落盘（即使体检失败也不丢）
    st = json.loads((ft.FT_DIR / DST / "status.json").read_text("utf-8"))
    assert Path(st["init_from"]) == env["src_model"]


def test_ft_train_rejects_bad_init_before_thread(env, monkeypatch):
    started = []
    monkeypatch.setattr(ft.threading, "Thread",
                        lambda *a, **k: started.append(1) or type("T", (), {"start": lambda s: None})())
    with pytest.raises(HTTPException) as ei:
        ft.ft_train(DST, epochs=8, init_from="bad/name")
    assert ei.value.status_code == 400
    assert not started          # 校验失败绝不进后台线程


# ---------------- _train_job cmd 组装 ----------------

def test_train_job_cmd_contains_init_model_path(env, monkeypatch):
    """init_model 非空 → sft 命令带 --init_model_path；空 → 不带。"""
    spawned = []
    monkeypatch.setattr(ft, "_tee_run", lambda *a, **k: 0)

    def fake_popen(cmd, **kw):
        spawned.append(list(cmd))
        return type("P", (), {"pid": 1, "stdout": io.BytesIO(b""), "wait": lambda s: 0})()

    monkeypatch.setattr(ft.subprocess, "Popen", fake_popen)
    init_dir = str(env["src_model"])

    ft._train_job(DST, 2, init_model=init_dir)
    ft._train_job(DST, 2, init_model="")
    assert len(spawned) == 2
    for cmd in spawned:
        assert any(c.endswith("sft_8gb.py") for c in cmd[:3])
    assert spawned[0][spawned[0].index("--init_model_path") + 1] == init_dir
    assert "--init_model_path" not in spawned[1]
