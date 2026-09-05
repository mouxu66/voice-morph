# -*- coding: utf-8 -*-
"""Seed-VC 接口默认参数对齐测试。

背景（2026-09-05 优化项①）：experiments/seedvc_param_sweep.py 实测 + 前端默认
sim=0.5 时音色相似度最高（CAM++ 0.807）且漏源最低；后端 /seedvc/run 与
run_conversion 原默认 0.7，前端不传参时（post_seedvc 等场景）会用到与最优经验
不一致的值。本测试锁死「默认=0.5」契约，防止默认值回退或前后端再次漂移。
"""
import inspect

import seed_vc


def _form_default(func, name):
    """取 FastAPI Form() 参数的实际默认值（Form 对象 → .default）。"""
    p = inspect.signature(func).parameters[name]
    return getattr(p.default, "default", p.default)


def test_seedvc_run_form_defaults_aligned():
    """/seedvc/run Form 默认 similarity_cfg_rate=0.5（与前端/实验最优对齐）。"""
    assert _form_default(seed_vc.seedvc_run, "similarity_cfg_rate") == 0.5


def test_run_conversion_defaults_aligned():
    """run_conversion 默认 similarity_cfg_rate=0.5（offline_vc post_seedvc 复用同一契约）。"""
    assert inspect.signature(seed_vc.run_conversion).parameters["similarity_cfg_rate"].default == 0.5