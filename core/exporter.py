# -*- coding: utf-8 -*-
"""导出清洗后的结果。CSV / Excel / JSON。"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import EXPORT_DIR


def export_path(dataset: str, fmt: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORT_DIR / f"{dataset}_{ts}.{fmt}"


def to_bytes(df: pd.DataFrame, fmt: str) -> tuple[bytes, str, str]:
    fmt = (fmt or "csv").lower()
    if fmt == "csv":
        buf = io.StringIO()
        # utf-8-sig：Excel 直接双击打开不乱码，国内同事不会用你是否 utf-8 那套
        df.to_csv(buf, index=False, encoding="utf-8-sig")
        data = buf.getvalue().encode("utf-8-sig")
        return data, "text/csv; charset=utf-8", "csv"
    if fmt == "xlsx":
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise RuntimeError("导出 Excel 需要 openpyxl：pip install openpyxl") from e
        buf = io.BytesIO()
        df.to_excel(buf, index=False, engine="openpyxl")
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    if fmt == "json":
        data = df.to_json(orient="records", force_ascii=False, date_format="iso").encode("utf-8")
        return data, "application/json; charset=utf-8", "json"
    if fmt == "md":
        # 不依赖 tabulate（pandas 的 to_markdown 需要它），这里手拼 Markdown 表格
        parts = ["| " + " | ".join(str(c) for c in df.columns) + " |",
                 "| " + " | ".join("---" for _ in df.columns) + " |"]
        for rec in df.head(500).itertuples(index=False, name=None):
            parts.append("| " + " | ".join(
                "" if v is None or (isinstance(v, float) and v != v)
                else str(v).replace("|", "\\|") for v in rec) + " |")
        data = "\n".join(parts).encode("utf-8")
        return data, "text/markdown; charset=utf-8", "md"
    raise RuntimeError(f"不支持的导出格式: {fmt}")
