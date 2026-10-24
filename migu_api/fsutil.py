# -*- coding: utf-8 -*-
"""
fsutil.py —— 文件系统小工具

存在的唯一理由：本机（WorkBuddy 沙箱）里 `Path.mkdir(exist_ok=True)`
在目标目录**已存在**时也会抛 PermissionError(EEXIST)，
所以统一用 ensure_dir()：先判断存在性，再决定要不要 mkdir。
"""
from pathlib import Path


def ensure_dir(path) -> Path:
    """确保目录存在；已存在时直接返回，不重复 mkdir。"""
    p = Path(path)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    return p


def write_text(path, content: str) -> Path:
    """写文本文件，父目录自动确保存在。"""
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(content, encoding="utf-8")
    return p
