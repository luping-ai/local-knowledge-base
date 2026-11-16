"""对话：流式请求、知识库范围、来源快照。

## 这一层的三个职责

1. **构造请求**：把"用户勾选的库"翻译成 `kb_ids` 的三种形态（具体列表 /
   `[ALL_BASES]` / `None`）。这三者的语义差别很大，搞混会让答案意外带上
   全库内容或意外不查库（方案 §8.1.1）。

2. **解析事件流**：SDK 的 `StreamEvent` 是"判别名 + 各事件专属字段"的
   结构，UI 需要的是"一句一句的文字 + 最终的来源"。这一层负责转换，
   让 UI 不必知道 SSE 的细节。

3. **来源快照**：`sources` 只来自 `meta` 事件（不是 `done`）。若等 `done`
   才拿来源，流式中途失败时用户看不到任何依据——而那时他更需要知道
   "系统本来打算引用什么"。

## 一个必须记住的缺口：sources 不含片段文本

`KnowledgeSource` 只有 `source` / `page_start` / `heading_path`，**没有
片段正文**。而来源卡片要显示"引用了哪一段"（方案 §2.1 的示意图）。

两条路：
  - 用 `search()` 按同样的问题再查一次，用 `hitResult.text` 补片段
  - 服务端在 `KnowledgeSource` 里加 `text`（属契约变更）

本文档按第一条实现（`fetch_snippets`），代价是多一次检索（10~30ms），
收益是不必等契约变更。第二条已记为服务端待办 S9。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Optional

from localai import (
    ALL_BASES,
    ChatRequest,
    KnowledgeInfo,
    Message,
    Priority,
    SearchRequest,
    StreamEventType,
)

__all__ = [
    "KbScope",
    "ChatEvent",
    "ChatTurn",
    "build_chat_request",
    "stream_turn",
    "fetch_snippets",
    "check_chat_ready",
]


KB_MODE_NONE = "none"
KB_MODE_SELECTED = "selected"
KB_MODE_ALL = "all"


@dataclass
class KbScope:
    """用户对"这次要不要查库、查哪些库"的选择。

    ## 为什么要三值而不是"kb_ids 为空即不用"

    空数组在 `/search` 里是错误、在 `/chat` 里是"不查库"，两边语义不同。
    显式记下意图，才能：
      - 还原会话的勾选状态
      - 不把"我选过库但后来全取消"与"我从没用过库"混为一谈
    """

    mode: str = KB_MODE_NONE
    kb_ids: list[str] = field(default_factory=list)

    def to_request_kb_ids(self) -> Optional[list[str]]:
        """翻译成 `ChatRequest.kb_ids`。

        ## 三种映射（方案 §8.1.1）

        | mode | 传什么 | 服务端行为 |
        | --- | --- | --- |
        | none | `None` | 不做检索（普通对话） |
        | selected | 具体 ID 列表 | 只查这些 |
        | all | `[ALL_BASES]` | 展开为全部库，响应 `all_bases=true` |

        **不能"一个都没选就传 all"**：用户取消所有勾选的意思是"不用知识库"，
        不是"查全部"。传 all 会让答案意外带上全库内容，用户无法解释来源。
        """
        if self.mode == KB_MODE_ALL:
            return [ALL_BASES]
        if self.mode == KB_MODE_SELECTED and self.kb_ids:
            return list(self.kb_ids)
        return None

    @property
    def enabled(self) -> bool:
        return self.to_request_kb_ids() is not None


@dataclass
class ChatEvent:
    """给 UI 的统一事件。

    与 SDK 的 `StreamEvent` 分开的原因：UI 只关心"开始等 / 排队 / 文字 /
    提示 / 结束"，不需要知道 `usage` 里有哪些字段。中间加一层让 UI 的
    事件循环简单得多，也让"换 SDK 版本"的改动限制在一处。
    """

    kind: str
    """queued / meta / warning / delta / done / error / cancelled"""

    text: str = ""
    # queued
    ahead: int = 0
    wait_ms: int = 0
    # meta
    model: str = ""
    degraded: bool = False
    knowledge: Optional[KnowledgeInfo] = None
    # warning
    warnings: list[str] = field(default_factory=list)
    # done
    finish_reason: str = ""
    # error
    error: str = ""


@dataclass
class ChatTurn:
    """一轮问答的累积结果，用于落库与渲染来源卡片。"""

    question: str = ""
    answer: str = ""
    sources: list = field(default_factory=list)
    model: str = ""
    degraded: bool = False
    used_chunks: int = 0
    all_bases: bool = False
    base_count: int = 0
    kb_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finish_reason: str = ""
    # 流式中途失败/取消时非空
    error: str = ""
    # 本次使用的库范围（none / selected / all），用于落库
    kb_mode: str = "none"
    # 每条的片段正文（doc_id → 前 40 字），由 fetch_snippets 补
    snippets: dict[str, str] = field(default_factory=dict)
    # 落库用的来源快照 JSON（由 dump_sources 生成）
    source_snapshot: str = "[]"


def build_chat_request(
    *,
    question: str,
    history: list[tuple[str, str]],
    scope: KbScope,
    max_tokens: int = 512,
    temperature: float = 0.2,
    history_tokens: int = 600,
) -> ChatRequest:
    """构造对话请求。

    ## `history` 是 (role, content) 元组列表

    不直接收 `Message` 对象，是为了让调用方（UI 与测试）不必 import SDK
    类型。转换在这里做。

    ## 为什么 `max_tokens=512`

    1.5B 模型 15.8 tok/s，512 token 约 32 秒。再长用户会以为卡死。需要
    更长的回答时应显示进度而不是调大这个值。
    """
    msgs = [Message(role=r, content=c) for r, c in history]
    msgs.append(Message("user", question))

    return ChatRequest(
        messages=msgs,
        kb_ids=scope.to_request_kb_ids(),
        knowledge_mode="hybrid",
        # 先多召回（20），再由服务端按预算筛到 6。
        # 反过来的话"预算裁剪"就没有选择余地了。
        knowledge_top_k=20,
        max_knowledge_chunks=6,
        history_tokens=history_tokens,
        priority=Priority.INTERACTIVE,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def stream_turn(conn, req: ChatRequest) -> Iterator[ChatEvent]:
    """执行一次流式对话，逐个产出 `ChatEvent`。

    ## 为什么是生成器

    UI 在工作线程里迭代它，每个 delta 立刻上抛（signal）。若改成"攒完
    再返回"，用户会盯着空白界面等几秒到几十秒。

    ## 来源在 `meta` 就取

    `done` 可能永远不来（用户点取消、网络断）。而 `meta` 一定在第一个
    delta 之前，所以"系统打算引用什么"这个信息在 `meta` 时就可靠了。
    """
    try:
        with conn.call(lambda c: c.stream_events(req)) as events:
            for ev in events:
                t = ev.type
                if t == StreamEventType.QUEUED:
                    yield ChatEvent(kind="queued", ahead=ev.ahead,
                                    wait_ms=ev.estimated_wait_ms)
                elif t == StreamEventType.META:
                    info = KnowledgeInfo.from_dict(ev.knowledge)
                    yield ChatEvent(kind="meta", model=ev.model,
                                    degraded=ev.degraded, knowledge=info)
                    if info is not None and info.warnings:
                        # 服务端把检索告警放在 meta.knowledge.warnings 里。
                        # 单独产一个 warning 事件，让 UI 的"提示区"逻辑统一。
                        yield ChatEvent(kind="warning", warnings=info.warnings)
                elif t == StreamEventType.WARNING:
                    yield ChatEvent(kind="warning", warnings=list(ev.warnings))
                elif t == StreamEventType.DELTA:
                    yield ChatEvent(kind="delta", text=ev.text)
                elif t == StreamEventType.DONE:
                    yield ChatEvent(kind="done", finish_reason=ev.finish_reason)
                elif t == StreamEventType.ERROR:
                    msg = str(ev.error) if ev.error else "流中断"
                    yield ChatEvent(kind="error", error=msg)
    except GeneratorExit:
        # 用户取消：迭代器被 close()。SDK 的 ClosingIterator 会关掉底层
        # 连接，服务端随之停止生成。这里不产事件（调用方已经知道取消了）。
        raise
    except Exception as e:  # noqa: BLE001 - 边界收敛
        yield ChatEvent(kind="error", error=str(e))


def fetch_snippets(conn, *, question: str, scope: KbScope,
                   doc_ids: list[str], limit: int = 6) -> dict[str, str]:
    """补查来源片段的正文。

    ## 为什么需要这一步

    `KnowledgeSource` 不含片段文本（只有文件名/页码/章节），而来源卡片要
    显示"引用了哪一段"（方案 §2.1）。重新 `search()` 一次能拿到
    `hitResult.text`，代价是 10~30ms 的额外检索。

    服务端加 `text` 字段后这一步可以删掉（已记为待办 S9）。

    ## 匹配方式

    用 `document_id` 对齐：同一个文档可能被引用多段，我们只需要"能展示
    一句"就够——卡片本来就是提示性的，不需要精确到模型实际用了哪一段。
    优先取在该文档里的第一条命中。
    """
    if not doc_ids or not scope.enabled:
        return {}
    kb_ids = scope.to_request_kb_ids()
    if kb_ids is None:
        return {}
    try:
        res = conn.call(lambda c: c.search(SearchRequest(
            query=question, kb_ids=kb_ids, top_k=max(limit, len(doc_ids) * 2),
        )))
    except Exception:  # noqa: BLE001 - 补片段失败不该影响主流程
        return {}

    wanted = set(doc_ids)
    out: dict[str, str] = {}
    for hit in res.hits:
        if hit.document_id in wanted and hit.document_id not in out:
            out[hit.document_id] = _trim(hit.text, 120)
    return out


def _trim(text: str, limit: int) -> str:
    """截断到 limit 字，尽量在句末或空白处断开。

    ## 为什么不留半个字

    卡片上显示"……整机保修期为两年，自购买之日"比"……自购买之日起……"
    难受得多。在 80% 位置往回找最近的标点/空白，找不到才硬截。
    """
    s = " ".join(text.split())   # 折叠空白（PDF 抽取常有大量换行）
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for mark in ("。", "！", "？", "；", "，", ".", " ", "\n"):
        i = cut.rfind(mark)
        if i > limit * 0.7:
            return cut[:i + 1].rstrip() + "……"
    return cut + "……"


def check_chat_ready(conn) -> str:
    """检查"现在能不能提问"。返回空串表示可以，否则返回给用户看的原因。

    ## 为什么要单独检查

    `capability().chat_enabled` 只说"这台机器能不能跑模型"，**不代表
    现在有模型已加载**。实测踩到过：`chat_enabled=true`、`rag_enabled=true`，
    但实际提问返回 `model_unavailable: 没有已加载的模型`。

    原因：Runtime 重启后不会自动加载模型（除非启动参数带 `-preload`），
    而 `capability` 是启动时算的静态判断。

    ## 三档检查

    1. `chat_enabled=false` → 内存不足，连模型都装不下
    2. `active_model()` 为空 → 有模型但没加载（最常见）
    3. 都通过 → 可以提问
    """
    try:
        cap = conn.call(lambda c: c.capability())
    except Exception as e:  # noqa: BLE001
        return f"无法读取服务能力：{e}"

    if not cap.chat_enabled:
        return (
            f"本机内存不足，无法运行对话模型"
            f"（{cap.reason}）。请关闭部分程序后重启本地服务。"
        )

    try:
        active = conn.call(lambda c: c.active_model())
    except Exception:  # noqa: BLE001 - 读不到就当作未加载
        active = ""
    if not active:
        # 最常见的状态：服务在跑，但模型没加载。
        # 提示要指向动作（去后台加载），而不是只说"模型不可用"。
        return (
            "本地服务还没有加载对话模型。"
            "请到管理后台的模型页加载一个（推荐 qwen2.5-1.5b），然后重试。"
        )
    return ""


def dump_sources(sources: list, snippets: dict[str, str]) -> str:
    """把来源快照成 JSON（存进 `messages.sources`）。

    ## 为什么要存快照而不是只存 doc_id

    用户删掉或重建知识库之后，历史消息里的引用会变成空卡片——而"我当时
    依据什么回答的"恰恰是历史消息唯一的用处。
    """
    out = []
    for s in sources:
        out.append({
            "index": getattr(s, "index", 0),
            "doc_id": getattr(s, "document_id", ""),
            "name": getattr(s, "source", ""),
            "page_start": getattr(s, "page_start", 0),
            "page_end": getattr(s, "page_end", 0),
            "heading_path": getattr(s, "heading_path", ""),
            "score": getattr(s, "score", 0.0),
            "snippet": snippets.get(getattr(s, "document_id", ""), ""),
        })
    return json.dumps(out, ensure_ascii=False)
