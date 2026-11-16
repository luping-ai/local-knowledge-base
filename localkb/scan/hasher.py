"""sha256 计算。

## 为什么要分块读

一个 500 MB 的 PDF 整体读入会占 500 MB 内存，而扫描可能连续处理上百个
文件。分块（8 MB）让峰值内存与文件大小无关。

## 为什么要单独一个模块

有三处要用它：扫描时的变更检测、完全重复分组、重命名后的同名判定。
三处必须用同一个算法（同一个 buffer 大小也会影响速度，但不会影响结果），
散在三处就会有人用 `hashlib.sha256(f.read())` 图省事。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["sha256_file", "sha256_bytes", "DEFAULT_BUFFER"]

# 8 MB：在机械盘上顺序读的吞吐最佳区间，同时不会让 SSD 上的并发读
# 抢走太多页缓存（用户可能同时在用 Word）。
DEFAULT_BUFFER = 8 * 1024 * 1024


def sha256_file(path: Path | str, *, buffer_size: int = DEFAULT_BUFFER) -> str:
    """计算文件的 sha256（十六进制小写）。

    文件不存在或不可读时抛 OSError —— 调用方需要区分"读不了"与"内容变了"，
    吞掉异常会让一个读不了的文件被当成"内容未变"而永久跳过。
    """
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as f:
        # buffering=0 关掉 Python 层缓冲：我们自己按 8MB 读，再叠一层
        # 缓冲只是多一次拷贝。
        while True:
            chunk = f.read(buffer_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """计算内存数据的 sha256（用于测试与小文件）。"""
    return hashlib.sha256(data).hexdigest()


def file_stat(path: Path | str) -> tuple[int, int]:
    """返回 (size, mtime)。mtime 是整数秒。

    用整数秒而不是浮点：SQLite 存浮点会有精度漂移，"同一个文件两次扫描
    得到不同 mtime"会让变更检测永远认为文件变了。
    """
    st = os.stat(path)
    return st.st_size, int(st.st_mtime)
