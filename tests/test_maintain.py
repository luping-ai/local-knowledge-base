"""增量维护测试：对账、删除、重命名。

## 哪些需要真服务

- 对账的"磁盘 → 本地表"与"服务端 → 本地表"两向：**不需要**（用假 conn）
- `delete_local_document` / `apply_rename`：需要真服务（要真删/真改）

分界的理由：前两向是纯数据比对，用假 conn 能精确控制服务端返回什么，
比真服务更容易覆盖"doc_id 悬空"这类边界。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from localkb.maintain import (
    ReconcileReport,
    apply_rename,
    delete_local_document,
    dump_actions,
    reconcile,
)
from localkb.store.db import LocalFile, StateDB


@pytest.fixture()
def db(tmp_path: Path) -> StateDB:
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


class _FakeDoc:
    def __init__(self, id: str, name: str = ""):
        self.id = id
        self.name = name


class _FakeConn:
    """假连接：`list_documents` 返回预设值，其余调用按需模拟。"""

    def __init__(self, docs=None, *, fail_list: bool = False,
                 fail_delete: bool = False):
        self._docs = docs or []
        self._fail_list = fail_list
        self._fail_delete = fail_delete
        self.deleted: list[str] = []

    def call(self, fn):
        name = getattr(getattr(fn, "__code__", None), "co_name", "")
        if name == "<lambda>":
            # 无法从 lambda 区分，靠调用顺序：第一次是 list_documents
            if self._fail_list:
                raise RuntimeError("list failed")
            return self._docs
        return self._docs

    def list_documents(self, kb_id):
        return self._docs

    def delete_document(self, kb_id, doc_id, keep_file=True):
        if self._fail_delete:
            from localai import APIError, ErrInvalidRequest

            raise APIError(ErrInvalidRequest, message="boom")
        self.deleted.append(doc_id)


# ------------------------------------------------------------ 磁盘 → 本地表

def test_missing_file_is_marked_not_deleted(db: StateDB, tmp_path: Path) -> None:
    """文件不在时只标 `missing`，**不删记录**（§7.7.1）。

    自动删的后果：用户移动一次目录就丢掉几小时的解析成果。
    """
    ghost = tmp_path / "gone.md"
    db.upsert_file(LocalFile(kb_id="kb1", path=str(ghost), sha256="h",
                             status="imported", doc_id="d1"))
    conn = _FakeConn([_FakeDoc("d1", "gone.md")])
    rep = reconcile(db, "kb1", conn)

    assert len(rep.missing) == 1
    row = db.get_file("kb1", str(ghost))
    assert row is not None, "记录被删了"
    assert row.status == "missing"


def test_existing_file_not_marked_missing(db: StateDB, tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d1"))
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d1", "a.md")]))
    assert rep.missing == []


# --------------------------------------------------- 本地表 → 服务端

def test_orphaned_doc_id_is_cleared(db: StateDB, tmp_path: Path) -> None:
    """服务端没有该文档时，清空 doc_id 让它下次重导。

    ## 为什么要清空

    留着悬空 id 的后果：下次改文件会 `ingest_chunks(update_document_id=悬空id)`
    → "文档不存在" → 这一篇**永远导入不了**。清空后它被当作新文件，
    虽然多解析一次，但至少能恢复。
    """
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d_gone"))

    rep = reconcile(db, "kb1", _FakeConn([]))   # 服务端空

    assert len(rep.orphaned) == 1
    assert rep.orphaned[0].doc_id == "d_gone"
    row = db.get_file("kb1", str(f))
    assert row.doc_id is None, "悬空 doc_id 没被清空"
    assert row.status == "orphaned"


def test_valid_doc_id_untouched(db: StateDB, tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d1"))
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d1", "a.md")]))
    assert rep.orphaned == []
    assert db.get_file("kb1", str(f)).doc_id == "d1"


# --------------------------------------------------- 服务端 → 本地表

def test_unowned_doc_reported_not_touched(db: StateDB, tmp_path: Path) -> None:
    """服务端有、本机没映射：只提示，不删除、不改动。

    ## 为什么只提示

    Runtime 是共享的。文档可能是管理后台、CLI、或未来的第二台桌面端导入的。
    删掉别人导的内容是越权，而且不可恢复（我们不知道那些文件的路径）。
    """
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d_remote", "别人的.pdf")]))
    assert len(rep.unowned) == 1
    assert rep.unowned[0].doc_id == "d_remote"
    assert db.list_files("kb1") == [], "不该凭空创建本地行"


# --------------------------------------------------- 文件名同步

def test_rename_needed_detected(db: StateDB, tmp_path: Path) -> None:
    """本地文件名与服务端显示名不一致时提示同步。"""
    f = tmp_path / "新名.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d1",
                             name_sent="新名.md"))
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d1", "旧名.md")]))
    assert len(rep.rename_needed) == 1
    assert "旧名.md" in rep.rename_needed[0].detail


def test_rename_not_flagged_for_legacy_rows(db: StateDB, tmp_path: Path) -> None:
    """`name_sent` 为空（M2 之前的旧数据）不提示改名。

    那些行的名字本来就没提交过，无法判断"是改名了还是从来不一致"。
    """
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d1", name_sent=""))
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d1", "完全不同的名字.md")]))
    assert rep.rename_needed == []


def test_names_match_no_rename(db: StateDB, tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id="kb1", path=str(f), sha256="h",
                             status="imported", doc_id="d1", name_sent="a.md"))
    rep = reconcile(db, "kb1", _FakeConn([_FakeDoc("d1", "a.md")]))
    assert rep.rename_needed == []


# --------------------------------------------------- 报告

def test_report_summary() -> None:
    r = ReconcileReport()
    assert r.clean is True
    assert r.summary() == "一致"


def test_report_summary_with_issues() -> None:
    from localkb.maintain import ReconcileAction

    r = ReconcileReport(
        missing=[ReconcileAction(kind="gone")],
        orphaned=[ReconcileAction(kind="orphaned")],
    )
    assert r.clean is False
    s = r.summary()
    assert "源文件已不在" in s
    assert "服务端已不存在" in s


def test_dump_actions_is_json() -> None:
    from localkb.maintain import ReconcileAction

    out = dump_actions([ReconcileAction(kind="gone", path="D:/a.md",
                                        doc_id="d1", detail="x")])
    data = json.loads(out)
    assert data[0]["kind"] == "gone"
    assert data[0]["path"] == "D:/a.md"


def test_reconcile_handles_list_failure(db: StateDB, tmp_path: Path) -> None:
    """读服务端列表失败时，磁盘向的对账仍要完成。"""
    ghost = tmp_path / "gone.md"
    db.upsert_file(LocalFile(kb_id="kb1", path=str(ghost), sha256="h",
                             status="imported"))
    rep = reconcile(db, "kb1", _FakeConn(fail_list=True))
    assert len(rep.missing) == 1, "磁盘向没完成"
    assert rep.errors, "列表失败要记一条"


# ------------------------------------------------------------ 真服务

@pytest.fixture()
def live_conn():
    from localkb.runtime_conn import RuntimeConnector

    conn = RuntimeConnector()
    if not conn.alive():
        pytest.skip("本机 LocalAIHub 未运行")
    return conn


@pytest.fixture()
def temp_kb(live_conn):
    name = f"localkb-maint-{uuid.uuid4().hex[:8]}"
    kb = live_conn.call(lambda c: c.create_kb(name))
    try:
        yield kb.id
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            live_conn.call(lambda c: c.delete_kb(kb.id))


def _ingest_file(live_conn, kb_id: str, path: Path, name: str = "") -> str:
    from localkb.chunk.splitter import split
    from localkb.ingest import build_request, ingest
    from localkb.parse.registry import parse
    from localkb.scan.hasher import file_stat, sha256_file

    doc = parse(path)
    chunks, chunker = split(doc)
    digest = sha256_file(path)
    size, mtime = file_stat(path)
    req = build_request(path=path, name=name or path.name, file_hash=digest,
                        chunks=chunks, chunker=chunker, file_size=size,
                        modified_at=mtime)
    ok, fail = ingest(live_conn, kb_id, req)
    assert fail is None, fail.reason
    return ok.document_id


def test_delete_marks_excluded_not_removed(db: StateDB, live_conn, temp_kb,
                                           tmp_path) -> None:
    """删除要留一条 `excluded` 标记，**不能只删记录**。

    ## 这个缺陷的实际表现

    早期实现里"从库里移除"= 删掉 `local_files` 行，而文件还在磁盘上
    → 下次扫描按"新增"又导了一遍。用户看到"我删了它，它又回来了"，
    只会以为删除功能坏了。

    实测踩到：A5 验收时删除后再扫描，文档数从 0 变回 1。
    """
    f = tmp_path / "keep-excluded.md"
    f.write_text("# 标题\n\n内容。", encoding="utf-8")
    doc_id = _ingest_file(live_conn, temp_kb, f)
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(f), doc_id=doc_id,
                             status="imported"))

    ok, _ = delete_local_document(
        db, temp_kb, live_conn, path=str(f), doc_id=doc_id, remove_file=False)
    assert ok is True

    row = db.get_file(temp_kb, str(f))
    assert row is not None, "本地行被删掉了（应该保留 excluded 标记）"
    assert row.status == "excluded"
    assert row.doc_id is None


def test_delete_without_file(db: StateDB, live_conn, temp_kb, tmp_path) -> None:
    """只移出知识库、文件保留（默认行为）。"""
    f = tmp_path / "a.md"
    f.write_text("# 标题\n\n内容。", encoding="utf-8")
    doc_id = _ingest_file(live_conn, temp_kb, f)
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(f), doc_id=doc_id,
                             status="imported"))

    ok, note = delete_local_document(
        db, temp_kb, live_conn, path=str(f), doc_id=doc_id, remove_file=False)

    assert ok is True
    assert "文件保留" in note
    assert f.exists(), "文件不该被删"
    # 本地行保留为 excluded（不是删除，见 test_delete_marks_excluded_not_removed）
    row = db.get_file(temp_kb, str(f))
    assert row is not None and row.status == "excluded"
    docs = live_conn.call(lambda c: c.list_documents(temp_kb))
    assert not any(d.id == doc_id for d in docs)


def test_delete_with_recycle(db: StateDB, live_conn, temp_kb, tmp_path) -> None:
    """勾选"同时删文件"时只移到回收站。"""
    f = tmp_path / "b.md"
    f.write_text("# 标题\n\n内容。", encoding="utf-8")
    doc_id = _ingest_file(live_conn, temp_kb, f)
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(f), doc_id=doc_id,
                             status="imported"))

    ok, note = delete_local_document(
        db, temp_kb, live_conn, path=str(f), doc_id=doc_id, remove_file=True)

    assert ok is True
    assert not f.exists(), "文件应已被移走"
    assert "回收站" in note
    row = db.get_file(temp_kb, str(f))
    assert row is not None and row.status == "excluded"


def test_delete_missing_doc_id_still_cleans_local(
    db: StateDB, live_conn, temp_kb, tmp_path
) -> None:
    """服务端已没有该文档时，本地行仍要清理（不能卡住）。"""
    f = tmp_path / "c.md"
    f.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(f), doc_id="d_nonexistent",
                             status="imported"))

    ok, note = delete_local_document(
        db, temp_kb, live_conn, path=str(f), doc_id="d_nonexistent")

    assert ok is True, note
    row = db.get_file(temp_kb, str(f))
    assert row is not None and row.status == "excluded"


def test_rename_syncs_server_name(db: StateDB, live_conn, temp_kb, tmp_path) -> None:
    """A20 的验收：改名后服务端 `name` 真的变了（依赖服务端 S2）。"""
    old = tmp_path / "旧名.md"
    old.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    doc_id = _ingest_file(live_conn, temp_kb, old, name="旧名.md")
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(old), doc_id=doc_id,
                             status="imported", name_sent="旧名.md"))

    new = tmp_path / "新名.md"
    old.rename(new)

    ok, note = apply_rename(db, temp_kb, live_conn,
                            old_path=str(old), new_path=str(new))
    assert ok is True, note

    docs = live_conn.call(lambda c: c.list_documents(temp_kb))
    doc = next(d for d in docs if d.id == doc_id)
    assert doc.name == "新名.md", f"服务端名字没更新：{doc.name}"

    row = db.get_file(temp_kb, str(new))
    assert row is not None
    assert row.name_sent == "新名.md"


def test_rename_without_doc_id_is_noop(db: StateDB, live_conn, temp_kb,
                                       tmp_path) -> None:
    """未入库的文件改名：不需要同步，直接告知。"""
    old = tmp_path / "x.md"
    old.write_text("x", encoding="utf-8")
    db.upsert_file(LocalFile(kb_id=temp_kb, path=str(old), doc_id=None,
                             status="pending"))
    new = tmp_path / "y.md"
    old.rename(new)

    ok, note = apply_rename(db, temp_kb, live_conn,
                            old_path=str(old), new_path=str(new))
    assert ok is False
    assert "还没有入库" in note
