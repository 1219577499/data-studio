# -*- coding: utf-8 -*-
"""自动 EDA / 数据质量剖析。

产出三块东西：
  overview  整体概览（行列数、内存、重复行、缺失率）
  columns   每列的类型画像（统计量 / Top 值 / 分布 / 异常）
  quality   问题清单 + 质量分 + 每条问题的修复代码

设计原则：不只要指出问题，还要给出"下一步怎么做"的 pandas 代码，
这样拿到报告的人能直接照着改，而不是对着一堆红字发呆。
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from .jsonio import jsonable

# ------------------------------------------------------------------ 类型画像
def _role(s: pd.Series, rows: int) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    if pd.api.types.is_timedelta64_dtype(s):
        return "timedelta"
    if pd.api.types.is_numeric_dtype(s):
        return "numeric"
    # 字符串 / object：进一步区分布尔字面量、日期字面量
    sample = s.dropna().head(300)
    if len(sample) == 0:
        return "unknown"
    lowered = set(str(v).strip().lower() for v in sample)
    if lowered <= {"true", "false", "0", "1", "yes", "no", "y", "n", "是", "否"}:
        return "boolean_like"
    date_hit = 0
    for v in sample:
        if isinstance(v, str) and re.fullmatch(
            r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", str(v).strip()
        ):
            date_hit += 1
    if len(sample) and date_hit / len(sample) > 0.8:
        return "datetime_like"
    return "categorical"


def _numeric_stats(s: pd.Series) -> dict:
    v = pd.to_numeric(s, errors="coerce").dropna()
    if v.empty:
        return {}
    q = v.quantile([0.01, 0.25, 0.5, 0.75, 0.99]).tolist()
    iqr = q[3] - q[1]
    low, high = q[1] - 1.5 * iqr, q[3] + 1.5 * iqr
    outliers = int(((v < low) | (v > high)).sum()) if iqr > 0 else 0
    return {
        "min": q[0], "p1": q[0], "p25": q[1], "p50": q[2], "p75": q[3], "p99": q[4],
        "max": q[4],
        "mean": float(v.mean()), "std": float(v.std(ddof=1)) if len(v) > 1 else 0.0,
        "sum": float(v.sum()), "sum_note": "ddof=1",
        "skew": float(v.skew()) if len(v) > 2 else None,
        "kurt": float(v.kurt()) if len(v) > 3 else None,
        "zeros": int((v == 0).sum()),
        "negatives": int((v < 0).sum()),
        "iqr": float(iqr),
        "outlier_bounds": [float(low), float(high)],
        "outliers": outliers,
        "outlier_pct": round(outliers / len(v), 6) if len(v) else 0.0,
    }


def _cat_stats(s: pd.Series, topn: int = 12) -> dict:
    vc = s.value_counts(dropna=True)
    total = int(vc.sum())
    tops = [
        {"value": str(k), "count": int(v), "pct": round(int(v) / total, 6) if total else 0.0}
        for k, v in vc.head(topn).items()
    ]
    lens = s.dropna().astype(str).map(len)
    return {
        "top_values": tops,
        "distinct_total": int(len(vc)),
        "avg_len": round(float(lens.mean()), 2) if len(lens) else None,
        "max_len": int(lens.max()) if len(lens) else None,
        "whitespace_rows": int((s.dropna().astype(str) != s.dropna().astype(str).str.strip()).sum()),
    }


def _date_stats(s: pd.Series) -> dict:
    v = pd.to_datetime(s, errors="coerce").dropna()
    if v.empty:
        return {}
    span = (v.max() - v.min())
    return {
        "min": v.min().isoformat(), "max": v.max().isoformat(),
        "span_days": round(span.total_seconds() / 86400, 2),
        "unique_days": int(v.dt.date.nunique()),
    }


# ------------------------------------------------------------------ 主入口
def profile(df: pd.DataFrame, *, topn: int = 12, name: str = "") -> dict:
    rows, cols = df.shape
    memory = int(df.memory_usage(deep=True).sum())
    dup_rows = int(df.duplicated().sum())
    miss_cells = int(df.isna().sum().sum())
    total_cells = max(rows * cols, 1)

    col_info: list[dict] = []
    for c in df.columns:
        s = df[c]
        name_str = str(c)
        missing = int(s.isna().sum())
        n_unique = int(s.nunique(dropna=True))
        role = _role(s, rows)
        info: dict = {
            "name": name_str,
            "index": int(df.columns.get_loc(c)),
            "dtype": str(s.dtype),
            "role": role,
            "missing": missing,
            "missing_pct": round(missing / rows, 6) if rows else 0.0,
            "unique": n_unique,
            "unique_pct": round(n_unique / rows, 6) if rows else 0.0,
        }
        samples = s.dropna().head(5).tolist()
        info["sample"] = [str(x) for x in samples]

        if role in ("numeric",):
            info["stats"] = _numeric_stats(s)
        elif role in ("datetime", "datetime_like"):
            info["stats"] = _date_stats(s)
        elif role in ("boolean", "boolean_like"):
            vc = s.astype(str).value_counts()
            info["stats"] = {"value_counts": {str(k): int(v) for k, v in vc.head(8).items()}}
        else:
            info["stats"] = _cat_stats(s, topn)

        col_info.append(info)

    overview = {
        "name": name,
        "rows": rows,
        "cols": cols,
        "memory_bytes": memory,
        "memory_mb": round(memory / 1024 / 1024, 3),
        "duplicate_rows": dup_rows,
        "duplicate_pct": round(dup_rows / rows, 6) if rows else 0.0,
        "missing_cells": miss_cells,
        "missing_pct": round(miss_cells / total_cells, 6),
        "numeric_cols": sum(1 for i in col_info if i["role"] == "numeric"),
        "cat_cols": sum(1 for i in col_info if i["role"] == "categorical"),
        "datetime_cols": sum(1 for i in col_info if i["role"].startswith("datetime")),
        "bool_cols": sum(1 for i in col_info if i["role"].startswith("boolean")),
    }

    quality = assess_quality(df, col_info, rows)
    corr = correlation(df)
    missing_mat = missing_matrix(df)

    return jsonable({
        "overview": overview,
        "columns": col_info,
        "quality": quality,
        "correlation": corr,
        "missing_matrix": missing_mat,
    })


# ------------------------------------------------------------------ 质量评估
def assess_quality(df: pd.DataFrame, col_info: list[dict], rows: int) -> dict:
    issues: list[dict] = []
    penalty = 0

    def add(level: str, code: str, column: str | None, message: str,
            suggestion: str, fix: str, weight: int, count: int = 0):
        issues.append({
            "level": level, "code": code, "column": column, "message": message,
            "suggestion": suggestion, "pandas_code": fix, "weight": weight, "count": count,
        })

    for info in col_info:
        c = info["name"]
        quote = f"df['{c}']"
        mp = info["missing_pct"]
        # 1) 缺失
        if rows and mp >= 0.6:
            add("error", "high_missing", c,
                f"缺失率 {mp:.1%}，这个字段基本不可用",
                "确认上游是否真的会写这列；不可用就删，别硬填。",
                f"# 方案A：直接删列\n"
                f"df = df.drop(columns=['{c}'])\n\n"
                f"# 方案B：显式标记为缺失，避免误判为 0\n"
                f"df['{c}_isnull'] = {quote}.isna()", 12, info["missing"])
        elif rows and mp > 0:
            add("warn", "missing", c, f"缺失 {info['missing']} 个值（{mp:.1%}）",
                "先判断缺失是不是随机；数值列用中位数、类别列用众数或 '未知'。",
                f"{quote} = {quote}.fillna({quote}.median())   # 数值型\n"
                f"# 类别型：df['{c}'] = df['{c}'].fillna('未知')", 3, info["missing"])

        # 2) 常量列
        if rows > 1 and info["unique"] <= 1:
            add("warn", "constant", c, "整列只有一个值，没有任何信息量",
                "建模前应当剔除，会让特征重要性失真。",
                f"df = df.drop(columns=['{c}'])", 6)

        # 3) 全空列
        if rows and info["missing"] == rows:
            add("error", "all_null", c, "整列为空",
                "直接删除。", f"df = df.drop(columns=['{c}'])", 10)

        # 4) 疑似 ID / 主键
        if rows > 10 and info["unique_pct"] >= 0.999 and rows >= 10:
            add("info", "id_like", c, f"唯一值占比 {info['unique_pct']:.1%}，疑似主键或 ID",
                "别把它当特征喂进模型；也别参与聚合分组。",
                f"df = df.set_index('{c}')   # 或建模时剔除", 0)

        # 5) 高基数
        elif (info["role"] == "categorical" and info["unique"] > 200
              and rows and info["unique_pct"] > 0.3):
            add("info", "high_cardinality", c, f"基数 {info['unique']}，取值过于分散",
                "考虑合并长尾：低于某个频次的统一归为 '其他'。",
                f"vc = {quote}.value_counts()\n"
                f"{quote} = {quote}.where({quote}.map(vc) >= 10, '其他')", 2, info["unique"])

        # 6) 异常值
        st = info.get("stats") or {}
        if isinstance(st, dict) and st.get("outlier_pct", 0) > 0:
            pct = st["outlier_pct"]
            lvl = "warn" if pct > 0.05 else "info"
            add(lvl, "outliers", c,
                f"IQR 法检出 {st['outliers']} 个异常值（{pct:.2%}）",
                "业务上如果是笔误/系统脏数据就该删或截断；如果是真实极值（比如大额订单）别删。",
                f"lo, hi = {st['outlier_bounds'][0]:.6g}, {st['outlier_bounds'][1]:.6g}\n"
                f"{quote} = {quote}.clip(lo, hi)   # 截断\n"
                f"# 或直接过滤：df = df[df['{c}'].between(lo, hi)]",
                4 if pct > 0.05 else 1, st["outliers"])

        # 7) 前后空格
        if info["role"] == "categorical" and isinstance(st, dict) and st.get("whitespace_rows"):
            add("warn", "whitespace", c, f"{st['whitespace_rows']} 行存在首尾空格",
                "会造成同一取值被切成两类（'北京 ' vs '北京'），必须先 strip。",
                f"{quote} = {quote}.str.strip()", 4, st["whitespace_rows"])

        # 8) 日期存成字符串
        if info["role"] == "datetime_like":
            add("info", "date_as_string", c, "日期以字符串存储",
                "转成 datetime 才能做时间序列分析、resample。",
                f"{quote} = pd.to_datetime({quote}, errors='coerce')", 2)

        # 9) 列名不规范
        if re.search(r"\s|（|\(|）|\)|/|-", str(c)):
            add("info", "bad_column_name", c, f"列名 {c!r} 含空格或特殊字符",
                "统一成 snake_case，写 SQL 和 df.xxx 都省事。",
                f"df = df.rename(columns={{'{c}': '{_snake(str(c))}'}})", 1)

    # 10) 重复行
    dups = int(df.duplicated().sum())
    if dups:
        add("warn", "duplicates", None, f"存在 {dups} 行完全重复记录",
            "如果是全字段去重没问题；若是 JOIN 放大导致的重复，先查上游关联键。",
            "df = df.drop_duplicates()", 8, dups)

    penalty = sum(i["weight"] for i in issues)
    # 按严重度做加权，而不是简单求和
    weighted = 0.0
    for i in issues:
        base = i["weight"]
        if i["level"] == "error":
            base *= 1.6
        elif i["level"] == "info":
            base *= 0.4
        weighted += base
    score = max(0, min(100, int(round(100 - weighted))))

    level_count = {
        "error": sum(1 for i in issues if i["level"] == "error"),
        "warn": sum(1 for i in issues if i["level"] == "warn"),
        "info": sum(1 for i in issues if i["level"] == "info"),
    }
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], -(i["count"] or 0)))
    return {"score": score, "issues": issues, "counts": level_count,
            "grade": _grade(score)}


def _grade(score: int) -> str:
    if score >= 90:
        return "优秀"
    if score >= 75:
        return "良好"
    if score >= 60:
        return "及格"
    if score >= 40:
        return "较差"
    return "很差"


def _snake(s: str) -> str:
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"[（）()]", "", s)
    return re.sub(r"_+", "_", s).strip("_").lower()


# ------------------------------------------------------------------ 相关矩阵
def correlation(df: pd.DataFrame, method: str = "pearson") -> dict:
    num = df.select_dtypes(include="number")
    # 常量列 std=0，corr 会产出 NaN，提前剔除
    keep = [c for c in num.columns if num[c].nunique(dropna=True) > 1]
    num = num[keep]
    if num.shape[1] < 2:
        return {"columns": [], "matrix": [], "method": method}
    mat = num.corr(method=method)
    mat = mat.round(4)
    cols = [str(c) for c in mat.columns]
    data = []
    for i, ri in enumerate(cols):
        for j, cj in enumerate(cols):
            v = mat.iloc[i, j]
            data.append([j, i, None if pd.isna(v) else float(v)])
    return {"columns": cols, "matrix": data, "method": method}


# ------------------------------------------------------------------ 缺失矩阵
def missing_matrix(df: pd.DataFrame, max_rows: int = 60) -> dict:
    """缺失分布热力图：横轴=列，纵轴=行切片。"""
    cols = [str(c) for c in df.columns]
    if len(df) == 0:
        return {"columns": cols, "rows": [], "row_missing_pct": []}

    step = max(1, len(df) // max_rows)
    sub = df.iloc[::step].head(max_rows)
    miss = sub.isna()
    rows_data = []
    for i in range(len(sub)):
        for j, c in enumerate(cols):
            if bool(miss.iloc[i, j]):
                rows_data.append([j, i, 1])
    per_row = [round(float(miss.iloc[i].mean()), 4) for i in range(len(sub))]
    per_col = [
        {"column": str(c), "missing": int(df[c].isna().sum()),
         "pct": round(float(df[c].isna().mean()), 6)}
        for c in df.columns
    ]
    return {
        "columns": cols, "points": rows_data,
        "row_missing_pct": per_row, "row_count_sampled": len(sub),
        "per_column": per_col,
    }
