# -*- coding: utf-8 -*-
"""会话与数据集注册表。

每个会话持有：
  - 若干 DataFrame（清洗链路会产生派生表）
  - 一个 SQLite 内存连接，所有表注册进去后可以直接写 SQL
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

import pandas as pd

from . import sqle


@dataclass
class Dataset:
    name: str
    df: pd.DataFrame
    source: str = "unknown"
    rows: int = 0
    cols: int = 0
    created_at: float = field(default_factory=time.time)
    parent: str | None = None
    lineage: list[dict] = field(default_factory=list)  # 清洗步骤留痕


@dataclass
class Session:
    sid: str
    datasets: dict[str, Dataset] = field(default_factory=dict)
    conn: "sqle.sqlite3.Connection" = None  # type: ignore[assignment]
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    order: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.last_seen = time.time()

    # -- 数据集操作 ----------------------------------------------------
    def register(self, name: str, df: pd.DataFrame, *, source: str = "unknown",
                 parent: str | None = None, lineage: list[dict] | None = None) -> Dataset:
        name = _safe_name(name)
        ds = Dataset(name=name, df=df, source=source, rows=int(len(df)),
                     cols=int(df.shape[1]), parent=parent, lineage=lineage or [])
        self.datasets[name] = ds
        if name not in self.order:
            self.order.append(name)
        try:
            sqle.register_df(self.conn, name, df)
        except Exception:
            # SQL 层失败不该阻断主流程，前端仍可用其它面板
            pass
        self.touch()
        return ds

    def drop(self, name: str) -> None:
        self.datasets.pop(name, None)
        if name in self.order:
            self.order.remove(name)
        try:
            sqle.unregister(self.conn, name)
        except Exception:
            pass

    def get(self, name: str) -> Dataset:
        if name not in self.datasets:
            raise KeyError(f"数据集不存在: {name}")
        self.touch()
        return self.datasets[name]

    def active_or_first(self, name: str | None = None) -> Dataset:
        if name and name in self.datasets:
            return self.datasets[name]
        if self.order:
            return self.datasets[self.order[0]]
        raise KeyError("会话内还没有数据集")

    def summary(self) -> list[dict]:
        out = []
        for name in self.order:
            ds = self.datasets[name]
            out.append({
                "name": ds.name,
                "source": ds.source,
                "rows": ds.rows,
                "cols": ds.cols,
                "parent": ds.parent,
                "steps": len(ds.lineage),
                "created_at": ds.created_at,
            })
        return out


def _safe_name(name: str) -> str:
    """规范化数据集名。

    数据集名可以是任意字符串（Excel sheet 名常带空格、文件名带中文），
    拼进 SQL 的安全性由 sqle.quote_ident() 负责，这里不做字符限制。
    """
    return sqle.safe_table_name(name)


SESSIONS: dict[str, Session] = {}
MAX_SESSIONS = 8


def create_session() -> Session:
    if len(SESSIONS) >= MAX_SESSIONS:
        # LRU 淘汰
        oldest = min(SESSIONS.values(), key=lambda s: s.last_seen)
        SESSIONS.pop(oldest.sid, None)
    sid = uuid.uuid4().hex[:12]
    s = Session(sid=sid, conn=sqle.new_conn())
    SESSIONS[sid] = s
    return s


def get_session(sid: str) -> Session:
    if sid not in SESSIONS:
        raise KeyError(f"会话已过期或不存在: {sid}（请点左上角重新加载数据源）")
    return SESSIONS[sid]
