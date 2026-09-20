# -*- coding: utf-8 -*-
"""SQLite 内存 SQL 引擎。

为什么不装 duckdb？因为本机 pip 源拉不到 duckdb，而 sqlite3 是标准库、
零依赖，且 Python 3.13 自带的 SQLite 3.53 已经支持窗口函数 / CTE /
STRICT 表，做分析查询完全够用。

安全：入口一律走 sqlite3 的 authorizer 回调，只放行 SELECT 只读操作，
杜绝 drop / attach / pragma 写操作把宿主文件搞坏。
"""
from __future__ import annotations

import re
import sqlite3
import time

import pandas as pd

def _auth_code(*names) -> set:
    """按名字取 authorizer 的 action 码。

    注意：不同 Python 版本暴露的常量集合不一样（比如 3.13 没有
    SQLITE_TRANSIENT），直接 getattr 会在 import 阶段就把服务炸掉，
    所以这里统一容错取值。
    """
    codes = set()
    for n in names:
        v = getattr(sqlite3, n, None)
        if isinstance(v, int):
            codes.add(v)
    return codes


# 只读白名单：只允许读表、调用函数、执行 SELECT
SQLITE_READONLY_CODES = _auth_code(
    "SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE",
)


class SqlError(Exception):
    pass


def _make_authorizer(sql: str):
    def _auth(action, a1, a2, a3, a4, _a5=None):
        if action in SQLITE_READONLY_CODES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return _auth


def quote_ident(name) -> str:
    """把标识符安全地包进双引号。

    关键认知：SQLite 双引号内的标识符可以包含**空格、中文、点号**等任意字符，
    只要把内部的 `"` 写成 `""` 就不会被注入。所以根本不需要限制字符集 ——
    之前用 `[\\w\\u4e00-\\u9fff]+` 做白名单，直接把 Excel 里常见的
    sheet 名 "Result 1"（带空格）给判成非法，导入直接失败。
    """
    return '"' + str(name).replace('"', '""') + '"'


def safe_table_name(name) -> str:
    """数据集名 -> 可用作表名的字符串。

    只做合理规范化（去首尾空白、限长），不拒绝任何字符。
    真正的安全由 quote_ident() 在拼 SQL 时保证。
    """
    s = str(name).strip()
    if not s:
        raise SqlError("数据集名不能为空")
    return s[:120]


# 兼容旧调用名
_sanitize_table_name = safe_table_name


def new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _dtype_to_sqlite(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "INTEGER"
    if pd.api.types.is_integer_dtype(s):
        return "INTEGER"
    if pd.api.types.is_float_dtype(s):
        return "REAL"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "TEXT"
    return "TEXT"


def register_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """把一个 DataFrame 注册成 SQLite 虚拟表（重名则覆盖）。"""
    table = safe_table_name(table)
    tq = quote_ident(table)
    conn.execute(f"DROP TABLE IF EXISTS {tq}")

    cols_sql = ", ".join(
        f"{quote_ident(c)} {_dtype_to_sqlite(df[c])}" for c in df.columns
    )
    conn.execute(f"CREATE TABLE {tq} ({cols_sql})")

    if len(df) == 0:
        return 0

    insert_cols = ", ".join(quote_ident(c) for c in df.columns)
    ph = ", ".join("?" * len(df.columns))

    # pandas 3.x 的 StringDtype / Arrow 类型直接喂 sqlite 会报
    # "Error binding parameter: unsupported type"，统一先把对象列转成 str
    work = df.copy()
    for c in work.columns:
        s = work[c]
        if isinstance(s.dtype, pd.StringDtype) or s.dtype == object:
            work[c] = s.map(lambda v: None if pd.isna(v) else str(v))
        elif pd.api.types.is_datetime64_any_dtype(s):
            work[c] = s.dt.strftime("%Y-%m-%d %H:%M:%S").where(s.notna(), None)
        elif pd.api.types.is_bool_dtype(s):
            work[c] = s.map(lambda v: None if pd.isna(v) else int(v))

    rows = [tuple(None if pd.isna(v) else v for v in rec) for rec in work.itertuples(index=False, name=None)]
    conn.executemany(f"INSERT INTO {tq} ({insert_cols}) VALUES ({ph})", rows)
    conn.commit()
    return len(rows)


def unregister(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {quote_ident(safe_table_name(table))}")
    conn.commit()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r[0] for r in cur.fetchall()]


def preview(conn: sqlite3.Connection, sql: str, limit: int = 10_000) -> dict:
    """执行只读 SQL，返回 {columns, rows, dtypes, elapsed_ms, truncated}。"""
    sql = (sql or "").strip()
    if not sql:
        raise SqlError("SQL 为空")

    # 兜底：authorizer 之外再加一层文本校验，双保险
    head = sql.lstrip().lower()
    if head.startswith(("insert", "update", "delete", "drop", "alter", "create",
                        "replace", "attach", "detach", "vacuum", "reindex",
                        "pragma", "begin", "commit", "rollback")):
        raise SqlError("只允许 SELECT / WITH 查询，不接受写操作与非查询语句")
    # 去掉行注释后再查一次，防止 /*+ comment */ select 这类绕过
    stripped = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    stripped = re.sub(r"--[^\n]*", " ", stripped).strip().lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise SqlError("只允许以 SELECT 或 WITH 开头的查询语句")

    conn.set_authorizer(_make_authorizer(sql))
    t0 = time.perf_counter()
    try:
        cur = conn.execute(sql)
        raw = cur.fetchmany(limit + 1)
        columns = [d[0] for d in cur.description] if cur.description else []
        truncated = len(raw) > limit
        rows = raw[:limit]
    except sqlite3.Error as e:
        raise SqlError(str(e)) from e
    finally:
        conn.set_authorizer(None)
    elapsed = (time.perf_counter() - t0) * 1000

    # 推断结果列类型
    dtypes = []
    for i in range(len(columns)):
        kind = "null"
        for r in rows:
            v = r[i]
            if v is not None:
                if isinstance(v, bool):
                    kind = "bool"
                elif isinstance(v, int):
                    kind = "int"
                elif isinstance(v, float):
                    kind = "float"
                else:
                    kind = "str"
                break
        dtypes.append(kind)

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "dtypes": dtypes,
        "rowcount": len(rows),
        "truncated": truncated,
        "elapsed_ms": round(elapsed, 2),
    }


def save_result(conn: sqlite3.Connection, table: str, columns: list[str], rows: list[list]) -> None:
    """把查询结果固化成一张新虚拟表，方便后续二次查询（分析师常用套路）。"""
    tq = quote_ident(safe_table_name(table))
    conn.set_authorizer(None)
    conn.execute(f"DROP TABLE IF EXISTS {tq}")
    cols_sql = ", ".join(f"{quote_ident(c)} TEXT" for c in columns)
    conn.execute(f"CREATE TABLE {tq} ({cols_sql})")
    ph = ", ".join("?" * len(columns))
    insert_cols = ", ".join(quote_ident(c) for c in columns)
    conn.executemany(
        f"INSERT INTO {tq} ({insert_cols}) VALUES ({ph})",
        [[None if v is None else (v if isinstance(v, (int, float)) else str(v)) for v in r] for r in rows],
    )
    conn.commit()
