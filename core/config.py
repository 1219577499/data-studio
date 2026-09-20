# -*- coding: utf-8 -*-
"""路径与全局常量。

两种运行形态要区分开：
  源码运行         —— 资源和数据都在项目目录下
  PyInstaller 打包 —— 只读资源被解到 sys._MEIPASS（临时目录，退出就没了），
                      用户的数据必须放在 exe 同级目录，否则一关就丢
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # 打包后：web/ 这类只读资源在解包目录，data/ 落在 exe 旁边
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    EXE_DIR = Path(sys.executable).resolve().parent
    WEB_DIR = ROOT / "web"
    DATA_DIR = EXE_DIR / "data"
else:
    ROOT = Path(__file__).resolve().parent.parent
    EXE_DIR = ROOT
    WEB_DIR = ROOT / "web"
    DATA_DIR = ROOT / "data"

VENDOR_DIR = WEB_DIR / "vendor"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
SAMPLE_DIR = DATA_DIR / "samples"

for _d in (UPLOAD_DIR, EXPORT_DIR, SAMPLE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 单次读库的最大行数，防止误操作把几千万行拉爆内存
MAX_DB_ROWS = 500_000
# 图表返回的最大点数
MAX_CHART_POINTS = 2_000
# 相关矩阵 / 缺失矩阵预览的最大行数
MAX_MATRIX_ROWS = 5_000

SUPPORTED_FILE_SUFFIX = {
    ".csv", ".tsv", ".txt", ".xlsx", ".xls", ".xlsm", ".json", ".jsonl", ".parquet",
}
