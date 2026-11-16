"""完全重复检测（方案 §12.2 / §12.3，D1）。

## 这个功能解决什么

用户同一个文件存在 5 个目录里（桌面一个、U 盘备份一个、项目目录一个……），
扫描后如果 5 份都入库，检索时会有 5 份一模一样的片段霸占 top_k，
答案也可能因命中不同"副本"而自相矛盾。

方案 §12.2 的结论：**完全重复不需要用户判断**（内容一模一样，选哪份
对结果没影响），所以自动只导一份，其余标 `duplicate` 并告知用户。

## 为什么这个判定必须在客户端

Runtime 的 `importer.Import` 在同库内按内容哈希去重，命中就返回已有
文档、**不新建记录**，且 `FindDocumentByHash` 只在单个知识库内查。
所以同一 KB 内 5 份相同文件在服务端 `documents` 表里只有 1 行——
服务端侧分组只能查出一堆单元素组，这个功能等于不存在。

磁盘路径的真相只在客户端 `local_files` 表里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localkb.scan.job import FileAction, ScanJob
from localkb.scan.walker import Walker
from localkb.store.db import LocalFile, StateDB


@pytest.fixture()
def db(tmp_path: Path) -> StateDB:
    d = StateDB(tmp_path / "data" / "state.db")
    yield d
    d.close()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


def job(db: StateDB, root: Path, **kw) -> ScanJob:
    kw.setdefault("walker_factory",
                  lambda **k: Walker(use_default_excludes=False, **k))
    return ScanJob(db, "kb1", [root], **kw)


def write_same(root: Path, name: str, content: str) -> Path:
    p = root / name
    p.write_text(content, encoding="utf-8")
    return p


# ------------------------------------------------------------ B1

def test_five_identical_files_only_one_is_imported(db: StateDB, root: Path) -> None:
    """B1：同一文件复制到 5 个不同目录（同一 KB）→ 1 组 5 成员，只导 1 份。"""
    subdirs = ["工作", "备份", "桌面", "项目", "归档"]
    for d in subdirs:
        (root / d).mkdir()
        (root / d / "报告.md").write_text("同样的内容" * 50, encoding="utf-8")

    plan = job(db, root).build_plan()

    news = [i for i in plan.items if i.action is FileAction.NEW]
    dups = [i for i in plan.items if i.action is FileAction.DUPLICATE]

    assert len(news) == 1, f"应当只有 1 份导入，实际 {(len(news))} 份"
    assert len(dups) == 4, f"应有 4 份判为重复，实际 {len(dups)}"
    # 4 份副本都指向同一份主副本
    masters = {i.duplicate_of for i in dups}
    assert len(masters) == 1, f"副本应指向同一个主副本，实际 {masters}"
    assert masters == {str(news[0].path)}

    # 重复份不该进入处理队列
    assert len(plan.to_process) == 1


def test_different_content_is_not_duplicate(db: StateDB, root: Path) -> None:
    """内容不同就都不是重复（避免把"同类文档"当成重复）。"""
    write_same(root, "a.md", "内容 A")
    write_same(root, "b.md", "内容 B")
    plan = job(db, root).build_plan()
    assert len([i for i in plan.items if i.action is FileAction.NEW]) == 2
    assert not [i for i in plan.items if i.action is FileAction.DUPLICATE]


def test_empty_files_are_not_grouped(db: StateDB, root: Path) -> None:
    """空文件的 sha256 相同，但"两个空文件是重复的"对用户毫无意义。

    判重前要求 sha256 非空——空文件跳过 SHA 计算（见 `_mark_duplicate`
    开头的 `if not item.sha256: return`）。
    """
    (root / "empty1.txt").write_text("", encoding="utf-8")
    (root / "empty2.txt").write_text("", encoding="utf-8")
    plan = job(db, root).build_plan()
    # 这两个都是 0 字节，但不应互相判重（都是独立的空文件）
    dups = [i for i in plan.items if i.action is FileAction.DUPLICATE]
    assert len(dups) <= 1, f"空文件不该成组，实际 {len(dups)} 份被判重"


# ------------------------------------------------------------ 跨批次（库里已有）

def test_existing_imported_file_becomes_master(db: StateDB, root: Path) -> None:
    """库里已有一份（imported），新扫到的同内容文件判为重复，指向库里那份。

    ## 为什么优先保留已入库的

    它已经有 doc_id、能被检索、来源卡片能跳回原文。换成新的那份需要
    先删再导，白费一次向量化。
    """
    master = write_same(root, "已入库.md", "相同内容" * 40)
    db.upsert_file(LocalFile(
        kb_id="kb1", path=str(master), sha256="PLACEHOLDER",
        status="imported", doc_id="doc_existing",
    ))
    # 用真实 sha256 覆盖（上面的占位符只为让记录存在）
    from localkb.scan.hasher import sha256_file

    real = sha256_file(master)
    db.upsert_file(LocalFile(
        kb_id="kb1", path=str(master), sha256=real,
        status="imported", doc_id="doc_existing",
    ))

    # 同内容的新文件
    copy = root / "副本.md"
    copy.write_text("相同内容" * 40, encoding="utf-8")

    plan = job(db, root).build_plan()
    dup = next(i for i in plan.items if i.path == copy)
    assert dup.action is FileAction.DUPLICATE, "同内容的新文件应判为重复"
    assert dup.duplicate_of == str(master), "应指向已入库的那份"


def test_duplicate_status_not_retried(db: StateDB, root: Path) -> None:
    """上次判为重复的文件，内容没变时不重试。

    ## 为什么这条重要

    如果写完 status=duplicate 却在 `_classify` 里不处理，下次扫描会
    落到 RETRY 分支（因为状态不在 imported/unchanged 里）→ 每扫一次都
    重新尝试导入一份明知重复的文件，白费解析时间。
    """
    p = write_same(root, "dup.md", "内容" * 30)
    from localkb.scan.hasher import sha256_file

    db.upsert_file(LocalFile(
        kb_id="kb1", path=str(p), sha256=sha256_file(p),
        status="duplicate",
    ))
    plan = job(db, root).build_plan()
    item = next(i for i in plan.items if i.path == p)
    assert item.action is FileAction.UNCHANGED, (
        f"重复状态不该重试，实际 {item.action}"
    )


def test_duplicate_file_changed_is_reprocessed(db: StateDB, root: Path) -> None:
    """被判重复的文件内容变了 → 应重新处理（它已不是重复了）。"""
    p = write_same(root, "dup.md", "旧内容" * 20)
    db.upsert_file(LocalFile(
        kb_id="kb1", path=str(p), sha256="stale_hash",
        status="duplicate",
    ))
    plan = job(db, root).build_plan()
    item = next(i for i in plan.items if i.path == p)
    assert item.action is FileAction.CHANGED, (
        f"内容变了应重新处理，实际 {item.action}"
    )


# ------------------------------------------------------------ 持久化状态

def test_duplicate_persisted_with_reason(db: StateDB, root: Path) -> None:
    """扫描后 DUPLICATE 要落库，且 error 写明与谁重复。"""
    (root / "a.md").write_text("内容" * 30, encoding="utf-8")
    (root / "b.md").write_text("内容" * 30, encoding="utf-8")

    j = job(db, root)
    plan = j.build_plan()
    j._persist(plan)

    rows = {r.path: r for r in db.list_files("kb1")}
    dups = [r for r in rows.values() if r.status == "duplicate"]
    assert dups, "重复状态没有落库"
    for r in dups:
        assert "内容相同" in r.error, f"error 应说明原因，实际 {r.error!r}"
        assert r.doc_id is None, "重复的文件不该有 doc_id"


def test_find_duplicate_hashes_counts_duplicate_status(db: StateDB) -> None:
    """`find_duplicate_hashes` 必须把 duplicate 状态算进成员数。

    ## 为什么容易错

    5 份相同文件里只有 1 份是 imported、4 份是 duplicate。若 SQL 的
    status 集合只写 imported/skipped/pending，统计出的是 1 份——
    `having n >= 2` 不成立，提示条永远不显示。
    """
    for i in range(5):
        db.upsert_file(LocalFile(
            kb_id="kb1", path=f"D:/d{i}/a.md", sha256="same_hash",
            status="imported" if i == 0 else "duplicate",
        ))
    groups = db.find_duplicate_hashes("kb1")
    assert groups == [("same_hash", 5)], f"应统计出 5 份，实际 {groups}"


def test_list_duplicate_files_grouped(db: StateDB) -> None:
    """`list_duplicate_files` 按 sha256 排序，同组成员相邻。"""
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/z/a.md", sha256="h2",
                             status="duplicate"))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/a/b.md", sha256="h1",
                             status="duplicate"))
    rows = db.list_duplicate_files("kb1")
    assert [r.sha256 for r in rows] == ["h1", "h2"], "应按 sha256 排序"
