# -*- coding: utf-8 -*-
"""图表聚合层：输入 DataFrame + 字段选择，输出 ECharts option。

把 option 放后端生成的理由没变 —— 分组聚合、分箱、百分位这些活儿
pandas 干着比 JS 顺手，前后端只传一份 JSON。

v2 的改进（解决「只能指定 X 轴、一个图只有一条线」）：
  - 所有笛卡尔图都支持**多指标（Y 多选）**，一个图上同时画多条线/多组柱
  - 支持**分组列**（group），相当于 SQL 里 GROUP BY x, group 再 pivot
  - 支持堆叠 / 面积 / 平滑 / 横向 / 数值标签 / 双 Y 轴
  - 图表类型扩到 13 种
"""
from __future__ import annotations

import contextlib
import contextvars

import numpy as np
import pandas as pd

from .config import MAX_CHART_POINTS
from .jsonio import jsonable, py

# 中国区习惯：涨=红 跌=绿。主色板偏专业冷色，涨跌语义才套用红绿。
PALETTE = [
    "#3b6fd4", "#e8734a", "#2fa8a5", "#c9a227", "#8f5fc0",
    "#4a9e6b", "#d1668f", "#5c86c1", "#b8863b", "#6b8f9c",
    "#c2564f", "#7d86b3", "#59a06b", "#b5703c", "#4fa3b8",
]
UP_COLOR = "#d64541"    # 涨 / 正向
DOWN_COLOR = "#2e9e6b"  # 跌 / 负向

BASE_TEXT_STYLE = {"fontFamily": "-apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif"}

# 图表里的文字 / 坐标轴 / 网格线 / tooltip 都要跟着界面主题走。
# 注意：涨跌红绿（UP_COLOR / DOWN_COLOR）是语义色，两套主题共用，不进这张表。
THEMES = {
    "light": {
        "title": "#333a45",
        "text": "#5b6472",
        "axis": "#d5dae2",
        "split": "#eef1f5",
        "tip_bg": "rgba(255,255,255,.96)",
        "tip_border": "#d8dce3",
        "tip_text": "#2b3038",
        "heat_mid": "#ffffff",
        "heat_low": "#e8f2ec",
        "heat_high": "#fbeaea",
        "shadow": "rgba(0,0,0,.2)",
        "label": "#2b3038",
    },
    "dark": {
        "title": "#e7eaf0",
        "text": "#aab2c0",
        "axis": "#39414f",
        "split": "#272d38",
        "tip_bg": "rgba(28,33,43,.97)",
        "tip_border": "#39414f",
        "tip_text": "#e7eaf0",
        "heat_mid": "#161a22",
        "heat_low": "#16291f",
        "heat_high": "#2e1d1f",
        "shadow": "rgba(0,0,0,.55)",
        "label": "#e7eaf0",
    },
}

_THEME = contextvars.ContextVar("ds_chart_theme", default="light")


def _c(key: str) -> str:
    """取当前主题下的颜色。"""
    return THEMES.get(_THEME.get(), THEMES["light"]).get(key, "#000000")


def current_theme() -> str:
    return _THEME.get()


@contextlib.contextmanager
def use_theme(theme: str):
    """临时切到某个主题，出作用域自动还原。

    compare.py 里的图表函数也要用主题色，但它不负责决定主题，
    由调用方（app.py）用这个包一下最省事。
    """
    token = _THEME.set(theme if theme in THEMES else "light")
    try:
        yield
    finally:
        _THEME.reset(token)


MAX_GROUP_VALUES = 8   # 分组列的取值上限，超出的归到「其他」，否则图没法看

# 图表顶部的三段高度：标题 → 图例 → 绘图区。图例单独一行，避免和标题打架。
TITLE_TOP = 10
LEGEND_TOP = 32
GRID_TOP = 58


# ------------------------------------------------------------------ 底座
def _base(title: str = "") -> dict:
    o = {
        "color": PALETTE,
        "textStyle": BASE_TEXT_STYLE,
        "backgroundColor": "transparent",
        "animationDuration": 380,
        "grid": {"left": 20, "right": 28, "top": GRID_TOP, "bottom": 24, "containLabel": True},
    }
    if title:
        o["title"] = {
            "text": title, "left": 14, "top": TITLE_TOP,
            "textStyle": {"fontSize": 13.5, "fontWeight": 600, "color": _c("title")},
            # 标题过长时截断，不要撑到右边压住图例
            "overflow": "truncate", "maxWidth": "86%",
        }
    return o


def _cat_axis(name: str = "", rotate: int = 0, data=None) -> dict:
    a = {
        "type": "category",
        "name": name,
        "nameLocation": "middle",
        "nameGap": 30,
        "axisLabel": {"rotate": rotate, "color": _c("text"), "fontSize": 11, "hideOverlap": True},
        "axisLine": {"lineStyle": {"color": _c("axis")}},
        "axisTick": {"show": False},
    }
    if data is not None:
        a["data"] = data
    return a


def _value_axis(name: str = "", position: str = "left") -> dict:
    return {
        "type": "value",
        "name": name,
        "position": position,
        "nameTextStyle": {"color": _c("text"), "fontSize": 11},
        "axisLabel": {"color": _c("text"), "fontSize": 11},
        "splitLine": {"lineStyle": {"color": _c("split"), "type": "dashed"}},
    }


def _tooltip(trigger: str = "axis") -> dict:
    return {
        "trigger": trigger,
        "axisPointer": {"type": "shadow" if trigger == "axis" else "cross"},
        "backgroundColor": _c("tip_bg"),
        "borderColor": _c("tip_border"), "borderWidth": 1,
        "textStyle": {"color": _c("label"), "fontSize": 12},
        "confine": True,
    }


def _legend(extra_bottom: int = 0) -> dict:
    """图例单独占标题下面一行。

    之前和标题同一行（top:8 / left:120），标题稍微长一点就会压在图例上，
    看起来像文字互相覆盖。标题区高度固定，图例另起一行最稳。
    """
    return {
        "type": "scroll", "top": LEGEND_TOP, "right": 14, "left": 14,
        "itemHeight": 8, "itemWidth": 13, "itemGap": 10,
        "textStyle": {"fontSize": 11, "color": _c("text")},
    }


def _pct_fmt() -> str:
    """把 0.1234 显示成 12.3% 的 label formatter。"""
    return "function(p){var v=p.value;return v==null?'':(typeof v==='number'?(v*100).toFixed(1)+'%':v);}"


# ------------------------------------------------------------------ 聚合核心
def _aggregate(df: pd.DataFrame, x: str, y: str | None, agg: str,
               group: str | None = None, limit: int = 30, sort: str = "desc",
               stack: bool = False) -> tuple[list[str], dict[str, list]]:
    """按 x 聚合 y。

    返回 (cat_labels, {series_key: values})
      - 无分组：series_key 就是 None
      - 有分组：series_key 是分组取值，同时会把长尾归到「其他」
    """
    limit = int(limit or 30)
    agg = agg or "sum"

    def _agg_value(g):
        if y is None:
            return g.size()
        if agg == "nunique":
            return g[y].nunique()
        if agg == "count":
            return g[y].count()
        if agg == "median":
            return g[y].median()
        if agg in ("sum", "mean", "min", "max", "std", "var"):
            return getattr(g[y], agg)()
        raise ValueError(f"不支持的聚合方式: {agg}")

    if group:
        # 先把长尾分组值合并，避免图例爆炸
        vc = df[group].astype(str).value_counts()
        keep = vc.head(MAX_GROUP_VALUES - 1).index.tolist()
        tmp = df.copy()
        tmp["__g"] = tmp[group].astype(str).where(tmp[group].astype(str).isin(keep), "其他")

        piv = _agg_value(tmp.groupby([x, "__g"], dropna=False, observed=True)).unstack("__g")
        piv = piv.reindex(columns=keep + (["其他"] if "其他" in piv.columns else []))

        if isinstance(piv.columns, pd.MultiIndex):
            piv.columns = [str(c[-1]) for c in piv.columns]
        piv.columns = [str(c) for c in piv.columns]

        # 按各分组合计排序，取 TopN 个 X
        order = piv.sum(axis=1).sort_values(ascending=(sort == "asc"), na_position="last")
        piv = piv.reindex(order.index)
        if limit > 0:
            piv = piv.head(limit)
        # X 轴标签：保持时间类型的可读格式
        cats = [_x_label(v) for v in piv.index]
        fill = 0 if stack else np.nan
        return cats, {g: piv[g].fillna(fill).tolist() for g in piv.columns}

    s = _agg_value(df.groupby(x, dropna=False, observed=True))
    if limit > 0:
        if sort == "x":
            s = s.sort_index(ascending=True, na_position="last").head(limit)
        elif sort == "x_desc":
            s = s.sort_index(ascending=False, na_position="last").head(limit)
        else:
            s = s.sort_values(ascending=(sort == "desc"), na_position="last").head(limit)
    cats = [_x_label(v) for v in s.index]
    return cats, {None: s.tolist()}


def _x_label(v) -> str:
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        ts = pd.Timestamp(v)
        if ts.hour or ts.minute or ts.second:
            return ts.strftime("%Y-%m-%d %H:%M")
        return ts.strftime("%Y-%m-%d")
    if v is None or (isinstance(v, float) and v != v):
        return "缺失"
    return str(v)


def _numeric_cols(df: pd.DataFrame) -> list[str]:
    return [str(c) for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _metric_name(y: str | None, agg: str) -> str:
    if y is None:
        return "行数"
    suffix = {"sum": "求和", "mean": "均值", "median": "中位数", "max": "最大", "min": "最小",
              "count": "计数", "nunique": "去重数", "std": "标准差", "var": "方差"}.get(agg, agg)
    return f"{y}·{suffix}"


# ------------------------------------------------------------------ 通用多系列图
def chart_multi(df: pd.DataFrame, spec: dict) -> dict:
    """柱 / 线 / 面积 / 堆叠图统一实现。

    spec 字段：
      x        维度列
      ys       指标列（list，可多选）
      agg      聚合方式
      group    分组列（可选）
      limit    TopN
      sort     desc / asc / x（按 X 轴自然顺序）
      stack    是否堆叠
      area     是否填充面积
      smooth   是否平滑曲线
      horizontal 横向
      label    是否显示数值标签
    """
    chart = spec.get("chart", "bar")
    x = spec.get("x")
    ys = _as_list(spec.get("ys") or spec.get("y"))
    agg = spec.get("agg", "sum")
    group = spec.get("group") or None
    limit = int(spec.get("limit") or 30)
    sort = spec.get("sort", "desc")
    stack = bool(spec.get("stack")) and chart in ("bar", "hbar", "area", "stack_bar", "stack_area")
    area = bool(spec.get("area")) or chart in ("area", "stack_area")
    smooth = bool(spec.get("smooth", True))
    horizontal = chart == "hbar" or bool(spec.get("horizontal"))
    # 兼容老参数名 label
    show_label = bool(spec.get("show_label") or spec.get("label"))

    if not x:
        raise ValueError("请指定 X 轴（维度）列")
    if not ys:
        raise ValueError("请至少选择一个指标（Y 轴）列")

    # 多指标时，用第一个指标确定 X 轴的 TopN，其余按同一套 X 对齐
    base_cats, _ = _aggregate(df, x, ys[0], agg, group, limit, sort, stack)
    if not base_cats:
        raise ValueError(f"X 轴列 {x} 没有可用取值")

    series, cat_order = [], base_cats
    for y in ys:
        cats, series_map = _aggregate(df, x, y, agg, group, 0, sort, stack)
        idx_map_all = {k: dict(zip(cats, vals)) for k, vals in series_map.items()}
        for key, vals in series_map.items():
            lookup = idx_map_all[key]
            aligned = [lookup.get(c) for c in cat_order]
            name = _metric_name(y, agg) if key is None else f"{_metric_name(y, agg)} · {key}"
            s = {
                "name": name,
                "type": "bar" if chart in ("bar", "hbar", "stack_bar") else "line",
                "data": aligned,
                "emphasis": {"focus": "series"},
            }
            if s["type"] == "bar":
                s["barMaxWidth"] = 44
                s["itemStyle"] = {"borderRadius": [3, 3, 0, 0] if not horizontal else [0, 3, 3, 0]}
            else:
                s["smooth"] = smooth
                s["symbolSize"] = 5
                s["lineStyle"] = {"width": 2}
                if area:
                    s["areaStyle"] = {"opacity": .18}
                s["connectNulls"] = True
            if stack:
                s["stack"] = key if group else "total"
            if show_label:
                s["label"] = {"show": True, "position": "top" if not horizontal else "right",
                              "fontSize": 10, "color": _c("text")}
            series.append(s)
    if not series:
        raise ValueError("没有生成任何数据系列，检查所选指标列是否有数值数据")

    metric_desc = "、".join(_metric_name(y, agg) for y in ys)
    extra_desc = f"，按 {group} 分组" if group else ""
    o = _base(f"{metric_desc} × {x}{extra_desc}")
    o["tooltip"] = _tooltip("axis")
    o["legend"] = _legend()

    if horizontal:
        o["grid"] = {"left": 20, "right": 34, "top": 46, "bottom": 20, "containLabel": True}
        o["xAxis"] = _value_axis()
        o["yAxis"] = _cat_axis(x, data=cat_order[::-1])
        for s in series:
            if s["type"] == "bar":
                s["data"] = list(reversed(s["data"]))
    else:
        long_labels = max((len(str(c)) for c in cat_order), default=0) > 6
        o["xAxis"] = _cat_axis(x, rotate=30 if long_labels else 0, data=cat_order)
        o["yAxis"] = _value_axis()

    o["series"] = series
    o["extra"] = {"kind": chart, "metrics": ys, "groups": len(series)}
    return jsonable(o)


# ------------------------------------------------------------------ 双轴组合图
def chart_combo(df: pd.DataFrame, spec: dict) -> dict:
    """左轴柱状 + 右轴折线 —— 比如「收入（柱）和毛利率（线）」。"""
    x = spec.get("x")
    ys = _as_list(spec.get("ys") or spec.get("y"))
    secondary = _as_list(spec.get("secondary") or [])
    agg = spec.get("agg", "sum")
    limit = int(spec.get("limit") or 30)
    sort = spec.get("sort", "desc")
    smooth = bool(spec.get("smooth", True))

    if not x:
        raise ValueError("请指定 X 轴（维度）列")
    bars = [y for y in ys if y not in secondary]
    lines = secondary or []
    if not bars:
        bars, lines = ys[:1], ys[1:]
    if not bars:
        raise ValueError("请至少选择一个指标作为柱状（左轴）")

    cats, _ = _aggregate(df, x, bars[0], agg, None, limit, sort)

    series = []
    for y in bars:
        c, m = _aggregate(df, x, y, agg, None, 0, sort)
        idx = {k: v for k, v in zip(c, m[None])}
        series.append({
            "name": _metric_name(y, agg), "type": "bar", "yAxisIndex": 0,
            "data": [idx.get(k) for k in cats], "barMaxWidth": 40,
            "itemStyle": {"borderRadius": [3, 3, 0, 0]},
        })
    for y in lines:
        c, m = _aggregate(df, x, y, agg, None, 0, sort)
        idx = {k: v for k, v in zip(c, m[None])}
        series.append({
            "name": _metric_name(y, agg), "type": "line", "yAxisIndex": 1,
            "data": [idx.get(k) for k in cats],
            "smooth": smooth, "symbolSize": 5, "lineStyle": {"width": 2.2},
        })

    o = _base(f"{'、'.join(bars)}（柱） + {'、'.join(lines) or '—'}（线） × {x}")
    o["tooltip"] = _tooltip("axis")
    o["legend"] = _legend()
    long_labels = max((len(str(c)) for c in cats), default=0) > 6
    o["xAxis"] = _cat_axis(x, rotate=30 if long_labels else 0, data=cats)
    o["yAxis"] = [
        _value_axis(" / ".join(bars)),
        {**_value_axis(" / ".join(lines) or "", position="right"),
         "splitLine": {"show": False}},
    ]
    o["series"] = series
    return jsonable(o)


# ------------------------------------------------------------------ 分布类
def chart_histogram(df: pd.DataFrame, x: str = None, bins: int = 30, spec: dict = None) -> dict:
    if spec:
        x = spec.get("x") or x
        bins = int(spec.get("bins") or 30)
        cols = _as_list(spec.get("ys") or spec.get("y")) or [x]
    else:
        cols = [x]
    cols = [c for c in cols if c]
    if not cols:
        raise ValueError("请选择要画分布的数值列")

    bins = max(3, min(int(bins), 100))
    o = _base("分布直方图" + (f"（{bins} 个分箱）" if len(cols) == 1 else f"（{len(cols)} 列叠加）"))
    o["tooltip"] = _tooltip("axis")
    o["legend"] = _legend()

    series, labels = [], []
    for c in cols:
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if s.empty:
            continue
        counts, edges = np.histogram(s, bins=bins)
        if not labels:
            labels = [f"{edges[i]:.4g}" for i in range(bins)]
        series.append({
            "name": c, "type": "bar", "data": [int(v) for v in counts],
            "barWidth": "92%" if len(cols) == 1 else None,
            "itemStyle": {"borderRadius": [2, 2, 0, 0]},
        })
    if not series:
        raise ValueError("所选列没有可用的数值数据")

    o["xAxis"] = _cat_axis("分箱", rotate=35 if bins > 20 else 0, data=labels)
    o["yAxis"] = _value_axis("频数")
    o["series"] = series
    return jsonable(o)


def chart_boxplot(df: pd.DataFrame, ys: list[str] | None = None, spec: dict = None) -> dict:
    if spec:
        ys = _as_list(spec.get("ys") or spec.get("y")) or []
    ys = [c for c in (ys or []) if c]
    if not ys:
        raise ValueError("请至少选择一个需要画箱线的数值列")

    box, outliers = [], []
    for i, c in enumerate(ys):
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if s.empty:
            continue
        q1, med, q3 = np.percentile(s, [25, 50, 75])
        iqr = q3 - q1
        lo = float(s[s >= q1 - 1.5 * iqr].min()) if len(s) else float(q1)
        hi = float(s[s <= q3 + 1.5 * iqr].max()) if len(s) else float(q3)
        box.append([lo, float(q1), float(med), float(q3), hi])
        for v in s[(s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)]:
            outliers.append([i, py(v)])

    o = _base("箱线图（五数概括 + IQR 异常点）")
    o["tooltip"] = _tooltip("item")
    o["xAxis"] = _cat_axis(data=ys)
    o["yAxis"] = _value_axis()
    o["series"] = [
        {"name": "分布", "type": "boxplot", "data": box, "boxWidth": [12, 46],
         "itemStyle": {"borderWidth": 1.6, "color": "rgba(59,111,212,.14)", "borderColor": PALETTE[0]}},
        {"name": "异常点", "type": "scatter", "data": outliers, "symbolSize": 5,
         "itemStyle": {"color": UP_COLOR, "opacity": .75}},
    ]
    return jsonable(o)


def chart_scatter(df: pd.DataFrame, spec: dict) -> dict:
    x, y = spec.get("x"), spec.get("y") or (_as_list(spec.get("ys")) or [None])[0]
    color = spec.get("group") or spec.get("color") or None
    size_by = spec.get("size") or None
    if not x or not y:
        raise ValueError("散点图需要指定 X 和 Y 两列")

    cols = [x, y] + ([color] if color else []) + ([size_by] if size_by else [])
    sub = df[cols].copy()
    sub[x] = pd.to_numeric(sub[x], errors="coerce")
    sub[y] = pd.to_numeric(sub[y], errors="coerce")
    sub = sub.dropna(subset=[x, y])
    if sub.empty:
        raise ValueError("X、Y 两列需要有可对应的数值数据")

    n_total = len(sub)
    sampled = False
    if len(sub) > MAX_CHART_POINTS:
        sub = sub.sample(MAX_CHART_POINTS, random_state=42)
        sampled = True

    title = f"{y} × {x} 散点图（{n_total} 点" + (f"，抽样 {MAX_CHART_POINTS}）" if sampled else "）")
    o = _base(title)
    o["grid"] = {"left": 20, "right": 30, "top": 46, "bottom": 20, "containLabel": True}
    o["xAxis"] = _value_axis(x)
    o["yAxis"] = _value_axis(y)

    if color:
        vc = sub[color].astype(str).value_counts()
        keep = vc.head(MAX_GROUP_VALUES - 1).index.tolist()
        sub["__c"] = sub[color].astype(str).where(sub[color].astype(str).isin(keep), "其他")
        series = []
        for i, g in enumerate(keep + (["其他"] if len(keep) < len(vc) else [])):
            gsub = sub[sub["__c"] == g]
            if gsub.empty:
                continue
            series.append({
                "name": str(g), "type": "scatter", "symbolSize": 7,
                "data": jsonable(gsub[[x, y]].values.tolist()),
                "itemStyle": {"opacity": .72, "color": PALETTE[i % len(PALETTE)]},
            })
        o["legend"] = _legend()
    else:
        series = [{"name": f"{x} vs {y}", "type": "scatter", "symbolSize": 7,
                   "data": jsonable(sub[[x, y]].values.tolist()),
                   "itemStyle": {"opacity": .62, "color": PALETTE[0]}}]

    o["tooltip"] = _tooltip("item")
    o["series"] = series
    return jsonable(o)


def chart_pie(df: pd.DataFrame, spec: dict) -> dict:
    x = spec.get("x")
    y = (spec.get("y") or None)
    agg = spec.get("agg", "sum")
    topn = int(spec.get("limit") or 10)
    donut = spec.get("chart") == "ring"

    if not x:
        raise ValueError("请选择分类维度")

    cats_all, m_all = _aggregate(df, x, y, agg, None, 0, "desc")
    pairs = list(zip(cats_all, m_all[None]))
    total = sum((v or 0) for _, v in pairs)

    top = pairs[:topn]
    data = [{"name": c, "value": py(v)} for c, v in top]
    if len(pairs) > topn:
        rest = sum((v or 0) for _, v in pairs[topn:])
        data.append({"name": f"其他 ({len(pairs) - topn} 项)", "value": py(rest)})

    o = _base(f"{_metric_name(y, agg)} 构成 × {x}")
    o["tooltip"] = {"trigger": "item",
                    "formatter": "{b}: {c} ({d}%)",
                    "backgroundColor": _c("tip_bg"), "borderColor": _c("tip_border"),
                    "borderWidth": 1, "textStyle": {"color": _c("label")}}
    o["legend"] = {"type": "scroll", "orient": "vertical", "right": 8, "top": 40, "bottom": 20,
                   "itemHeight": 8, "itemWidth": 12,
                   "textStyle": {"fontSize": 11, "color": _c("text")}}
    o["grid"] = {}
    o["series"] = [{
        "type": "pie",
        "radius": ["42%", "66%"] if donut else ["0%", "66%"],
        "center": ["38%", "54%"],
        "avoidLabelOverlap": True, "data": data,
        "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "borderRadius": 3},
        "label": {"formatter": "{d}%", "fontSize": 11, "color": _c("text")},
        "labelLine": {"length": 8, "length2": 8},
    }]
    o["extra"] = {"total": py(total)}
    return jsonable(o)


# ------------------------------------------------------------------ 矩阵类
def chart_corr(df: pd.DataFrame, method: str = "pearson", spec: dict = None) -> dict:
    from .profiler import correlation
    if spec:
        method = spec.get("method") or method
    cor = correlation(df, method)
    if not cor["columns"]:
        raise ValueError("至少需要 2 个有效的数值列才能算相关性")
    cols = cor["columns"]
    vals = [abs(m[2]) for m in cor["matrix"] if m[2] is not None]
    vmax = max(vals) if vals else 1
    o = _base(f"数值列相关系数矩阵（{len(cols)}×{len(cols)}，{method}）")
    o["tooltip"] = {"position": "top",
                    "backgroundColor": _c("tip_bg"), "borderColor": _c("tip_border"),
                    "borderWidth": 1, "textStyle": {"color": _c("label")},
                    "formatter": "function(p){return p.data[2]==null?'—':p.data[2].toFixed(3);}"}
    o["grid"] = {"left": 20, "right": 24, "top": 46, "bottom": 60, "containLabel": True}
    o["xAxis"] = dict(_cat_axis(), data=cols)
    o["xAxis"]["splitArea"] = {"show": True}
    o["yAxis"] = dict(_cat_axis(), data=cols)
    o["yAxis"]["splitArea"] = {"show": True}
    o["visualMap"] = {
        "min": -vmax, "max": vmax, "calculable": True, "orient": "horizontal",
        "left": "center", "bottom": 6, "itemWidth": 12, "itemHeight": 90,
        "textStyle": {"fontSize": 11, "color": _c("text")},
        "inRange": {"color": [DOWN_COLOR, _c("heat_low"), _c("heat_mid"), _c("heat_high"), UP_COLOR]},
    }
    o["series"] = [{
        "type": "heatmap", "data": cor["matrix"],
        "label": {"show": len(cols) <= 10, "fontSize": 10, "color": _c("label"),
                  "formatter": "function(p){return p.data[2]==null?'':p.data[2].toFixed(2);}"},
        "itemStyle": {"borderColor": "#fff", "borderWidth": 1},
        "emphasis": {"itemStyle": {"shadowBlur": 8, "shadowColor": _c("shadow")}},
    }]
    return jsonable(o)


def chart_missing(df: pd.DataFrame, spec: dict = None) -> dict:
    from .profiler import missing_matrix
    mm = missing_matrix(df)
    cols = mm["columns"]
    o = _base("缺失值分布（每行/每列缺失情况）")
    o["tooltip"] = {"backgroundColor": _c("tip_bg"), "borderColor": _c("tip_border"),
                    "borderWidth": 1, "textStyle": {"color": _c("label")},
                    "formatter": "function(p){return p.data[2]?'缺失':'正常';}"}
    o["grid"] = {"left": 20, "right": 24, "top": 46, "bottom": 60, "containLabel": True}
    o["xAxis"] = dict(_cat_axis("列"), data=cols)
    o["yAxis"] = dict(_cat_axis(f"行（抽样 {mm['row_count_sampled']} 行）"),
                      data=[f"row_{i}" for i in range(mm["row_count_sampled"])])
    o["visualMap"] = {
        "min": 0, "max": 1, "calculable": False, "orient": "horizontal",
        "left": "center", "bottom": 6, "itemWidth": 12, "itemHeight": 90,
        "textStyle": {"fontSize": 11, "color": _c("text")},
        "inRange": {"color": [_c("split"), UP_COLOR]},
    }
    o["series"] = [{"type": "heatmap", "data": mm["points"],
                    "itemStyle": {"borderColor": "#fff", "borderWidth": .5}}]
    o["extra"] = {"per_column": mm["per_column"]}
    return jsonable(o)


def chart_missing_bar(df: pd.DataFrame, spec: dict = None) -> dict:
    vals = [{"column": str(c), "missing": int(df[c].isna().sum()),
             "pct": round(float(df[c].isna().mean()), 6)} for c in df.columns]
    vals = [v for v in vals if v["missing"] > 0]
    if not vals:
        raise ValueError("这份数据没有缺失值")
    vals.sort(key=lambda v: -v["pct"])
    o = _base("各列缺失率排序")
    o["xAxis"] = dict(_cat_axis("列"), data=[v["column"] for v in vals])
    o["yAxis"] = dict(_value_axis("缺失率"))
    o["yAxis"]["axisLabel"] = {"color": _c("text"), "fontSize": 11,
                               "formatter": "function(v){return (v*100).toFixed(0)+'%';}"}
    o["tooltip"] = _tooltip("axis")
    o["series"] = [{"name": "缺失率", "type": "bar",
                    "data": [v["pct"] for v in vals], "barMaxWidth": 36,
                    "itemStyle": {"color": UP_COLOR, "borderRadius": [3, 3, 0, 0]},
                    "label": {"show": True, "position": "top", "fontSize": 10,
                              "color": _c("text"),
                              "formatter": "function(p){return (p.value*100).toFixed(1)+'%';}"}}]
    return jsonable(o)


def chart_topcount(df: pd.DataFrame, spec: dict) -> dict:
    return chart_multi(df, {**spec, "chart": "bar", "ys": [None], "agg": "count"})


# ------------------------------------------------------------------ 目录
# 按分析目的分类，对齐主流 BI / 报表工具的卡片式选图入口。
# 术语保持行业通用（比较、趋势、构成、分布、相关性），不额外解释。
CHART_INTENTS = [
    {"key": "compare", "q": "比较与排名", "a": "柱状图 / 条形图",
     "charts": ["bar", "hbar"]},
    {"key": "trend", "q": "趋势", "a": "折线图 / 面积图",
     "charts": ["line", "area"]},
    {"key": "part", "q": "构成与占比", "a": "饼图 / 环形图 / 堆叠图",
     "charts": ["pie", "ring", "stack_bar", "stack_area"]},
    {"key": "dist", "q": "分布", "a": "直方图 / 箱线图",
     "charts": ["histogram", "boxplot"]},
    {"key": "relation", "q": "相关性", "a": "散点图 / 热力图",
     "charts": ["scatter", "corr"]},
    {"key": "rank", "q": "Top N", "a": "频次排行",
     "charts": ["topcount"]},
    {"key": "quality", "q": "缺失分析", "a": "缺失分布 / 缺失率",
     "charts": ["missing_map", "missing_bar"]},
    {"key": "mix", "q": "双轴对比", "a": "柱线组合图",
     "charts": ["combo"]},
]

CHART_CATALOG = [
    # 注意：`label` 是这个图表的显示名称，开关字段一律用 show_* 前缀，
    # 否则同名字段会互相覆盖（bar/line/hbar 的 label 曾被 show_label 冲掉，下拉框显示 true）
    {"chart": "bar", "label": "柱状图", "intent": "compare",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "stack": True, "horizontal": True, "show_label": True,
     "desc": "按维度对比数值大小，支持多指标、分组、堆叠",
     "purpose": "各城市销售额对比"},

    {"chart": "hbar", "label": "横向柱状图", "intent": "compare",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "stack": True, "show_label": True,
     "desc": "同柱状图，横向排列，适合维度名较长的场景",
     "purpose": "维度名称较长的对比场景"},

    {"chart": "line", "label": "折线图", "intent": "trend",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "smooth": True, "area": True, "show_label": True,
     "desc": "展示指标随连续维度的变化趋势",
     "purpose": "月度订单量走势"},

    {"chart": "area", "label": "面积图", "intent": "trend",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "smooth": True, "stack": True,
     "desc": "在折线下填充色块，适合表现累计量",
     "purpose": "累计销售额随时间变化"},

    {"chart": "stack_bar", "label": "堆叠柱状图", "intent": "part",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "stack": True, "show_label": True,
     "desc": "在同一柱体内分段展示构成",
     "purpose": "各城市中各品类的占比"},

    {"chart": "stack_area", "label": "堆叠面积图", "intent": "part",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "group": True, "smooth": True, "stack": True,
     "desc": "堆叠的面积图，同时体现总量与构成变化",
     "purpose": "各渠道占比随时间的变化"},

    {"chart": "pie", "label": "饼图", "intent": "part",
     "need_x": True, "need_y": True, "agg": True, "limit": True,
     "desc": "展示各分类占比，建议分类数不超过 8",
     "purpose": "各渠道销售额占比"},

    {"chart": "ring", "label": "环形图", "intent": "part",
     "need_x": True, "need_y": True, "agg": True, "limit": True,
     "desc": "同饼图，中心区域可用于标注合计值",
     "purpose": "占比展示且需标注合计"},

    {"chart": "combo", "label": "柱线组合图", "intent": "mix",
     "need_x": True, "need_y": True, "multi_y": True,
     "agg": True, "limit": True, "secondary": True, "smooth": True,
     "desc": "柱与线共用双 Y 轴，适合量级差异较大的指标",
     "purpose": "销售额（万元）与转化率（%）"
                "共用一个刻度的话转化率会被压成一条直线。这时就选它。"},

    {"chart": "scatter", "label": "散点图", "intent": "relation",
     "need_x": True, "need_y": True, "group": True,
     "desc": "展示两个数值变量的关系，可按类别着色",
     "purpose": "单价与销量的关系"
                "还能用第三个类别列给点上色，看不同类别的分布差异。"},

    {"chart": "histogram", "label": "直方图", "intent": "dist",
     "need_x": True, "need_y": True, "multi_y": True, "bins": True,
     "desc": "展示数值在各区间的分布频次",
     "purpose": "订单金额的分布区间"},

    {"chart": "boxplot", "label": "箱线图", "intent": "dist",
     "need_y": True, "multi_y": True,
     "desc": "对比多列的分布区间与离群点",
     "purpose": "各城市客单价分布对比"},

    {"chart": "topcount", "label": "频次排行", "intent": "rank",
     "need_x": True, "limit": True,
     "desc": "统计某列出现频次最高的 N 个取值",
     "purpose": "下单次数最多的前 10 个用户"},

    {"chart": "corr", "label": "相关性热力图", "intent": "relation",
     "desc": "数值列两两相关系数矩阵",
     "purpose": "建模前筛查多重共线性"},

    {"chart": "missing_map", "label": "缺失分布图", "intent": "quality",
     "desc": "按行列展示缺失值的位置分布",
     "purpose": "判断缺失是否集中在特定区间"
                "后者往往意味着采集环节有问题。"},

    {"chart": "missing_bar", "label": "缺失率柱状图", "intent": "quality",
     "desc": "按列展示缺失率排序",
     "purpose": "确定优先处理的列"},
]

# 标签里带上对应的 SQL 聚合函数名，方便对照着写 SQL
AGG_OPTIONS = [
    {"value": "sum", "label": "求和 SUM"},
    {"value": "mean", "label": "平均值 AVG"},
    {"value": "median", "label": "中位数 MEDIAN"},
    {"value": "count", "label": "计数 COUNT"},
    {"value": "nunique", "label": "去重计数 COUNT(DISTINCT)"},
    {"value": "max", "label": "最大值 MAX"},
    {"value": "min", "label": "最小值 MIN"},
    {"value": "std", "label": "标准差 STDDEV"},
]

SORT_OPTIONS = [
    {"value": "desc", "label": "降序"},
    {"value": "asc", "label": "升序"},
    {"value": "x", "label": "按 X 轴升序"},
    {"value": "x_desc", "label": "按 X 轴降序"},
]


def _as_list(v) -> list:
    """把 spec 里的 y / ys 统一成列表。

    注意要保留 None —— 它是「按行数计数」的哨兵（topcount 用），
    不能当成「没选指标」给过滤掉。只过滤空字符串（前端未选时会传这个）。
    """
    if v is None:
        return []
    items = list(v) if isinstance(v, (list, tuple)) else [v]
    return [x for x in items if x is None or x != ""]


def build(df: pd.DataFrame, spec: dict, theme: str = "light") -> dict:
    """按 spec 生成 ECharts option。

    theme 只影响图表的文字/坐标轴/tooltip 配色，不影响数据本身。
    用 contextvar 传递，省得给十几个 chart_* 函数都加一遍参数。
    """
    token = _THEME.set(theme if theme in THEMES else "light")
    try:
        return _build_inner(df, spec)
    finally:
        _THEME.reset(token)


def _build_inner(df: pd.DataFrame, spec: dict) -> dict:
    chart = spec.get("chart", "bar")

    # 兼容老的单 y 参数
    if spec.get("y") and not spec.get("ys"):
        spec = {**spec, "ys": [spec["y"]]}

    if chart in ("bar", "hbar", "line", "area", "stack_bar", "stack_area"):
        # stack_bar / stack_area 只是预设了 stack=True 的别名
        if chart == "stack_bar":
            spec = {**spec, "chart": "bar", "stack": True}
        if chart == "stack_area":
            spec = {**spec, "chart": "area", "stack": True}
        return chart_multi(df, spec)
    if chart == "combo":
        return chart_combo(df, spec)
    if chart in ("pie", "ring"):
        return chart_pie(df, spec)
    if chart == "scatter":
        return chart_scatter(df, spec)
    if chart == "histogram":
        return chart_histogram(df, spec=spec)
    if chart == "boxplot":
        return chart_boxplot(df, spec=spec)
    if chart == "topcount":
        return chart_topcount(df, spec)
    if chart == "corr":
        return chart_corr(df, spec=spec)
    if chart == "missing_map":
        return chart_missing(df, spec)
    if chart == "missing_bar":
        return chart_missing_bar(df, spec)
    raise ValueError(f"未知图表类型: {chart}")
