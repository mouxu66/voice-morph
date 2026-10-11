# -*- coding: utf-8 -*-
"""tts_trial 测试包：把 tts_trial 根目录放进 sys.path，以便导入 qlora_quant_config。"""
import os
import sys

_TTS_TRIAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TTS_TRIAL not in sys.path:
    sys.path.insert(0, _TTS_TRIAL)
