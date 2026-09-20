# -*- coding: utf-8 -*-
"""两份数据集的差异对比。

真实工作里常见的几种「对账」场景：
  - 今天的表 vs 昨天的表：多了哪些记录、少了哪些、哪些字段被改了
  - 上游抽出来的 vs 落库之后查出来的：有没有丢数、有没有串字段
  - 上线前 vs 上线后：整体分布有没有漂

对比分三层：
  1. structure  结构：行数、列的新增/删除、类型变化
  2. row_diff   行级：指定主键后做全外连接，分出「只在左」「只在右」「键值变了」
  3. drift      分布：每个数值列的分位数漂移、类别列的取值构成变化
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .viz import _c
from .jsonio import jsonable, py

MAX_ROWS_FOR_DIFF = 300_000      # 超过这个量级就提示用户先抽样/先聚合
MAX_SAMPLE_ROWS = 200            # 差异明细最多返回多少条


class CompareError(Exception):
    pass


def _canonical(df: pd.DataFrame) -> pd.DataFrame:
    """把 object/StringDtype 列统一成 str 再做比较。

    否则左表是 object('2025-01-01')、右表是 str('2025-01-01')，
    明明一样的值会被判成不一样 —— 这是对比功能最容易翻车的地方。
    """
    out = df.copy()
    for c in out.columns:
        s = out[c]
        if isinstance(s.dtype, pd.StringDtype) or s.dtype == object:
            out[c] = s.map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
    return out


def suggest_keys(left: pd.DataFrame, right: pd.DataFrame) -> list[dict]:
    """推荐可用作关联键的列：两边都有、非空、唯一性够高。

    完全唯一的列很少见（业务表经常有重复记录），所以这里放宽成两档：
      - unique=True：严格唯一，可以放心当主键
      - 唯一率 ≥ 99%：近似唯一，也能用，但重复行会被按首条处理
    """
    out = []
    for c in left.columns:
        if c not in right.columns:
            continue
        ls, rs = left[c], right[c]
        try:
            lu, ru = ls.nunique(dropna=True), rs.nunique(dropna=True)
            ratio = min(lu / max(len(ls), 1), ru / max(len(rs), 1))
        except TypeError:      # 列里混了 list/dict 这种不可哈希的值，跳过
            continue
        clean = bool(ls.notna().all() and rs.notna().all())
        if not clean or ratio < 0.9:
            continue
        if lu == len(ls) and ru == len(rs):
            out.append({"column": str(c), "unique": True, "coverage": 1.0, "recommended": True})
        else:
            out.append({"column": str(c), "unique": False, "coverage": round(ratio, 6),
                        "recommended": ratio >= 0.99})
    out.sort(key=lambda x: (not x["recommended"], not x["unique"], -x["coverage"]))
    return out


# ------------------------------------------------------------------ 结构对比
def compare_structure(left: pd.DataFrame, right: pd.DataFrame,
                      left_name: str, right_name: str) -> dict:
    lc = [str(c) for c in left.columns]
    rc = [str(c) for c in right.columns]
    common = [c for c in lc if c in rc]

    dtype_changed = []
    for c in common:
        ld, rd = str(left[c].dtype), str(right[c].dtype)
        if ld != rd:
            dtype_changed.append({"column": c, "left": ld, "right": rd})

    dup_l = int(left.duplicated().sum())
    dup_r = int(right.duplicated().sum())

    return {
        "left_name": left_name,
        "right_name": right_name,
        "rows_left": int(len(left)),
        "rows_right": int(len(right)),
        "rows_delta": int(len(right) - len(left)),
        "cols_left": len(lc),
        "cols_right": len(rc),
        "only_in_left": [c for c in lc if c not in rc],
        "only_in_right": [c for c in rc if c not in lc],
        "common_columns": common,
        "dtype_changed": dtype_changed,
        "dup_rows_left": dup_l,
        "dup_rows_right": dup_r,
        "memory_left_mb": round(float(left.memory_usage(deep=True).sum()) / 1048576, 3),
        "memory_right_mb": round(float(right.memory_usage(deep=True).sum()) / 1048576, 3),
    }


# ------------------------------------------------------------------ 行级对比
def compare_rows(left: pd.DataFrame, right: pd.DataFrame, key: str) -> dict:
    if key not in left.columns or key not in right.columns:
        raise CompareError(f"关联键 {key!r} 在两份数据里都存在才能对比")

    l = _canonical(left)
    r = _canonical(right)

    left_dup = int(l[key].duplicated().sum())
    right_dup = int(r[key].duplicated().sum())
    if left_dup or right_dup:
        # 键重复会让 .loc 取数成笛卡尔放大，先按首条去重并如实告知
        l = l.drop_duplicates(subset=[key], keep="first")
        r = r.drop_duplicates(subset=[key], keep="first")

    li = l.set_index(key)
    ri = r.set_index(key)

    only_left = li.index.difference(ri.index)
    only_right = ri.index.difference(li.index)
    both = li.index.intersection(ri.index)

    compare_cols = [c for c in l.columns if c in r.columns and c != key]

    changed_mask = pd.DataFrame(index=both)
    for c in compare_cols:
        lv, rv = li.loc[both, c], ri.loc[both, c]
        # NaN == NaN 视为相等，不能直接 !=
        ne = (lv != rv) & ~(lv.isna() & rv.isna())
        changed_mask[c] = ne.values

    changed_per_col = [
        {"column": str(c), "changed_rows": int(changed_mask[c].sum())}
        for c in compare_cols
    ]
    changed_per_col = [x for x in changed_per_col if x["changed_rows"] > 0]
    changed_per_col.sort(key=lambda x: -x["changed_rows"])

    any_changed = changed_mask.any(axis=1) if compare_cols else pd.Series(False, index=both)
    changed_rows = int(any_changed.sum()) if len(both) else 0

    # 差异明细：挑前若干行、每行只列真正变化的列
    samples = []
    for kv in list(both[any_changed.values])[:MAX_SAMPLE_ROWS]:
        row = changed_mask.loc[kv]
        for c in compare_cols:
            if bool(row[c]):
                samples.append({
                    "_key": py(kv), "column": str(c),
                    "left": py(li.loc[kv, c]), "right": py(ri.loc[kv, c]),
                })
                if len(samples) >= MAX_SAMPLE_ROWS:
                    break
        if len(samples) >= MAX_SAMPLE_ROWS:
            break

    return {
        "key": key,
        "key_has_duplicates": bool(left_dup or right_dup),
        "dupe_rows_left": left_dup,
        "dupe_rows_right": right_dup,
        "only_left_rows": int(len(only_left)),
        "only_right_rows": int(len(only_right)),
        "matched_rows": int(len(both)),
        "unchanged_rows": int(len(both) - changed_rows),
        "changed_rows": changed_rows,
        "changed_columns": changed_per_col,
        "samples": samples,
        "only_left_keys": [py(v) for v in list(only_left)[:50]],
        "only_right_keys": [py(v) for v in list(only_right)[:50]],
        "truncated": changed_rows > MAX_SAMPLE_ROWS,
    }


# ------------------------------------------------------------------ 分布漂移
def compare_drift(left: pd.DataFrame, right: pd.DataFrame) -> dict:
    numeric, categorical = [], []

    for c in left.columns:
        if c not in right.columns:
            continue
        ls, rs = left[c], right[c]
        name = str(c)

        if pd.api.types.is_numeric_dtype(ls) and pd.api.types.is_numeric_dtype(rs):
            lv = pd.to_numeric(ls, errors="coerce").dropna()
            rv = pd.to_numeric(rs, errors="coerce").dropna()
            if lv.empty or rv.empty:
                continue
            lm, rm = float(lv.mean()), float(rv.mean())
            delta = rm - lm
            pct = (delta / abs(lm)) if abs(lm) > 1e-12 else None
            numeric.append({
                "column": name,
                "left_mean": lm, "right_mean": rm,
                "left_p50": float(lv.median()), "right_p50": float(rv.median()),
                "left_min": float(lv.min()), "right_min": float(rv.min()),
                "left_max": float(lv.max()), "right_max": float(rv.max()),
                "left_sum": float(lv.sum()), "right_sum": float(rv.sum()),
                "left_null_pct": round(float(ls.isna().mean()), 6),
                "right_null_pct": round(float(rs.isna().mean()), 6),
                "mean_delta": delta,
                "mean_delta_pct": pct,
                "sum_delta": float(rv.sum() - lv.sum()),
                "alert": pct is not None and abs(pct) >= 0.2,
            })
        else:
            lc = ls.astype(str).value_counts()
            rc = rs.astype(str).value_counts()
            if lc.empty and rc.empty:
                continue
            n_l, n_r = max(int(lc.sum()), 1), max(int(rc.sum()), 1)
            vals = list(dict.fromkeys(list(lc.head(8).index) + list(rc.head(8).index)))[:10]
            tops = [{
                "value": str(v),
                "left": int(lc.get(v, 0)),
                "right": int(rc.get(v, 0)),
                "left_pct": round(int(lc.get(v, 0)) / n_l, 6),
                "right_pct": round(int(rc.get(v, 0)) / n_r, 6),
            } for v in vals]
            lv = [t for t in tops if t["left"] > 0 and t["right"] == 0]
            rv2 = [t for t in tops if t["right"] > 0 and t["left"] == 0]
            categorical.append({
                "column": name,
                "left_unique": int(ls.nunique(dropna=True)),
                "right_unique": int(rs.nunique(dropna=True)),
                "left_null_pct": round(float(ls.isna().mean()), 6),
                "right_null_pct": round(float(rs.isna().mean()), 6),
                "tops": tops,
                "new_values": [t["value"] for t in rv2][:10],
                "missing_values": [t["value"] for t in lv][:10],
            })

    numeric.sort(key=lambda x: -(abs(x["mean_delta_pct"] or 0)))
    return {"numeric": numeric, "categorical": categorical}


# ------------------------------------------------------------------ 漂移图
def drift_chart(drift: list[dict], metric: str = "mean") -> dict:
    """均值漂移图：每个数值列两根柱子（前 / 后），加一条变化率折线。"""
    rows = [r for r in drift if r.get("left_mean") is not None][:20]
    if not rows:
        raise CompareError("没有可对比的数值列")

    cols = [r["column"] for r in rows]
    left_vals = [round(r["left_mean"], 4) for r in rows]
    right_vals = [round(r["right_mean"], 4) for r in rows]
    pct = [round((r["mean_delta_pct"] or 0) * 100, 2) for r in rows]

    from . import viz
    o = viz._base("数值列均值漂移（左：基准 / 右：对比）")
    o["tooltip"] = viz._tooltip("axis")
    o["legend"] = viz._legend()
    o["grid"] = {"left": 20, "right": 24, "top": 46, "bottom": 24, "containLabel": True}
    o["xAxis"] = viz._cat_axis(rotate=30, data=cols)
    o["yAxis"] = [
        viz._value_axis("均值"),
        {**viz._value_axis("变化率", position="right"),
         "splitLine": {"show": False},
         "axisLabel": {"color": _c("text"), "fontSize": 11,
                       "formatter": "function(v){return v+'%';}"}},
    ]
    o["series"] = [
        {"name": "基准", "type": "bar", "data": left_vals, "barMaxWidth": 26,
         "itemStyle": {"color": "#8ea6cf", "borderRadius": [3, 3, 0, 0]}},
        {"name": "对比", "type": "bar", "data": right_vals, "barMaxWidth": 26,
         "itemStyle": {"color": "#3b6fd4", "borderRadius": [3, 3, 0, 0]}},
        {"name": "变化率", "type": "line", "yAxisIndex": 1, "data": pct,
         "smooth": True, "symbolSize": 6,
         "lineStyle": {"width": 2, "color": "#e8734a"},
         "itemStyle": {"color": "#e8734a"}},
    ]
    return jsonable(o)


def category_shift_chart(cats: list[dict], column: str) -> dict:
    """指定类别列的取值构成变化。"""
    target = next((c for c in cats if c["column"] == column), None)
    if not target:
        raise CompareError(f"没有找到类别列 {column!r}")
    tops = target["tops"]
    if not tops:
        raise CompareError("该列没有可对比的取值")

    from . import viz
    cats_labels = [t["value"] for t in tops]
    o = viz._base(f"{column} 构成变化")
    o["tooltip"] = viz._tooltip("axis")
    o["legend"] = viz._legend()
    o["grid"] = {"left": 20, "right": 24, "top": 46, "bottom": 24, "containLabel": True}
    o["xAxis"] = viz._cat_axis(rotate=30, data=cats_labels)
    o["yAxis"] = dict(viz._value_axis("占比"))
    o["yAxis"]["axisLabel"] = {"color": _c("text"), "fontSize": 11,
                               "formatter": "function(v){return (v*100).toFixed(0)+'%';}"}
    o["series"] = [
        {"name": "基准", "type": "bar", "data": [t["left_pct"] for t in tops], "barMaxWidth": 26,
         "itemStyle": {"color": "#8ea6cf", "borderRadius": [3, 3, 0, 0]}},
        {"name": "对比", "type": "bar", "data": [t["right_pct"] for t in tops], "barMaxWidth": 26,
         "itemStyle": {"color": "#3b6fd4", "borderRadius": [3, 3, 0, 0]}},
    ]
    return jsonable(o)


# ------------------------------------------------------------------ 汇总入口
def compare_all(left: pd.DataFrame, right: pd.DataFrame, key: str | None = None,
                left_name: str = "基准", right_name: str = "对比") -> dict:
    structure = compare_structure(left, right, left_name, right_name)
    drift = compare_drift(left, right)

    rows = None
    if key:
        rows = compare_rows(left, right, key)
    elif len(left) + len(right) > MAX_ROWS_FOR_DIFF:
        structure["note"] = (
            f"两份数据合计 {len(left) + len(right):,} 行，未指定关联键时"
            f"只做了结构层对比；想逐行比对请指定主键。"
        )

    out = {
        "structure": structure,
        "rows": rows,
        "drift": drift,
        "keys": suggest_keys(left, right),
    }
    return jsonable(out)
