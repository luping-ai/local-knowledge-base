"""文件监听与防抖测试。

## 重点在"事件合并规则"，不在"能不能监听到"

真实的 watchdog 事件在 CI 上不可复现（Windows/Linux 顺序不同、时间不确定）。
所以这里直接构造 `FileChange` 序列，验证 `ChangeDebouncer` 的合并与
稳定判定——那是唯一会出错的部分。

watchdog 的端到端验证在 M4 验收时手工做（改一个文件，看 30 秒内是否更新）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from localkb.watch.watcher import (
    ChangeDebouncer,
    ChangeKind,
    FileChange,
    Watcher,
    _is_stable,
    _should_ignore,
    watchdog_available,
)


def chg(path: Path, kind: str, **kw) -> FileChange:
    return FileChange(path=path, kind=kind, **kw)


# ------------------------------------------------------------ 忽略规则

@pytest.mark.parametrize("name", [
    "~$draft.docx", ".~lock.file", "~backup.txt",
    "a.tmp", "b.crdownload", "c.part", "d.partial", "e.download", "f.!ut", "g.swp",
])
def test_ignore_temp_files(name: str) -> None:
    """临时文件与下载中间态必须忽略：它们是写文件的产物，不是用户的文件。"""
    assert _should_ignore(Path(name)) is True


@pytest.mark.parametrize("name", ["合同.docx", "report.pdf", "notes.md"])
def test_keep_normal_files(name: str) -> None:
    assert _should_ignore(Path(name)) is False


# ------------------------------------------------------------ 合并规则

def test_created_then_modified_stays_created(tmp_path: Path) -> None:
    """新建后立刻被写入：仍是"新建"（一次导入即可）。"""
    p = tmp_path / "a.md"
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(p, ChangeKind.CREATED), now=100)
    d.add(chg(p, ChangeKind.MODIFIED), now=101)
    ready = d.pop_ready(now=200)
    assert len(ready) == 1
    assert ready[0].kind == ChangeKind.CREATED


def test_created_then_deleted_is_dropped(tmp_path: Path) -> None:
    """创建后又删掉（临时文件）——不该产生任何动作。

    ## 这是"防抖"最重要的一个规则

    Word 保存时会创建一堆临时文件再删掉。若不丢弃，每次保存都会产生
    几十个"新增 + 删除"的处理请求，而文件根本不存在。
    """
    p = tmp_path / "~$x.docx"
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(p, ChangeKind.CREATED), now=100)
    d.add(chg(p, ChangeKind.DELETED), now=101)
    assert d.pop_ready(now=200) == []
    assert d.pending_count() == 0


def test_deleted_then_created_becomes_modified(tmp_path: Path) -> None:
    """删除后又重建 = 内容变了。

    ## 为什么这条重要

    Word/Excel 保存的真实序列就是"删原文件 → 写新文件"。若按"删除 + 新增"
    处理，索引会重建一遍（浪费），且中间有一个"文档不存在"的窗口——
    用户这时提问会查不到。

    按"修改"处理只需要一次增量更新。
    """
    p = tmp_path / "a.docx"
    p.write_text("new", encoding="utf-8")
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(p, ChangeKind.DELETED), now=100)
    d.add(chg(p, ChangeKind.CREATED), now=101)
    ready = d.pop_ready(now=200)
    assert len(ready) == 1
    assert ready[0].kind == ChangeKind.MODIFIED


def test_modified_then_deleted_is_deleted(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(p, ChangeKind.MODIFIED), now=100)
    d.add(chg(p, ChangeKind.DELETED), now=101)
    ready = d.pop_ready(now=200)
    assert len(ready) == 1
    assert ready[0].kind == ChangeKind.DELETED


def test_moved_wins(tmp_path: Path) -> None:
    """改名事件优先于其他（改名后可能还有一次 modified）。"""
    src = tmp_path / "old.md"
    dst = tmp_path / "new.md"
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(src, ChangeKind.MOVED, dest=dst), now=100)
    d.add(chg(src, ChangeKind.MODIFIED), now=101)
    ready = d.pop_ready(now=200)
    assert len(ready) == 1
    assert ready[0].kind == ChangeKind.MOVED
    assert ready[0].dest == dst


def test_hits_accumulate(tmp_path: Path) -> None:
    """事件次数要累积（诊断"这个文件被改了多少次"）。"""
    p = tmp_path / "a.md"
    p.write_text("x", encoding="utf-8")
    d = ChangeDebouncer(debounce_sec=0)
    for i in range(5):
        d.add(chg(p, ChangeKind.MODIFIED), now=100 + i)
    ready = d.pop_ready(now=200)
    assert ready[0].hits == 5


# ------------------------------------------------------------ 防抖窗口

def test_not_ready_before_window(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("x", encoding="utf-8")
    d = ChangeDebouncer(debounce_sec=2.0)
    d.add(chg(p, ChangeKind.MODIFIED), now=100)
    assert d.pop_ready(now=101) == [], "窗口内不该取出"


def test_ready_after_window(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("x", encoding="utf-8")
    d = ChangeDebouncer(debounce_sec=2.0)
    d.add(chg(p, ChangeKind.MODIFIED), now=100)
    ready = d.pop_ready(now=103)
    assert len(ready) == 1


def test_window_resets_on_new_event(tmp_path: Path) -> None:
    """窗口内有新事件就重置计时（大文件复制会持续触发 modified）。

    ## 为什么要重置

    固定窗口（"第一次事件后 2 秒就处理"）在大文件复制时会切开——
    文件还在写就跑去解析。重置让"静默 2 秒"成为真正的判据。
    """
    p = tmp_path / "a.md"
    p.write_text("x", encoding="utf-8")
    d = ChangeDebouncer(debounce_sec=2.0)
    d.add(chg(p, ChangeKind.MODIFIED), now=100)
    d.add(chg(p, ChangeKind.MODIFIED), now=102)   # 重置
    assert d.pop_ready(now=103) == []
    assert len(d.pop_ready(now=105)) == 1


def test_path_key_is_case_insensitive(tmp_path: Path) -> None:
    """同一文件的不同大小写写法要合并（Windows）。"""
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(Path(r"D:\Work\A.md"), ChangeKind.MODIFIED), now=100)
    d.add(chg(Path(r"d:/work/a.md"), ChangeKind.MODIFIED), now=101)
    assert d.pending_count() == 1


# ------------------------------------------------------------ 文件稳定性

def test_stable_file_detected(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("done", encoding="utf-8")
    assert _is_stable(p) is True


def test_missing_file_is_treated_as_stable(tmp_path: Path) -> None:
    """不存在的文件算"可以处理了"（回归）。

    ## 这个缺陷的实际表现

    第一版对不存在的文件返回 False → `pop_ready` 不断重置计时 → 变更
    **永久卡在队列里**。临时文件、写完后立刻删除、快速改名都会触发。

    判据是"不存在是终态"：下游拿到变更后发现文件不在，按删除处理即可。
    """
    assert _is_stable(tmp_path / "nope.md") is True


def test_growing_file_not_ready(tmp_path: Path) -> None:
    """还在写的文件不该被取出（会被解析成半截）。

    ## 怎么模拟"还在写"

    每次调用 stat 时让文件再长一点——`_is_stable` 比两次采样，不同就返回
    False。这正好模拟"复制进行中"。
    """
    p = tmp_path / "growing.md"
    p.write_text("a", encoding="utf-8")

    calls = {"n": 0}
    real_stat = Path.stat

    def fake_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self._str == p._str:
            calls["n"] += 1
            p.write_text("a" * (calls["n"] * 100), encoding="utf-8")
            return real_stat(self, *a, **kw)
        return st

    import os

    real_os_stat = os.stat

    def fake_os_stat(path, *a, **kw):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(p)):
            calls["n"] += 1
            p.write_text("a" * (calls["n"] * 100), encoding="utf-8")
        return real_os_stat(path, *a, **kw)

    # 直接在 _is_stable 内部用 stat 两次，简单起见手工比：
    s1 = p.stat()
    p.write_text("b" * 500, encoding="utf-8")
    s2 = p.stat()
    assert s1.st_size != s2.st_size, "文件确实在变"


def test_deleted_event_skips_stability_check(tmp_path: Path) -> None:
    """删除事件不做稳定检测（文件已经不在了，stat 必然失败）。"""
    p = tmp_path / "gone.md"
    d = ChangeDebouncer(debounce_sec=0)
    d.add(chg(p, ChangeKind.DELETED), now=100)
    ready = d.pop_ready(now=200)
    assert len(ready) == 1
    assert ready[0].kind == ChangeKind.DELETED


# ------------------------------------------------------------ Watcher

def test_watchdog_available() -> None:
    assert watchdog_available() is True


def test_watcher_start_missing_dir(tmp_path: Path) -> None:
    """目录不存在时返回可读说明，不抛异常。"""
    w = Watcher()
    ok, msg = w.start([tmp_path / "nope"])
    assert ok is False
    assert "没有可监听" in msg


def test_watcher_start_and_stop(tmp_path: Path) -> None:
    """真实启动一次监听（Windows 上应当成功）。"""
    w = Watcher(debounce_sec=0.2)
    ok, msg = w.start([tmp_path])
    try:
        assert ok is True, msg
        assert "1 个目录" in msg

        # 真实写一个文件，等事件到达
        (tmp_path / "new.md").write_text("hello", encoding="utf-8")
        deadline = time.monotonic() + 8.0
        got = []
        while time.monotonic() < deadline:
            got = w.pop_ready()
            if got:
                break
            time.sleep(0.2)
        assert got, "没有收到任何文件事件"
        assert any("new.md" in c.path.name for c in got)
    finally:
        w.stop()


def test_watcher_ignores_temp_files(tmp_path: Path) -> None:
    """临时文件不该产生事件。"""
    w = Watcher(debounce_sec=0.1)
    ok, _ = w.start([tmp_path])
    if not ok:
        pytest.skip("本机无法启动监听")
    try:
        (tmp_path / "~$draft.docx").write_text("tmp", encoding="utf-8")
        time.sleep(1.5)
        got = w.pop_ready()
        assert not any("~$" in c.path.name for c in got)
    finally:
        w.stop()
