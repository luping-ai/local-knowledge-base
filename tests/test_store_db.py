"""`state.db` 的单元测试。

## 这些测试刻意不依赖 Qt 与 Runtime

存储层是纯 SQLite。让它依赖 GUI 会让"跑一次测试要开窗口"变成常态，
而这类测试恰恰是改动最频繁、最需要快速反馈的部分。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localkb.store.db import (
    SCHEMA_VERSION,
    LocalFile,
    StateDB,
    _norm_path,
    open_state_db,
)


@pytest.fixture()
def db(tmp_path: Path) -> StateDB:
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


# ---------------------------------------------------------------- 迁移

def test_schema_version_is_set(db: StateDB) -> None:
    assert db.version == SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """重复打开不能重复建表或改版本号。"""
    p = tmp_path / "state.db"
    for _ in range(3):
        d = StateDB(p)
        assert d.version == SCHEMA_VERSION
        d.close()


def test_tables_exist(db: StateDB) -> None:
    names = {
        r["name"]
        for r in db._conn.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    for expected in (
        "scan_roots", "excludes", "local_files", "pending_dups",
        "settings", "conversations", "messages",
    ):
        assert expected in names, f"缺少表 {expected}"


def test_wal_mode(db: StateDB) -> None:
    """WAL 是"扫描时界面不卡"的前提，不能被无意改回 rollback。"""
    mode = db._conn.execute("pragma journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# ------------------------------------------------------------ scan_roots

def test_add_root_returns_id(db: StateDB) -> None:
    rid = db.add_root("kb1", r"D:\工作")
    assert rid > 0


def test_add_root_is_idempotent(db: StateDB) -> None:
    a = db.add_root("kb1", r"D:\工作")
    b = db.add_root("kb1", r"D:\工作")
    assert a == b
    assert len(db.list_roots("kb1")) == 1


def test_same_path_in_two_kbs(db: StateDB) -> None:
    """同一目录可以进两个库（方案 P8 已定案）。"""
    a = db.add_root("kb1", r"D:\工作")
    b = db.add_root("kb2", r"D:\工作")
    assert a != b
    assert len(db.list_roots("kb1")) == 1
    assert len(db.list_roots("kb2")) == 1


def test_path_normalization(db: StateDB) -> None:
    """大小写与斜杠差异不能产生两行。"""
    db.add_root("kb1", r"D:\Work\Sub")
    db.add_root("kb1", "d:/work/sub/")
    assert len(db.list_roots("kb1")) == 1


def test_remove_root(db: StateDB) -> None:
    rid = db.add_root("kb1", r"D:\工作")
    db.remove_root(rid)
    assert db.list_roots("kb1") == []


def test_enabled_only(db: StateDB) -> None:
    rid = db.add_root("kb1", r"D:\工作")
    with db.tx() as c:
        c.execute("update scan_roots set enabled=0 where id=?", (rid,))
    assert db.list_roots("kb1") == []
    assert len(db.list_roots("kb1", enabled_only=False)) == 1


# ----------------------------------------------------------- local_files

def test_upsert_insert_then_update(db: StateDB) -> None:
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h1",
                             size=10, mtime=100, status="pending"))
    f = db.get_file("kb1", r"D:\a.pdf")
    assert f is not None
    assert f.sha256 == "h1"
    assert f.status == "pending"

    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h2",
                             size=20, mtime=200, status="imported",
                             doc_id="doc1"))
    f = db.get_file("kb1", r"D:\a.pdf")
    assert f.sha256 == "h2"
    assert f.doc_id == "doc1"


def test_upsert_preserves_imported_at_when_none(db: StateDB) -> None:
    """扫描发现文件变了时不该抹掉"上次成功导入时间"。"""
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h1",
                             status="imported", imported_at=111))
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h2",
                             status="pending", imported_at=None))
    f = db.get_file("kb1", r"D:\a.pdf")
    assert f.imported_at == 111, "imported_at 被 None 覆盖了"


def test_get_missing_file_returns_none(db: StateDB) -> None:
    assert db.get_file("kb1", r"D:\nope.pdf") is None


def test_set_status(db: StateDB) -> None:
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", status="pending"))
    db.set_status("kb1", r"D:\a.pdf", "failed", "文件已损坏")
    f = db.get_file("kb1", r"D:\a.pdf")
    assert f.status == "failed"
    assert f.error == "文件已损坏"


def test_list_files_filter_by_status(db: StateDB) -> None:
    for i, st in enumerate(["pending", "imported", "failed"]):
        db.upsert_file(LocalFile(kb_id="kb1", path=f"D:\\{i}.pdf", status=st))
    assert len(db.list_files("kb1")) == 3
    assert len(db.list_files("kb1", statuses=["imported"])) == 1


def test_count_by_status(db: StateDB) -> None:
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\b.pdf", status="failed"))
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\c.pdf", status="failed"))
    assert db.count_by_status("kb1") == {"imported": 1, "failed": 2}


def test_delete_file_row(db: StateDB) -> None:
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf"))
    db.delete_file_row("kb1", r"D:\a.pdf")
    assert db.get_file("kb1", r"D:\a.pdf") is None


# ------------------------------------------------------- 完全重复分组

def test_find_duplicate_hashes(db: StateDB) -> None:
    """§12.3 的核心查询：同库内 sha256 相同的文件。"""
    for i in range(3):
        db.upsert_file(LocalFile(kb_id="kb1", path=f"D:\\copy{i}.pdf",
                                 sha256="same", status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\unique.pdf",
                             sha256="other", status="imported"))

    dups = db.find_duplicate_hashes("kb1")
    assert dups == [("same", 3)]


def test_find_duplicate_skips_empty_hash(db: StateDB) -> None:
    """尚未算 hash 的文件不能因为都是空串被分成一组。"""
    for i in range(3):
        db.upsert_file(LocalFile(kb_id="kb1", path=f"D:\\n{i}.pdf", sha256=""))
    assert db.find_duplicate_hashes("kb1") == []


def test_find_duplicate_ignores_deleted(db: StateDB) -> None:
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h",
                             status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\b.pdf", sha256="h",
                             status="deleted"))
    assert db.find_duplicate_hashes("kb1") == []


def test_find_duplicate_is_per_kb(db: StateDB) -> None:
    """跨库不分组：两个库各自入库 1 份是预期行为（B2）。"""
    db.upsert_file(LocalFile(kb_id="kb1", path=r"D:\a.pdf", sha256="h",
                             status="imported"))
    db.upsert_file(LocalFile(kb_id="kb2", path=r"D:\a.pdf", sha256="h",
                             status="imported"))
    assert db.find_duplicate_hashes("kb1") == []
    assert db.find_duplicate_hashes("kb2") == []


# -------------------------------------------------------------- settings

def test_setting_roundtrip(db: StateDB) -> None:
    assert db.get_setting("missing", "dflt") == "dflt"
    db.set_setting("k", "v1")
    assert db.get_setting("k") == "v1"
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"


# --------------------------------------------------------------- 事务

def test_tx_rolls_back_on_error(db: StateDB) -> None:
    with pytest.raises(RuntimeError), db.tx() as c:
        c.execute("insert into settings(key, value) values ('x','y')")
        raise RuntimeError("boom")
    assert db.get_setting("x") == ""


# ------------------------------------------------------------ 路径归一化

@pytest.mark.parametrize(
    "raw, expected",
    [
        (r"D:\A\B.txt", "d:/a/b.txt"),
        ("d:/a/b.txt", "d:/a/b.txt"),
        (r"D:\A\B.txt\\", "d:/a/b.txt"),
        ("D:/A/", "d:/a"),
        ("C:\\", "c:"),
    ],
)
def test_norm_path(raw: str, expected: str) -> None:
    assert _norm_path(raw) == expected


def test_norm_path_keeps_deep_paths() -> None:
    """不能把 `C:\\` 削成空串（循环条件必须保留 1 个字符）。"""
    assert _norm_path("C:\\") != ""


def test_conversation_crud(db: StateDB) -> None:
    db.create_conversation("c1", "标题", "selected", '["kb_1"]')
    convs = db.list_conversations()
    assert len(convs) == 1
    assert convs[0].kb_mode == "selected"
    assert db.get_conversation("c1") is not None


def test_conversation_scope_update(db: StateDB) -> None:
    db.create_conversation("c1")
    db.update_conversation_scope("c1", "all", '["kb_1","kb_2"]')
    c = db.get_conversation("c1")
    assert c.kb_mode == "all"
    assert c.kb_ids == '["kb_1","kb_2"]'


def test_messages_append_in_order(db: StateDB) -> None:
    db.create_conversation("c1")
    db.add_message("c1", "user", "问")
    db.add_message("c1", "assistant", "答", used_chunks=2, model="m")
    msgs = db.list_messages("c1")
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].used_chunks == 2
    assert msgs[1].model == "m"


def test_message_sources_snapshot_roundtrip(db: StateDB) -> None:
    """sources 存 JSON 快照，读回来要能解析（历史消息依赖它）。"""
    import json

    db.create_conversation("c1")
    snap = [{"name": "手册.pdf", "page_start": 3, "snippet": "整机保修两年"}]
    db.add_message("c1", "assistant", "答", sources=json.dumps(snap))
    row = db.list_messages("c1")[0]
    assert json.loads(row.sources) == snap


def test_delete_conversation_cascades_messages(db: StateDB) -> None:
    """删会话必须连带删消息。

    ## 这条测试保护的是一个静默缺陷

    SQLite **默认不启用外键约束**，所以 `on delete cascade` 在没有
    `pragma foreign_keys=on` 时完全无效。表现为：删除会话后 `messages`
    里的行仍在（孤儿数据），不报错、只是库文件慢慢变大。

    实测踩到过：`delete_conversation` 之后 `list_messages` 仍返回两条。
    """
    db.create_conversation("c1")
    db.add_message("c1", "user", "问")
    db.add_message("c1", "assistant", "答")

    db.delete_conversation("c1")

    assert db.list_conversations() == []
    assert db.list_messages("c1") == [], "级联删除没生效（外键未开启）"


def test_foreign_keys_are_enabled(db: StateDB) -> None:
    """显式断言 pragma 状态，避免将来被人误删那行。"""
    assert db._conn.execute("pragma foreign_keys").fetchone()[0] == 1


# ------------------------------------------------------------ 默认位置

def test_open_state_db_uses_given_dir(tmp_path: Path) -> None:
    db = open_state_db(tmp_path)
    assert Path(db.path).parent == tmp_path
    assert Path(db.path).name == "state.db"
    db.close()
