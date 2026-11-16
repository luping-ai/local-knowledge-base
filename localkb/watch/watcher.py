"""文件监听：watchdog 事件 → 防抖 → 稳定后的批次。

## 为什么必须防抖（不是一个可选优化）

编辑器保存一个文件会触发**多次**事件：

    1. 写临时文件 `~$doc.docx`          → created
    2. 写新内容到临时文件                → modified × N
    3. 删除原文件                        → deleted
    4. 临时文件改名为正式名              → created + moved
    5. 可能还有一次 modified             → modified

不防抖的话，一次"保存"会触发 5~10 次导入，而其中大部分时候文件还是
**半成品**——导入到一半的文件会解析失败，或更糟：导入进一个截断的版本。

方案 §7.2 定的两条规则：

  - 防抖 2 秒，按 path 合并
  - 窗口内有新事件就**重置**计时（大文件复制会持续触发 modified）

## 为什么要"等文件稳定"

防抖只能保证"两次事件间隔 > 2 秒"，不能保证"文件已经写完"。一个 500 MB
的复制过程可能持续 30 秒且事件稀疏（取决于 OS 缓冲），中间会有 2 秒的
静默期。

所以再加一层：确认文件大小与 mtime 在两次采样之间不变，才认为写完了。

## 为什么不用 watchdog 的 Observer 直接做业务

`Observer` 的回调跑在**它自己的线程**里，且会阻塞事件分发。在回调里做
重活（解析 500 页 PDF）会让后续事件堆积、丢失。所以这里只做"收集 + 防抖"，
真正的处理由消费方在自己的线程里做。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "FileChange",
    "ChangeKind",
    "ChangeDebouncer",
    "Watcher",
    "watchdog_available",
]

# 临时文件与下载中间态。这些不是"用户的文件变了"，是写文件的中间产物。
#
# 与 walker 里的同一份清单：扫描时忽略它们，监听时也要忽略。两处不一致的
# 后果是"扫描时没导、但改一下就被导进去了"（或反过来）。
_IGNORE_PREFIXES = ("~$", ".~", "~")
_IGNORE_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download", ".!ut", ".swp")


class ChangeKind:
    """变更类型。用常量而不是 Enum：要进比较与日志，字符串更省事。"""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


@dataclass
class FileChange:
    """一个待处理的变更（防抖后合并的结果）。"""

    path: Path
    kind: str
    # 重命名时的新路径（kind == MOVED 时有效）
    dest: Optional[Path] = None
    # 首次事件时间与最后一次事件时间（用于诊断"这个文件被改了多久"）
    first_seen: float = 0.0
    last_seen: float = 0.0
    # 事件次数（合并了几个原始事件）
    hits: int = 1

    def merge(self, other: FileChange) -> None:
        """把新事件合并进来。

        ## 合并规则（顺序很重要）

        | 已有 | 新来 | 合并后 | 理由 |
        | --- | --- | --- | --- |
        | created | modified | created | 新建后立刻改，仍是"新建" |
        | created | deleted | **丢弃** | 创建后又删了（临时文件），不用处理 |
        | modified | deleted | deleted | 改完又删了，按删除处理 |
        | moved | modified | moved | 改名后的写入，按改名处理 |
        | deleted | created | modified | 删了又建（保存时的常见模式），按修改处理 |
        """
        self.last_seen = max(self.last_seen, other.last_seen)
        self.hits += other.hits

        if self.kind == ChangeKind.CREATED and other.kind == ChangeKind.DELETED:
            # 标记为"消失"：调用方应丢弃，不产生任何动作。
            # 用一个特殊 kind 而不是 None，让 merge 的语义显式。
            self.kind = ChangeKind.DELETED
            self.hits = 0
            return
        if self.kind == ChangeKind.DELETED and other.kind == ChangeKind.CREATED:
            # 删除后重建 = 内容变了（Word 保存就是这条路）
            self.kind = ChangeKind.MODIFIED
            return
        if other.kind == ChangeKind.DELETED:
            self.kind = ChangeKind.DELETED
            return
        if self.kind == ChangeKind.DELETED:
            # 已经删了又来别的事件（少见），按删除保持
            return
        if other.kind == ChangeKind.MOVED:
            self.kind = ChangeKind.MOVED
            self.dest = other.dest
            return
        # created 与 moved 都是"强"类型，不被后续的 modified 覆盖。
        #
        # ## 为什么要保留 MOVED（踩过的坑）
        #
        # "改名后可能还有一次 modified"（编辑器在改名后写完缓冲）。若让
        # modified 覆盖 MOVED，改名就会被当成"内容修改"处理——结果是
        # 库里留着旧名的文档，同时新增了一份新名的（因为 src 已不存在，
        # 下游会按删除处理 src）。用户看到的是"我改个名，库里多了一篇"。
        if self.kind in (ChangeKind.CREATED, ChangeKind.MOVED):
            return
        self.kind = other.kind


def _should_ignore(p: Path) -> bool:
    name = p.name.lower()
    if name.startswith(_IGNORE_PREFIXES):
        return True
    return name.endswith(_IGNORE_SUFFIXES)


class ChangeDebouncer:
    """把原始事件收集成"稳定后的批次"。

    ## 用法

        d = ChangeDebouncer(debounce_sec=2.0)
        d.add(FileChange(path, CREATED))
        ...
        ready = d.pop_ready()      # 返回已经静默够久的变更

    ## 为什么用"pull"而不是"push 回调"

    回调会让"什么时候处理"分散在 watchdog 的线程里，而消费方（扫描 worker）
    需要在**自己的线程**里处理（它要用 SQLite 与解析库）。pull 模式让
    消费方决定节奏，也让测试可以同步驱动（`pop_ready(now=...)`）。

    ## `now` 参数为何可注入

    测试不想真的 sleep 2 秒。所有时间比较都通过 `now` 参数，默认取
    `time.monotonic()`。这是"可测试性"最省事的做法——不需要 mock time 模块。
    """

    def __init__(self, *, debounce_sec: float = 2.0) -> None:
        self._debounce = debounce_sec
        self._pending: dict[str, FileChange] = {}
        self._lock = threading.Lock()

    def add(self, change: FileChange, *, now: Optional[float] = None) -> None:
        """记录一个事件（会与已有事件合并）。"""
        now = time.monotonic() if now is None else now
        key = str(change.path).replace("\\", "/").lower()
        with self._lock:
            cur = self._pending.get(key)
            if cur is None:
                change.first_seen = change.first_seen or now
                change.last_seen = now
                self._pending[key] = change
            else:
                cur.merge(change)
                # merge 之后 last_seen 已更新（窗口重置）
                if change.last_seen:
                    cur.last_seen = max(cur.last_seen, change.last_seen)
                else:
                    cur.last_seen = now
                # created→deleted 被 merge 标记为 hits=0：直接从待处理里移除。
                # 不删的话它会一直占着，且 poll 时还要额外判断。
                if cur.hits == 0:
                    del self._pending[key]

    def pop_ready(self, *, now: Optional[float] = None) -> list[FileChange]:
        """取出已经静默够久、且文件已稳定的变更。"""
        now = time.monotonic() if now is None else now
        out: list[FileChange] = []
        with self._lock:
            for key, ch in list(self._pending.items()):
                if now - ch.last_seen < self._debounce:
                    continue
                # 文件稳定检测：删除事件不需要（文件已经不在）
                if ch.kind != ChangeKind.DELETED and not _is_stable(ch.path):
                    # 还在写：重置计时，下轮再看
                    ch.last_seen = now
                    continue
                del self._pending[key]
                out.append(ch)
        return out

    def pending_count(self, *, now: Optional[float] = None) -> int:
        with self._lock:
            return len(self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


def _is_stable(path: Path, *, sample_gap: float = 0.0) -> bool:
    """文件是否"可以处理了"（写完了，或者已经不在了）。

    ## 为什么"不存在"也算稳定（踩过的坑）

    第一版对不存在的文件返回 False，于是 `pop_ready` 会不断重置计时、
    永不取出。后果是：任何"写完后立刻被删/改名"的事件会**永久卡在
    待处理队列里**，越积越多。

    实测症状：两个纯逻辑测试（created→modified、moved→modified）取不到
    任何变更，因为它们没有真的创建文件——而真实场景里"文件不存在"
    同样会发生（临时文件、快速删除）。

    "不存在"本身是终态：调用方拿到这个变更后会发现文件不在，按删除处理
    （`scan/job.py` 的 diff 逻辑已经覆盖）。所以这里返回 True，让变更
    尽快流出去。

    ## 为什么这不是万无一失

    同一秒内写完的文件、以及"文件系统时间戳精度只有 2 秒"的情况（FAT32）
    都可能让"两次采样相同"发生在文件仍在写的时候。

    真正的保证是**解析成功**：解析器读不通就记为失败，下次扫描重试。
    这个检查只是减少"读到半个文件"的概率，不是消除。
    """
    try:
        s1 = path.stat()
    except OSError:
        # 不存在（或被删/被锁）：当作稳定，让变更流出去由下游判断
        return True
    if sample_gap > 0:
        time.sleep(sample_gap)
    try:
        s2 = path.stat()
    except OSError:
        return True
    return s1.st_size == s2.st_size and s1.st_mtime == s2.st_mtime


def watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
    except ImportError:
        return False
    return True


class Watcher:
    """watchdog 的薄封装：只负责把事件转成 `FileChange` 喂给 debouncer。

    ## 为什么不在这里做处理

    见模块头的说明：watchdog 的回调线程不能做重活。这个类的作用就是把
    "OS 事件"变成"结构化变更"，让消费方在自己的线程里 pull。
    """

    def __init__(self, *, debounce_sec: float = 2.0,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        self._debouncer = ChangeDebouncer(debounce_sec=debounce_sec)
        self._observer = None
        self._on_error = on_error

    @property
    def debouncer(self) -> ChangeDebouncer:
        return self._debouncer

    def start(self, roots: list[Path],
              excludes: Optional[list[Path]] = None) -> tuple[bool, str]:
        """开始监听。返回 `(成功, 说明)`。

        ## 为什么失败只返回说明而不抛

        监听失败（目录不存在、权限不足、数量超限）不该让整个程序不可用——
        用户仍能手工点"开始扫描"。调用方拿到说明后在界面上提示即可。

        ## 为什么每个根目录一个 watch

        `Observer.schedule` 可以对多个目录各注册一个 watch。合并成一个
        "共同祖先"会把用户的整个 D 盘纳入监听，代价极高且会收到大量无关事件。
        """
        if not watchdog_available():
            return False, "缺少 watchdog 依赖，无法自动监听文件变化。"

        try:
            from watchdog.observers import Observer

            from localkb.watch.handlers import make_handler
        except ImportError as e:
            return False, f"加载监听模块失败：{e}"

        obs = Observer()
        handler = make_handler(
            on_change=self._debouncer.add,
            excludes=[Path(p) for p in (excludes or [])],
            should_ignore=_should_ignore,
        )
        scheduled = 0
        for root in roots:
            r = Path(root)
            if not r.is_dir():
                continue
            try:
                obs.schedule(handler, str(r), recursive=True)
                scheduled += 1
            except OSError as e:
                if self._on_error is not None:
                    self._on_error(f"无法监听 {r}：{e}")
        if scheduled == 0:
            return False, "没有可监听的目录。"

        try:
            obs.start()
        except Exception as e:  # noqa: BLE001 - Observer 可能因资源耗尽失败
            return False, f"启动文件监听失败：{e}"

        self._observer = obs
        return True, f"正在监听 {scheduled} 个目录。"

    def stop(self, timeout: float = 5.0) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        except Exception:  # noqa: BLE001 - 停止失败不该卡住退出
            pass
        self._observer = None

    def pop_ready(self) -> list[FileChange]:
        return self._debouncer.pop_ready()
