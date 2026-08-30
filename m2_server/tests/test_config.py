import importlib
import sys
from pathlib import Path

# 兜底把 m2_server 加入 path（conftest 通常已处理）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_defaults_point_to_sensible_locations():
    import config
    assert isinstance(config.ROOT, Path)
    assert str(config.RVC_ROOT).replace("\\", "/").lower() == "d:/rvc"
    assert config.RVC_DEFAULT_EXP == "meituan_rat"
    assert config.SERVER_HOST == "0.0.0.0"
    assert config.SERVER_PORT == 8000
    assert config.CORS_ORIGINS == ["*"]


def test_rvc_texts_loaded_from_bundled_file():
    import config
    texts = config.load_rvc_texts()
    # 只断言下界：语料会随音色迭代扩充（已从通用 20 句扩到外卖场景 100+ 句），
    # 写死条数会让每次扩充语料都误报失败。
    assert isinstance(texts, list) and len(texts) >= 20
    assert all(isinstance(t, str) and t.strip() for t in texts)
    assert len(set(texts)) == len(texts), "语料不应有重复句"


def test_env_override(monkeypatch):
    monkeypatch.setenv("VM_RVC_ROOT", "C:/tmp/rvc")
    monkeypatch.setenv("VM_CORS_ORIGINS", "http://localhost:5173, https://a.test")
    import config as cfg
    importlib.reload(cfg)
    try:
        assert str(cfg.RVC_ROOT).replace("\\", "/").lower() == "c:/tmp/rvc"
        assert cfg.CORS_ORIGINS == ["http://localhost:5173", "https://a.test"]
    finally:
        importlib.reload(cfg)  # 还原模块级缓存，避免影响其他测试
