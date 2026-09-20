# -*- coding: utf-8 -*-
"""数据源加载：本地文件 + 数据库。

文件侧优先解决两个真实世界的坑：
  1. 编码——国内数据源 csv 常是 gbk/gb18030，直接 utf-8 读必炸
  2. 分隔符——有些 csv 实际是分号或制表符分隔
数据库侧：sqlite 走标准库，mysql 走 sqlalchemy+pymysql，postgres 需要 psycopg2。
"""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pandas as pd

from .config import MAX_DB_ROWS


class LoadError(Exception):
    pass


# ------------------------------------------------------------------ 文件
_CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin1")


def _read_csv_smart(path: Path, **kw) -> pd.DataFrame:
    last: Exception | None = None
    for enc in _CANDIDATE_ENCODINGS:
        try:
            return pd.read_csv(path, encoding=enc, **kw)
        except UnicodeDecodeError as e:
            last = e
        except Exception as e:  # 其它解析错误直接抛出，和编码无关
            raise LoadError(f"CSV 解析失败: {e}") from e
    raise LoadError(f"CSV 编码探测失败（已尝试 {_CANDIDATE_ENCODINGS}）: {last}")


def sniff_csv(path: Path) -> dict:
    """不看数据先摸一遍结构：编码、分隔符、行数、表头。"""
    enc, head = None, ""
    for e in _CANDIDATE_ENCODINGS:
        try:
            with path.open("r", encoding=e) as f:
                head = "".join([f.readline() for _ in range(5)])
            enc = e
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if enc is None:
        enc = "utf-8"

    sep = ","
    if head:
        counts = {c: sum(line.count(c) for line in head.splitlines()) for c in (",", ";", "\t", "|")}
        sep = max(counts, key=counts.get)
        if counts[sep] == 0:
            sep = ","
    return {"encoding": enc, "sep": sep, "header_preview": head[:400]}


def load_file(path: Path, sheet: str | None = None) -> dict[str, pd.DataFrame]:
    """返回 {表名: DataFrame}，Excel 多 sheet 会返回多个。"""
    if not path.exists():
        raise LoadError(f"文件不存在: {path}")
    suf = path.suffix.lower()

    if suf in (".csv", ".tsv", ".txt"):
        meta = sniff_csv(path)
        sep = "\t" if suf == ".tsv" else meta["sep"]
        df = _read_csv_smart(path, sep=sep)
        # 常见脏数据：pandas 自动生成的 Unnamed 索引列
        df = _drop_index_cols(df)
        return {path.stem: df}

    if suf in (".xlsx", ".xls", ".xlsm"):
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise LoadError("读取 Excel 需要 openpyxl，请执行 pip install openpyxl") from e
        book = pd.ExcelFile(path)
        sheets = book.sheet_names
        if sheet and sheet in sheets:
            return {f"{path.stem}__{sheet}": pd.read_excel(book, sheet_name=sheet)}
        out = {f"{path.stem}__{s}": pd.read_excel(book, sheet_name=s) for s in sheets}
        return out

    if suf in (".json",):
        try:
            return {path.stem: pd.read_json(path, encoding="utf-8")}
        except Exception:
            return {path.stem: pd.read_json(path, encoding="utf-8", lines=True, orient="records")}

    if suf in (".jsonl",):
        return {path.stem: pd.read_json(path, lines=True, encoding="utf-8")}

    if suf in (".parquet",):
        try:
            import pyarrow  # noqa: F401
        except ImportError as e:
            raise LoadError("读取 parquet 需要 pyarrow，请执行 pip install pyarrow") from e
        return {path.stem: pd.read_parquet(path)}

    raise LoadError(f"暂不支持的文件类型: {suf}")


def load_bytes(filename: str, raw: bytes, sheet: str | None = None) -> dict[str, pd.DataFrame]:
    """内存版：直接用 pandas 解析上传的原始字节，避免临时文件。"""
    suf = Path(filename).suffix.lower()
    buf = io.BytesIO(raw)
    if suf in (".csv", ".tsv", ".txt"):
        try:
            df = pd.read_csv(buf)
        except UnicodeDecodeError:
            buf.seek(0)
            df = pd.read_csv(buf, encoding="gb18030")
        except Exception:
            buf.seek(0)
            df = pd.read_csv(buf, sep=";")
        return {Path(filename).stem: _drop_index_cols(df)}
    if suf in (".xlsx", ".xls", ".xlsm"):
        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise LoadError("读取 Excel 需要 openpyxl，请执行 pip install openpyxl") from e
        book = pd.ExcelFile(buf)
        if sheet and sheet in book.sheet_names:
            return {f"{Path(filename).stem}__{sheet}": pd.read_excel(book, sheet_name=sheet)}
        return {f"{Path(filename).stem}__{s}": pd.read_excel(book, sheet_name=s)
                for s in book.sheet_names}
    if suf in (".json",):
        return {Path(filename).stem: pd.read_json(io.BytesIO(raw))}
    if suf in (".jsonl",):
        return {Path(filename).stem: pd.read_json(io.BytesIO(raw), lines=True)}
    raise LoadError(f"暂不支持的文件类型: {suf}")


def _drop_index_cols(df: pd.DataFrame) -> pd.DataFrame:
    junk = [c for c in df.columns
            if isinstance(c, str) and c.startswith("Unnamed:") and df[c].notna().sum() == 0]
    return df.drop(columns=junk) if junk else df


# ------------------------------------------------------------------ 数据库
DB_LABELS = {"sqlite": "SQLite", "mysql": "MySQL", "postgres": "PostgreSQL"}


def _make_url(dbtype: str, params: dict) -> str:
    if dbtype == "sqlite":
        return f"sqlite:///{params['path']}"
    if dbtype == "mysql":
        cred = f"{params['user']}:{params['password']}" if params.get("user") else ""
        host = params.get("host", "127.0.0.1")
        port = params.get("port", 3306)
        db = params.get("database") or ""
        return f"mysql+pymysql://{cred}@{host}:{port}/{db}"
    if dbtype == "postgres":
        cred = f"{params['user']}:{params['password']}" if params.get("user") else ""
        host = params.get("host", "127.0.0.1")
        port = params.get("port", 5432)
        db = params.get("database") or ""
        return f"postgresql+psycopg2://{cred}@{host}:{port}/{db}"
    raise LoadError(f"不支持的数据库类型: {dbtype}")


def connect_raw(dbtype: str, params: dict):
    """建立连接并做一次真实握手校验。

    返回 (engine, sqlite_path) —— sqlite 只有路径，不需要 engine。
    真正会把连接登记进注册表的是 core.datasources.register()，这里只管连通。
    """
    dbtype = (dbtype or "").lower()
    if dbtype == "sqlite":
        p = Path(params.get("path", ""))
        if not p.exists():
            raise LoadError(f"SQLite 文件不存在: {p}")
        conn = sqlite3.connect(str(p))
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return None, str(p)

    if dbtype in ("mysql", "postgres"):
        try:
            from sqlalchemy import create_engine, inspect
        except ImportError as e:
            raise LoadError("需要 sqlalchemy：pip install sqlalchemy") from e
        url = _make_url(dbtype, params)
        engine = None
        try:
            engine = create_engine(url, pool_pre_ping=True,
                                   connect_args={"connect_timeout": 8})
            with engine.connect() as c:
                c.exec_driver_sql("SELECT 1")
        except Exception as e:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception:
                    pass
            msg = str(e)
            if "psycopg2" in msg:
                raise LoadError("连接 PostgreSQL 需要 psycopg2：pip install psycopg2-binary") from e
            if "pymysql" in msg:
                raise LoadError("连接 MySQL 需要 pymysql：pip install pymysql") from e
            raise LoadError(f"数据库连接失败: {msg}") from e
        return engine, None

    raise LoadError(f"不支持的数据库类型: {dbtype}")


def _resolve(cid: str):
    from . import datasources
    return datasources.get(cid)


def _q_ident(dbtype: str, ident) -> str:
    """按数据库方言给标识符加引号。

    MySQL 用反引号，sqlite/postgres 用双引号；内部同引号字符翻倍转义。
    表名、列名一律走这里，避免手工拼 `f'"{name}"'` 拼出问题。
    """
    s = str(ident)
    if dbtype == "mysql":
        return "`" + s.replace("`", "``") + "`"
    return '"' + s.replace('"', '""') + '"'


def list_db_tables(cid: str) -> list[dict]:
    src = _resolve(cid)
    if src.dbtype == "sqlite":
        conn = sqlite3.connect(src._sqlite_path)
        try:
            cur = conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view') ORDER BY type, name")
            items = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()
        # 顺手补上行数，前端可以直接显示「多大一张表」
        conn = sqlite3.connect(src._sqlite_path)
        try:
            for it in items:
                try:
                    it["rows"] = int(conn.execute(f'SELECT COUNT(*) FROM "{it["name"]}"').fetchone()[0])
                except Exception:
                    it["rows"] = None
        finally:
            conn.close()
        return items

    from sqlalchemy import inspect
    insp = inspect(src._engine)
    items = []
    for t in insp.get_table_names():
        items.append({"name": t, "type": "table", "rows": None})
    if src.dbtype == "mysql":
        for v in insp.get_view_names():
            items.append({"name": v, "type": "view", "rows": None})
    return items


def describe_db_table(cid: str, table: str) -> dict:
    """表的字段名与类型 —— 前端用它拼 SELECT 语句。"""
    src = _resolve(cid)
    cols = []
    if src.dbtype == "sqlite":
        conn = sqlite3.connect(src._sqlite_path)
        try:
            for r in conn.execute(f"PRAGMA table_info({_q_ident('sqlite', table)})").fetchall():
                cols.append({"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])})
        finally:
            conn.close()
    else:
        from sqlalchemy import inspect
        insp = inspect(src._engine)
        for c in insp.get_columns(table):
            cols.append({"name": c["name"], "type": str(c["type"]),
                         "notnull": bool(c.get("nullable") is False), "pk": bool(c.get("primary_key"))})
    return {"table": table, "columns": cols}


def read_db_table(cid: str, table: str, limit: int | None = None,
                  columns: list[str] | None = None) -> pd.DataFrame:
    src = _resolve(cid)
    n = max(1, min(int(limit or MAX_DB_ROWS), MAX_DB_ROWS))
    dbtype = src.dbtype
    cols = ", ".join(_q_ident(dbtype, c) for c in columns) if columns else "*"

    if dbtype == "sqlite":
        conn = sqlite3.connect(src._sqlite_path)
        try:
            df = pd.read_sql_query(f"SELECT {cols} FROM {_q_ident(dbtype, table)} LIMIT {n}", conn)
        finally:
            conn.close()
        return df

    from sqlalchemy import text
    with src._engine.connect() as c:  # type: ignore[attr-defined]
        df = pd.read_sql_query(text(f"SELECT {cols} FROM {_q_ident(dbtype, table)} LIMIT {n}"), c)
    return df


def query_sql(cid: str, sql: str, limit: int | None = None) -> pd.DataFrame:
    """在指定数据源上跑一条查询，返回 DataFrame。

    只放行查询语句 —— 这是给「创建数据源」当取数用的，
    不应该顺手把源库改了。
    """
    src = _resolve(cid)
    head = sql.lstrip(" \t\r\n(").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise ValueError("来源 SQL 仅支持查询语句（SELECT / WITH）")

    if src.dbtype == "sqlite":
        conn = sqlite3.connect(src._sqlite_path)
        try:
            df = pd.read_sql_query(sql, conn)
        finally:
            conn.close()
        return df.head(limit) if limit else df

    from sqlalchemy import text
    with src._engine.connect() as c:  # type: ignore[attr-defined]
        df = pd.read_sql_query(text(sql), c)
    return df.head(limit) if limit else df


def preview_db_table(cid: str, table: str, n: int = 20) -> dict:
    """不导入整表，先看几行长什么样 —— 决定要不要导、导多少。"""
    src = _resolve(cid)
    n = max(1, min(int(n or 20), 200))

    dbtype = src.dbtype
    tq = _q_ident(dbtype, table)
    if dbtype == "sqlite":
        conn = sqlite3.connect(src._sqlite_path)
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tq})").fetchall()]
            total = int(conn.execute(f"SELECT COUNT(*) FROM {tq}").fetchone()[0])
            rows = [list(r) for r in conn.execute(f"SELECT * FROM {tq} LIMIT {n}").fetchall()]
        finally:
            conn.close()
        return {"columns": cols, "rows": rows, "total": total}

    from sqlalchemy import text
    with src._engine.connect() as c:  # type: ignore[attr-defined]
        total = int(c.execute(text(f"SELECT COUNT(*) FROM {tq}")).scalar() or 0)
        res = c.execute(text(f"SELECT * FROM {tq} LIMIT {n}"))
        cols = list(res.keys())
        rows = [list(r) for r in res.fetchall()]
    return {"columns": cols, "rows": rows, "total": total}
