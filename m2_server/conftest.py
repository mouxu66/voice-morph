# 让 pytest 能直接 import m2_server 内的模块（config / rvc_live / server）
import os
import sys
from pathlib import Path

# 测试环境禁止启动时预热：server.py 在导入期就会 start_background() 预热
# TTS worker + 常驻 RVC 模型（加载 1.7B 模型占 GPU），测试里绝不能真起这个
# 子进程。warmup.ENABLED 在导入时读 VM_WARMUP，故必须在 import server 之前置 0。
os.environ.setdefault("VM_WARMUP", "0")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
