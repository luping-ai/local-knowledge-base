"""对话层测试：范围映射、事件解析、来源快照。

## 哪些不需要真服务

`KbScope.to_request_kb_ids` 与 `dump_sources` / `_trim` 是纯逻辑。它们
最容易出错（三态映射搞混会让答案意外带上全库内容），且完全不依赖网络。

`stream_turn` / `fetch_snippets` 需要真服务，用 skip 保护。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from localkb.chat import (
    KB_MODE_ALL,
    KB_MODE_NONE,
    KB_MODE_SELECTED,
    ChatTurn,
    KbScope,
    _trim,
    build_chat_request,
    dump_sources,
    fetch_snippets,
    stream_turn,
)

# ------------------------------------------------------------ 范围映射

def test_scope_none_sends_none() -> None:
    """不查库必须传 None，而不是空列表或 all。

    ## 为什么这条最关键

    "/chat 传 None" 与 "/chat 传 all" 的差别是"普通对话"与"检索全部库"。
    搞错的后果是用户的随口一问（"你好"）也去查库，答案带上他完全没
    想到的来源。
    """
    assert KbScope(KB_MODE_NONE).to_request_kb_ids() is None


def test_scope_selected_sends_ids() -> None:
    s = KbScope(KB_MODE_SELECTED, ["kb_1", "kb_2"])
    assert s.to_request_kb_ids() == ["kb_1", "kb_2"]


def test_scope_all_sends_all_bases() -> None:
    """all 必须转成 `[ALL_BASES]`（即 `["all"]`），不是展开后的列表。

    展开是服务端的事——客户端不知道"现在到底有几个库"，而且库可能在
    请求发出前被增删。
    """
    assert KbScope(KB_MODE_ALL).to_request_kb_ids() == ["all"]


def test_scope_selected_but_empty_falls_back_to_none() -> None:
    """选了 selected 但一个都没勾 → 视为不用知识库。

    绝不能退回 all：用户取消所有勾选的意思是"不用库"，
    不是"查全部"（方案 §8.1.1 的错误 1）。
    """
    assert KbScope(KB_MODE_SELECTED, []).to_request_kb_ids() is None


def test_scope_enabled_flag() -> None:
    assert KbScope(KB_MODE_NONE).enabled is False
    assert KbScope(KB_MODE_SELECTED, ["kb_1"]).enabled is True
    assert KbScope(KB_MODE_ALL).enabled is True


def test_scope_selected_copies_list() -> None:
    """返回副本：调用方改原列表不该影响已发出的请求。"""
    ids = ["kb_1"]
    s = KbScope(KB_MODE_SELECTED, ids)
    got = s.to_request_kb_ids()
    assert got is not None
    got.append("kb_2")
    assert s.kb_ids == ["kb_1"]


# ------------------------------------------------------------ 请求构造

def test_build_request_includes_history() -> None:
    req = build_chat_request(
        question="保修期多久",
        history=[("user", "你好"), ("assistant", "你好，有什么可以帮你")],
        scope=KbScope(KB_MODE_SELECTED, ["kb_1"]),
    )
    w = req.to_wire()
    assert len(w["messages"]) == 3
    assert w["messages"][-1] == {"role": "user", "content": "保修期多久"}
    assert w["messages"][0]["role"] == "user"


def test_build_request_sets_knowledge_params() -> None:
    req = build_chat_request(question="q", history=[],
                             scope=KbScope(KB_MODE_ALL))
    w = req.to_wire()
    assert w["knowledge_mode"] == "hybrid"
    assert w["knowledge_top_k"] == 20
    assert w["max_knowledge_chunks"] == 6
    assert w["priority"] == "interactive"
    assert w["kb_ids"] == ["all"]


def test_build_request_none_scope_omits_kb_ids() -> None:
    req = build_chat_request(question="q", history=[], scope=KbScope(KB_MODE_NONE))
    assert "kb_ids" not in req.to_wire()


def test_build_request_max_tokens_default() -> None:
    """512 是刻意的上限：1.5B 15.8 tok/s，再长用户会以为卡死。"""
    req = build_chat_request(question="q", history=[], scope=KbScope())
    assert req.to_wire()["max_tokens"] == 512


# ------------------------------------------------------------ 片段截断

def test_trim_short_text_unchanged() -> None:
    assert _trim("很短的一段话。", 120) == "很短的一段话。"


def test_trim_breaks_at_punctuation() -> None:
    """截断要在标点处，不留半个字。"""
    text = "第一句话到这里。第二句话开始了但是会被截断掉一些内容。"
    out = _trim(text, 12)
    assert out.endswith("……")
    # 截断点应在"。"之后，而不是"到"字中间
    assert out.startswith("第一句话到这里。")


def test_trim_collapses_whitespace() -> None:
    """PDF 抽取的片段常有大量换行，折叠成空格。"""
    assert _trim("第一行\n\n第二行", 120) == "第一行 第二行"


def test_trim_hard_cut_when_no_punctuation() -> None:
    """没有任何标点时硬截。"""
    out = _trim("字" * 200, 50)
    assert len(out) <= 52
    assert out.endswith("……")


# ------------------------------------------------------------ 来源快照

class _FakeSource:
    """模拟 SDK 的 KnowledgeSource。"""

    def __init__(self, **kw):
        self.index = kw.get("index", 1)
        self.document_id = kw.get("document_id", "doc_1")
        self.source = kw.get("source", "手册.pdf")
        self.page_start = kw.get("page_start", 3)
        self.page_end = kw.get("page_end", 0)
        self.heading_path = kw.get("heading_path", "第三章 > 3.2")
        self.score = kw.get("score", 0.9)


def test_dump_sources_includes_snippet() -> None:
    """快照要带片段正文：删库后历史消息仍能显示"当时引用了什么"。"""
    snap = dump_sources(
        [_FakeSource(document_id="doc_1")],
        {"doc_1": "整机保修期为两年"},
    )
    data = json.loads(snap)
    assert len(data) == 1
    assert data[0]["doc_id"] == "doc_1"
    assert data[0]["snippet"] == "整机保修期为两年"
    assert data[0]["page_start"] == 3
    assert data[0]["heading_path"] == "第三章 > 3.2"


def test_dump_sources_missing_snippet_is_empty() -> None:
    snap = dump_sources([_FakeSource(document_id="doc_2")], {})
    assert json.loads(snap)[0]["snippet"] == ""


def test_dump_sources_empty_list() -> None:
    assert json.loads(dump_sources([], {})) == []


# ------------------------------------------------------------ 真服务

@pytest.fixture()
def live_conn():
    from localkb.runtime_conn import RuntimeConnector

    conn = RuntimeConnector()
    if not conn.alive():
        pytest.skip("本机 LocalAIHub 未运行")
    return conn


@pytest.fixture()
def loaded_conn(live_conn):
    """确保有模型已加载。

    ## 为什么需要这一步（实测踩到）

    空闲卸载会在"模型空闲 90 秒 + 用户空闲 180 秒"后卸载模型
    （方案 §7.4）。开发时写完一批代码再跑测试，模型早就被卸了——
    表现为 `meta` 事件永远不来，实际是流一开始就返回
    `model_unavailable`。

    这不是代码 bug，是**测试没有自备前置条件**。所以这里显式加载一次：
    已加载就跳过，未加载就 load。
    """
    active = live_conn.call(lambda c: c.active_model())
    if active:
        return live_conn
    cap = live_conn.call(lambda c: c.capability())
    model = cap.rag_model or "qwen2.5-1.5b-instruct-q4"
    try:
        live_conn.call(lambda c: c.load_model(model))
    except Exception as e:
        pytest.skip(f"无法加载模型（{e}），跳过需要模型生成能力的测试")
    return live_conn


@pytest.fixture()
def kb_with_content(loaded_conn):
    """一个含固定内容的临时库，用于验证检索与拒答。"""
    import uuid

    from localkb.chunk.splitter import Chunk, ChunkerInfo
    from localkb.ingest import build_request, ingest

    name = f"localkb-chat-{uuid.uuid4().hex[:8]}"
    kb = loaded_conn.call(lambda c: c.create_kb(name))
    chunks = [
        Chunk(ordinal=0, text="[保修条款 > 3.2 保修期限]\n整机保修期为两年，自购买之日起计算。",
              page_start=3, page_end=3, heading_path="保修条款 > 3.2 保修期限",
              chunk_type="paragraph", rune_count=30),
    ]
    req = build_request(path=Path("保修手册.pdf"), name="保修手册.pdf",
                        file_hash="chat-" + kb.id, chunks=chunks,
                        chunker=ChunkerInfo(name="localkb-py", version="1.0.0",
                                            target_chars=450, max_chars=600,
                                            overlap_chars=54))
    ingest(loaded_conn, kb.id, req)
    try:
        yield kb.id
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            loaded_conn.call(lambda c: c.delete_kb(kb.id))


def test_stream_turn_yields_meta_before_delta(loaded_conn, kb_with_content) -> None:
    """`meta` 必须在第一个 `delta` 之前。

    ## 为什么这条重要

    来源卡片按 `meta` 渲染（方案 §6.5）。若 `meta` 在 `delta` 之后，
    用户会先看到文字、后看到"依据"，而在流式中途失败时什么都看不到。
    """
    req = build_chat_request(
        question="保修期多久？", history=[],
        scope=KbScope(KB_MODE_SELECTED, [kb_with_content]),
        max_tokens=32,
    )
    kinds = []
    for ev in stream_turn(loaded_conn, req):
        kinds.append(ev.kind)
    assert "meta" in kinds
    if "delta" in kinds:
        assert kinds.index("meta") < kinds.index("delta")


def test_stream_turn_meta_has_sources(loaded_conn, kb_with_content) -> None:
    """有命中的库必须带来源，且来源有页码与章节路径。"""
    req = build_chat_request(
        question="整机保修多久？", history=[],
        scope=KbScope(KB_MODE_SELECTED, [kb_with_content]),
        max_tokens=32,
    )
    meta = None
    for ev in stream_turn(loaded_conn, req):
        if ev.kind == "meta":
            meta = ev
            break
    assert meta is not None
    assert meta.knowledge is not None
    if meta.knowledge.used_chunks > 0:
        assert meta.knowledge.sources, "注入了资料但没有 sources"
        src = meta.knowledge.sources[0]
        assert src.page_start == 3
        assert "保修" in src.heading_path


def test_stream_turn_no_kb_for_plain_chat(loaded_conn) -> None:
    """不选库时不发起检索（knowledge 为 None 或 used_chunks=0）。"""
    req = build_chat_request(question="你好", history=[],
                             scope=KbScope(KB_MODE_NONE), max_tokens=16)
    meta = None
    for ev in stream_turn(loaded_conn, req):
        if ev.kind == "meta":
            meta = ev
            break
    assert meta is not None
    if meta.knowledge is not None:
        assert meta.knowledge.used_chunks == 0


def test_fetch_snippets_finds_text(live_conn, kb_with_content) -> None:
    """补查片段能拿到正文（KnowledgeSource 不含 text，S9 的绕过方案）。"""
    from localai import SearchRequest

    hit = live_conn.call(lambda c: c.search(SearchRequest(
        query="保修期", kb_ids=[kb_with_content], top_k=3))).hits[0]
    got = fetch_snippets(
        live_conn, question="保修期多久",
        scope=KbScope(KB_MODE_SELECTED, [kb_with_content]),
        doc_ids=[hit.document_id],
    )
    assert hit.document_id in got
    assert "保修" in got[hit.document_id]


def test_fetch_snippets_no_scope_returns_empty(live_conn) -> None:
    """不查库时不补片段（没有检索就没有片段）。"""
    got = fetch_snippets(live_conn, question="q",
                         scope=KbScope(KB_MODE_NONE), doc_ids=["doc_1"])
    assert got == {}


def test_check_chat_ready_reports_unloaded_model() -> None:
    """回归：`chat_enabled=true` 但模型未加载时必须报出来。

    ## 这个缺陷的实际表现

    实测踩到：`capability()` 返回 `chat_enabled=true`、`rag_enabled=true`，
    但提问直接得到 `model_unavailable: 没有已加载的模型`。

    原因是 `capability` 是**启动时**算的静态判断（"这台机器能不能跑"），
    而"现在有没有模型加载"是另一回事——Runtime 重启后不会自动加载模型
    （除非启动参数带 `-preload`）。

    不做这个检查的话，用户看到的是一个空回答加一行原始英文错误。
    """
    from localkb.chat import check_chat_ready

    class _Cap:
        chat_enabled = True
        rag_enabled = True
        reason = ""

    class _Conn:
        def __init__(self):
            self.calls = 0

        def call(self, fn):
            self.calls += 1
            if self.calls == 1:
                return _Cap()
            return ""   # active_model 为空 → 未加载

    reason = check_chat_ready(_Conn())
    assert reason, "模型未加载时必须返回提示"
    assert "加载" in reason
    assert "管理后台" in reason


def test_check_chat_ready_ok_when_model_loaded() -> None:
    from localkb.chat import check_chat_ready

    class _Cap:
        chat_enabled = True
        rag_enabled = True
        reason = ""

    class _Conn:
        def __init__(self):
            self.calls = 0

        def call(self, fn):
            self.calls += 1
            return _Cap() if self.calls == 1 else "qwen2.5-1.5b"

    assert check_chat_ready(_Conn()) == ""


def test_check_chat_ready_reports_capability_unmet() -> None:
    from localkb.chat import check_chat_ready

    class _Cap:
        chat_enabled = False
        rag_enabled = False
        reason = "可用内存不足"

    class _Conn:
        def call(self, fn):
            return _Cap()

    reason = check_chat_ready(_Conn())
    assert "内存" in reason


def test_chat_turn_defaults() -> None:
    t = ChatTurn(question="q")
    assert t.answer == ""
    assert t.kb_mode == "none"
    assert t.source_snapshot == "[]"
