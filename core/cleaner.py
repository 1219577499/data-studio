# -*- coding: utf-8 -*-
"""数据清洗算子。

每个算子干两件事：真实地改 DataFrame，同时吐出等价的 pandas 代码。
后者不是装饰品 —— 你照着这份代码就能把操作固化进自己的 ETL 脚本里。
"""
from __future__ import annotations

import re
import warnings

import numpy as np
import pandas as pd

AGG_FUNCS = {
    "sum": "sum", "mean": "mean", "median": "median", "max": "max", "min": "min",
    "count": "count", "nunique": "nunique", "std": "std", "var": "var",
}

CMP_TEXT = {"=", "!=", ">", ">=", "<", "<=", "between", "contains", "startswith", "is_null", "not_null", "in"}


def _q(v) -> str:
    """把 Python 值转成代码里的字面量。"""
    if isinstance(v, str):
        return repr(v)
    if v is None:
        return "None"
    if isinstance(v, bool):
        return str(v)
    return str(v)


def _list_q(vs) -> str:
    return "[" + ", ".join(_q(v) for v in vs) + "]"


def _check_cols(df: pd.DataFrame, cols: list[str], op_name: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"[{op_name}] 列不存在: {missing}；可用列: {list(df.columns)[:20]}")


# ------------------------------------------------------------------ 单个算子
def _op_drop_duplicates(df, p) -> tuple[pd.DataFrame, str, dict]:
    subset = p.get("subset") or None
    keep = p.get("keep", "first") or "first"
    if subset:
        _check_cols(df, subset, "去重")
    before = len(df)
    out = df.drop_duplicates(subset=subset, keep=keep)
    args = [] if not subset else [f"subset={_list_q(subset)}"]
    args.append(f"keep={_q(keep)}")
    code = f"df = df.drop_duplicates({', '.join(args)})"
    return out, code, {"removed_rows": before - len(out)}


def _op_drop_columns(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = list(p.get("columns") or [])
    _check_cols(df, cols, "删除列")
    if not cols:
        return df, "# 未选择列，跳过", {}
    return df.drop(columns=cols), f"df = df.drop(columns={_list_q(cols)})", {"removed_cols": len(cols)}


def _op_keep_columns(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = list(p.get("columns") or [])
    _check_cols(df, cols, "保留列")
    if not cols:
        raise ValueError("请至少选择一列")
    removed = [c for c in df.columns if c not in cols]
    return df[cols], f"df = df[{_list_q(cols)}]", {"removed_cols": len(removed)}


def _op_rename(df, p) -> tuple[pd.DataFrame, str, dict]:
    col, new = p.get("column"), p.get("new_name")
    _check_cols(df, [col], "重命名")
    if not new:
        raise ValueError("新列名不能为空")
    code = f"df = df.rename(columns={{'{col}': '{new}'}})"
    return df.rename(columns={col: new}), code, {"renamed": f"{col} -> {new}"}


def _op_astype(df, p) -> tuple[pd.DataFrame, str, dict]:
    col, target = p.get("column"), p.get("target")
    _check_cols(df, [col], "类型转换")
    s = df[col]
    rounded = False
    if target == "int":
        num = pd.to_numeric(s, errors="coerce")
        # 直接把带小数的 float 转 int 会抛 "cannot safely cast"，先四舍五入
        out = num.round(0).astype("Int64")
        rounded = True
        code = f"df['{col}'] = pd.to_numeric(df['{col}'], errors='coerce').round(0).astype('Int64')"
    elif target == "float":
        out = pd.to_numeric(s, errors="coerce").astype("float64")
        code = f"df['{col}'] = pd.to_numeric(df['{col}'], errors='coerce')"
    elif target == "str":
        out = s.map(lambda v: None if pd.isna(v) else str(v))
        code = f"df['{col}'] = df['{col}'].astype(str)"
    elif target == "datetime":
        fmt = p.get("fmt") or None
        out = pd.to_datetime(s, errors="coerce", format=fmt)
        if fmt:
            code = f"df['{col}'] = pd.to_datetime(df['{col}'], errors='coerce', format={fmt!r})"
        else:
            code = f"df['{col}'] = pd.to_datetime(df['{col}'], errors='coerce')"
    elif target == "category":
        out = s.astype("category")
        code = f"df['{col}'] = df['{col}'].astype('category')"
    elif target == "bool":
        out = s.map(lambda v: None if pd.isna(v) else (str(v).strip().lower() in ("true", "1", "yes", "y", "是")))
        code = f"df['{col}'] = df['{col}'].astype(str).str.lower().isin(['true','1','yes','y','是'])"
    else:
        raise ValueError(f"不支持的目标类型: {target}")
    new = df.copy()
    new[col] = out
    coerced = int(out.isna().sum() - s.isna().sum())
    effect = {"new_dtype": str(out.dtype), "coerced_to_null": max(coerced, 0)}
    if rounded:
        effect["rounded"] = "小数已四舍五入取整"
    return new, code, effect


def _op_fillna(df, p) -> tuple[pd.DataFrame, str, dict]:
    col, strat = p.get("column"), p.get("strategy")
    value = p.get("value")
    _check_cols(df, [col], "填充缺失")
    s = df[col]
    filled = int(s.isna().sum())
    if strat == "mean":
        v = pd.to_numeric(s, errors="coerce").mean()
        out, code = s.fillna(v), f"df['{col}'] = df['{col}'].fillna(df['{col}'].mean())"
    elif strat == "median":
        v = pd.to_numeric(s, errors="coerce").median()
        out, code = s.fillna(v), f"df['{col}'] = df['{col}'].fillna(df['{col}'].median())"
    elif strat == "mode":
        m = s.mode(dropna=True)
        v = m.iloc[0] if len(m) else None
        out, code = s.fillna(v), f"df['{col}'] = df['{col}'].fillna(df['{col}'].mode().iloc[0])"
    elif strat == "zero":
        out, code = s.fillna(0), f"df['{col}'] = df['{col}'].fillna(0)"
    elif strat == "group_mode":
        by = p.get("by")
        _check_cols(df, [by], "分组填充")
        modes = df.groupby(by, observed=True)[col].transform(
            lambda x: x.mode().iloc[0] if x.notna().any() else None)
        out = s.fillna(modes)
        code = (f"df['{col}'] = df['{col}'].fillna(\n"
                f"    df.groupby('{by}')['{col}'].transform(lambda x: x.mode().iloc[0] if x.notna().any() else np.nan)\n"
                f")")
    elif strat == "ffill":
        out, code = s.ffill(), f"df['{col}'] = df['{col}'].ffill()"
    elif strat == "bfill":
        out, code = s.bfill(), f"df['{col}'] = df['{col}'].bfill()"
    elif strat == "interpolate":
        out = pd.to_numeric(s, errors="coerce").interpolate()
        code = f"df['{col}'] = df['{col}'].interpolate(method='linear')"
    elif strat == "constant":
        out, code = s.fillna(value), f"df['{col}'] = df['{col}'].fillna({_q(value)})"
    else:
        raise ValueError(f"不支持的填充策略: {strat}")
    new = df.copy()
    new[col] = out
    return new, code, {"filled": filled, "remaining_null": int(out.isna().sum())}


def _op_dropna(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = list(p.get("columns") or [])
    if cols:
        _check_cols(df, cols, "删除缺失行")
    how = p.get("how", "any")
    before = len(df)
    out = df.dropna(subset=cols or None, how=how)
    args = [] if not cols else [f"subset={_list_q(cols)}"]
    args.append(f"how={_q(how)}")
    return out, f"df = df.dropna({', '.join(args)})", {"removed_rows": before - len(out)}


def _resolve_text_cols(df, cols, action: str) -> list[str]:
    """不指定列时自动挑出所有文本列。

    这样「一键基础清理」里的全表去空格才好用 —— 不然用户得自己
    一列列勾，而文本列本来就能自动识别出来。
    """
    if cols:
        _check_cols(df, cols, action)
        return list(cols)
    out = [c for c in df.columns
           if isinstance(df[c].dtype, pd.StringDtype) or df[c].dtype == object]
    if not out:
        raise ValueError(f"这张表里没有文本列，「{action}」用不上")
    return out


def _op_strip(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = _resolve_text_cols(df, p.get("columns"), "去空格")
    new = df.copy()
    changed = 0
    for c in cols:
        s = new[c]
        # 先按字符串比一遍，数出有多少行真的带首尾空格
        as_str = s.map(lambda v: v if isinstance(v, str) else str(v))
        changed += int((as_str != as_str.str.strip()).sum())
        # 实际处理只对字符串值下手，非字符串原样保留，避免把数值列整列搞坏
        new[c] = s.map(lambda v: v.strip() if isinstance(v, str) else v)
    code = ("# 只对字符串值生效，其它类型原样保留\n"
            "for c in " + _list_q(cols) + ":\n"
            "    df[c] = df[c].map(lambda v: v.strip() if isinstance(v, str) else v)")
    return new, code, {"fixed": changed}


def _op_replace(df, p) -> tuple[pd.DataFrame, str, dict]:
    """值替换。

    默认是**子串替换**（和 Excel 的查找替换、Ctrl+H 一个语义）：
    '12㎡' 里把 '㎡' 换成 '' 就得到 '12'。
    之前用的是 Series.replace()，那是**整值精确匹配** —— 只有整个单元格
    恰好等于 '㎡' 才会被替换，'12㎡' 一点反应都没有，很容易让人以为功能坏了。
    需要精确匹配的可以切到 exact 模式。
    """
    col, pairs = p.get("column"), p.get("pairs") or []
    _check_cols(df, [col], "替换值")
    if not pairs:
        raise ValueError("没有配置替换规则")
    mode = p.get("mode") or "substring"
    use_regex = bool(p.get("regex"))
    # 保留输入顺序，且过滤掉空的原值
    items = [(str(x.get("from")), x.get("to")) for x in pairs if x.get("from") not in (None, "")]
    if not items:
        raise ValueError("至少要填一条「原值」，新值可以留空（表示删除）")

    mapping = {k: ("" if v is None else str(v)) for k, v in items}
    s = df[col]

    if mode == "exact":
        hit = int(s.isin(list(mapping.keys())).sum())
        new = df.copy()
        new[col] = s.replace(mapping)
        code = f"df['{col}'] = df['{col}'].replace({mapping!r})   # 整值精确替换"
        effect = {"replaced_rows": hit, "mode": "精确匹配（整个值相等才替换）"}
    else:
        work = s.astype("string")
        keys = list(mapping.keys())

        # 命中统计：正则模式下必须用 re.search，不能拿模式串去做字面 in 判断
        # （否则 (\d+)㎡ 这种永远统计成 0 行，让人以为没生效）
        if use_regex:
            compiled = []
            for k in keys:
                try:
                    compiled.append(re.compile(k))
                except re.error as e:
                    raise ValueError(
                        f"原值 {k!r} 不是合法的正则表达式：{e}\n"
                        f"提示：如果只想替换普通文字，请把「原值按正则解释」关掉。"
                    ) from e
            hit = int(work.map(
                lambda v: isinstance(v, str) and any(c.search(v) is not None for c in compiled)
            ).sum())
        else:
            hit = int(work.map(
                lambda v: isinstance(v, str) and any(k in v for k in keys)
            ).sum())

        try:
            for k, v in mapping.items():
                work = work.str.replace(k, v, regex=use_regex)
        except re.error as e:
            hint = ("提示：勾了「原值按正则解释」之后，**新值里不能写正则**，"
                    "只能是字面文本，或者用反向引用 \\1 \\2 指代原值里捕获到的内容。"
                    "比如要保留数字：原值填 (\\d+)㎡，新值填 \\1" if use_regex else "")
            raise ValueError(f"替换失败：{e}。{hint}")

        new = df.copy()
        new[col] = work
        lines = ["# 子串替换（新值填空字符串 = 删除该片段）"]
        for k, v in mapping.items():
            pat = k if use_regex else repr(k)
            lines.append(f"df['{col}'] = df['{col}'].astype('string').str.replace({pat}, {v!r}, regex={use_regex})")
        code = "\n".join(lines)
        effect = {"replaced_rows": hit,
                  "mode": "正则替换" if use_regex else "子串替换（包含即替换）"}

    if hit == 0:
        effect["warning"] = (
            "没有任何行被替换。"
            + ("检查正则是否写对（可以用「按正则提取」先试试能不能匹配到）。"
               if use_regex else
               "子串替换是「包含就换」，直接写要删掉的那段文字即可，"
               "不需要 % * ? 这类通配符。原值填 %d*㎡ 是匹配不到任何东西的。")
        )

    return new, code, effect


def _as_float(v, col):
    """数值列做比较时把过滤值转成 float，转不了就给出人话提示。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"列 {col} 是数值列，过滤值 {v!r} 需要填数字") from None


def _op_filter(df, p) -> tuple[pd.DataFrame, str, dict]:
    col, opr = p.get("column"), p.get("operator")
    v, v2 = p.get("value"), p.get("value2")
    _check_cols(df, [col], "过滤")
    s = df[col]
    if opr == "is_null":
        mask, code = s.isna(), f"mask = df['{col}'].isna()"
    elif opr == "not_null":
        mask, code = s.notna(), f"mask = df['{col}'].notna()"
    elif opr == "contains":
        mask = s.astype("string").str.contains(str(v), case=False, na=False, regex=False)
        code = f"mask = df['{col}'].str.contains({_q(str(v))}, na=False, case=False)"
    elif opr == "startswith":
        mask = s.astype("string").str.startswith(str(v), na=False)
        code = f"mask = df['{col}'].str.startswith({_q(str(v))}, na=False)"
    elif opr == "in":
        vals = [x.strip() for x in str(v).split(",") if x.strip()]
        mask = s.isin(vals)
        code = f"mask = df['{col}'].isin({_list_q(vals)})"
    elif opr == "between":
        num = pd.to_numeric(s, errors="coerce")
        lo, hi = _as_float(v, col), _as_float(v2, col)
        mask = num.between(lo, hi)
        code = f"mask = df['{col}'].between({_q(lo)}, {_q(hi)})"
    else:
        sym = {"=": "==", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}[opr]
        if pd.api.types.is_numeric_dtype(s):
            num = pd.to_numeric(s, errors="coerce")
            fv = _as_float(v, col)
            mask = {"==": num == fv, "!=": num != fv, ">": num > fv,
                    ">=": num >= fv, "<": num < fv, "<=": num <= fv}[sym]
            code = f"mask = df['{col}'] {sym} {_q(fv)}"
        else:
            mask = {"==": s.astype(str) == str(v), "!=": s.astype(str) != str(v),
                    ">": s.astype(str) > str(v), ">=": s.astype(str) >= str(v),
                    "<": s.astype(str) < str(v), "<=": s.astype(str) <= str(v)}[sym]
            code = f"mask = df['{col}'] {sym} {_q(v)}"
    out = df[mask]
    return out, f"{code}\ndf = df[mask]", {"kept_rows": len(out), "removed_rows": len(df) - len(out)}


def _op_clip_outliers(df, p) -> tuple[pd.DataFrame, str, dict]:
    col, how = p.get("column"), p.get("method", "iqr")
    _check_cols(df, [col], "异常值处理")
    num = pd.to_numeric(df[col], errors="coerce")
    if how == "iqr":
        q1, q3 = num.quantile(0.25), num.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        code = (f"q1, q3 = df['{col}'].quantile([.25, .75])\n"
                f"iqr = q3 - q1\n"
                f"df['{col}'] = df['{col}'].clip(q1 - 1.5*iqr, q3 + 1.5*iqr)")
    elif how == "p99":
        lo, hi = num.quantile(0.01), num.quantile(0.99)
        code = f"df['{col}'] = df['{col}'].clip(df['{col}'].quantile(.01), df['{col}'].quantile(.99))"
    else:
        raise ValueError(f"不支持的方法: {how}")
    new = df.copy()
    hits = int(((num < lo) | (num > hi)).sum())
    new[col] = num.clip(lo, hi)
    return new, code, {"clipped": hits, "bounds": [float(lo), float(hi)]}


def _op_lowercase(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = _resolve_text_cols(df, p.get("columns"), "大小写转换")
    upper = bool(p.get("upper"))
    new = df.copy()
    for c in cols:
        new[c] = new[c].map(lambda v: v.upper() if upper else v.lower() if isinstance(v, str) else v)
    fn = "upper" if upper else "lower"
    code = ("# 只对该列为字符串的行生效，非字符串原样保留\n"
            "for c in " + _list_q(cols) + ":\n"
            f"    df[c] = df[c].map(lambda v: v.{fn}() if isinstance(v, str) else v)")
    return new, code, {"cols": len(cols)}


def _op_sort(df, p) -> tuple[pd.DataFrame, str, dict]:
    cols = list(p.get("columns") or [])
    _check_cols(df, cols, "排序")
    asc = p.get("ascending", True)
    out = df.sort_values(cols, ascending=asc, na_position="last")
    return out, f"df = df.sort_values({_list_q(cols)}, ascending={asc})", {"sorted": True}


def _op_reset_index(df, p) -> tuple[pd.DataFrame, str, dict]:
    out = df.reset_index(drop=True)
    return out, "df = df.reset_index(drop=True)", {}


def _op_auto_convert_dates(df, p) -> tuple[pd.DataFrame, str, dict]:
    """扫描并转换「存成字符串的日期列」。

    CSV / Excel 读进来的日期经常是 object 列，做不了时间序列分析。
    这里会判断：不仅要求 to_datetime 成功率高，还要求字符串里确实
    有日期分隔符 —— 否则 '20250101' 这种纯数字 ID 会被误伤。
    """
    threshold = float(p.get("threshold") or 0.9)
    threshold = min(max(threshold, 0.5), 1.0)
    fmt = (p.get("fmt") or "").strip() or None
    cols = list(p.get("columns") or [])
    if not cols:
        cols = [str(c) for c in df.columns
                if not pd.api.types.is_numeric_dtype(df[c])
                and not pd.api.types.is_datetime64_any_dtype(df[c])]

    out = df.copy()
    converted: list[str] = []
    tried = 0
    for c in cols:
        s = out[c]
        non_null = s.dropna()
        if non_null.empty:
            continue
        sample = non_null.head(500)
        tried += 1
        with warnings.catch_warnings():
            # 不指定 format 时 pandas 会对每个元素单独解析，会刷一堆 UserWarning
            warnings.simplefilter("ignore", UserWarning)
            try:
                parsed = pd.to_datetime(sample, errors="coerce", format=fmt)
            except Exception:
                continue
        hit = float(parsed.notna().mean())
        # 数字/日期分隔符的占比，用来排除纯数字 ID 列
        looks = float(sample.astype(str).str.contains(r"[-/:年月.]", regex=True).mean())
        if hit >= threshold and looks >= 0.8:
            out[c] = pd.to_datetime(out[c], errors="coerce", format=fmt)
            converted.append(str(c))

    if not converted:
        raise ValueError(f"扫描了 {tried} 列，没有发现可以安全转为日期的字符串列"
                         f"（可调低阈值，或手动用「转换类型」指定某一列）")
    code = [
        "# 自动把字符串日期列转成 datetime",
        "for c in " + repr(converted) + ":",
        "    df[c] = pd.to_datetime(df[c], errors='coerce'"
        + (f", format={fmt!r}" if fmt else "") + ")",
    ]
    return out, "\n".join(code), {"converted": converted, "scanned": tried}


def _op_drop_constant(df, p) -> tuple[pd.DataFrame, str, dict]:
    """删除常量列 / 全空列 —— 建模前最该先做的一步。"""
    also_null = bool(p.get("drop_all_null", True))
    keep_cols = list(p.get("keep") or [])
    victims = []
    for c in df.columns:
        c = str(c)
        if c in keep_cols:
            continue
        s = df[c]
        if also_null and s.isna().all():
            victims.append(c)
        elif s.nunique(dropna=True) <= 1:
            victims.append(c)
    if not victims:
        return df, "# 没有发现常量列或全空列", {"removed": [], "kept": int(df.shape[1])}
    out = df.drop(columns=victims)
    return out, f"df = df.drop(columns={victims!r})", {
        "removed": victims, "kept": int(out.shape[1]), "removed_count": len(victims),
    }


def _op_clean_numeric(df, p) -> tuple[pd.DataFrame, str, dict]:
    """把带货币符号 / 千分位 / 百分号的文本列转成真正的数值。

    真实数据里 '¥1,234.50'、'12.3%'、'(888)'（会计负数）都是文本，
    不处理的话整列只能当字符串看，聚合全废。
    """
    cols = list(p.get("columns") or [])
    _check_cols(df, cols, "数值清洗")
    if not cols:
        raise ValueError("请至少选择一列")
    percent = bool(p.get("percent"))
    out = df.copy()

    def clean(v):
        if pd.isna(v):
            return np.nan
        if isinstance(v, (int, float, np.number)):
            return float(v)
        s = str(v).strip()
        if not s:
            return np.nan
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        body = re.sub(r"[^\d.\-+eE]", "", s)
        if body in ("", "-", "+", ".", "-.", "+."):
            return np.nan
        try:
            num = float(body)
        except ValueError:
            return np.nan
        if percent:
            num /= 100.0
        return -num if neg else num

    report = []
    for c in cols:
        before_null = int(out[c].isna().sum())
        conv = out[c].map(clean)
        bad_after_other = int(conv.isna().sum())
        out[c] = conv
        report.append(str(c))

    code = [
        "# 剥离货币符号 / 千分位 / 百分号，转成数值",
        "def _to_num(v):",
        "    if pd.isna(v): return np.nan",
        "    s = str(v).strip()",
        "    neg = s.startswith('(') and s.endswith(')')",
        "    if neg: s = s[1:-1]",
        "    s = re.sub(r'[^\\d.\\-+eE]', '', s)" + (" or '0'" if False else ""),
        "    try: n = float(s)",
        "    except ValueError: return np.nan",
        ("    return -n / 100.0 if neg else n / 100.0" if percent else
         "    return -n if neg else n"),
        "",
    ]
    for c in cols:
        code.append(f"df['{c}'] = df['{c}'].map(_to_num)")
    return out, "\n".join(code), {
        "converted_cols": report,
        "percent_applied": percent,
        "remaining_null": int(sum(out[c].isna().sum() for c in cols)),
    }


# 数字提取：默认允许小数，decimals=False 时只认整数
_NUM_RE = r"[-+]?\d+(?:\.\d+)?"
_INT_RE = r"[-+]?\d+"


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _do_extract(df: pd.DataFrame, p: dict, pattern: str,
                default_suffix: str, kind_label: str) -> tuple[pd.DataFrame, str, dict]:
    """提取算子的公共实现：按正则从文本里抠片段，再按 mode 组装成新列。

    mode:
      first     只取首个（'12㎡' -> '12'）
      last      只取末个
      all_join  全部匹配用 sep 连成一列（'12组5个' -> '12,5'）
      all_split 全部匹配拆成多列（'12组5个' -> 12 / 5 两列）
      max/min/sum/count  把匹配到的数字做聚合
    """
    col = p.get("column")
    _check_cols(df, [col], kind_label)
    mode = p.get("mode") or "first"
    sep = p.get("sep")
    sep = "," if sep is None else str(sep)
    keep_original = bool(p.get("keep_original", True))
    want = str(p.get("new_name") or "").strip()

    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"正则表达式不合法：{pattern!r}（{e}）") from e

    def find_parts(text):
        """抠出单元格里的所有匹配。

        统一返回 list[list[str]]：外层是每个匹配，内层是该匹配的各个捕获组。
        坑：str.findall 在正则带捕获组时返回的是 tuple 列表而不是字符串列表，
        join 会直接 TypeError。这里用 finditer 自己控制，就不会踩。
        """
        if not isinstance(text, str):
            return []
        out = []
        for m in rx.finditer(text):
            groups = m.groups()
            if groups:
                out.append(["" if g is None else str(g) for g in groups])
            else:
                out.append([m.group(0)])
        return out

    s = df[col].astype("string")
    found = s.map(find_parts)                                   # list[list[str]]
    flat = found.map(lambda v: [x for parts in v for x in parts])  # 展平后用于连接/拆分

    hits = flat.map(len)
    matched_rows = int((hits > 0).sum())
    total = int(len(df))
    out = df.copy()
    effect: dict = {"matched_rows": matched_rows, "total_rows": total}
    if any(len(parts) > 1 for v in found for parts in v):
        effect["note"] = f"正则含捕获组，同一处的多段值用 {sep!r} 连接"

    if mode in ("first", "last"):
        nm = _unique_name(want or f"{col}{default_suffix}", set(out.columns))

        def pick(v, want_last):
            if not v:
                return None
            return sep.join(v[-1] if want_last else v[0])

        out[nm] = found.map(lambda v: pick(v, mode == "last"))
        code = (f"parts = df['{col}'].astype('string').map(\n"
                f"    lambda t: [list(m.groups()) or [m.group(0)]\n"
                f"               for m in re.finditer(r'{pattern}', t)] if isinstance(t, str) else []\n"
                f")\n"
                f"df['{nm}'] = parts.map(\n"
                f"    lambda v: {sep!r}.join(v[{0 if mode == 'first' else -1}]) if v else None)"
                )
        effect["new_column"] = nm
        effect["mode"] = mode

    elif mode == "all_join":
        nm = _unique_name(want or f"{col}{default_suffix}", set(out.columns))
        out[nm] = flat.map(lambda v: sep.join(v) if v else None)
        code = (f"df['{nm}'] = df['{col}'].astype('string').map(\n"
                f"    lambda t: [g for m in re.finditer(r'{pattern}', t)\n"
                f"                  for g in (m.groups() or (m.group(0),))] if isinstance(t, str) else []\n"
                f").map(lambda v: {sep!r}.join(v) if v else None)")
        effect["new_column"] = nm
        effect["mode"] = f"全部匹配用 {sep!r} 连成一列"

    elif mode == "all_split":
        base = want or col
        taken = set(out.columns)
        width = min(int(hits.max()) if len(hits) else 0, 30)   # 防止一列几十个数字炸出几十列
        new_cols = []
        for i in range(width):
            nm = _unique_name(f"{base}_{i + 1}", taken)
            taken.add(nm)
            out[nm] = flat.map(lambda v, i=i: v[i] if len(v) > i else None)
            new_cols.append(nm)
        if not new_cols:
            raise ValueError(f"列 {col} 里没有匹配到任何内容")
        code = (f"parts = df['{col}'].astype('string').map(\n"
                f"    lambda t: [g for m in re.finditer(r'{pattern}', t)\n"
                f"                  for g in (m.groups() or (m.group(0),))] if isinstance(t, str) else []\n"
                f")\n"
                f"df[{new_cols!r}] = pd.DataFrame(\n"
                f"    parts.map(lambda v: v + [None] * ({len(new_cols)} - len(v))).tolist(),\n"
                f"    index=df.index\n"
                f")")
        effect["new_columns"] = new_cols
        effect["max_parts"] = width

    elif mode in ("max", "min", "sum", "count"):
        nm = _unique_name(want or f"{col}{default_suffix}", set(out.columns))
        if mode == "count":
            out[nm] = hits
            code = (f"df['{nm}'] = df['{col}'].astype('string').map(\n"
                    f"    lambda t: len(re.findall(r'{pattern}', t)) if isinstance(t, str) else None\n"
                    f")")
        else:
            fn = {"max": max, "min": min, "sum": sum}[mode]
            nums = flat.map(lambda v: [x for x in (_to_float(y) for y in v) if x is not None])
            out[nm] = nums.map(lambda v: float(fn(v)) if v else None)
            code = (f"nums = df['{col}'].astype('string').map(\n"
                    f"    lambda t: [float(x) for x in re.findall(r'{pattern}', t)]\n"
                    f"              if isinstance(t, str) else []\n"
                    f")\n"
                    f"df['{nm}'] = nums.map(lambda v: {mode}(v) if v else None)")
        effect["new_column"] = nm
        effect["mode"] = mode

    else:
        raise ValueError(f"不支持的提取方式: {mode}")

    if not keep_original and col not in (effect.get("new_column"),):
        out = out.drop(columns=[col])
        effect["dropped_original"] = str(col)
        code += f"\ndf = df.drop(columns=['{col}'])"

    unmatched = total - matched_rows
    if unmatched:
        effect["unmatched_rows"] = unmatched
    return out, code, effect


def _op_extract_number(df, p) -> tuple[pd.DataFrame, str, dict]:
    """从文本里提取数字。

    '12㎡' -> 12；'12组5个' -> 12,5；'¥1,234.5元/件' -> 1234.5
    和「文本数值清洗」的区别：那个是整串本来就是数字、只带符号；
    这个是数字混在文字里，需要有东西可抠。
    """
    decimals = bool(p.get("decimals", True))
    return _do_extract(df, p, _NUM_RE if decimals else _INT_RE,
                       "_num", "提取数字")


def _op_extract_pattern(df, p) -> tuple[pd.DataFrame, str, dict]:
    """按自定义正则提取 —— 进阶用法。

    比如提取括号里的内容：r'\\((.*?)\\)'
    或者提取所有中文：r'[\\u4e00-\\u9fff]+'
    """
    pattern = str(p.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("必须填写正则表达式")
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"正则表达式不合法：{e}") from e
    return _do_extract(df, p, pattern, "_ext", "正则提取")


def _unique_name(base: str, taken: set) -> str:
    """新列名撞车时自动加后缀，避免默默覆盖掉已有列。"""
    if base not in taken:
        return base
    k = 2
    while f"{base}_{k}" in taken:
        k += 1
    return f"{base}_{k}"


def _op_split_column(df, p) -> tuple[pd.DataFrame, str, dict]:
    """按分隔符把一列拆成多列。

    典型场景：'北京市/朝阳区' -> 市 + 区；'2025-01-01' -> 年 + 月 + 日。
    坑有三个，这里都处理了：
      1. str.split 的 pat 长度 >1 时必须显式给 regex=False，否则 pandas 会警告甚至当正则读
      2. concat 前必须 reset_index，否则索引对不上会炸出一堆 NaN
      3. 新列名撞上已有列名会覆盖原列，所以统一走 _unique_name
    """
    col = p.get("column")
    _check_cols(df, [col], "拆分列")
    sep = p.get("sep")
    if sep is None or sep == "":
        raise ValueError("分隔符不能为空")
    names = [x.strip() for x in str(p.get("names") or "").split(",") if x.strip()]
    limit = int(p.get("n") or 0)          # 0 / 负数 = 不限制拆分份数
    drop = bool(p.get("drop"))
    regex = bool(p.get("regex"))
    trim = p.get("trim", True)

    n = (limit if limit and limit > 0 else -1)
    # 非字符串列没有 .str accessor，先转成 string 再拆
    try:
        parts = df[col].astype("string").str.split(sep, n=n, expand=True, regex=regex)
    except Exception as e:
        raise ValueError(f"拆分失败（检查分隔符或是否该勾选正则）：{e}") from e
    if parts is None or parts.shape[1] == 0:
        raise ValueError(f"没有拆分出任何片段，请确认分隔符 {sep!r} 在列 {col!r} 里存在")

    n_new = parts.shape[1]
    taken = set(df.columns)
    new_names: list[str] = []
    for i in range(n_new):
        want = names[i] if i < len(names) else f"{col}_{i + 1}"
        nm = _unique_name(str(want), taken)
        taken.add(nm)
        new_names.append(nm)
    parts.columns = new_names
    if trim:
        for c in new_names:
            parts[c] = parts[c].str.strip() if hasattr(parts[c], "str") else parts[c]

    out = pd.concat([df.reset_index(drop=True), parts.reset_index(drop=True)], axis=1)
    if drop:
        out = out.drop(columns=[col])

    hit = int(df[col].notna().sum())
    code = [
        f"# 把 {col} 按 {sep!r} 拆开",
        f"parts = df['{col}'].str.split({sep!r}, n={n}, expand=True, regex={regex})",
        f"parts.columns = {new_names!r}",
    ]
    if trim:
        code.append("parts = parts.apply(lambda s: s.str.strip())")
    code.append("df = pd.concat([df.reset_index(drop=True), parts.reset_index(drop=True)], axis=1)")
    if drop:
        code.append(f"df = df.drop(columns=['{col}'])")
    return out, "\n".join(code), {
        "new_columns": new_names,
        "parts": n_new,
        "source_rows": hit,
        "renamed": [n for n in new_names],
    }


def _op_concat_columns(df, p) -> tuple[pd.DataFrame, str, dict]:
    """多列合并成一列 —— split 的反向操作。"""
    cols = list(p.get("columns") or [])
    _check_cols(df, cols, "合并列")
    if len(cols) < 2:
        raise ValueError("请至少选择 2 列来合并")
    sep = p.get("sep") or ""
    want = str(p.get("new_name") or "").strip() or "_".join(cols)
    nm = _unique_name(want, set(df.columns))

    out = df.copy()
    # StringDtype 下 pd.NA 参与拼接会让整段变 NA，先填成空串
    combined = None
    for c in cols:
        piece = out[c].astype("string").where(out[c].notna(), "")
        combined = piece if combined is None else combined + sep + piece
    out[nm] = combined

    code = (f"df[{cols!r}] = df[{cols!r}].astype('string').fillna('')\n"
            f"df['{nm}'] = df[{cols!r}].astype(str).agg({sep!r}.join, axis=1)")
    return out, code, {"new_column": nm, "merged_cols": len(cols)}


def _op_group_agg(df, p) -> tuple[pd.DataFrame, str, dict]:
    """把明细直接聚合 —— 省得为了看汇总再去写 SQL。"""
    by = list(p.get("by") or [])
    metrics = list(p.get("metrics") or [])   # [{column, agg}]
    _check_cols(df, by, "分组聚合")
    if not by or not metrics:
        raise ValueError("需要至少 1 个分组列和 1 个聚合指标")
    agg_map = {}
    for m in metrics:
        _check_cols(df, [m["column"]], "聚合")
        if m["agg"] not in AGG_FUNCS:
            raise ValueError(f"不支持的聚合方式: {m['agg']}")
        agg_map[m["column"]] = m["agg"]
    out = df.groupby(by, as_index=False, observed=True).agg(**{f"{c}_{a}": pd.NamedAgg(column=c, aggfunc=a)
                                                              for c, a in agg_map.items()})
    names = ", ".join(f"{c}_{a}" for c, a in agg_map.items())
    code = (f"df = df.groupby({_list_q(by)}, as_index=False).agg(\n"
            f"    {names}=pd.NamedAgg(column='{list(agg_map)[0]}', aggfunc='{list(agg_map.values())[0]}')\n"
            f")")
    return out, code, {"grouped_rows": len(out), "groups": len(out)}


OPS = {
    "drop_duplicates": _op_drop_duplicates,
    "drop_columns": _op_drop_columns,
    "keep_columns": _op_keep_columns,
    "rename": _op_rename,
    "astype": _op_astype,
    "fillna": _op_fillna,
    "dropna": _op_dropna,
    "strip": _op_strip,
    "replace": _op_replace,
    "filter": _op_filter,
    "clip_outliers": _op_clip_outliers,
    "lowercase": _op_lowercase,
    "sort": _op_sort,
    "reset_index": _op_reset_index,
    "group_agg": _op_group_agg,
    "split_column": _op_split_column,
    "concat_columns": _op_concat_columns,
    "auto_convert_dates": _op_auto_convert_dates,
    "drop_constant": _op_drop_constant,
    "clean_numeric": _op_clean_numeric,
    "extract_number": _op_extract_number,
    "extract_pattern": _op_extract_pattern,
}

COL_MODE_NONE = "none"              # 不需要选列（重置索引、分组聚合）
COL_MODE_SINGLE = "single"          # 必须选且只能选一列（填充缺失、类型转换、行过滤…）
COL_MODE_MULTI = "multi"            # 必须选，可多选（删除列、去空格…）
COL_MODE_OPTIONAL = "multi_optional"  # 可多选，也可以一列都不选（= 作用于全表）

# 算子分组：前端按这个顺序分块展示，用户按「我现在要解决什么」去找
GROUP_MISSING = "空值与异常"
GROUP_TYPE = "类型转换"
GROUP_TEXT = "文本处理"
GROUP_ROW = "行操作"
GROUP_COL = "列与汇总"
GROUP_ORDER = [GROUP_MISSING, GROUP_TYPE, GROUP_TEXT, GROUP_ROW, GROUP_COL]

OP_META = [
    # ---------------------------------------------------------------- 补空缺
    {"op": "fillna", "label": "填充空值", "group": GROUP_MISSING, "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "strategy", "type": "select", "label": "填充策略", "options": [
         "median", "mean", "mode", "group_mode", "zero", "constant", "ffill", "bfill", "interpolate"],
         "default": "median",
         "labels": {"median": "中位数",
                    "mean": "平均值",
                    "mode": "众数",
                    "group_mode": "按分组众数",
                    "zero": "0",
                    "constant": "固定值",
                    "ffill": "向前填充",
                    "bfill": "向后填充",
                    "interpolate": "线性插值"}},
         {"key": "value", "type": "text", "label": "填充值", "default": "",
          "placeholder": "选了「补一个固定值」时填这里，如 未知"},
         {"key": "by", "type": "text", "label": "按哪列分组", "default": "",
          "placeholder": "选了「按分组里的众数补」时填，如 city"}],
     "desc": "按中位数、众数等策略填充空值",
     "when": "空值占比较小、删除会损失样本量时使用。数值列建议中位数，类别列建议众数。",
     "example": {"before": "user_age 有 120 行为空", "after": "空值已按中位数填充"}},

    {"op": "dropna", "label": "删除含空值的行", "group": GROUP_MISSING, "col_mode": COL_MODE_OPTIONAL,
     "extra": [{"key": "how", "type": "select", "label": "判定条件", "options": ["any", "all"],
                "default": "any",
                "labels": {"any": "任一列为空", "all": "整行全空"}}],
     "desc": "删除含空值的记录",
     "when": "空值占比较小且不影响整体结论时使用。",
     "example": {"before": "3045 行，其中 200 行有空值", "after": "2845 行，没有空值了"}},

    {"op": "clip_outliers", "label": "异常值处理", "group": GROUP_MISSING,
     "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "method", "type": "select", "label": "方法", "options": ["iqr", "p99"],
                "default": "iqr",
                "labels": {"iqr": "IQR 四分位",
                           "p99": "1/99 分位"}}],
     "desc": "按 IQR 或分位数对极端值缩尾",
     "when": "存在明显超出合理范围的数值时使用，相比删除记录能保留样本量。",
     "example": {"before": "年龄有 1 个 999", "after": "极端值已缩尾至上限"}},

    {"op": "drop_constant", "label": "删除常量列", "group": GROUP_MISSING,
     "col_mode": COL_MODE_NONE,
     "extra": [{"key": "drop_all_null", "type": "bool", "default": True,
                "label": "同时删除全空列"}],
     "desc": "删除取值唯一或全空的列",
     "when": "分析或建模前的常规清理步骤。",
     "example": {"before": "20 列，其中 source_system 整列都是 'APP'",
                 "after": "19 列"}},

    # ---------------------------------------------------------------- 类型格式
    {"op": "astype", "label": "更改数据类型", "group": GROUP_TYPE, "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "target", "type": "select", "label": "目标类型",
                "options": ["float", "int", "str", "datetime", "category", "bool"], "default": "float",
                "labels": {"float": "数值", "int": "整数",
                           "str": "文本", "datetime": "日期时间",
                           "category": "分类",
                           "bool": "布尔"}}],
     "desc": "转换列的数据类型",
     "when": "数值或日期以文本形式存储时，需先转换才能参与计算与排序。",
     "example": {"before": "金额是文本 '1234.5'", "after": "数字 1234.5，可以求和了"}},

    {"op": "auto_convert_dates", "label": "识别并转换日期", "group": GROUP_TYPE,
     "col_mode": COL_MODE_OPTIONAL,
     "extra": [{"key": "threshold", "type": "text", "label": "识别率阈值", "default": "0.9",
                "placeholder": "0.5~1，越高越保守，默认 0.9"},
               {"key": "fmt", "type": "text", "label": "日期格式", "default": "",
                "placeholder": "如 %Y/%m/%d，留空自动推断"}],
     "desc": "将日期文本转换为 datetime",
     "when": "日期以文本形式存储时使用，转换后方可进行时间维度分析。",
     "example": {"before": "'2025/01/15'（文本）", "after": "2025-01-15（真日期，能按月份汇总）"}},

    {"op": "clean_numeric", "label": "数值清洗", "group": GROUP_TYPE,
     "col_mode": COL_MODE_MULTI,
     "extra": [{"key": "percent", "type": "bool", "default": False,
                "label": "百分号转为小数"}],
     "desc": "清除货币符号、千分位、括号负数并转为数值",
     "when": "数值带格式符号导致无法计算时使用；数值与文本混排请改用「提取数字」。",
     "example": {"before": "'¥1,234.50'", "after": "1234.5"}},

    # ---------------------------------------------------------------- 文字处理
    {"op": "strip", "label": "修整", "group": GROUP_TEXT, "col_mode": COL_MODE_OPTIONAL,
     "desc": "清除文本首尾的空白字符",
     "when": "同一取值因首尾空格被识别为不同类别时使用。",
     "example": {"before": "' 北京 '", "after": "'北京'"}},

    {"op": "replace", "label": "替换值", "group": GROUP_TEXT,
     "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "pairs", "type": "pairs", "label": "替换规则", "default": []},
               {"key": "mode", "type": "select", "label": "匹配方式",
                "options": ["substring", "exact"], "default": "substring",
                "labels": {"substring": "子串替换",
                           "exact": "整值替换"}},
               {"key": "regex", "type": "bool", "default": False,
                "label": "按正则解析"}],
     "desc": "按子串或整值替换文本内容",
     "when": "统一同义表述或清除固定文本时使用，子串模式下无需通配符。",
     "example": {"before": "'12㎡'", "after": "左边填「㎡」、右边留空 → '12'"}},

    {"op": "extract_number", "label": "提取数字", "group": GROUP_TEXT,
     "col_mode": COL_MODE_SINGLE,
     "extra": [
         {"key": "mode", "type": "select", "label": "提取方式", "default": "first",
          "options": ["first", "last", "all_join", "all_split", "sum", "max", "min", "count"],
          "labels": {"first": "首个数值",
                     "last": "末个数值",
                     "all_join": "全部合并",
                     "all_split": "全部拆列",
                     "sum": "求和", "max": "最大值", "min": "最小值",
                     "count": "计数"}},
         {"key": "sep", "type": "text", "label": "连接符", "default": ",",
          "placeholder": "选了「连成一列」时，数字之间用什么隔开"},
         {"key": "decimals", "type": "bool", "default": True, "label": "保留小数"},
         {"key": "new_name", "type": "text", "label": "新列名", "default": "",
          "placeholder": "留空自动生成，如 面积_num"},
         {"key": "keep_original", "type": "bool", "default": True, "label": "保留原列"}],
     "desc": "从混合文本中提取数值",
     "when": "适用于面积、规格、时长等数值与单位混排的字段。",
     "example": {"before": "'12㎡'、'12组5个'", "after": "'12'、'12,5'"}},

    {"op": "extract_pattern", "label": "按规则提取", "group": GROUP_TEXT,
     "col_mode": COL_MODE_SINGLE,
     "extra": [
         {"key": "pattern", "type": "text", "label": "匹配规则（正则表达式）", "default": "",
          "placeholder": r"例：\((.*?)\) 表示抠出括号里的内容"},
         {"key": "mode", "type": "select", "label": "提取方式", "default": "first",
          "options": ["first", "last", "all_join", "all_split"],
          "labels": {"first": "首个", "last": "末个",
                     "all_join": "全部合并", "all_split": "全部拆列"}},
         {"key": "sep", "type": "text", "label": "连接符", "default": "|"},
         {"key": "new_name", "type": "text", "label": "新列名", "default": ""},
         {"key": "keep_original", "type": "bool", "default": True, "label": "保留原列"}],
     "desc": "按正则表达式提取内容",
     "when": "需提取的内容不符合数字模式时，自定义正则提取。"
             r"规则写 \((.*?)\)。不懂正则的话建议先用上面那个算子。",
     "example": {"before": "'张三(北京)'", "after": r"规则 \((.*?)\) → 抠出 '北京'"}},

    {"op": "split_column", "label": "按分隔符拆分列", "group": GROUP_TEXT, "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "sep", "type": "text", "label": "分隔符", "default": ",",
                "placeholder": "常见的有 , - | / 和空格"},
               {"key": "names", "type": "text", "label": "新列名", "default": "",
                "placeholder": "英文逗号分隔，如 省,市；留空自动命名"},
               {"key": "n", "type": "number", "label": "最大拆分数", "default": 0,
                "placeholder": "0 表示不限"},
               {"key": "drop", "type": "bool", "default": False, "label": "拆分后删除原列"},
               {"key": "regex", "type": "bool", "default": False, "label": "分隔符按正则解析"}],
     "desc": "按分隔符拆分为多列",
     "when": "适用于「省-市-区」这类单字段包含多段信息的情况。",
     "example": {"before": "'北京/朝阳区'", "after": "拆成「北京」和「朝阳区」两列"}},

    {"op": "concat_columns", "label": "合并列", "group": GROUP_TEXT, "col_mode": COL_MODE_MULTI,
     "extra": [{"key": "sep", "type": "text", "label": "连接符", "default": "",
                "placeholder": "留空则直接接在一起，如 - 或空格"},
               {"key": "new_name", "type": "text", "label": "新列名", "default": "",
                "placeholder": "留空自动生成"}],
     "desc": "将多列拼接为一列",
     "when": "需要构造复合键或合并展示字段时使用。",
     "example": {"before": "『北京』+『朝阳』", "after": "'北京-朝阳'"}},

    {"op": "lowercase", "label": "大小写转换", "group": GROUP_TEXT, "col_mode": COL_MODE_OPTIONAL,
     "extra": [{"key": "upper", "type": "bool", "default": False, "label": "转为大写"}],
     "desc": "统一英文字符大小写",
     "when": "分组结果受英文大小写影响时使用。",
     "example": {"before": "'Apple'、'APPLE'", "after": "'apple'"}},

    # ---------------------------------------------------------------- 挑行排序
    {"op": "filter", "label": "筛选行", "group": GROUP_ROW, "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "operator", "type": "select", "label": "判断条件", "options": [
         "=", "!=", ">", ">=", "<", "<=", "between", "contains", "startswith", "in",
         "is_null", "not_null"], "default": "=",
         "labels": {"=": "等于", "!=": "不等于", ">": "大于", ">=": "大于等于",
                    "<": "小于", "<=": "小于等于", "between": "介于",
                    "contains": "包含", "startswith": "开头为",
                    "in": "属于", "is_null": "是空的", "not_null": "不是空的"}},
         {"key": "value", "type": "text", "label": "值", "default": "",
          "placeholder": "选「在列表里」时用逗号分隔"},
         {"key": "value2", "type": "text", "label": "上界", "default": "",
          "placeholder": "选「介于」时填第二个值"}],
     "desc": "按条件筛选记录",
     "when": "分析仅针对特定子集时使用，建议后续执行「重置索引」。",
     "example": {"before": "全部 3045 行", "after": "城市 = 北京 → 剩 620 行"}},

    {"op": "drop_duplicates", "label": "删除重复项", "group": GROUP_ROW,
     "col_mode": COL_MODE_OPTIONAL,
     "extra": [{"key": "keep", "type": "select", "label": "保留", "options": ["first", "last"],
                "default": "first", "labels": {"first": "保留首条", "last": "保留末条"}}],
     "desc": "删除重复记录",
     "when": "存在重复录入或多次导入合并时使用，注意判重列的选择。",
     "example": {"before": "3045 行", "after": "按订单号去重 → 3000 行"}},

    {"op": "sort", "label": "排序", "group": GROUP_ROW, "col_mode": COL_MODE_MULTI,
     "extra": [{"key": "ascending", "type": "bool", "default": False,
                "label": "升序"}],
     "desc": "按指定列排序",
     "when": "查看极值或按时间顺序浏览时使用。",
     "example": {"before": "乱序", "after": "按金额从大到小"}},

    {"op": "reset_index", "label": "重置索引", "group": GROUP_ROW, "col_mode": COL_MODE_NONE,
     "desc": "重建连续索引",
     "when": "筛选或删除行后重建连续索引，导出前建议执行。",
     "example": {"before": "行号 0, 3, 7, 12…", "after": "行号 0, 1, 2, 3…"}},

    # ---------------------------------------------------------------- 增删列汇总
    {"op": "drop_columns", "label": "删除列", "group": GROUP_COL, "col_mode": COL_MODE_MULTI,
     "desc": "删除指定列",
     "when": "移除与分析无关的字段，删除后需重新载入才能恢复。",
     "example": {"before": "20 列", "after": "删掉 3 个无关列 → 17 列"}},

    {"op": "keep_columns", "label": "保留选中列", "group": GROUP_COL, "col_mode": COL_MODE_MULTI,
     "desc": "仅保留指定列",
     "when": "仅使用少量字段时，比逐列删除更高效。",
     "example": {"before": "20 列", "after": "只留 4 列"}},

    {"op": "rename", "label": "重命名列", "group": GROUP_COL, "col_mode": COL_MODE_SINGLE,
     "extra": [{"key": "new_name", "type": "text", "label": "新列名", "default": "",
                "placeholder": "如 order_amount"}],
     "desc": "修改列名",
     "when": "列名需规范化或与关联表保持一致时使用。",
     "example": {"before": "'订单金额'", "after": "'order_amount'"}},

    {"op": "group_agg", "label": "分组汇总", "group": GROUP_COL, "col_mode": COL_MODE_NONE,
     "extra": [{"key": "by", "type": "columns", "label": "分组列", "default": [],
                "placeholder": "多个列名用英文逗号分隔，如 city, channel"},
               {"key": "metrics", "type": "metrics", "label": "聚合指标", "default": [],
                "placeholder": "每行一条：列名,统计方式\n如 amount,sum"}],
     "desc": "按维度分组聚合统计",
     "when": "明细数据量较大时使用，聚合方式支持 sum / mean / median / count / nunique / max / min。",
     "example": {"before": "3045 行明细", "after": "按城市汇总 → 9 行"}},
]


def filter_df(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """只做行筛选，返回筛选后的 DataFrame。

    给「数据明细」面板的交互式筛选复用 —— 和清洗算子里的「筛选行」
    用的是同一套比较逻辑，避免两处行为不一致。
    """
    return _op_filter(df, params)[0]


def sort_df(df: pd.DataFrame, column: str, ascending: bool = True) -> pd.DataFrame:
    """按列排序。空值统一排在末尾，不然升序时空值会霸占前面。"""
    if not column or column not in df.columns:
        return df
    return df.sort_values(by=column, ascending=ascending,
                          na_position="last", kind="stable")


def op_meta_grouped() -> list[dict]:
    """把算子按分组整理好给前端，省得前端硬编码顺序。"""
    out = []
    for g in GROUP_ORDER:
        items = [m for m in OP_META if m.get("group") == g]
        if items:
            out.append({"group": g, "ops": items})
    # 没有分组字段的兜底放最后，避免漏显示
    rest = [m for m in OP_META if not m.get("group")]
    if rest:
        out.append({"group": "其它", "ops": rest})
    return out


def apply_ops(df: pd.DataFrame, ops: list[dict]) -> dict:
    """顺序执行一串清洗算子。

    返回 {df, code, steps, before, after}
    """
    work = df
    code_lines = ["# -*- coding: utf-8 -*-", "import re", "import pandas as pd",
                  "import numpy as np", "", "df = pd.read_csv('你的数据源.csv')", ""]
    steps = []
    before = {"rows": int(len(df)), "cols": int(df.shape[1])}

    for i, item in enumerate(ops, 1):
        op_name = item.get("op")
        params = item.get("params") or {}
        if op_name not in OPS:
            raise ValueError(f"未知算子: {op_name}")

        # 列选择的前置校验：报错要说清楚该选几列，别等到 pandas 抛 KeyError
        meta = next((m for m in OP_META if m["op"] == op_name), {})
        label = meta.get("label", op_name)
        mode = meta.get("col_mode", COL_MODE_NONE)
        if mode == COL_MODE_SINGLE and not params.get("column"):
            raise ValueError(f"第 {i} 步 [{label}] 需要先指定作用列（一次只能选一列）")
        if mode == COL_MODE_MULTI and not params.get("columns"):
            raise ValueError(f"第 {i} 步 [{label}] 需要至少选择一列（可多选）")

        rows_before = len(work)
        try:
            work, code, effect = OPS[op_name](work, params)
        except Exception as e:
            raise ValueError(f"第 {i} 步 [{op_name}] 执行失败: {e}") from e
        rows_after = len(work)
        steps.append({
            "index": i, "op": op_name,
            "label": next((m["label"] for m in OP_META if m["op"] == op_name), op_name),
            "params": params, "effect": effect,
            "row_delta": rows_after - rows_before,
            "rows": int(rows_after), "cols": int(work.shape[1]),
        })
        code_lines.append(f"# --- step {i}: {op_name} ---")
        code_lines.append(code)
        code_lines.append("")

    return {
        "df": work,
        "code": "\n".join(code_lines),
        "steps": steps,
        "before": before,
        "after": {"rows": int(len(work)), "cols": int(work.shape[1])},
    }
