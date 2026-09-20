# -*- coding: utf-8 -*-
"""把数据写出去，物化成新的数据源（落表）。

三条来源：
  1. 会话里的数据集
  2. SQL 查询结果（在会话引擎或某个已连接的数据源上跑）
  3. 已连接数据源的某张表（可用于跨库搬运）

两个去向：
  1. 新建本地 SQLite 文件 —— 零依赖，最常用
  2. 写入已连接的数据源（sqlite / mysql / postgres）

写入模式对应 pandas to_sql 的 if_exists：fail / replace / append
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .sqle import quote_ident

WRITE_MODES = ("fail", "replace", "append")
MODE_LABELS = {"fail": "表已存在则报错", "replace": "覆盖（重建表）", "append": "追加到末尾"}


class WriteError(Exception):
    pass


def _clean_for_sql(df: pd.DataFrame) -> pd.DataFrame:
    """to_sql 之前先把列规整好。

    object 列里混着 str / int / None 时，MySQL 驱动经常直接报
    "can't adapt type" 或者把整列建成 TEXT 又插不进去。统一转一遍最省事。
    """
    work = df.copy()
    for c in work.columns:
        s = work[c]
        if isinstance(s.dtype, pd.StringDtype) or s.dtype == object:
            work[c] = s.map(lambda v: None if pd.isna(v) else str(v))
        elif pd.api.types.is_datetime64_any_dtype(s):
            work[c] = s.dt.strftime("%Y-%m-%d %H:%M:%S").where(s.notna(), None)
        elif pd.api.types.is_bool_dtype(s):
            work[c] = s.map(lambda v: None if pd.isna(v) else int(v))
    return work


def _count_rows_sqlite(path: str, table: str) -> int:
    con = sqlite3.connect(str(path))
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])
    finally:
        con.close()


def write_sqlite_file(path: str, table: str, df: pd.DataFrame, mode: str = "replace") -> dict:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        _clean_for_sql(df).to_sql(table, con, if_exists=mode, index=False)
        con.commit()
    except ValueError as e:
        # pandas 在 if_exists='fail' 且表已存在时抛 ValueError
        raise WriteError(f"写入失败：{e}") from e
    except Exception as e:
        raise WriteError(f"写入 SQLite 失败：{e}") from e
    finally:
        con.close()
    return {
        "target": "sqlite_file", "path": str(p), "table": table,
        "rows": _count_rows_sqlite(str(p), table),
    }


def write_to_source(cid: str, table: str, df: pd.DataFrame, mode: str = "replace") -> dict:
    from . import datasources, loader

    src = datasources.get(cid)
    if src.dbtype == "sqlite":
        return write_sqlite_file(src._sqlite_path, table, df, mode)

    from sqlalchemy import text  # noqa: PLC0415
    try:
        work = _clean_for_sql(df)
        with src._engine.begin() as con:  # type: ignore[attr-defined]
            work.to_sql(table, con, if_exists=mode, index=False)
        with src._engine.connect() as con:  # type: ignore[attr-defined]
            n = int(con.execute(text(f"SELECT COUNT(*) FROM {quote_ident(table)}")).scalar() or 0)
    except Exception as e:
        raise WriteError(f"写入 {src.label} 失败：{e}") from e
    return {
        "target": "source", "cid": cid, "label": src.label,
        "dbtype": src.dbtype, "table": table, "rows": n,
    }


def resolve_source(spec: dict, sess) -> pd.DataFrame:
    """按来源配置取出 DataFrame。"""
    kind = spec.get("kind")

    if kind == "dataset":
        name = spec.get("name")
        if not name:
            raise WriteError("未指定数据集")
        return sess.get(name).df

    if kind == "sql":
        sql = (spec.get("sql") or "").strip()
        if not sql:
            raise WriteError("SQL 不能为空")
        if not _looks_readonly(sql):
            raise WriteError("来源 SQL 仅支持查询语句（SELECT / WITH）")
        on = spec.get("on")          # 为空 = 在当前会话的引擎上跑
        limit = int(spec.get("limit") or 500_000)
        if not on:
            from . import sqle  # noqa: PLC0415
            res = sqle.preview(sess.conn, sql, limit=limit)
            return pd.DataFrame(res["rows"], columns=res["columns"])
        from . import loader  # noqa: PLC0415
        return loader.query_sql(on, sql, limit)

    if kind == "table":
        cid = spec.get("cid")
        table = spec.get("table")
        if not cid or not table:
            raise WriteError("未指定来源表")
        from . import loader  # noqa: PLC0415
        return loader.read_db_table(cid, table, limit=int(spec.get("limit") or 500_000))

    raise WriteError(f"不支持的数据来源：{kind}")


def _looks_readonly(sql: str) -> bool:
    head = sql.lstrip(" \t\r\n(").lower()
    return head.startswith("select") or head.startswith("with")


def materialize(*, source: dict, target: dict, table: str,
                mode: str = "replace", sess=None, register: bool = False) -> dict:
    """取数 → 落表。返回写入结果。"""
    if mode not in WRITE_MODES:
        raise WriteError(f"不支持的写入模式：{mode}")
    table = str(table or "").strip()
    if not table:
        raise WriteError("必须填写目标表名")

    df = resolve_source(source, sess)
    if df is None:
        raise WriteError("没有取到数据")
    if len(df) == 0:
        raise WriteError("取到的数据是空的，没有东西可写")

    kind = target.get("kind")
    if kind == "sqlite_file":
        path = str(target.get("path") or "").strip()
        if not path:
            raise WriteError("未指定 SQLite 文件路径")
        result = write_sqlite_file(path, table, df, mode)
        if register:
            from . import datasources  # noqa: PLC0415
            datasources.register(
                dbtype="sqlite", params={"path": path},
                connect_fn=None, label=Path(path).stem,
            )
            result["registered"] = True
        result["source_rows"] = int(len(df))
        return result

    if kind == "source":
        cid = target.get("cid")
        if not cid:
            raise WriteError("未指定目标数据源")
        result = write_to_source(cid, table, df, mode)
        result["source_rows"] = int(len(df))
        return result

    raise WriteError(f"不支持的目标类型：{kind}")
