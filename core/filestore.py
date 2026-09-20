# -*- coding: utf-8 -*-
"""本机文件浏览。

之前只能从 Path.home() 往下钻，等于把用户锁死在 C 盘。
这里补上两件事：枚举所有磁盘驱动器（含 U 盘、移动硬盘、网络驱动器），
以及桌面/下载/文档这类高频目录的快捷入口。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def list_drives() -> list[dict]:
    """枚举本机可用驱动器。

    Windows 下用 GetLogicalDrives + GetDriveType，能区分
    本地磁盘 / U盘 / 光驱 / 网络盘符；非 Windows 回退到 / 下的一级目录。
    """
    out: list[dict] = []

    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            bitmask = kernel32.GetLogicalDrives()
            type_names = {
                0: "未知", 1: "无效路径", 2: "可移动磁盘", 3: "本地磁盘",
                4: "网络驱动器", 5: "光盘", 6: "内存盘",
            }
            for i in range(26):
                if not (bitmask & (1 << i)):
                    continue
                letter = chr(65 + i)
                root = f"{letter}:\\"
                try:
                    dtype = int(kernel32.GetDriveTypeW(root))
                except Exception:
                    dtype = 0
                # 光驱没盘会卡很久，跳过空盘；网络盘也单独标记
                try:
                    total, used, free = _disk_usage(root)
                except Exception:
                    total, free = None, None
                # 探测可访问性，不可达的盘符（比如断开的网络映射）标注出来
                reachable = os.path.exists(root)
                out.append({
                    "name": f"{type_names.get(dtype, '磁盘')} ({letter}:)",
                    "letter": letter,
                    "path": root,
                    "type": "drive",
                    "drive_type": type_names.get(dtype, "磁盘"),
                    "reachable": reachable,
                    "free_bytes": free,
                    "total_bytes": total,
                })
            return out
        except Exception:
            pass

    # 非 Windows：列根目录下的一级条目
    for p in sorted(Path("/").iterdir()):
        try:
            if p.is_dir():
                out.append({"name": p.name, "path": str(p), "type": "dir", "reachable": True})
        except OSError:
            continue
    return out


def _disk_usage(root: str):
    try:
        import ctypes

        free = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free))
        return int(total.value), 0, int(free.value)
    except Exception:
        import shutil
        u = shutil.disk_usage(root)
        return u.total, 0, u.free


def quick_links() -> list[dict]:
    """桌面 / 下载 / 文档 / 工作区等高频入口。"""
    home = Path.home()
    cands = [
        ("桌面", home / "Desktop"),
        ("下载", home / "Downloads"),
        ("文档", home / "Documents"),
    ]
    out = [{"name": "用户主目录", "path": str(home), "type": "quick"}]
    for name, p in cands:
        if p.exists():
            out.append({"name": name, "path": str(p), "type": "quick"})
    # 工作区目录（当前项目根）
    here = Path.cwd()
    if str(here) != str(home):
        out.append({"name": "当前工作区", "path": str(here), "type": "quick"})
    return out


def normalize(path: str) -> Path:
    """把用户粘进来的路径整理成可用的绝对路径。"""
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        return Path.home()
    # 允许用户直接输入 "D:"、"D:\\"、"d:/data" 这类写法
    if len(p) == 2 and p[1] == ":":
        p = p + os.sep
    return Path(p).expanduser()


def browse(path: str = "", only_readable: bool = False) -> dict:
    """列目录。path 为空时返回根层（驱动器列表 + 快捷入口）。"""
    from .config import SUPPORTED_FILE_SUFFIX

    if not path:
        return {
            "level": "root",
            "cwd": "",
            "parent": "",
            "drives": list_drives(),
            "quick": quick_links(),
            "entries": [],
        }

    target = normalize(path)
    if not target.exists():
        raise FileNotFoundError(f"路径不存在: {target}")
    if target.is_file():
        target = target.parent

    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError as e:
        raise PermissionError(f"没有权限读取: {target}") from e

    for p in items:
        if p.name.startswith("."):
            continue
        try:
            is_dir = p.is_dir()
            size = None if is_dir else p.stat().st_size
        except (OSError, ValueError):
            continue
        readable = (not is_dir) and p.suffix.lower() in SUPPORTED_FILE_SUFFIX
        if only_readable and not (is_dir or readable):
            continue
        entries.append({
            "name": p.name, "path": str(p), "is_dir": is_dir,
            "size": size, "readable": readable,
            "ext": p.suffix.lower(),
        })

    # 回到上一级：已经在盘符根目录时，上一级就是驱动器列表
    parent = target.parent
    try:
        is_root = (target.resolve() == Path(target.drive + os.sep).resolve()) or \
                  str(parent) == str(target)
    except Exception:
        is_root = False

    return {
        "level": "dir",
        "cwd": str(target),
        "parent": "" if is_root else str(parent),
        "drives": list_drives(),
        "quick": quick_links(),
        "entries": entries,
    }
