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


def test_env_override_packaged_external_resources(monkeypatch):
    """安装版语义：后端代码在包内（resources/backend），模型/解释器在包外，
    靠 VM_TTS_MODELS_DIR / VM_TTS_VENV_PY 指过去 —— 覆盖必须生效，
    且 QWEN 模型目录跟随 TTS_MODELS_DIR 推导。"""
    monkeypatch.setenv("VM_TTS_MODELS_DIR", "D:/变声/tts_models")
    monkeypatch.setenv("VM_TTS_VENV_PY", "D:/变声/tts_trial/venv312/Scripts/python.exe")
    import config as cfg
    importlib.reload(cfg)
    try:
        assert str(cfg.TTS_MODELS_DIR).replace("\\", "/").lower() == "d:/变声/tts_models"
        assert str(cfg.QWEN_MODEL_DIR) == str(cfg.TTS_MODELS_DIR / "qwen3-tts-1.7b-base")
        assert str(cfg.QWEN_TOKENIZER_DIR) == str(cfg.TTS_MODELS_DIR / "qwen3-tts-tokenizer-12hz")
        assert str(cfg.TTS_VENV_PY).replace("\\", "/").lower() == "d:/变声/tts_trial/venv312/scripts/python.exe"
    finally:
        importlib.reload(cfg)


def test_env_override_full_chain_from_config_json(monkeypatch, tmp_path):
    """首启引导链路（干净机器）：用户在 userData/config.json 里选定模型目录后，
    Electron 把解析结果注入 VM_TTS_MODELS_DIR / VM_TTS_VENV_PY / VM_PROJECT_ROOT /
    VM_RVC_ROOT —— 后端的四个消费点必须全部生效，缺一不可：

      VM_TTS_MODELS_DIR → TTS_MODELS_DIR（进而推导 QWEN_MODEL_DIR / QWEN_TOKENIZER_DIR）
      VM_TTS_VENV_PY    → TTS_VENV_PY（qwen3_tts 客户端用它拉起 worker）
      VM_RVC_ROOT       → RVC_ROOT（实时变声 / 训练 / 离线变声都依赖）
      VM_PROJECT_ROOT   → 由 qwen3_tts.py 直接读 os.environ，不在 config.py 里

    用 tmp_path 造真实的目录树，确保推导出的子路径确实落在注入根下。
    """
    models = tmp_path / "tts_models"
    (models / "qwen3-tts-1.7b-base").mkdir(parents=True)
    (models / "qwen3-tts-tokenizer-12hz").mkdir()
    venv_py = tmp_path / "tts_trial" / "venv312" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("stub", encoding="utf-8")
    rvc = tmp_path / "RVC"
    (rvc / "logs").mkdir(parents=True)

    monkeypatch.setenv("VM_TTS_MODELS_DIR", str(models))
    monkeypatch.setenv("VM_TTS_VENV_PY", str(venv_py))
    monkeypatch.setenv("VM_RVC_ROOT", str(rvc))
    monkeypatch.setenv("VM_PROJECT_ROOT", str(tmp_path))

    import config as cfg
    importlib.reload(cfg)
    try:
        assert cfg.TTS_MODELS_DIR == models
        assert cfg.QWEN_MODEL_DIR == models / "qwen3-tts-1.7b-base"
        assert cfg.QWEN_TOKENIZER_DIR == models / "qwen3-tts-tokenizer-12hz"
        assert cfg.TTS_VENV_PY == venv_py
        assert cfg.RVC_ROOT == rvc
        # 推导出的 RVC 实验目录必须落在注入的根下（而不是回落到 D:/RVC）
        log_dir, dataset_dir = cfg.rvc_exp_dirs("kangaroo_v2")
        assert log_dir == rvc / "logs" / "kangaroo_v2"
        assert dataset_dir == rvc / "dataset" / "kangaroo_v2"
    finally:
        importlib.reload(cfg)


def test_missing_config_falls_back_and_reports_unavailable(monkeypatch):
    """未配置时的降级语义：环境变量缺失 → config.py 回落项目内默认路径，
    路径不存在但**导入不报错**；/diagnose 据此报「未配置」而不是静默错乱。

    这是首启引导的另一半：缺模型时不阻塞后端启动，只让相关功能明确不可用。
    """
    for var in ("VM_TTS_MODELS_DIR", "VM_TTS_VENV_PY", "VM_RVC_ROOT", "VM_PROJECT_ROOT"):
        monkeypatch.delenv(var, raising=False)
    import config as cfg
    importlib.reload(cfg)
    try:
        # 回落默认值（项目内相对路径），不抛异常
        assert cfg.TTS_MODELS_DIR == cfg.ROOT / "tts_models"
        assert cfg.TTS_VENV_PY == cfg.ROOT / "tts_trial" / "venv312" / "Scripts" / "python.exe"
        # RVC 默认是机器级固定路径 D:/RVC（干净机器上不存在 → /diagnose 报未配置）
        assert str(cfg.RVC_ROOT).replace("\\", "/").lower() == "d:/rvc"
        # 推导关系不能被破坏
        assert cfg.QWEN_MODEL_DIR == cfg.TTS_MODELS_DIR / "qwen3-tts-1.7b-base"
    finally:
        importlib.reload(cfg)
