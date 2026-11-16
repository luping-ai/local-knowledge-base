"""`ScanJob` 的单元测试（不依赖 Qt，同步执行）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from localkb.scan.job import FileAction, ScanJob, ScanPhase
from localkb.scan.walker import Walker
from localkb.store.db import LocalFile, StateDB


@pytest.fixture()
def db(tmp_path: Path) -> StateDB:
    d = StateDB(tmp_path / "data" / "state.db")
    yield d
    d.close()


@pytest.fixture()
def docs(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("# a", encoding="utf-8")
    (d / "b.txt").write_text("b", encoding="utf-8")
    return d


def job(db: StateDB, docs: Path, **kw) -> ScanJob:
    """测试用 ScanJob：关掉内置排除项（tmp_path 在 AppData\\Local\\Temp 下）。"""
    kw.setdefault("walker_factory", lambda **k: Walker(use_default_excludes=False, **k))
    return ScanJob(db, "kb1", [docs], **kw)


# ------------------------------------------------------------ 基本分类

def test_new_files_are_detected(db: StateDB, docs: Path) -> None:
    plan = job(db, docs).build_plan()
    assert {i.path.name for i in plan.to_process} == {"a.md", "b.txt"}
    assert all(i.action is FileAction.NEW for i in plan.items)


def test_unchanged_files_are_skipped(db: StateDB, docs: Path) -> None:
    """第一次扫描入库，第二次应该全部 unchanged。"""
    plan1 = job(db, docs).build_plan()
    for item in plan1.items:
        db.upsert_file(LocalFile(
            kb_id="kb1", path=str(item.path), sha256=item.sha256,
            size=item.size, mtime=item.mtime, status="imported",
            doc_id=f"doc-{item.path.name}",
        ))

    plan2 = job(db, docs).build_plan()
    assert plan2.to_process == []
    assert {i.path.name for i in plan2.unchanged} == {"a.md", "b.txt"}


def test_changed_file_is_detected(db: StateDB, docs: Path) -> None:
    plan1 = job(db, docs).build_plan()
    for item in plan1.items:
        db.upsert_file(LocalFile(
            kb_id="kb1", path=str(item.path), sha256=item.sha256,
            status="imported", doc_id=f"doc-{item.path.name}",
        ))

    (docs / "a.md").write_text("# a changed", encoding="utf-8")
    plan2 = job(db, docs).build_plan()

    changed = [i for i in plan2.items if i.action is FileAction.CHANGED]
    assert [i.path.name for i in changed] == ["a.md"]
    assert changed[0].doc_id == "doc-a.md", "变更时必须带上旧 doc_id（否则会新建重复文档）"

    unchanged = [i.path.name for i in plan2.items if i.action is FileAction.UNCHANGED]
    assert unchanged == ["b.txt"]


def test_failed_file_is_retried(db: StateDB, docs: Path) -> None:
    """上次失败的文件，即使内容未变也要重试。

    否则用户修好权限/关掉 Word 之后，永远等不到第二次机会。
    """
    plan1 = job(db, docs).build_plan()
    for item in plan1.items:
        db.upsert_file(LocalFile(
            kb_id="kb1", path=str(item.path), sha256=item.sha256,
            status="failed", error="文件已损坏",
        ))
    plan2 = job(db, docs).build_plan()
    assert {i.path.name for i in plan2.to_process} == {"a.md", "b.txt"}
    assert all(i.action is FileAction.RETRY for i in plan2.to_process)


@pytest.mark.parametrize("status", ["skipped", "locked", "too_long", "cloud_only", "too_large"])
def test_various_skip_states_are_retried(db: StateDB, docs: Path, status: str) -> None:
    plan1 = job(db, docs).build_plan()
    for item in plan1.items:
        db.upsert_file(LocalFile(kb_id="kb1", path=str(item.path),
                                 sha256=item.sha256, status=status))
    plan2 = job(db, docs).build_plan()
    assert all(i.action is FileAction.RETRY for i in plan2.to_process)


def test_deleted_local_row_is_not_missing(db: StateDB, docs: Path) -> None:
    """已标记 deleted 的行不该再出现在 missing 里。"""
    plan1 = job(db, docs).build_plan()
    for item in plan1.items:
        db.upsert_file(LocalFile(kb_id="kb1", path=str(item.path),
                                 sha256=item.sha256, status="deleted"))
    plan2 = job(db, docs).build_plan()
    assert plan2.missing == []


def test_excluded_file_not_reimported(db: StateDB, docs: Path) -> None:
    """回归：用户排除的文件，下次扫描不能再导进来。

    ## 这个缺陷的实际表现

    用户点"从知识库移除"后，文件还在磁盘上，扫描又把它当新增导入。
    界面上的表现是"我删了它，它又回来了"。

    判据是 `local_files.status == 'excluded'`：扫描时直接跳过，
    **内容变了也不导**（用户可能只是编辑了这个他不想入库的文件）。
    """
    job(db, docs).build_plan()
    # 模拟"用户排除了 a.md"
    from localkb.store.db import LocalFile

    db.upsert_file(LocalFile(kb_id="kb1", path=str(docs / "a.md"),
                             status="excluded", sha256=""))

    plan2 = job(db, docs).build_plan()
    a_item = next(i for i in plan2.items if i.path.name == "a.md")
    assert a_item.action is FileAction.UNCHANGED, \
        f"被排除的文件又进了处理队列：{a_item.action}"
    assert all(i.path.name != "a.md" for i in plan2.to_process)


def test_excluded_file_not_reimported_even_after_edit(db: StateDB,
                                                      docs: Path) -> None:
    """内容变了也不导（用户明确说过不要它）。"""
    from localkb.store.db import LocalFile

    db.upsert_file(LocalFile(kb_id="kb1", path=str(docs / "a.md"),
                             status="excluded", sha256="old-hash"))
    (docs / "a.md").write_text("# a 改过了\n\n新内容。", encoding="utf-8")

    plan = job(db, docs).build_plan()
    assert all(i.path.name != "a.md" for i in plan.to_process)


def test_excluded_status_not_overwritten_by_persist(db: StateDB,
                                                    docs: Path) -> None:
    """扫描落库时不能把 excluded 改回 pending（否则下轮又会被导入）。"""
    from localkb.store.db import LocalFile

    db.upsert_file(LocalFile(kb_id="kb1", path=str(docs / "a.md"),
                             status="excluded", sha256=""))
    plan = job(db, docs).build_plan()
    job(db, docs).parse_all(plan)

    row = db.get_file("kb1", str(docs / "a.md"))
    assert row.status == "excluded", f"状态被覆盖成 {row.status}"


# ------------------------------------------------------------ missing 检测

def test_missing_file_is_reported(db: StateDB, docs: Path) -> None:
    """本地表有、磁盘上没有 → 进 missing，不自动删（§7.7.1）。"""
    ghost = docs / "ghost.md"
    db.upsert_file(LocalFile(kb_id="kb1", path=str(ghost), sha256="h",
                             status="imported", doc_id="doc-ghost"))
    plan = job(db, docs).build_plan()
    assert [m.path for m in plan.missing] == [str(ghost)]


def test_missing_not_reported_when_file_exists_but_out_of_scope(
    db: StateDB, docs: Path, tmp_path: Path
) -> None:
    """文件还在、只是不在当前扫描根下 → 不算 missing。

    这是"用户移除了一个扫描目录"的场景，与该管的 missing（文件真的没了）
    要区分开：前者只是范围变化，不需要提示用户。
    """
    other = tmp_path / "other"
    other.mkdir()
    f = other / "outside.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="doc-x"))

    plan = job(db, docs).build_plan()
    assert plan.missing == []


def test_missing_can_be_cleaned_after_delete(db: StateDB, docs: Path) -> None:
    ghost = docs / "ghost.md"
    db.upsert_file(LocalFile(kb_id="kb1", path=str(ghost), sha256="h",
                             status="imported"))
    job(db, docs).build_plan()
    db.set_status("kb1", str(ghost), "deleted")
    plan = job(db, docs).build_plan()
    assert plan.missing == []


# ------------------------------------------------------------ 进度上报

def test_progress_reaches_done(db: StateDB, docs: Path) -> None:
    events = []
    job(db, docs, on_progress=events.append).build_plan()
    assert events, "没有上报任何进度"
    assert events[0].phase is ScanPhase.WALKING
    assert events[-1].phase is ScanPhase.DONE
    assert events[-1].done == events[-1].total


def test_progress_phases_order(db: StateDB, docs: Path) -> None:
    events = []
    job(db, docs, on_progress=events.append).build_plan()
    phases = [e.phase for e in events]
    assert phases[0] is ScanPhase.WALKING
    assert ScanPhase.HASHING in phases
    assert phases[-1] is ScanPhase.DONE


def test_walking_phase_has_no_total(db: StateDB, docs: Path) -> None:
    """枚举阶段分母未知，UI 应显示"已发现 N 个"而不是百分比。"""
    events = []
    job(db, docs, on_progress=events.append).build_plan()
    walking = [e for e in events if e.phase is ScanPhase.WALKING]
    assert all(e.total == 0 for e in walking)


def test_percent_never_exceeds_100(db: StateDB, docs: Path) -> None:
    events = []
    job(db, docs, on_progress=events.append).build_plan()
    assert all(0 <= e.percent <= 100 for e in events)


def test_summary_mentions_counts(db: StateDB, docs: Path) -> None:
    events = []
    job(db, docs, on_progress=events.append).build_plan()
    assert "新增 2" in events[-1].message


# ------------------------------------------------------------ 取消

def test_cancel_before_start_does_nothing(db: StateDB, docs: Path) -> None:
    ev = threading.Event()
    ev.set()
    plan = job(db, docs, cancel=ev).build_plan()
    assert plan.items == []


def test_cancel_midway_keeps_partial_plan(db: StateDB, docs: Path) -> None:
    """取消后已处理的条目保留——否则用户取消一次要重扫全部。"""
    for i in range(20):
        (docs / f"f{i}.md").write_text(f"x{i}", encoding="utf-8")

    ev = threading.Event()
    seen: list[ScanPhase] = []

    def on_progress(p):
        seen.append(p.phase)
        if p.phase is ScanPhase.HASHING and p.done >= 5:
            ev.set()

    plan = job(db, docs, cancel=ev, on_progress=on_progress).build_plan()
    assert 0 < len(plan.items) < 22
    assert ScanPhase.CANCELLED in seen


# ------------------------------------------------------------ 跳过原因

def test_parse_persists_pending_status(db: StateDB, docs: Path) -> None:
    """解析成功的文件必须落库为 pending，否则下次会重复全量解析。

    ## 为什么是 pending 而不是 imported

    M1 阶段还没有真正提交给 Runtime。若写成 imported，下一次 diff 会
    认为"已经在库里"，于是一篇从未入库的文档被永久跳过——用户会看到
    知识库页显示 N 篇文档，但提问时一条都检索不到。
    """
    plan = job(db, docs).build_plan()
    job(db, docs).parse_all(plan)
    assert all(i.chunk_count > 0 for i in plan.items), \
        f"有文件切不出块：{[(i.path.name, i.parse_error) for i in plan.items]}"

    rows = {Path(r.path).name: r for r in db.list_files("kb1")}
    assert set(rows) == {"a.md", "b.txt"}
    assert all(r.status == "pending" for r in rows.values())
    assert all(r.error == "" for r in rows.values())


def test_parse_failure_persisted_as_failed(db: StateDB, docs: Path) -> None:
    """解析失败要落库并把原因写进 error（供 UI 展示与下次重试）。"""
    (docs / "bad.md").write_bytes(b"")   # 空文件 → 解析失败
    plan = job(db, docs).build_plan()
    plan = job(db, docs).parse_all(plan)

    rows = {Path(r.path).name: r for r in db.list_files("kb1")}
    bad = rows.get("bad.md")
    if bad is not None:
        assert bad.status == "failed"
        assert bad.error, "失败必须有可读原因"


def test_second_scan_skips_unchanged(db: StateDB, docs: Path) -> None:
    """第二次扫描时内容未变的文件应全部 unchanged（不重复解析）。"""
    plan = job(db, docs).build_plan()
    job(db, docs).parse_all(plan)

    plan2 = job(db, docs).build_plan()
    assert plan2.to_process == [], \
        f"第二次扫描仍要处理：{[i.path.name for i in plan2.to_process]}"
    assert len(plan2.unchanged) == 2


def test_unchanged_rows_not_rewritten(db: StateDB, docs: Path) -> None:
    """unchanged 的行不该被重写（要保留 imported_at 等历史）。

    ## 构造

    先落一条"已导入"的行（sha256 与磁盘一致），再跑一次解析落库，
    然后检查那行的 `imported_at` 没被抹掉。
    """
    from localkb.scan.hasher import sha256_file

    a = docs / "a.md"
    digest = sha256_file(a)
    db.upsert_file(LocalFile(kb_id="kb1", path=str(a), sha256=digest,
                             status="imported", imported_at=999,
                             doc_id="doc-a"))
    plan = job(db, docs).build_plan()
    a_item = next(i for i in plan.items if i.path.name == "a.md")
    assert a_item.action is FileAction.UNCHANGED

    job(db, docs).parse_all(plan)
    row = db.get_file("kb1", str(a))
    assert row is not None
    assert row.imported_at == 999, "unchanged 的行被重写了，历史丢了"
    assert row.doc_id == "doc-a"


def test_skipped_files_are_counted(db: StateDB, tmp_path: Path) -> None:
    d = tmp_path / "docs2"
    d.mkdir()
    (d / "ok.md").write_text("x", encoding="utf-8")
    (d / "skip.exe").write_bytes(b"MZ")
    plan = job(db, d).build_plan()
    assert plan.walk is not None
    # .exe 是静默忽略（不在 skipped、不在 files）
    assert plan.walk.skipped == []
    assert [i.path.name for i in plan.items] == ["ok.md"]


def test_file_deleted_between_walk_and_hash(db: StateDB, docs: Path, monkeypatch) -> None:
    """遍历后、算 hash 前文件被删：记 skipped 而不是整体失败。"""
    import localkb.scan.job as job_mod

    real = job_mod.sha256_file

    def flaky(p, **kw):
        if Path(p).name == "a.md":
            raise FileNotFoundError(2, "gone")
        return real(p, **kw)

    monkeypatch.setattr(job_mod, "sha256_file", flaky)
    plan = job(db, docs).build_plan()
    assert [i.path.name for i in plan.items] == ["b.txt"]
    assert any(p.name == "a.md" for p, _ in plan.walk.skipped)
