import importlib
import re
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
    assert config.SERVER_HOST == "0.0.0.0"
    assert config.SERVER_PORT == 8000
    assert config.CORS_ORIGINS == ["*"]


def test_no_private_voice_baked_into_config():
    """config.py 里不得把**具体音色名**当默认值。

    2026-09-13 前 `VM_RVC_EXP` 的默认值是作者本人的音色（meituan_rat）——对开源
    用户而言那是个必然不存在的实验名，也属于私人音色指纹。

    这里查**源码文本**而不是运行时值：本机 .env 会把值盖回去（config.py 会
    load_dotenv），跑起来看根本区分不出“默认写死了”还是“.env 提供了”。
    """
    src = (Path(__file__).resolve().parents[1] / "config.py").read_text(encoding="utf-8")
    m = re.search(r'RVC_DEFAULT_EXP\s*=\s*_str\(\s*"VM_RVC_EXP"\s*,\s*"([^"]*)"', src)
    assert m, "找不到 RVC_DEFAULT_EXP 的定义（改名？那就同步改这条检查）"
    assert (
        m.group(1) == ""
    ), f"默认音色被写死为 {m.group(1)!r}；应留空，由 VM_RVC_EXP 提供（见 .env.example）"


def test_default_voice_and_ref_are_env_driven(monkeypatch, tmp_path):
    """默认音色 / 兜底参考音都必须是环境变量驱动的。"""
    monkeypatch.setenv("VM_RVC_EXP", "my_voice")
    monkeypatch.setenv("VM_DEFAULT_REF", str(tmp_path / "ref.wav"))
    import config as cfg

    importlib.reload(cfg)
    try:
        assert cfg.RVC_DEFAULT_EXP == "my_voice"
        assert str(cfg.DEFAULT_REF_AUDIO) == str(tmp_path / "ref.wav")
        assert str(cfg.rvc_exp_dirs()[0]).replace("\\", "/").endswith("logs/my_voice")
    finally:
        importlib.reload(cfg)


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
        assert (
            str(cfg.TTS_VENV_PY).replace("\\", "/").lower()
            == "d:/变声/tts_trial/venv312/scripts/python.exe"
        )
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
        assert models == cfg.TTS_MODELS_DIR
        assert models / "qwen3-tts-1.7b-base" == cfg.QWEN_MODEL_DIR
        assert models / "qwen3-tts-tokenizer-12hz" == cfg.QWEN_TOKENIZER_DIR
        assert venv_py == cfg.TTS_VENV_PY
        assert rvc == cfg.RVC_ROOT
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
