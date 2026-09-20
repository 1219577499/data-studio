# -*- coding: utf-8 -*-
"""Data Studio —— 本地数据分析工作台。

一个进程干完：文件/数据库接入 → 自动 EDA → 质量诊断 → 可视化 → 清洗 → SQL。
数据全程留在本地内存和 data/ 目录里，不外发。

启动：
    python data-studio/app.py
然后浏览器打开 http://127.0.0.1:8848
"""
from __future__ import annotations

import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import sys

import pandas as pd
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile

# 允许以 `python data-studio/app.py` 或 `uvicorn app:app` 两种方式启动
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from core import (  # noqa: E402
    cleaner, compare, config, datasources, exporter, filestore,
    loader, profiler, registry, samples, sqle, viz, writer,
)
from core.jsonio import jsonable  # noqa: E402

app = FastAPI(title="Data Studio", version="2.0.0", docs_url="/api/docs")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 无论用 `python app.py` 还是 `uvicorn app:app` 启动，都能恢复已保存的数据源
    try:
        restored = datasources.restore_saved(loader.connect_raw)
        good = sum(1 for x in restored if not x.get("offline"))
        if restored:
            # 英文输出：双击 run.bat 时跑在 GBK 代码页的 cmd 里，中文会乱码
            print(f"  Data sources: reconnected {good}/{len(restored)}")
    except Exception as e:
        print(f"  数据源恢复失败（不影响使用）：{e}")
    yield


app.router.lifespan_context = lifespan

app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")

MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60MB


# ------------------------------------------------------------------ 工具函数
def ok(data=None, **extra):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return payload


def fail(msg: str, status: int = 400, **extra):
    raise HTTPException(status_code=status, detail={"message": str(msg), **extra})


def get_sid(req: Request) -> str:
    sid = req.headers.get("x-session-id") or req.query_params.get("sid")
    if not sid:
        fail("缺少会话标识 X-Session-Id", 400)
    try:
        return registry.get_session(sid).sid
    except KeyError as e:
        fail(str(e), 410)


@app.exception_handler(Exception)
async def _unhandled(req: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"[Error] {req.url.path}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "message": f"{type(exc).__name__}: {exc}", "trace": tb[-1500:]},
    )


# ------------------------------------------------------------------ 页面
@app.get("/", response_class=HTMLResponse)
def index():
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
def health():
    return ok({"version": app.version, "pandas": pd.__version__,
               "sessions": len(registry.SESSIONS)})


# ------------------------------------------------------------------ 会话
@app.post("/api/session")
def create_session():
    s = registry.create_session()
    return ok({"sid": s.sid}, datasets=s.summary())


@app.get("/api/catalog")
def catalog(sid: str = ""):
    """前端下拉框要用的全部元数据。"""
    return ok({
        "charts": viz.CHART_CATALOG,
        "intents": viz.CHART_INTENTS,
        "aggs": viz.AGG_OPTIONS,
        "sorts": viz.SORT_OPTIONS,
        "ops": cleaner.OP_META,
        "op_groups": cleaner.op_meta_grouped(),
        "agg_funcs": sorted(cleaner.AGG_FUNCS.keys()),
        "upload_max_mb": MAX_UPLOAD_BYTES // 1024 // 1024,
        "db_types": [
            {"key": "sqlite", "label": "SQLite 文件", "fields": ["path"]},
            {"key": "mysql", "label": "MySQL", "fields": ["host", "port", "user", "password", "database"]},
            {"key": "postgres", "label": "PostgreSQL", "fields": ["host", "port", "user", "password", "database"]},
        ],
        "samples": samples.ensure_samples(),
    })


# ------------------------------------------------------------------ 数据源：文件
@app.post("/api/upload")
async def upload(req: Request, file: UploadFile = File(...)):
    sid = get_sid(req)
    sess = registry.get_session(sid)
    raw = await file.read()
    if not raw:
        fail("上传的文件是空的")
    if len(raw) > MAX_UPLOAD_BYTES:
        fail(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")

    try:
        tables = loader.load_bytes(file.filename or "upload.csv", raw)
    except Exception as e:
        fail(f"解析失败：{e}")

    info = []
    for name, df in tables.items():
        ds = sess.register(name, df, source=f"file:{file.filename}")
        info.append({"name": ds.name, "rows": ds.rows, "cols": ds.cols})
    return ok(info, datasets=sess.summary())


@app.post("/api/load_path")
def load_path(payload: dict = Body(...)):
    """直接加载本机文件路径（不上传，适合大文件）。"""
    sid = payload.get("sid") or ""
    sess = registry.get_session(sid)
    p = Path(payload.get("path", ""))
    if not p.exists():
        fail(f"路径不存在: {p}")
    try:
        tables = loader.load_file(p, sheet=payload.get("sheet"))
    except Exception as e:
        fail(f"加载失败：{e}")
    info = []
    for name, df in tables.items():
        ds = sess.register(name, df, source=f"path:{p}")
        info.append({"name": ds.name, "rows": ds.rows, "cols": ds.cols})
    return ok(info, datasets=sess.summary())


@app.get("/api/file/pick")
def pick_file(path: str = "", only_readable: int = 0):
    """浏览本机目录。path 为空时返回磁盘列表 + 常用目录，不再被锁在 C 盘。"""
    try:
        data = filestore.browse(path, only_readable=bool(only_readable))
        return ok(data)
    except PermissionError as e:
        fail(f"没有权限访问：{e}", 403)
    except FileNotFoundError as e:
        fail(str(e))
    except OSError as e:
        fail(f"无法读取该路径：{e}")


# ------------------------------------------------------------------ 数据源：样例
@app.post("/api/sample/load")
def load_sample(payload: dict = Body(...)):
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    key = payload.get("key")
    try:
        df = samples.load_sample(key)
    except Exception as e:
        fail(f"加载样例失败：{e}")
    ds = sess.register(key, df, source=f"sample:{key}")
    return ok({"name": ds.name, "rows": ds.rows, "cols": ds.cols}, datasets=sess.summary())


def get_sid_from_body(payload: dict) -> str:
    sid = payload.get("sid")
    if not sid:
        fail("缺少 sid")
    try:
        return registry.get_session(sid).sid
    except KeyError as e:
        fail(str(e), 410)


# ------------------------------------------------------------------ 数据源：数据库
# 连接一次即登记注册，之后可以反复导入表、刷新、预览，不用再填一遍密码。
@app.get("/api/db/list")
def db_list():
    return ok(datasources.list_sources())


@app.post("/api/db/connect")
def db_connect(payload: dict = Body(...)):
    dbtype = (payload.get("dbtype") or "").lower()
    params = payload.get("params") or {}
    remember = bool(payload.get("remember_password"))
    try:
        src = datasources.register(
            dbtype=dbtype, params=params, connect_fn=loader.connect_raw,
            remember_password=remember,
        )
    except Exception as e:
        fail(str(e))
    return ok(src.to_dict())


@app.post("/api/db/{cid}/refresh")
def db_refresh(cid: str):
    try:
        tables = datasources.refresh_tables(cid)
        src = datasources.get(cid)
    except Exception as e:
        fail(str(e))
    return ok({"tables": tables, "tables_at": src.tables_at})


@app.get("/api/db/{cid}/preview")
def db_preview(cid: str, table: str, n: int = 20):
    """先看几行决定要不要导入，避免把没用的大表拉进来。"""
    try:
        return ok(loader.preview_db_table(cid, table, n=n))
    except Exception as e:
        fail(f"预览失败：{e}")


@app.get("/api/db/{cid}/describe")
def db_describe(cid: str, table: str):
    try:
        return ok(loader.describe_db_table(cid, table))
    except Exception as e:
        fail(f"读取表结构失败：{e}")


@app.delete("/api/db/{cid}")
def db_remove(cid: str):
    datasources.remove(cid)
    return ok(True)


@app.post("/api/db/import")
def db_import(payload: dict = Body(...)):
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    cid = payload.get("cid")
    tables = payload.get("tables") or ([payload["table"]] if payload.get("table") else [])
    limit = payload.get("limit") or 100_000
    if not cid or not tables:
        fail("缺少数据源或表名")
    imported, errors = [], []
    for t in tables:
        try:
            df = loader.read_db_table(cid, t, limit=limit)
            ds = sess.register(str(t), df, source=f"db:{cid}")
            imported.append({"name": ds.name, "rows": ds.rows, "cols": ds.cols})
        except Exception as e:
            errors.append({"table": t, "error": str(e)})
    if errors and not imported:
        fail("；".join(f"{e['table']}: {e['error']}" for e in errors))
    return ok(imported, errors=errors, datasets=sess.summary())


# ------------------------------------------------------------------ 数据对比
@app.post("/api/dataset/{name}/browse")
def browse_dataset(name: str, payload: dict = Body(...)):
    """带筛选和排序的明细浏览。

    筛选和排序都作用在**全量数据**上，不是只对当前这一页 ——
    否则在几万行里筛"金额 > 1000"却只搜到当前 100 行里的，没有意义。
    """
    sess = registry.get_session(payload.get("sid") or "")
    ds = sess.get(name)
    n = max(1, min(int(payload.get("n") or 100), 2000))

    df = ds.df
    before = int(len(df))

    for f in payload.get("filters") or []:
        try:
            df = cleaner.filter_df(df, f)
        except Exception as e:
            fail(f"筛选条件无效：{e}")

    sort = payload.get("sort")
    if sort and sort.get("column"):
        df = cleaner.sort_df(df, sort["column"], bool(sort.get("ascending", True)))

    total = int(len(df))
    part = df.head(n)
    return ok({
        "columns": [str(c) for c in part.columns],
        "dtypes": [str(part[c].dtype) for c in part.columns],
        "rows": [[None if pd.isna(v) else v for v in rec]
                 for rec in part.itertuples(index=False, name=None)],
        "total": total,
        "shown": int(len(part)),
        "before": before,
    })


# ------------------------------------------------------------------ 创建数据源（落表）
@app.post("/api/datasource/preview")
def datasource_preview(payload: dict = Body(...)):
    """看看来源会取到什么数据，写之前先确认一下。"""
    sess = registry.get_session(payload.get("sid") or "")
    try:
        df = writer.resolve_source(payload.get("source") or {}, sess)
    except Exception as e:
        fail(str(e))
    part = df.head(20)
    return ok({
        "columns": [str(c) for c in df.columns],
        "dtypes": [str(df[c].dtype) for c in df.columns],
        "rows": [[None if pd.isna(v) else v for v in rec]
                 for rec in part.itertuples(index=False, name=None)],
        "total": int(len(df)),
    })


@app.post("/api/datasource/create")
def create_datasource(payload: dict = Body(...)):
    """创建数据源：取数 → 落表。

    来源：数据集 / SQL 查询 / 已连接数据源的表
    去向：新建本地 SQLite 文件 / 已连接的数据源
    """
    sess = registry.get_session(payload.get("sid") or "")
    try:
        result = writer.materialize(
            source=payload.get("source") or {},
            target=payload.get("target") or {},
            table=payload.get("table"),
            mode=payload.get("mode") or "replace",
            sess=sess,
            register=bool(payload.get("register")),
        )
    except Exception as e:
        fail(str(e))
    toast_msg = f"已写入 {result['table']}（{result['rows']} 行）"
    return ok(result, message=toast_msg, sources=datasources.list_sources())


@app.post("/api/compare/run")
def compare_run(payload: dict = Body(...)):
    """两份数据集的差异分析：结构 / 行级 / 分布漂移。"""
    sess = registry.get_session(payload.get("sid") or "")
    left_name = payload.get("left")
    right_name = payload.get("right")
    key = payload.get("key") or None
    if not left_name or not right_name:
        fail("需要选择两份数据集")
    if left_name == right_name:
        fail("请选择两份不同的数据集")
    try:
        lds, rds = sess.get(left_name), sess.get(right_name)
    except KeyError as e:
        fail(str(e))

    t0 = time.perf_counter()
    try:
        res = compare.compare_all(lds.df, rds.df, key=key,
                                  left_name=lds.name, right_name=rds.name)
    except Exception as e:
        fail(f"对比失败：{e}")
    res["_meta"] = {"elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
    return ok(res)


@app.post("/api/compare/chart")
def compare_chart(payload: dict = Body(...)):
    """漂移对比图。"""
    sess = registry.get_session(payload.get("sid") or "")
    lds, rds = sess.get(payload.get("left")), sess.get(payload.get("right"))
    kind = payload.get("kind", "drift")
    column = payload.get("column")
    theme = payload.get("theme") or "light"
    with viz.use_theme(theme):
        if kind == "drift":
            drift = compare.compare_drift(lds.df, rds.df)["numeric"]
            return ok(compare.drift_chart(drift))
        if kind == "category":
            cats = compare.compare_drift(lds.df, rds.df)["categorical"]
            return ok(compare.category_shift_chart(cats, column))
    fail(f"未知的对比图类型: {kind}")


# ------------------------------------------------------------------ 数据集管理
@app.get("/api/datasets")
def datasets(sid: str = ""):
    sess = registry.get_session(sid)
    return ok(sess.summary())


@app.delete("/api/dataset/{name}")
def drop_dataset(name: str, sid: str = ""):
    sess = registry.get_session(sid)
    sess.drop(name)
    return ok(True, datasets=sess.summary())


@app.get("/api/dataset/{name}/profile")
def profile_dataset(name: str, sid: str = ""):
    sess = registry.get_session(sid)
    ds = sess.get(name)
    t0 = time.perf_counter()
    prof = profiler.profile(ds.df, name=ds.name)
    prof["_meta"] = {
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        "source": ds.source,
        "lineage": ds.lineage,
    }
    return ok(prof)


@app.get("/api/dataset/{name}/preview")
def preview_dataset(name: str, sid: str = "", n: int = 100, head: str = "head"):
    sess = registry.get_session(sid)
    ds = sess.get(name)
    n = max(1, min(int(n), 500))
    part = ds.df.head(n) if head == "head" else ds.df.tail(n)
    return ok({
        "columns": [str(c) for c in ds.df.columns],
        "dtypes": [str(t) for t in ds.df.dtypes],
        "rows": [jsonable(list(r)) for r in part.itertuples(index=False, name=None)],
        "total": int(len(ds.df)),
        "offset": 0 if head == "head" else max(0, len(ds.df) - n),
    })


@app.get("/api/dataset/{name}/columns")
def dataset_columns(name: str, sid: str = ""):
    """给图表/清洗面板用的列元数据。"""
    sess = registry.get_session(sid)
    ds = sess.get(name)
    cols = []
    for c in ds.df.columns:
        s = ds.df[c]
        role = profiler._role(s, len(ds.df))
        cols.append({"name": str(c), "dtype": str(s.dtype), "role": role,
                     "unique": int(s.nunique(dropna=True)),
                     "missing": int(s.isna().sum())})
    return ok(cols)


# ------------------------------------------------------------------ 图表
@app.post("/api/chart")
def build_chart(payload: dict = Body(...)):
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    name = payload.get("dataset") or None
    ds = sess.active_or_first(name)
    spec = payload.get("spec") or {}
    # 主题决定图表的文字/坐标轴/tooltip 配色，深色界面下必须传，否则图表会糊
    theme = payload.get("theme") or "light"
    t0 = time.perf_counter()
    try:
        option = viz.build(ds.df, spec, theme=theme)
    except Exception as e:
        fail(f"生成图表失败：{e}")
    return ok({"option": option, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)})


# ------------------------------------------------------------------ 清洗
@app.post("/api/clean/preview")
def clean_preview(payload: dict = Body(...)):
    """试跑一遍算子，只返回效果，不落库。"""
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    ds = sess.active_or_first(payload.get("dataset"))
    try:
        res = cleaner.apply_ops(ds.df, payload.get("ops") or [])
    except Exception as e:
        fail(str(e))
    return ok({
        "steps": res["steps"], "before": res["before"], "after": res["after"],
        "code": res["code"],
    })


@app.post("/api/clean/apply")
def clean_apply(payload: dict = Body(...)):
    """执行清洗并存为新数据集。"""
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    ds = sess.active_or_first(payload.get("dataset"))
    ops = payload.get("ops") or []
    try:
        res = cleaner.apply_ops(ds.df, ops)
    except Exception as e:
        fail(str(e))

    suggested = payload.get("save_as") or f"{ds.name}_clean"
    # 避免覆盖：重名自动加后缀
    new_name = suggested
    i = 2
    while new_name in sess.datasets:
        new_name = f"{suggested}_{i}"
        i += 1

    new_ds = sess.register(new_name, res["df"], source=f"derived:{ds.name}",
                           parent=ds.name, lineage=res["steps"])
    return ok({
        "name": new_ds.name, "rows": new_ds.rows, "cols": new_ds.cols,
        "before": res["before"], "after": res["after"],
        "steps": res["steps"], "code": res["code"],
    }, datasets=sess.summary())


# ------------------------------------------------------------------ SQL
@app.post("/api/sql")
def run_sql(payload: dict = Body(...)):
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    sql = payload.get("sql") or ""
    limit = int(payload.get("limit") or 5000)
    try:
        res = sqle.preview(sess.conn, sql, limit=limit)
    except Exception as e:
        fail(str(e))
    from core.jsonio import jsonable
    return ok({**res, "rows": [jsonable(r) for r in res["rows"]]})


@app.post("/api/sql/save")
def save_sql_result(payload: dict = Body(...)):
    """把查询结果固化成一张表，方便二次分析（分析师的常用套路）。"""
    sid = get_sid_from_body(payload)
    sess = registry.get_session(sid)
    sql = payload.get("sql") or ""
    name = payload.get("save_as") or "sql_result"
    try:
        res = sqle.preview(sess.conn, sql, limit=config.MAX_DB_ROWS)
    except Exception as e:
        fail(str(e))
    df = pd.DataFrame(res["rows"], columns=res["columns"])
    # 按列推断类型：纯数字列转回数值，否则会全变成 object
    for c in df.columns:
        conv = pd.to_numeric(df[c], errors="coerce")
        if len(conv) and conv.notna().mean() > 0.9:
            df[c] = conv
    base = name
    i = 2
    while name in sess.datasets:
        name = f"{base}_{i}"
        i += 1
    ds = sess.register(name, df, source="sql")
    return ok({"name": ds.name, "rows": ds.rows, "cols": ds.cols}, datasets=sess.summary())


@app.get("/api/sql/tables")
def sql_tables(sid: str = ""):
    sess = registry.get_session(sid)
    conn = sess.conn
    tables = sqle.list_tables(conn)
    info = []
    for t in tables:
        tq = sqle.quote_ident(t)
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {tq}")
            n = cur.fetchone()[0]
        except Exception:
            n = 0
        try:
            cur = conn.execute(f"SELECT * FROM {tq} LIMIT 1")
            cols = [d[0] for d in cur.description] if cur.description else []
        except Exception:
            cols = []
        info.append({"table": t, "rows": int(n), "columns": cols})
    return ok(info)


# ------------------------------------------------------------------ 导出
@app.get("/api/export/{name}")
def export_dataset(name: str, sid: str = "", fmt: str = "csv"):
    sess = registry.get_session(sid)
    ds = sess.get(name)
    try:
        data, media, ext = exporter.to_bytes(ds.df, fmt)
    except Exception as e:
        fail(str(e))
    fname = f"{name}.{ext}"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{name}.{ext}\"; filename*=UTF-8''{quote(fname)}"
    }
    return Response(content=data, media_type=media, headers=headers)


@app.post("/api/report/{name}")
def build_report(name: str, payload: dict = Body(default={})):
    """把当前数据集的剖析结果导出成一份 Markdown 分析报告。"""
    sess = registry.get_session(payload.get("sid") or "")
    ds = sess.get(name)
    prof = profiler.profile(ds.df, name=ds.name)

    ov = prof["overview"]
    q = prof["quality"]
    lines = [
        f"# 数据分析报告：{ds.name}",
        "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　数据源：{ds.source}",
        "",
        "## 一、数据概览",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 行数 | {ov['rows']:,} |",
        f"| 列数 | {ov['cols']} |",
        f"| 内存占用 | {ov['memory_mb']} MB |",
        f"| 重复行 | {ov['duplicate_rows']:,}（{ov['duplicate_pct']:.2%}） |",
        f"| 缺失单元格 | {ov['missing_cells']:,}（{ov['missing_pct']:.2%}） |",
        f"| 数值/类别/日期/布尔列 | {ov['numeric_cols']} / {ov['cat_cols']} / {ov['datetime_cols']} / {ov['bool_cols']} |",
        "",
        "## 二、数据质量",
        "",
        f"**质量分：{q['score']} / 100（{q['grade']}）**　",
        f"严重问题 {q['counts']['error']} 项，警告 {q['counts']['warn']} 项，提示 {q['counts']['info']} 项。",
        "",
    ]

    if q["issues"]:
        lines += ["| 级别 | 列 | 问题 | 建议 |", "| --- | --- | --- | --- |"]
        level_cn = {"error": "严重", "warn": "警告", "info": "提示"}
        for i in q["issues"][:40]:
            msg = i["message"].replace("|", "\\|")
            sug = i["suggestion"].replace("|", "\\|")
            lines.append(f"| {level_cn[i['level']]} | {i['column'] or '-'} | {msg} | {sug} |")
        lines.append("")

    lines += ["## 三、字段画像", "", "| 列 | 类型 | 角色 | 缺失率 | 唯一值 | 摘要 |", "| --- | --- | --- | --- | --- | --- |"]
    for c in prof["columns"]:
        st = c.get("stats") or {}
        if "mean" in st:
            summary = f"均值 {st['mean']:.2f}，中位 {st.get('p50', 0):.2f}，范围 [{st.get('min', 0):.2f}, {st.get('max', 0):.2f}]"
        elif isinstance(st, dict) and st.get("top_values"):
            top = st["top_values"][0]
            summary = f"Top1: {top['value']}（{top['pct']:.1%}）"
        elif isinstance(st, dict) and st.get("min") and "span_days" in st:
            summary = f"{st['min']} ~ {st['max']}"
        else:
            summary = ""
        lines.append(
            f"| {c['name']} | {c['dtype']} | {c['role']} | {c['missing_pct']:.1%} | {c['unique']:,} | {summary} |")

    return ok({"markdown": "\n".join(lines)})


# ------------------------------------------------------------------ main
if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Data Studio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    # 注意：数据源恢复统一交给 lifespan 做，这里不要再调一次，
    # 否则会建立两遍连接（uvicorn 会以 import string 方式再跑一次这个文件）
    print(f"\n  Data Studio  ->  http://{args.host}:{args.port}")
    print("")

    if getattr(sys, "frozen", False):
        # PyInstaller 打包后没有可导入的模块名，必须直接传 app 对象
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        raise SystemExit(0)
    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
