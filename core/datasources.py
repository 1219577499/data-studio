# -*- coding: utf-8 -*-
"""数据源管理：把「连接过的数据库」当成可反复使用的资源，而不是一次性动作。

之前的设计问题：导入一张表要连一次库，导入五张表要连五次，还要重填密码。
这里改成：
  1. 连接成功后进入 CONNECTIONS 注册表，前端列出来随时用
  2. 连接参数（不含密码，除非用户勾了「记住」）落盘到 data/sources.json，
     下次启动自动重连 —— sqlite 文件这种一定能连上的尤其有用
  3. 支持刷新表清单、预览表数据、批量导入多张表、断开连接
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR

SOURCES_FILE = DATA_DIR / "sources.json"
_lock = threading.Lock()

DB_LABELS = {"sqlite": "SQLite", "mysql": "MySQL", "postgres": "PostgreSQL"}


def _now() -> float:
    return time.time()


@dataclass
class Source:
    """一个已保存的数据源（数据库连接）。"""
    cid: str
    dbtype: str
    label: str
    params: dict = field(default_factory=dict)     # 不含密码（或含，见 remember_password）
    remember_password: bool = False
    saved_password: str | None = None              # 仅在用户显式允许时存在
    created_at: float = field(default_factory=_now)
    last_used: float = field(default_factory=_now)
    tables_cache: list[dict] = field(default_factory=list)
    tables_at: float = 0.0
    err: str | None = None
    # 运行时对象（不落盘）
    _engine: Any = None
    _sqlite_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "cid": self.cid,
            "dbtype": self.dbtype,
            "label": self.label,
            "params": {k: v for k, v in self.params.items() if k != "password"},
            "remember_password": self.remember_password,
            "has_password": bool(self.saved_password or self.params.get("password")),
            "created_at": self.created_at,
            "last_used": self.last_used,
            "tables": self.tables_cache,
            "tables_at": self.tables_at,
            "error": self.err,
        }


SOURCES: dict[str, Source] = {}


# ------------------------------------------------------------------ 持久化
def _load_disk() -> list[dict]:
    if not SOURCES_FILE.exists():
        return []
    try:
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def save_disk() -> None:
    payload = []
    for s in SOURCES.values():
        payload.append({
            "cid": s.cid,
            "dbtype": s.dbtype,
            "label": s.label,
            "params": s.params,
            "remember_password": s.remember_password,
            "saved_password": s.saved_password if s.remember_password else None,
            "created_at": s.created_at,
        })
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SOURCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SOURCES_FILE)


def restore_saved(connect_fn) -> list[dict]:
    """服务启动时自动重连上次保存的数据源。

    连不上的（比如 MySQL 关了）不会报错，只是标记 error，
    前端能看到「离线」，点一下就能重试或删除。
    """
    out = []
    for item in _load_disk():
        try:
            s = register(
                dbtype=item["dbtype"],
                params=item.get("params") or {},
                remember_password=bool(item.get("remember_password")),
                raw_password=item.get("saved_password"),
                connect_fn=connect_fn,
                cid=item.get("cid"),
                created_at=item.get("created_at"),
            )
            out.append(s.to_dict())
        except Exception as e:
            out.append({
                "cid": item.get("cid", "?"),
                "dbtype": item.get("dbtype", "?"),
                "label": item.get("label", item.get("params", {}).get("path", "?")),
                "params": item.get("params", {}),
                "offline": True,
                "error": str(e),
                "tables": [],
            })
    return out


# ------------------------------------------------------------------ 注册 / 释放
def register(*, dbtype: str, params: dict, connect_fn, remember_password: bool = False,
             raw_password: str | None = None, cid: str | None = None,
             created_at: float | None = None) -> Source:
    """建立连接并登记到注册表。

    remember_password / raw_password：只有用户勾了「记住密码」才会带进来。
    """
    with _lock:
        params = dict(params or {})
        pwd = params.pop("password", None)
        actual_pwd = pwd or raw_password or None
        label = _make_label(dbtype, params)

        _engine, _sqlite_path = connect_fn(dbtype, {**params, "password": actual_pwd})

        s = Source(
            cid=cid or uuid.uuid4().hex[:8],
            dbtype=dbtype,
            label=label,
            params={**params, **({"password": actual_pwd} if remember_password else {})},
            remember_password=remember_password,
            saved_password=actual_pwd if remember_password else None,
            created_at=created_at or _now(),
        )
        s._engine = _engine
        s._sqlite_path = _sqlite_path
        SOURCES[s.cid] = s
        try:
            refresh_tables(s.cid)
        except Exception as e:
            s.err = str(e)
        save_disk()
        return s


def get(cid: str) -> Source:
    s = SOURCES.get(cid)
    if not s:
        raise KeyError(f"数据源不存在或已断开: {cid}")
    s.last_used = _now()
    return s


def remove(cid: str) -> None:
    s = SOURCES.pop(cid, None)
    if s and s._engine is not None:
        try:
            s._engine.dispose()
        except Exception:
            pass
    save_disk()


def _make_label(dbtype: str, params: dict) -> str:
    if dbtype == "sqlite":
        return f"SQLite · {Path(params.get('path', '')).name}"
    return f"{DB_LABELS.get(dbtype, dbtype)} · {params.get('host')}/{params.get('database') or ''}"


def refresh_tables(cid: str, list_fn=None) -> list[dict]:
    s = get(cid)
    from . import loader  # 延迟导入，避免循环依赖
    try:
        items = loader.list_db_tables(cid)
        s.tables_cache = items
        s.tables_at = _now()
        s.err = None
    except Exception as e:
        s.err = str(e)
        raise
    return s.tables_cache


def list_sources() -> list[dict]:
    return [s.to_dict() for s in sorted(SOURCES.values(), key=lambda x: -x.created_at)]
