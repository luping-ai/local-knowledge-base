"""watchdog 事件处理器：把 OS 事件翻译成 `FileChange`。

## 为什么单独一个文件

watchdog 的 `FileSystemEventHandler` 有 5 个回调（created / modified /
deleted / moved / closed）。它们的调用顺序与平台强相关（Windows 与 Linux
不同），所以"事件 → 变更类型"的映射要集中在一处、可以单独测。

## 一个反直觉的事实：Windows 上"改名"会先报 deleted + created

`moved` 事件在 Windows 上**只对同一目录内的重命名可靠**。跨目录移动
（把文件从 `D:\\工作` 拖到 `D:\\归档`）会表现为"源 deleted + 目标 created"。

不处理的话，用户拖走一个文件会被当成"删了一个、新增了一个"——结果
是索引里多一份副本而不是移动。所以 handler 要记住"刚被删的路径"，
在紧接着的 created 里尝试配对（按 inode/大小/mtime 猜）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from localkb.watch.watcher import ChangeKind, FileChange

__all__ = ["make_handler"]


def make_handler(
    *,
    on_change: Callable[..., None],
    excludes: Optional[list[Path]] = None,
    should_ignore: Optional[Callable[[Path], bool]] = None,
):
    """构造一个 watchdog handler。

    ## 参数是函数而不是配置对象

    这样 `localkb.watch.watcher` 不需要 import watchdog，而 handler 也
    不需要知道 debouncer 的内部结构。两边只通过"一个接收 FileChange 的
    可调用对象"耦合。
    """
    from watchdog.events import FileSystemEventHandler

    ex = [str(p).replace("\\", "/").lower().rstrip("/") for p in (excludes or [])]

    def _excluded(p: Path) -> bool:
        norm = str(p).replace("\\", "/").lower()
        return any(
            e and (norm == e or norm.startswith(e + "/")) for e in ex
        )

    class _Handler(FileSystemEventHandler):
        def __init__(self) -> None:
            super().__init__()
            # 记录"最近删除的路径 → (size, mtime, 时间)"，用于跨目录移动的配对。
            # 只保留 2 秒内的记录——再久的就不可能是同一次移动。
            self._recent_deleted: dict[str, tuple[int, float, float]] = {}

        # ---------------------------------------------------------- 事件入口

        def on_created(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            if self._ignored(p) or _excluded(p):
                return
            # 先看能不能与刚删的配对成"移动"
            if self._match_move(p):
                return
            self._emit(p, ChangeKind.CREATED)

        def on_modified(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            if self._ignored(p) or _excluded(p):
                return
            self._emit(p, ChangeKind.MODIFIED)

        def on_deleted(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            if self._ignored(p) or _excluded(p):
                return
            self._remember_deleted(p)
            self._emit(p, ChangeKind.DELETED)

        def on_moved(self, event):
            if event.is_directory:
                return
            src = Path(event.src_path)
            dst = Path(event.dest_path)
            if _excluded(dst) or _excluded(src):
                return
            if self._ignored(dst) and self._ignored(src):
                return
            on_change(FileChange(path=src, kind=ChangeKind.MOVED, dest=dst))

        # -------------------------------------------------------------- 内部

        def _ignored(self, p: Path) -> bool:
            return bool(should_ignore and should_ignore(p))

        def _emit(self, p: Path, kind: str) -> None:
            on_change(FileChange(path=p, kind=kind))

        def _remember_deleted(self, p: Path) -> None:
            try:
                st = p.stat()
                self._recent_deleted[str(p).lower()] = (
                    st.st_size, st.st_mtime, time.monotonic())
            except OSError:
                self._recent_deleted[str(p).lower()] = (0, 0.0, time.monotonic())
            # 清理过期的
            now = time.monotonic()
            for k, (_, _, ts) in list(self._recent_deleted.items()):
                if now - ts > 2.0:
                    del self._recent_deleted[k]

        def _match_move(self, created: Path) -> bool:
            """尝试把"新建"识别为"从别处移来"。

            ## 怎么配对

            同一份内容移动前后：**大小与 mtime 不变**（移动不修改内容）。
            所以在 2 秒内找到了同大小同 mtime 的删除记录，就认为是移动。

            ## 为什么不用 inode

            Windows 上 `st_ino` 在某些文件系统（FAT32、网络盘）返回 0，
            不可靠。大小 + mtime 在"同一秒内移动"这个窗口里足够区分。

            ## 代价

            用户"删掉 A、立刻粘贴同名同大小的 B"会被误判成移动。
            这个误判的后果是"B 被当成 A 的新位置"——索引仍然正确指向内容，
            只是来源显示的名字是 A 的旧名。可接受。
            """
            try:
                st = created.stat()
            except OSError:
                return False
            now = time.monotonic()
            for src, (size, mtime, ts) in list(self._recent_deleted.items()):
                if now - ts > 2.0:
                    continue
                if size == st.st_size and abs(mtime - st.st_mtime) < 1.0:
                    del self._recent_deleted[src]
                    on_change(FileChange(
                        path=Path(src), kind=ChangeKind.MOVED, dest=created))
                    return True
            return False

    return _Handler()
