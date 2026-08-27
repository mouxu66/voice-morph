# 让 pytest 能直接 import m2_server 内的模块（config / rvc_live / server）
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
