"""`ScanJob.vectorize` 的完成判据。

## 为什么这个测试值得写

这个函数的返回值决定 UI 显示"向量已就绪"还是"未完成"。第一版用
**过程状态**（`vp.running`）判断成功，而 `running=False` 有五种可能：

  1. 正常完成
  2. 用户取消
  3. Runtime 重启
  4. 任务失败
  5. 引擎崩溃

只有第 1 种是成功。误判的后果不是崩溃，而是 UI 显示
**"向量已就绪（16000/100000）"** —— 一句话自相矛盾，用户会以为
"这个库就是没这段内容"，而不是"这段内容没被索引"。

这类"用中间状态推断终态"的缺陷不会报错、不会崩，只是悄悄让人得出
错误结论。所以用测试把判据钉死在"计数是否相等"上。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from localkb.scan.job import ScanJob
from localkb.store.db import StateDB


@dataclass
class _FakeVecProgress:
    running: bool
    embedded_chunks: int
    total_chunks: int


@dataclass
class _FakeStat:
    chunk_count: int
    embedded_count: int
    vectorize: _FakeVecProgress | None


@dataclass
class _FakeTriggerResult:
    note: str = "已触发向量化"


class _FakeConn:
    """第 1 次 call 是 `vectorize` 触发，之后都是 `kb_stat`。

    ## 为什么按调用序号区分

    `ScanJob.vectorize` 的调用序列是固定的：
      1. `trigger(conn, kb_id)` → 内部 `conn.call(lambda c: c.vectorize(...))`
      2. 轮询 `conn.call(lambda c: c.kb_stat(...))`

    假连接无法检查 lambda 的内容（那是黑盒），但知道第一次一定是触发。
    """

    def __init__(self, polls: list[_FakeStat]) -> None:
        self._polls = list(polls)
        self._calls = 0

    def call(self, fn):
        self._calls += 1
        if self._calls == 1:
            return _FakeTriggerResult()
        if not self._polls:
            return self._polls_last
        # 最后一个状态重复返回（模拟轮询）
        stat = self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]
        self._polls_last = stat
        return stat


def _job(tmp_path: Path, conn) -> ScanJob:
    db = StateDB(tmp_path / "state.db")
    return ScanJob(db, "kb_test", [], conn=conn)


def test_completed_when_counts_match(tmp_path: Path) -> None:
    """正常完成：计数相等 → True。"""
    conn = _FakeConn([_FakeStat(
        chunk_count=100, embedded_count=100,
        vectorize=_FakeVecProgress(running=False, embedded_chunks=100,
                                   total_chunks=100))])
    job = _job(tmp_path, conn)
    ok, note = job.vectorize(timeout_sec=2)
    assert ok is True, f"正常完成应返回 True，实际 {note!r}"
    assert "向量已就绪" in note
    assert "100/100" in note


def test_not_ready_when_stopped_early(tmp_path: Path) -> None:
    """中止/取消：running=False 但计数不相等 → False，且文案说明可继续。

    这是第一版会误报"已就绪"的场景。
    """
    conn = _FakeConn([_FakeStat(
        chunk_count=100_000, embedded_count=16_000,
        vectorize=_FakeVecProgress(running=False, embedded_chunks=16_000,
                                   total_chunks=100_000))])
    job = _job(tmp_path, conn)
    ok, note = job.vectorize(timeout_sec=2)

    assert ok is False, f"中止时不能报告成功，实际 note={note!r}"
    assert "16000/100000" in note, f"文案必须给出真实计数，实际 {note!r}"
    assert "未完成" in note
    # 必须告诉用户"已完成的可用"且"能继续"——否则他只会以为坏了
    assert "继续" in note or "补齐" in note, f"文案要给出下一步，实际 {note!r}"


def test_not_ready_when_vectorize_is_none(tmp_path: Path) -> None:
    """`vectorize` 字段为 None（老 Runtime / 未触发）时同样按计数判断。"""
    conn = _FakeConn([_FakeStat(
        chunk_count=50, embedded_count=10, vectorize=None)])
    job = _job(tmp_path, conn)
    ok, note = job.vectorize(timeout_sec=2)
    assert ok is False
    assert "10/50" in note


def test_cancelled_by_user_returns_false(tmp_path: Path) -> None:
    """用户点取消：必须返回 False，不能因为"任务已停"就算成功。"""
    conn = _FakeConn([_FakeStat(
        chunk_count=100, embedded_count=30,
        vectorize=_FakeVecProgress(running=True, embedded_chunks=30,
                                   total_chunks=100))])
    job = _job(tmp_path, conn)
    job.cancel()
    ok, note = job.vectorize(timeout_sec=2)
    assert ok is False
    assert "取消" in note
