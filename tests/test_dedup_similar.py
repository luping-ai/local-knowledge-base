"""相似组检测与待确认管理（方案 §12.4 / §12.5 / B3~B10）。

## 这一层最容易做错的三件事

  1. **`pending` 不能让结论不完整**（§12.4 的明确警告）
  2. **不能传空 doc_ids**（服务端语义是"全库"）
  3. **待确认状态要持久化**（B8：丢了比不提示更糟）

前两条不满足时，用户会看到"没有相似文件"——而实际上有 3 组等着确认。
这个错误**没有任何症状**：界面正常、日志正常、只是结论错了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localkb.dedup import (
    SETTING_KEY,
    SimilarGroup,
    SimilarMember,
    check_similar,
    load_pending_groups,
    similar_default_choice,
)
from localkb.store.db import LocalFile, StateDB


@pytest.fixture()
def db(tmp_path: Path) -> StateDB:
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


# ------------------------------------------------------------ 假 SDK 对象

class _FakeMember:
    def __init__(self, doc_id: str, name: str = "", hamming: int = 0) -> None:
        self.document_id = doc_id
        self.name = name
        self.hamming = hamming


class _FakeGroup:
    def __init__(self, gid: str, members: list[_FakeMember],
                 kind: str = "similar") -> None:
        self.id = gid
        self.kind = kind
        self.members = members


class _FakeCheckResult:
    def __init__(self, groups=None, pending=None, total_ms: int = 0) -> None:
        self.groups = groups or []
        self.pending = pending or []
        self.total_ms = total_ms


class _FakeConn:
    """记录被调用的参数，便于断言"没传空 doc_ids"。"""

    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list = []

    def call(self, fn):
        self.calls.append(fn)
        if self._exc is not None:
            raise self._exc
        return self._result or _FakeCheckResult()


# ------------------------------------------------------------ 空 doc_ids 必须跳过

def test_empty_doc_ids_skips_without_calling_server(db: StateDB) -> None:
    """空 doc_ids 时**不能**调服务端。

    ## 为什么这条是硬要求

    服务端语义：`doc_ids` 为空 = "该库全部文档"。增量扫描时传空会让它
    做全库重算（10 万块要几十秒），而 §12.5 明确要求"只对本次新入库
    的文档做"。

    更糟的是用户感知：他为"改了 1 个文件"等了几十秒，会以为程序很慢。
    """
    conn = _FakeConn()
    res = check_similar(conn, db, "kb1", [])
    assert not conn.calls, "空 doc_ids 不该调服务端"
    assert res.groups == []
    assert "跳过" in res.note


# ------------------------------------------------------------ pending 处理

def test_pending_marks_result_incomplete(db: StateDB) -> None:
    """返回的 pending 非空时，结果要标记为不完整并说明。

    §12.4：快路径只对已有指纹的文档分组。刚导入的文档可能还没有指纹，
    它们会出现在 pending 里——此时"没有重复"这个结论是**不完整的**。
    """
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/a.md", doc_id="d1",
                             status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/b.md", doc_id="d2",
                             status="imported"))
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [_FakeMember("d1", "a"), _FakeMember("d2", "b")])],
        pending=["d3", "d4"],
    ))
    res = check_similar(conn, db, "kb1", ["d1", "d2", "d3", "d4"])
    assert res.incomplete is True
    assert res.pending == ["d3", "d4"]
    assert "不完整" in res.note


def test_no_pending_is_complete(db: StateDB) -> None:
    conn = _FakeConn(_FakeCheckResult(pending=[]))
    res = check_similar(conn, db, "kb1", ["d1"])
    assert res.incomplete is False


# ------------------------------------------------------------ 路径补全

def test_members_get_local_path(db: StateDB) -> None:
    """成员必须补上本地路径——服务端只有 name，不知道磁盘位置。

    对话框要显示路径（用户靠它区分"桌面那份"与"备份那份"），
    "在文件夹中显示"更需要它。
    """
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/工作/报告_v2.docx",
                             doc_id="d1", status="imported", mtime=1000))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/备份/报告.docx",
                             doc_id="d2", status="imported", mtime=2000))
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [_FakeMember("d1", "报告_v2"), _FakeMember("d2", "报告")])],
    ))
    res = check_similar(conn, db, "kb1", ["d1", "d2"])
    m = res.groups[0].members
    assert m[0].path == "D:/工作/报告_v2.docx"
    assert m[0].mtime == 1000
    assert m[1].path == "D:/备份/报告.docx"


def test_group_with_single_member_dropped(db: StateDB) -> None:
    """只有一个成员的组没有意义（不该提示"1 个文件内容相似"）。"""
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [_FakeMember("d1", "a")])],
    ))
    res = check_similar(conn, db, "kb1", ["d1"])
    assert res.groups == []


def test_similarity_range_computed(db: StateDB) -> None:
    """相似度由汉明距离推算：64 位指纹，相似度 = (64-h)/64。"""
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/a", doc_id="d1",
                             status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/b", doc_id="d2",
                             status="imported"))
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [
            _FakeMember("d1", "a", hamming=2),
            _FakeMember("d2", "b", hamming=6),
        ])],
    ))
    g = check_similar(conn, db, "kb1", ["d1", "d2"]).groups[0]
    # h=2 → 96%，h=6 → 90%
    assert g.similarity_max == 96
    assert g.similarity_min == 90


# ------------------------------------------------------------ 服务不可用

def test_server_error_does_not_raise(db: StateDB) -> None:
    """检测失败不能抛——入库已完成，不该让扫描显示失败。"""
    conn = _FakeConn(exc=RuntimeError("boom"))
    res = check_similar(conn, db, "kb1", ["d1"])
    assert res.groups == []
    assert "未能完成" in res.note


# ------------------------------------------------------------ 持久化（B8）

def test_groups_persisted_and_reloadable(db: StateDB) -> None:
    """B8：组要落库，重启后能读回来。

    这是产品底线（§12.10）：待确认状态丢了比不提示更糟——用户会以为
    已经确认过了。
    """
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/a", doc_id="d1",
                             status="imported", mtime=100))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/b", doc_id="d2",
                             status="imported", mtime=200))
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [_FakeMember("d1", "a"), _FakeMember("d2", "b")])],
    ))
    check_similar(conn, db, "kb1", ["d1", "d2"])

    loaded = load_pending_groups(db, "kb1")
    assert len(loaded) == 1
    g = loaded[0]
    assert g.group_id == "g1"
    assert len(g.members) == 2
    assert {m.path for m in g.members} == {"D:/a", "D:/b"}


def test_resolved_group_not_recreated(db: StateDB) -> None:
    """已确认的组：重新扫描时不该又冒出来。

    ## 为什么这条重要

    用户点了"确定"入库某一版，下次扫描如果又把同一组写进 pending_dups，
    他会看到"确认过的又回来了"——于是不再相信确认动作有用。

    实现上：`upsert_pending_group` 对 resolved=1 的行直接跳过。
    """
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/a", doc_id="d1",
                             status="imported"))
    db.upsert_file(LocalFile(kb_id="kb1", path="D:/b", doc_id="d2",
                             status="imported"))
    conn = _FakeConn(_FakeCheckResult(
        groups=[_FakeGroup("g1", [_FakeMember("d1", "a"), _FakeMember("d2", "b")])],
    ))
    check_similar(conn, db, "kb1", ["d1", "d2"])
    db.resolve_pending_group("kb1", "g1", "D:/a")
    assert db.count_pending_groups("kb1") == 0

    # 再跑一次检测
    check_similar(conn, db, "kb1", ["d1", "d2"])
    assert db.count_pending_groups("kb1") == 0, "已确认的组不该重新出现"


def test_pending_scoped_to_kb(db: StateDB) -> None:
    """待确认状态按库隔离（跨库不该互相看到）。"""
    db.upsert_pending_group(kb_id="kb1", group_id="g1", kind="similar",
                            paths=json.dumps([{"doc_id": "d1"}, {"doc_id": "d2"}]))
    assert db.count_pending_groups("kb1") == 1
    assert db.count_pending_groups("kb2") == 0


# ------------------------------------------------------------ 默认选中策略（B9/B10）

def test_default_choice_falls_back_to_none(db: StateDB) -> None:
    """设置项非法值回退到 none（而不是静默不预选）。"""
    assert similar_default_choice(db) == "none"
    db.set_setting(SETTING_KEY, "bogus")
    assert similar_default_choice(db) == "none"
    db.set_setting(SETTING_KEY, "NEWEST")
    assert similar_default_choice(db) == "newest", "应大小写不敏感"


def test_none_means_no_preselection() -> None:
    """B9：`none` 时不预选任何一项。"""
    g = SimilarGroup(group_id="g1", members=[
        SimilarMember(doc_id="d1", name="a", mtime=100),
        SimilarMember(doc_id="d2", name="b", mtime=200),
    ])
    assert g.pick_default("none") == ""


def test_newest_picks_latest_mtime() -> None:
    """B10：`newest` 预选修改时间最新的那份。"""
    g = SimilarGroup(group_id="g1", members=[
        SimilarMember(doc_id="d1", name="旧", mtime=100),
        SimilarMember(doc_id="d2", name="新", mtime=300),
        SimilarMember(doc_id="d3", name="中", mtime=200),
    ])
    assert g.pick_default("newest") == "d2"


def test_oldest_picks_earliest_mtime() -> None:
    g = SimilarGroup(group_id="g1", members=[
        SimilarMember(doc_id="d1", name="旧", mtime=100),
        SimilarMember(doc_id="d2", name="新", mtime=300),
    ])
    assert g.pick_default("oldest") == "d1"


def test_members_without_mtime_sorted_last() -> None:
    """没有 mtime 的成员（本地找不到路径）不该被优先选中。

    它们的"新旧"未知。若把它们排在前面，`newest` 会选中一个我们
    根本不知道时间的东西——而用户看到的是"程序选了那个"。
    """
    g = SimilarGroup(group_id="g1", members=[
        SimilarMember(doc_id="d_unknown", name="?", mtime=0),
        SimilarMember(doc_id="d_new", name="新", mtime=300),
    ])
    assert g.pick_default("newest") == "d_new"
    assert g.pick_default("oldest") == "d_new", "只有一份有时间，无论哪种策略都选它"


def test_all_without_mtime_no_preselection() -> None:
    """全部没有 mtime 时不预选（无法判断新旧）。"""
    g = SimilarGroup(group_id="g1", members=[
        SimilarMember(doc_id="d1", name="a", mtime=0),
        SimilarMember(doc_id="d2", name="b", mtime=0),
    ])
    assert g.pick_default("newest") == ""
