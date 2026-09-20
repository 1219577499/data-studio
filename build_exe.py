# -*- coding: utf-8 -*-
"""把 Data Studio 打包成免安装的 exe（对方不需要装 Python）。

用法：
    python build_exe.py

产物：dist/DataStudio/ 整个文件夹，压缩后发给别人。
对方解压后双击 DataStudio.exe 即可，数据会存在 exe 同级的 data/ 目录里。

说明：
  - 用 --onedir 而不是 --onefile：单文件模式每次启动都要解压几百 MB，
    要等十几秒，而且杀毒软件更容易误报。
  - web/ 是必须一起打进去的（前端页面和 ECharts 都在里面），
    core/config.py 里对 frozen 状态做了路径适配。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

HIDDEN = [
    # uvicorn 的循环 / 协议 / 生命周期都是运行时动态导入的，PyInstaller 扫不到
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 数据库可选依赖
    "sqlalchemy",
    "pymysql",
    # pandas / openpyxl 的引擎是懒加载的
    "openpyxl",
    "openpyxl.styles",
    "pandas._libs.tslibs.base",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("  需要先装 PyInstaller：pip install pyinstaller")
        return 1

    if not (ROOT / "web" / "index.html").exists():
        print("  找不到 web/index.html，请在 data-studio 目录下运行本脚本")
        return 1

    print(f"\n  正在打包 {ROOT.name} …（首次约 1~3 分钟）\n")
    t0 = time.time()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "app.py"),
        "--name", "DataStudio",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        # 前端资源必须一起打包（Windows 上用分号分隔）
        "--add-data", f"{ROOT / 'web'}{';' if sys.platform == 'win32' else ':'}web",
    ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    cmd += ["--collect-submodules", "uvicorn"]

    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print(f"\n  打包失败，退出码 {rc}。上面的日志里有具体原因。")
        return rc

    out_dir = DIST / "DataStudio"
    exe = out_dir / ("DataStudio.exe" if sys.platform == "win32" else "DataStudio")
    if not exe.exists():
        print("  打包命令成功，但没找到可执行文件，检查 dist 目录。")
        return 1

    size = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    print(f"\n  打包完成，耗时 {time.time() - t0:.0f} 秒")
    print(f"  产物: {out_dir}")
    print(f"  体积: {size / 1048576:.0f} MB")
    print("\n  把整个 DataStudio 文件夹压缩后发给别人，")
    print("  对方解压双击 DataStudio.exe 即可，不需要装 Python。")
    print("  数据会存在 exe 同级的 data/ 目录里。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
