# -*- coding: utf-8 -*-
"""numpy / pandas 类型 -> 纯 Python，保证 json.dumps 不炸。

这是最容易翻车的地方：NaN / NaT / int64 / Timestamp / Decimal 全都
不是 JSON 原生类型，序列化时会报 TypeError 或者产出 NaN（前端 JSON.parse 直接崩）。
统一从这里过一遍。
"""
from __future__ import annotations

import datetime as _dt
import math
from decimal import Decimal

import numpy as np
import pandas as pd


def is_missing(v) -> bool:
    """判断是否缺失值，兼容 numpy 标量与 Python 原生。"""
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if v is pd.NaT or v is pd.NA:
        return True
    if isinstance(v, np.floating) and bool(np.isnan(v)):
        return True
    if isinstance(v, np.datetime64) and str(v) in ("NaT", ""):
        return True
    return False


def py(v):
    """单个标量 -> JSON 安全值。"""
    if v is None:
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 10)
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, 10)
    if isinstance(v, (np.str_,)):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (np.datetime64, pd.Timestamp, _dt.datetime, _dt.date)):
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return None
        return ts.isoformat()
    if isinstance(v, pd.Timedelta):
        return v.value / 1e9
    if isinstance(v, np.timedelta64):
        return float(v / np.timedelta64(1, "s"))
    return str(v)


def jsonable(obj, *, float_ndigits: int = 10):
    """递归清洗任意结构。"""
    # 缺失：先于 np 标量处理
    if is_missing(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): jsonable(v, float_ndigits=float_ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v, float_ndigits=float_ndigits) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v, float_ndigits=float_ndigits) for v in obj.tolist()]
    if hasattr(obj, "item") and isinstance(obj, np.generic):
        return jsonable(obj.item(), float_ndigits=float_ndigits)
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return round(obj, float_ndigits)
    if isinstance(obj, str):
        return obj
    return py(obj)
