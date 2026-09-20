# -*- coding: utf-8 -*-
"""一键安装依赖 —— 由 install.bat 调用。

为什么逻辑放 Python 而不是全写在 bat 里：
  bat 必须是纯 ASCII（cmd 用启动代码页解析它），中文提示只能靠 Python 输出，
  而 Python 可以自己把 stdout 切到 UTF-8，中文就能正常显示。

做的事：
  1. 在脚本目录建 .venv（不动系统环境，不需要管理员权限）
  2. 用国内镜像装 requirements.txt（装不上再退官源）
  3. 验证能不能 import 关键包，顺手跑一次自检
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"

MIRRORS = [
    ("清华 PyPI 镜像", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云镜像", "https://mirrors.aliyun.com/pypi/simple"),
    ("官方 PyPI", "https://pypi.org/simple"),
]

MIN_VERSION = (3, 9)


def say(msg: str = "", color: str = "") -> None:
    codes = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
             "cyan": "\033[36m", "dim": "\033[90m"}
    prefix = codes.get(color, "")
    suffix = "\033[0m" if prefix else ""
    print(f"{prefix}{msg}{suffix}", flush=True)


def head(title: str) -> None:
    say("")
    say(f"  {'=' * 58}")
    say(f"    {title}")
    say(f"  {'=' * 58}")


def run(cmd: list[str], **kw) -> int:
    say(f"    $ {' '.join(str(c) for c in cmd)}", "dim")
    return subprocess.call(cmd, **kw)


def venv_python() -> Path:
    return VENV / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    head("Data Studio 安装程序")

    ver = sys.version_info
    say(f"  当前 Python: {sys.executable}")
    say(f"  版本: {ver.major}.{ver.minor}.{ver.micro}")
    if (ver.major, ver.minor) < MIN_VERSION:
        say(f"  [错误] 需要 Python {MIN_VERSION[0]}.{MIN_VERSION[1]} 或更高版本。", "red")
        return 1

    # ---------------- 1. 建虚拟环境 ----------------
    head("第 1 步 / 共 3 步：创建虚拟环境 .venv")
    if venv_python().exists():
        say("  .venv 已存在，跳过创建（想重建就先删掉这个目录）", "cyan")
    else:
        say("  正在创建（约 5~15 秒）…")
        rc = run([sys.executable, "-m", "venv", str(VENV)])
        if rc != 0 or not venv_python().exists():
            say("  [错误] 创建虚拟环境失败。", "red")
            say("  常见原因：Python 安装时没勾选 pip，或者没有写入权限。", "yellow")
            say("  换个可写目录（比如 D 盘）再试一次。", "yellow")
            return 1
        say("  创建完成。", "green")

    py = str(venv_python())

    # ---------------- 2. 装依赖 ----------------
    head("第 2 步 / 共 3 步：安装依赖")
    req = ROOT / "requirements.txt"
    if not req.exists():
        say("  [错误] 找不到 requirements.txt", "red")
        return 1

    # 不升级 pip：venv 自带的版本足够用，而升级要连 pypi.org，
    # 国内网络经常卡在那一句话上（实测卡了 40 秒以上还没动静）。
    ok = False
    for name, url in MIRRORS:
        say("")
        say(f"  使用 {name} 安装（pandas + numpy 是大包，约 3~6 分钟，请耐心等待）…", "cyan")
        say("")
        rc = subprocess.call(
            [py, "-m", "pip", "install", "-r", str(req),
             "-i", url, "--timeout", "60", "--retries", "2",
             "--disable-pip-version-check", "--progress-bar", "off"],
        )
        if rc == 0:
            say(f"  {name} 安装成功。", "green")
            ok = True
            break
        say(f"  {name} 失败（退出码 {rc}），换下一个源…", "yellow")

    if not ok:
        say("  [错误] 所有镜像都装不上。", "red")
        say("  检查网络 / 代理，或手动执行：", "yellow")
        say(f"      \"{py}\" -m pip install -r requirements.txt", "yellow")
        return 1

    # ---------------- 3. 自检 ----------------
    head("第 3 步 / 共 3 步：自检")
    probe = (
        "import sys, importlib, json\n"
        "mods = ['numpy','pandas','fastapi','uvicorn','openpyxl','multipart']\n"
        "missing, vers = [], {}\n"
        "for m in mods:\n"
        "    try:\n"
        "        mod = importlib.import_module(m)\n"
        "        vers[m] = getattr(mod, '__version__', '?')\n"
        "    except Exception:\n"
        "        missing.append(m)\n"
        "print(json.dumps({'missing': missing, 'versions': vers, 'py': sys.version.split()[0]}))\n"
    )
    try:
        out = subprocess.check_output([py, "-c", probe], text=True,
                                      encoding="utf-8", errors="replace", timeout=120)
        import json
        info = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        say(f"  [错误] 自检失败：{e}", "red")
        return 1

    if info["missing"]:
        say(f"  [错误] 这些包装不上：{info['missing']}", "red")
        return 1

    say(f"  Python {info['py']}", "green")
    for m, v in info["versions"].items():
        say(f"    {m:<12} {v}", "green")

    # 真正 import 一次应用核心，防止"装上了但跑不起来"
    try:
        subprocess.check_output(
            [py, "-c", "import sys; sys.path.insert(0, r'%s'); "
                       "import app; print('OK')" % ROOT],
            text=True, encoding="utf-8", errors="replace", timeout=180,
            stderr=subprocess.STDOUT,
        )
        say("  应用核心模块加载正常。", "green")
    except subprocess.CalledProcessError as e:
        say("  [错误] 应用加载失败，输出如下：", "red")
        say((e.output or "")[-1500:], "red")
        return 1

    head("安装完成")
    say("  下一步：双击 run.bat 启动，浏览器会自动打开。", "green")
    say("")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    code = main()
    print(f"\n  耗时 {time.time() - t0:.1f} 秒。")
    sys.exit(code)
