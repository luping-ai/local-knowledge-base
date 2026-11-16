"""后台任务：把阻塞调用放到 QThread，用信号回主线程。

## 为什么所有耗时操作都要走这里

Qt 的硬约束是"UI 只能在主线程更新"。而 SDK 是同步阻塞的
（`client.stream_events` 是一个同步迭代器），一次提问会阻塞几秒到几十秒。
直接在主线程调会让窗口变成"未响应"。

## 为什么用 QThread + 信号而不是 QThreadPool + QRunnable

两个原因：

  1. **取消需要对象**。`QRunnable` 没有 `cancel()`，而扫描/提问都必须能
     取消（用户会点）。QThread 子类挂一个 `threading.Event` 就够了。
  2. **生命周期可读**。`QThread.finished` 与 `deleteLater` 的配对是 Qt 里
     最容易出错的地方，用子类能明确写出"什么时候算结束"。

## 信号的参数用 object

PySide6 的自定义类型信号需要注册，而 `ScanProgress` / SDK 的
`StreamEvent` 都不是 QObject。用 `Signal(object)` 可以传任意 Python
对象，代价是失去静态类型检查——在这个规模下值得。
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal

from localkb.runtime_conn import RuntimeConnector
from localkb.scan.job import ScanJob, ScanPhase, ScanProgress
from localkb.store.db import StateDB

__all__ = ["ScanWorker", "ConnectionProbeWorker", "CallWorker", "ChatWorker"]


class ScanWorker(QThread):
    """扫描线程：枚举 → hash → diff，上报进度。

    ## 为什么不让 ScanJob 继承 QThread

    `ScanJob` 是纯逻辑（可以用 pytest 同步跑，现在的 45 个扫描测试就是
    这么做的）。让它继承 QThread 会把 Qt 依赖带进业务逻辑，测试就必须
    开 QApplication。这一层只做"搬运"。

    ## 取消

    `cancel()` 是线程安全的（设一个 threading.Event）。**不要用
    `QThread.terminate()`** —— 它会在任意指令处杀掉线程，SQLite 事务
    可能停在 half-commit 状态。
    """

    progress = Signal(object)   # ScanProgress
    finished_ok = Signal(object)  # ScanPlan
    failed = Signal(str)          # 可读错误（含堆栈，由 UI 决定展示多少）

    def __init__(self, db: StateDB, kb_id: str, roots: list, *,
                 parse: bool = True, conn=None, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._kb_id = kb_id
        self._roots = roots
        self._cancel = threading.Event()
        self._job: Optional[ScanJob] = None
        # parse=False 时只做遍历 + diff（M0 行为）。保留这个开关是为了
        # "扫描前估算"这类只想看工作量的场景：解析是其中最慢的一步。
        self._parse = parse
        # conn 为 None 时只跑到解析（M1 行为）；非空时继续入库 + 向量化。
        self._conn = conn

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self._job = ScanJob(
                self._db, self._kb_id, self._roots,
                on_progress=self._on_progress,
                cancel=self._cancel,
                conn=self._conn,
            )
            plan = self._job.build_plan()
            if self._parse and not self._cancel.is_set():
                plan = self._job.parse_all(plan)
            if (self._conn is not None and self._parse
                    and not self._cancel.is_set()):
                plan = self._job.ingest_all(plan)
                if not self._cancel.is_set():
                    ok, note = self._job.vectorize()
                    if not ok:
                        # 向量化失败不是致命错误：关键词检索已经可用。
                        # 用 progress 通道把原因送到 UI，而不是当成扫描失败。
                        self.progress.emit(ScanProgress(
                            phase=ScanPhase.VECTORIZING, message=note))
                # 内容相似检测（方案 §12.4 / §12.5）。放在向量化之后：
                # 两者都要 CPU，串行比并行快（见 ScanJob.check_similar）。
                #
                # 用 progress 通道送出"发现 N 组相似"——它是一条提示，
                # 不是失败。真正需要用户处理的在扫描结束后由提示条呈现，
                # 不在这里弹窗（§12.5：240 个文件被弹 6 次窗，用户会关掉
                # 所有弹窗）。
                if not self._cancel.is_set():
                    try:
                        similar_note = self._job.check_similar(plan)
                    except Exception:  # noqa: BLE001 - 相似检测失败不影响扫描
                        similar_note = ""
                    if similar_note:
                        self.progress.emit(ScanProgress(
                            phase=ScanPhase.DONE, message=similar_note))
            self.finished_ok.emit(plan)
        except Exception:  # noqa: BLE001 - 线程里不能抛，必须转成信号
            self.failed.emit(traceback.format_exc())

    def _on_progress(self, p: ScanProgress) -> None:
        self.progress.emit(p)


class ConnectionProbeWorker(QThread):
    """探活 + 唤醒 Runtime，并把能力/资源状态取回来。

    ## 为什么用线程

    `ensure_alive()` 内部可能调安装器（最长等 30 秒）并轮询 20 秒。
    主线程做这个会让窗口在启动时白屏半分钟——用户会以为程序坏了。

    ## 为什么要"顺便"取能力与资源

    启动时需要这两份数据才能画状态栏与决定哪些功能可用。分两次请求
    会让状态栏先显示"未知"再跳变，而一次请求（`resources()` 里已内嵌
    `capability`）就够了。
    """

    ready = Signal(object, object)  # (WakeResult, ResourceStatus|None)
    failed = Signal(str)

    def __init__(self, conn: RuntimeConnector, *, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn

    def run(self) -> None:
        try:
            wake = self._conn.ensure_alive()
            if not wake.ok:
                self.ready.emit(wake, None)
                return
            st = self._conn.call(lambda c: c.resources())
            self.ready.emit(wake, st)
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())


class ChatWorker(QThread):
    """一次提问的完整生命周期：请求 → 流式事件 → 收尾。

    ## 为什么必须能取消

    SDK 的流式迭代器是 `ClosingIterator`：`close()` 会关闭底层 HTTP 连接，
    服务端随之停止生成。不 close 的话引擎会继续烧 CPU 到结束——用户以为
    已经取消了，实际上白等几十秒电。

    ## 为什么在这里补"来源片段"

    `KnowledgeSource` 不含片段正文（服务端缺口 S9）。补查放在 worker 里
    而不是 UI 线程，因为它是网络请求（10~30ms），主线程做会掉帧。
    """

    event = Signal(object)        # ChatEvent
    finished_turn = Signal(object)  # ChatTurn
    failed = Signal(str)

    def __init__(self, conn, *, question: str, history: list[tuple[str, str]],
                 scope, turn, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._question = question
        self._history = history
        self._scope = scope
        self._turn = turn
        self._cancelled = threading.Event()
        self._stream = None

    def cancel(self) -> None:
        """请求取消。线程安全（只设 flag + 关迭代器）。"""
        self._cancelled.set()
        # 直接 close 迭代器：这是唯一能让服务端停止生成的手段。
        # 包在 try 里是因为它可能已经在另一个线程里被关闭了。
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        from localkb.chat import (
            build_chat_request,
            dump_sources,
            fetch_snippets,
            stream_turn,
        )

        try:
            req = build_chat_request(
                question=self._question,
                history=self._history,
                scope=self._scope,
            )
            self._stream = stream_turn(self._conn, req)

            for ev in self._stream:
                if self._cancelled.is_set():
                    break
                self.event.emit(ev)
                self._absorb(ev)

            if self._cancelled.is_set():
                self._turn.error = "已停止"
                self.finished_turn.emit(self._turn)
                return

            # 补片段（只对"有来源且成功结束"的轮次做）
            if self._turn.sources and self._turn.answer:
                doc_ids = [getattr(s, "document_id", "")
                           for s in self._turn.sources]
                doc_ids = [d for d in doc_ids if d]
                self._turn.snippets = fetch_snippets(
                    self._conn, question=self._question,
                    scope=self._scope, doc_ids=doc_ids,
                )
            # 存快照给历史消息用
            self._turn.source_snapshot = dump_sources(
                self._turn.sources, self._turn.snippets)
            self.finished_turn.emit(self._turn)
        except Exception:  # noqa: BLE001 - 线程里不能抛
            self.failed.emit(traceback.format_exc())

    def _absorb(self, ev) -> None:
        """把事件累积到 turn 上（供落库与渲染来源卡片）。"""
        t = self._turn
        if ev.kind == "meta":
            t.model = ev.model
            t.degraded = ev.degraded
            if ev.knowledge is not None:
                t.sources = list(ev.knowledge.sources)
                t.used_chunks = ev.knowledge.used_chunks
                t.all_bases = ev.knowledge.all_bases
                t.base_count = ev.knowledge.base_count
                t.kb_ids = list(ev.knowledge.kb_ids)
                t.warnings.extend(ev.knowledge.warnings)
        elif ev.kind == "warning":
            t.warnings.extend(ev.warnings)
        elif ev.kind == "delta":
            t.answer += ev.text
        elif ev.kind == "done":
            t.finish_reason = ev.finish_reason
        elif ev.kind == "error":
            t.error = ev.error


class CallWorker(QThread):
    """通用的一次性调用：把一个同步函数丢到线程里跑。

    ## 什么时候用它，什么时候不用

    用它：一次性的、不产生流的 API 调用（建库、删库、列文档）。
    不用它：对话（需要逐 delta 上抛，有自己的 worker）、扫描（有进度）。

    ## 为什么 fn 接收 client 而不是 conn

    fn 拿到的是 `Client`，这样调用方写 `lambda c: c.list_kb()` 就够了。
    需要重连时的重建由 `RuntimeConnector.call` 负责——所以这里传的是
    `conn.call` 本身，而不是裸 client。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, conn: RuntimeConnector,
                 fn: Callable[[Any], Any], *, parent=None) -> None:
        super().__init__(parent)
        self._conn = conn
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._conn.call(self._fn))
        except Exception as e:  # noqa: BLE001
            # 业务错误（APIError）只给消息，不给堆栈——堆栈对用户没有意义，
            # 对排障也没用（服务端已经记了）。真正意外的异常才带堆栈。
            kind = getattr(e, "kind", None)
            if kind is not None:
                self.failed.emit(str(e))
            else:
                self.failed.emit(traceback.format_exc())
