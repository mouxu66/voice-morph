"""微调语料体检与一键剔除单测（A3）。

回归背景：微调工坊（/ft）此前完全不消费 clip_qc 的分数，录音切出来的 D 级垃圾
（静音/爆音/底噪/他人声）照样进训练集，是"调出来鬼叫/不像"的主要来源。

覆盖：
  - 体检：等级分布 / 建议 / 问题聚合 / 指纹缓存
  - 剔除：文件只移动不删除、jsonl 拆分、锚点重选与 ref_audio 回写、样本不足告警
  - 恢复：切片与样本完整回滚
  - 护栏：处理中 / 训练中拒绝改动语料
  - 路由壳：非法 voice_id 400
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import config  # noqa: E402
import ft_corpus_qc as ftq  # noqa: E402

sf = pytest.importorskip("soundfile")

VOICE = "testvoice"


@pytest.fixture()
def ft(tmp_path, monkeypatch):
    """把 MEDIA_DIR 指到 tmp，得到一套隔离的 media/ft 目录。"""
    media = tmp_path / "media"
    (media / "ft").mkdir(parents=True)
    monkeypatch.setattr(config, "MEDIA_DIR", media)
    monkeypatch.setattr(sys.modules["config"], "MEDIA_DIR", media)
    return media / "ft" / VOICE


def _write_wav(path: Path, seconds: float = 3.0, amp: float = 0.3, sr: int = 24000,
               seed: int = 0):
    """写一段"像语音"的调幅正弦：首尾各 0.2s 静音，中段连续有声。"""
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    env = 0.5 * (1.0 + np.sin(2 * np.pi * 1.5 * t))
    x = amp * env * np.sin(2 * np.pi * 220 * t) + 0.001 * rng.standard_normal(n)
    x[: int(0.2 * sr)] *= 0.0
    x[-int(0.2 * sr):] *= 0.0
    sf.write(str(path), x.astype(np.float32), sr)


def _make_corpus(ft: Path, good: int = 6, bad: int = 4):
    """造语料：good 条正常切片 + bad 条近静音切片，并写 train_raw.jsonl / status.json。"""
    cdir = ft / "clips"
    cdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(good):
        p = cdir / f"seg_{i:03d}.wav"
        _write_wav(p, seconds=3.0, amp=0.3, seed=i)
        rows.append({"audio": str(p).replace("\\", "/"), "text": f"第{i}句", "ref_audio": ""})
    for i in range(bad):
        p = cdir / f"seg_bad_{i:03d}.wav"
        _write_wav(p, seconds=3.0, amp=0.0005, seed=100 + i)
        rows.append({"audio": str(p).replace("\\", "/"), "text": f"坏{i}句", "ref_audio": ""})
    anchor = rows[0]["audio"]
    for r in rows:
        r["ref_audio"] = anchor
    (ft / "train_raw.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    (ft / "status.json").write_text(json.dumps(
        {"stage": "ready", "voice_id": VOICE, "clips": len(rows),
         "anchor": Path(anchor).name, "speech_s": 30.0}), "utf-8")
    return rows


# ---------------- 体检 ----------------

def test_report_flags_silent_clips_as_d(ft):
    _make_corpus(ft, good=6, bad=4)
    rep = ftq.build_report(VOICE, force=True)
    assert rep["count"] == 10
    assert rep["grades"]["A"] + rep["grades"]["B"] == 6      # 正常切片可用
    assert rep["grades"]["D"] == 4                            # 近静音被判废
    # 近静音切片（含 0.001 噪声底）触发的是"中段静音/有效语音/信噪比"判废，
    # 而不是响度判废（peak 仍在 -50dBFS 之上）——这里只要求给出人话原因
    assert any(("静音" in r["reason"] or "有效语音" in r["reason"])
               for r in rep["top_reasons"])
    assert any("剔除" in a for a in rep["advice"])
    assert rep["clips"]["seg_000"]["grade"] in ("A", "B")
    assert rep["clips"]["seg_bad_000"]["grade"] == "D"


def test_report_cache_reused_until_fingerprint_changes(ft):
    _make_corpus(ft, good=3, bad=1)
    rep1 = ftq.build_report(VOICE, force=True)
    sentinel = "2000-01-01 00:00:00"
    p = ftq.report_path(VOICE)
    data = json.loads(p.read_text("utf-8"))
    data["updated_at"] = sentinel
    p.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    rep2 = ftq.build_report(VOICE)             # 指纹未变 → 走缓存
    assert rep2["updated_at"] == sentinel

    (ft / "clips" / "seg_new.wav").write_bytes((ft / "clips" / "seg_000.wav").read_bytes())
    rep3 = ftq.build_report(VOICE)             # 切片数变化 → 重算
    assert rep3["updated_at"] != sentinel
    assert rep3["count"] == rep1["count"] + 1


def test_reason_key_strips_numbers():
    assert ftq._reason_key("信噪比 8dB 偏低（底噪或伴奏残留）") == "信噪比偏低"
    assert ftq._reason_key("时长 1.2s 不可用（合格 2.5–10.0s）") == "时长不可用"


def test_advice_warns_when_samples_too_few(ft):
    _make_corpus(ft, good=2, bad=6)
    rep = ftq.build_report(VOICE, force=True)
    assert rep["ok_count"] < ftq.MIN_TRAIN_SAMPLES
    assert any("补录" in a for a in rep["advice"])


# ---------------- 剔除 ----------------

def _fake_report(ft: Path, grades: dict):
    """构造一份假报告 {stem: grade}，让 prune 的过滤逻辑与真实打分解耦。"""
    clips = {name: {"name": name, "score": 90 if g in ("A", "B") else 20, "grade": g,
                    "reasons": [], "duration_s": 3.0}
             for name, g in grades.items()}
    rep = {"voice_id": VOICE, "count": len(clips), "grades": {"A": 0, "B": 0, "C": 0, "D": 0},
           "clips": clips, "avg_score": 60.0, "updated_at": "2026-09-05 00:00:00"}
    for g in grades.values():
        rep["grades"][g] += 1
    return rep


def test_prune_moves_files_and_rewrites_jsonl(ft, monkeypatch):
    _make_corpus(ft, good=9, bad=3)                   # 保留 9 条 ≥ 8，不应告警
    grades = {f"seg_{i:03d}": "B" for i in range(9)}
    grades.update({f"seg_bad_{i:03d}": "D" for i in range(3)})
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))

    res = ftq.prune(VOICE)

    assert res["moved"] == 3
    assert res["kept"] == 9
    assert len(ftq.clip_paths(VOICE)) == 9
    assert len(ftq.rejected_paths(VOICE)) == 3
    assert all(p.name.startswith("seg_bad") for p in ftq.rejected_paths(VOICE))

    kept = ftq._read_jsonl(ftq.train_jsonl(VOICE))
    assert len(kept) == 9
    assert all(Path(r["audio"]).exists() for r in kept)     # 保留样本音频仍在
    # 锚点重选后，所有行的 ref_audio 必须指向仍在 clips 里的文件
    assert len({r["ref_audio"] for r in kept}) == 1
    assert Path(kept[0]["ref_audio"]).exists()
    assert Path(kept[0]["ref_audio"]).parent.name == "clips"

    dropped = ftq._read_jsonl(ftq.rejected_jsonl(VOICE))
    assert len(dropped) == 3                                 # 转写文本留存，可恢复
    assert res["warning"] == ""                              # 9 条 ≥ 下限，无告警


def test_prune_keeps_c_when_asked(ft, monkeypatch):
    _make_corpus(ft, good=6, bad=4)
    grades = {f"seg_{i:03d}": ("C" if i < 2 else "B") for i in range(6)}
    grades.update({f"seg_bad_{i:03d}": "D" for i in range(4)})
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))

    ftq.prune(VOICE, keep_grades=("A", "B", "C"))
    assert len(ftq.clip_paths(VOICE)) == 6                   # 2 条 C + 4 条 B 保留，4 条 D 剔除

    ftq.restore(VOICE)                                        # 复位再剔一次
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))
    ftq.prune(VOICE, keep_grades=("A", "B"))
    assert len(ftq.clip_paths(VOICE)) == 4                   # 2 条 C + 4 条 D 一起剔除


def test_prune_honors_min_score(ft, monkeypatch):
    _make_corpus(ft, good=6, bad=4)
    grades = {f"seg_{i:03d}": ("A" if i < 3 else "B") for i in range(6)}
    grades.update({f"seg_bad_{i:03d}": "D" for i in range(4)})
    fake = _fake_report(ft, grades)
    for name in grades:                                       # B 级给低分，触发 min_score
        if fake["clips"][name]["grade"] == "B":
            fake["clips"][name]["score"] = 70
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: fake)

    ftq.prune(VOICE, min_score=85)
    assert len(ftq.clip_paths(VOICE)) == 3                   # 只留下 3 条 A 级


def test_prune_warns_when_below_min_samples(ft, monkeypatch):
    _make_corpus(ft, good=2, bad=8)
    grades = {f"seg_{i:03d}": "B" for i in range(2)}
    grades.update({f"seg_bad_{i:03d}": "D" for i in range(8)})
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))

    res = ftq.prune(VOICE)
    assert res["kept"] == 2
    assert "样本" in res["warning"]
    # 状态同步：clips 数与剔除计数都写回 status.json
    st = json.loads((ft / "status.json").read_text("utf-8"))
    assert st["clips"] == 2
    assert st["rejected"] == 8


def test_prune_noop_when_nothing_to_drop(ft, monkeypatch):
    _make_corpus(ft, good=4, bad=0)
    grades = {f"seg_{i:03d}": "A" for i in range(4)}
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))

    res = ftq.prune(VOICE)
    assert res["moved"] == 0 and res["kept"] == 4
    assert not ftq.rejected_dir(VOICE).exists() or not ftq.rejected_paths(VOICE)


# ---------------- 护栏 ----------------

def test_prune_blocked_while_processing(ft):
    _make_corpus(ft, good=3, bad=1)
    (ft / "status.json").write_text(json.dumps({"stage": "processing"}), "utf-8")
    with pytest.raises(RuntimeError, match="处理中"):
        ftq.prune(VOICE)


def test_prune_blocked_while_training(ft):
    _make_corpus(ft, good=3, bad=1)
    with pytest.raises(RuntimeError, match="训练中"):
        ftq.prune(VOICE, training=True)
    with pytest.raises(RuntimeError, match="训练中"):
        ftq.restore(VOICE, training=True)


def test_prune_requires_clips(ft):
    ft.mkdir(parents=True, exist_ok=True)
    (ft / "status.json").write_text(json.dumps({"stage": "ready"}), "utf-8")
    with pytest.raises(RuntimeError, match="没有可体检的切片"):
        ftq.prune(VOICE)


# ---------------- 恢复 ----------------

def test_restore_roundtrip(ft, monkeypatch):
    _make_corpus(ft, good=6, bad=4)
    grades = {f"seg_{i:03d}": "B" for i in range(6)}
    grades.update({f"seg_bad_{i:03d}": "D" for i in range(4)})
    monkeypatch.setattr(ftq, "build_report", lambda *a, **k: _fake_report(ft, grades))

    ftq.prune(VOICE)
    assert len(ftq.clip_paths(VOICE)) == 6
    assert len(ftq._read_jsonl(ftq.train_jsonl(VOICE))) == 6

    res = ftq.restore(VOICE)
    assert res["restored"] == 4
    assert len(ftq.clip_paths(VOICE)) == 10
    assert not ftq.rejected_paths(VOICE)

    rows = ftq._read_jsonl(ftq.train_jsonl(VOICE))
    assert len(rows) == 10
    assert len({r["ref_audio"] for r in rows}) == 1          # 锚点重选后统一
    assert Path(rows[0]["ref_audio"]).exists()
    assert not ftq._read_jsonl(ftq.rejected_jsonl(VOICE))    # 恢复后清单清空


def test_restore_noop_when_empty(ft):
    _make_corpus(ft, good=3, bad=0)
    res = ftq.restore(VOICE)
    assert res["restored"] == 0


# ---------------- 路由壳 ----------------

def test_api_rejects_invalid_voice_id():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import finetune
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(finetune.router)
    client = TestClient(app)
    assert client.get("/api/ft/corpus_qc", params={"voice_id": "../evil"}).status_code == 400
    assert client.post("/api/ft/corpus_prune", params={"voice_id": "a b"}).status_code == 400
    assert client.post("/api/ft/corpus_restore", params={"voice_id": "../x"}).status_code == 400
