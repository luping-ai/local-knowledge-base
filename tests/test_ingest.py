"""入库层的单元测试。

## 哪些需要真服务，哪些不需要

- `build_request` / `classify_error` / 块数上限：**纯逻辑**，不需要服务
- `ingest` / `vectorize`：需要真服务，用 `skip` 保护

分界的理由：契约映射（本地 `Chunk` → SDK `ChunkInput`）最容易出错，且
完全不依赖网络。把它测透了，真服务上的失败就只剩"服务端行为"这一类，
排查范围小得多。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localkb.chunk.splitter import Chunk, ChunkerInfo
from localkb.ingest import (
    MAX_CHUNKS_PER_DOC,
    IngestFailure,
    build_request,
    classify_error,
)


def chunk(i: int = 0, text: str = "内容") -> Chunk:
    return Chunk(
        ordinal=i, text=text, page_start=1, page_end=1,
        start_rune=0, rune_count=len(text),
        heading_path="第一章 > 1.1", chunk_type="paragraph",
    )


def info() -> ChunkerInfo:
    return ChunkerInfo(name="localkb-py", version="1.0.0",
                       target_chars=450, max_chars=600, overlap_chars=54)


# ------------------------------------------------------------ 请求组装

def test_build_request_maps_all_fields() -> None:
    """本地 Chunk 的每个字段都要进 SDK 的 ChunkInput。

    漏一个字段的表现是"检索结果缺页码/缺标题路径"，而不会报错——
    所以用逐字段断言，不用"能提交成功"来判。
    """
    req = build_request(
        path=Path("D:/工作/合同.pdf"), name="合同.pdf", file_hash="abc123",
        chunks=[chunk(0, "第一块")], chunker=info(),
        file_size=2048, modified_at=1700000000,
    )
    assert req.name == "合同.pdf"
    assert req.file_hash == "abc123"
    assert req.file_type == "pdf"
    assert req.file_size == 2048
    assert req.modified_at == 1700000000
    # 完整路径进 source_label（只存不解析，§6.3 允许）
    assert req.source_label == "D:\\工作\\合同.pdf" or \
        req.source_label == "D:/工作/合同.pdf"

    wire = req.to_wire()
    c = wire["chunks"][0]
    assert c["ordinal"] == 0
    assert c["text"] == "第一块"
    assert c["page_start"] == 1
    assert c["page_end"] == 1
    assert c["heading_path"] == "第一章 > 1.1"
    assert c["chunk_type"] == "paragraph"


def test_build_request_maps_chunker() -> None:
    req = build_request(path=Path("a.md"), name="a.md", file_hash="h",
                        chunks=[chunk()], chunker=info())
    w = req.to_wire()["chunker"]
    assert w["name"] == "localkb-py"
    assert w["version"] == "1.0.0"
    assert w["target_chars"] == 450
    assert w["max_chars"] == 600
    assert w["overlap_chars"] == 54


def test_build_request_file_type_lowercase() -> None:
    req = build_request(path=Path("A.PDF"), name="A.PDF", file_hash="h",
                        chunks=[chunk()], chunker=info())
    assert req.file_type == "pdf"


def test_build_request_no_extension() -> None:
    """无扩展名的文件不该崩（file_type 为空是允许的）。"""
    req = build_request(path=Path("README"), name="README", file_hash="h",
                        chunks=[chunk()], chunker=info())
    assert req.file_type == ""


def test_build_request_update_document_id() -> None:
    req = build_request(path=Path("a.md"), name="a.md", file_hash="h",
                        chunks=[chunk()], chunker=info(),
                        update_document_id="doc_old")
    assert req.to_wire()["update_document_id"] == "doc_old"


def test_build_request_without_update_id_omits_field() -> None:
    """新建时必须**不发** update_document_id（发空串是另一种语义）。"""
    req = build_request(path=Path("a.md"), name="a.md", file_hash="h",
                        chunks=[chunk()], chunker=info())
    assert "update_document_id" not in req.to_wire()


# ------------------------------------------------------------ 错误分类

class _FakeAPIError(Exception):
    """模拟 SDK 的 APIError（只需 kind 属性）。"""

    def __init__(self, kind, message="boom"):
        super().__init__(message)
        self.kind = kind


def test_classify_chunker_mismatch_not_retryable() -> None:
    """切块器版本冲突不能重试（重试必然再失败），且要提示重建。"""
    from localai import ErrChunkerMismatch

    f = classify_error(_FakeAPIError(ErrChunkerMismatch))
    assert isinstance(f, IngestFailure)
    assert f.retryable is False
    assert f.needs_rebuild is True
    assert "重建" in f.reason


def test_classify_queue_full_pauses() -> None:
    from localai import ErrQueueFull

    f = classify_error(_FakeAPIError(ErrQueueFull))
    assert f.should_pause is True
    assert f.retryable is True
    assert "忙" in f.reason


def test_classify_resource_limited_pauses() -> None:
    from localai import ErrResourceLimited

    f = classify_error(_FakeAPIError(ErrResourceLimited))
    assert f.should_pause is True
    assert "内存" in f.reason


def test_classify_model_unavailable_not_retryable() -> None:
    """Embedding 未配置：重试没有意义，要让用户去后台配。"""
    from localai import ErrModelUnavailable

    f = classify_error(_FakeAPIError(ErrModelUnavailable))
    assert f.retryable is False
    assert f.should_pause is False
    assert "管理后台" in f.reason


def test_classify_generic_api_error_retryable() -> None:
    from localai import ErrInvalidRequest

    f = classify_error(_FakeAPIError(ErrInvalidRequest, "bad"))
    assert f.retryable is True
    assert f.should_pause is False


def test_classify_non_api_error() -> None:
    """非 APIError（网络、序列化）也要收敛成可读原因。"""
    f = classify_error(ConnectionResetError("reset by peer"))
    assert f.retryable is True
    assert "ConnectionResetError" in f.detail


def test_classify_preserves_detail() -> None:
    """detail 要给排障用（异常原文），reason 给用户看。"""
    f = classify_error(_FakeAPIError(None, "some internal detail"))
    assert "some internal detail" in f.detail


# ------------------------------------------------------- 2000 块上限

def test_max_chunks_matches_server() -> None:
    """这个数字必须与服务端一致（`document.MaxChunksPerRequest`）。"""
    assert MAX_CHUNKS_PER_DOC == 2000


def test_ingest_rejects_oversized_without_network() -> None:
    """超限必须**在发请求之前**拒绝。

    服务端也会拒绝，但那时客户端已经解析完整个文件（可能几分钟）。
    提前拦截省掉一次必然失败的往返与等待。
    """
    from localkb.ingest import ingest

    class _NoCall:
        def call(self, fn):  # pragma: no cover - 不该被调用
            raise AssertionError("超限请求不该发到服务端")

    req = build_request(path=Path("big.pdf"), name="big.pdf", file_hash="h",
                        chunks=[chunk(i) for i in range(MAX_CHUNKS_PER_DOC + 1)],
                        chunker=info())
    ok, fail = ingest(_NoCall(), "kb", req)
    assert ok is None
    assert fail is not None
    assert fail.retryable is False
    assert str(MAX_CHUNKS_PER_DOC) in fail.reason
    assert "拆分" in fail.reason


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
    """建一个临时知识库，用完删掉。

    ## 为什么要独立库

    往共享库里写测试数据会污染它——后续的人打开管理后台会看到一堆
    "test-xxxx" 的文档。每次建一个、用完删掉，代价只有几十毫秒。
    """
    import uuid

    name = f"localkb-test-{uuid.uuid4().hex[:8]}"
    kb = live_conn.call(lambda c: c.create_kb(name, description="自动化测试"))
    try:
        yield kb.id
    finally:
        import contextlib

        # 清理失败不该让测试失败（服务可能已不可用）
        with contextlib.suppress(Exception):
            live_conn.call(lambda c: c.delete_kb(kb.id))


def test_ingest_and_search_end_to_end(live_conn, temp_kb) -> None:
    """入库 → 关键词检索能命中 → 页码与标题路径透出。

    这是 A1 的最小版本：不依赖解析器（直接构造块），只验证"入库-检索"链路。
    """
    from localai import SearchRequest

    from localkb.ingest import ingest

    chunks = [
        Chunk(ordinal=0, text="[第一章 总则]\n本文件规定了售后服务要求。",
              page_start=1, page_end=1, heading_path="第一章 总则",
              chunk_type="paragraph", rune_count=20),
        Chunk(ordinal=1, text="[第一章 总则 > 1.1 期限]\n整机保修期为两年。",
              page_start=3, page_end=3, heading_path="第一章 总则 > 1.1 期限",
              chunk_type="paragraph", rune_count=20),
    ]
    req = build_request(path=Path("保修手册.pdf"), name="保修手册.pdf",
                        file_hash="test-hash-" + temp_kb, chunks=chunks,
                        chunker=info())
    ok, fail = ingest(live_conn, temp_kb, req)
    assert fail is None, f"入库失败：{fail.reason if fail else ''}"
    assert ok.chunk_count == 2

    r = live_conn.call(lambda c: c.search(SearchRequest(
        query="保修期", kb_ids=[temp_kb], top_k=5)))
    assert r.hits, "关键词检索没有命中"
    hit = next(h for h in r.hits if "保修期" in h.text)
    assert hit.page_start == 3, f"页码没透出：{hit.page_start}"
    assert "1.1 期限" in hit.heading_path


def test_ingest_duplicate_returns_existing(live_conn, temp_kb) -> None:
    """同一 file_hash 提交两次：第二次 duplicate=True 且不新建文档。"""
    from localkb.ingest import ingest

    kwargs = dict(path=Path("a.md"), name="a.md",
                  file_hash="dup-" + temp_kb, chunks=[chunk()], chunker=info())
    ok1, _ = ingest(live_conn, temp_kb, build_request(**kwargs))
    ok2, _ = ingest(live_conn, temp_kb, build_request(**kwargs))

    assert ok1.duplicate is False
    assert ok2.duplicate is True
    assert ok2.existing_id == ok1.document_id

    st = live_conn.call(lambda c: c.kb_stat(temp_kb))
    assert st.document_count == 1, "重复内容不该新建文档"


def test_ingest_update_reuses_document(live_conn, temp_kb) -> None:
    """带 update_document_id 时是更新而不是新建（§7.2 的关键）。"""
    from localkb.ingest import ingest

    ok1, _ = ingest(live_conn, temp_kb, build_request(
        path=Path("a.md"), name="a.md", file_hash="v1-" + temp_kb,
        chunks=[chunk(0, "第一版内容")], chunker=info()))

    ok2, fail = ingest(live_conn, temp_kb, build_request(
        path=Path("a.md"), name="a.md", file_hash="v2-" + temp_kb,
        chunks=[chunk(0, "第二版内容")], chunker=info(),
        update_document_id=ok1.document_id))

    assert fail is None
    assert ok2.document_id == ok1.document_id, "更新不该产生新文档"
    st = live_conn.call(lambda c: c.kb_stat(temp_kb))
    assert st.document_count == 1


def test_ingest_rename_persists_name(live_conn, temp_kb) -> None:
    """S2 的验收：改名后 `list_documents` 里的 name 真的变了。

    ## 为什么这条放在客户端测试里

    它是"服务端已修复"的证据。若将来回退，这条会立刻失败——比等到
    用户看到卡片显示旧名再排查要早得多。
    """
    from localkb.ingest import ingest

    ok1, _ = ingest(live_conn, temp_kb, build_request(
        path=Path("旧名.md"), name="旧名.md", file_hash="rn-" + temp_kb,
        chunks=[chunk(0, "内容不变")], chunker=info()))

    ingest(live_conn, temp_kb, build_request(
        path=Path("新名.md"), name="新名.md", file_hash="rn-" + temp_kb,
        chunks=[chunk(0, "内容不变")], chunker=info(),
        update_document_id=ok1.document_id))

    docs = live_conn.call(lambda c: c.list_documents(temp_kb))
    doc = next(d for d in docs if d.id == ok1.document_id)
    assert doc.name == "新名.md", f"改名没生效：{doc.name}"
