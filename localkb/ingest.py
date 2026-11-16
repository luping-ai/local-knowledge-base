"""入库：把切好的块提交给 Runtime，并处理向量化。

## 这一层要解决的三件事

1. **契约映射**：本地的 `Chunk` 与 SDK 的 `ChunkInput` 字段几乎一一对应，
   但不能直接传——SDK 是**另一个仓库的公开 API**，它的字段演进与本地的
   内部结构无关。中间加一层映射，SDK 升级时只需要改这一个文件。

2. **错误分类**：`chunker_mismatch` 不能重试、`queue_full` 要退避、
   `resource_limited` 要暂停扫描。SDK 已经给了 `ErrorKind`，这里负责
   翻译成"扫描流程该怎么反应"。

3. **2000 块上限**（方案 §5.7）：这是硬约束。客户端在切块后就要判断，
   不能等到提交时才失败——那时已经花了几分钟的解析时间。

## 为什么不做"分批提交同一文档"

`ReplaceChunksIncremental` 按 ordinal 做差集删除：第二批提交会把第一批
里没出现的 ordinal 全部删掉。所以"分批续接"在当前实现下是坏的
（方案 §5.7 已记录）。超限的正确做法是明确拒绝，让用户拆分文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from localai import (
    APIError,
    ChunkInput,
    ErrChunkerMismatch,
    ErrModelUnavailable,
    ErrQueueFull,
    ErrResourceLimited,
    IngestChunksRequest,
)
from localai import (
    ChunkerInfo as SdkChunkerInfo,
)

from localkb.chunk.splitter import Chunk, ChunkerInfo

__all__ = [
    "MAX_CHUNKS_PER_DOC",
    "IngestOutcome",
    "IngestFailure",
    "build_request",
    "ingest",
    "classify_error",
]

# 与 Runtime 的 `document.MaxChunksPerRequest` 保持一致。
#
# ## 为什么要在这里重复这个数字
#
# 服务端会拒绝超限请求，但那时客户端已经解析完整个文件（可能几分钟）。
# 客户端提前判断能在解析完立刻给出"请拆分"的提示，省掉一次必然失败的
# 网络往返与等待。这个数字必须跟服务端同步——两边不一致时以服务端为准
# （它会拒绝），这里只是提前拦截。
MAX_CHUNKS_PER_DOC = 2000


@dataclass
class IngestOutcome:
    """一次入库成功的结果。"""

    document_id: str
    chunk_count: int
    duplicate: bool = False
    existing_id: str = ""
    # diff 统计（仅 update 时有意义）
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0


@dataclass
class IngestFailure:
    """一次入库失败的结果。

    ## 为什么不用异常

    扫描是"逐篇处理、单篇失败不中断"（§10.5）。用返回值表达失败让调用方
    的循环结构保持扁平，不必每篇写一次 try/except；而**需要中断扫描**的
    情况（资源不足、队列满）用 `should_pause` 表达，比异常更直白。
    """

    reason: str
    """给用户看的可读原因（§5.6）。"""

    detail: str = ""
    """排障细节（异常原文），不直接展示。"""

    retryable: bool = False
    """下次扫描是否应该重试。"""

    should_pause: bool = False
    """是否应该暂停整个扫描（资源/队列问题，不是这一篇的问题）。"""

    needs_rebuild: bool = False
    """库内切块器版本不一致，需要重建库（不要重试）。"""


def _to_chunk_input(c: Chunk) -> ChunkInput:
    """本地 `Chunk` → SDK `ChunkInput`。

    ## 为什么要显式列字段而不是 `**vars(c)`

    本地 `Chunk` 有 `raw_len` 这类仅供调试的字段，SDK 不认。显式映射让
    "哪些字段真的会发给服务端"一眼可见，也避免本地加字段时悄悄发出去。
    """
    return ChunkInput(
        ordinal=c.ordinal,
        text=c.text,
        page_start=c.page_start,
        page_end=c.page_end,
        start_rune=c.start_rune,
        rune_count=c.rune_count,
        heading_path=c.heading_path,
        chunk_type=c.chunk_type,
    )


def _to_chunker_info(info: ChunkerInfo) -> SdkChunkerInfo:
    return SdkChunkerInfo(
        name=info.name,
        version=info.version,
        target_chars=info.target_chars,
        max_chars=info.max_chars,
        overlap_chars=info.overlap_chars,
    )


def build_request(
    *,
    path: Path,
    name: str,
    file_hash: str,
    chunks: list[Chunk],
    chunker: ChunkerInfo,
    file_size: int = 0,
    modified_at: int = 0,
    update_document_id: Optional[str] = None,
) -> IngestChunksRequest:
    """组装入库请求。

    `name` 与 `source_label` 分开传：
      - `name` 是**显示名**（来源卡片上显示的文件名）
      - `source_label` 是**来源描述**（可以是完整路径，Runtime 只存不解析）

    方案 §6.3 要求路径不经 API 暴露"用户文件系统布局"，但 `source_label`
    是**客户端主动给的展示信息**，属于例外——它进了 `documents.original_path`
    且不对外返回（`docSummary` 不含该字段）。这里给完整路径是为了诊断时
    能定位到源文件。
    """
    return IngestChunksRequest(
        name=name,
        file_hash=file_hash,
        chunks=[_to_chunk_input(c) for c in chunks],
        chunker=_to_chunker_info(chunker),
        file_type=path.suffix.lstrip(".").lower(),
        file_size=file_size,
        modified_at=modified_at,
        source_label=str(path),
        update_document_id=update_document_id,
    )


def classify_error(e: BaseException) -> IngestFailure:
    """把 SDK 异常翻译成"扫描流程该怎么反应"。

    ## 分类依据来自方案 §6.4 与 §8.5

    | 错误 | 动作 | 理由 |
    | --- | --- | --- |
    | chunker_mismatch | 提示重建，**不重试** | 重试必然再失败 |
    | queue_full | 暂停扫描 + 退避 | 是"系统繁忙"，不是这一篇的问题 |
    | resource_limited | 暂停扫描 | 内存不足，继续只会更糟 |
    | model_unavailable | 不可重试 | Embedding 未配置，要用户去后台配 |
    | 其他 APIError | 记原因，下次重试 | 可能是暂态的 |
    | 非 APIError | 记原因，下次重试 | 网络/序列化等 |
    """
    kind = getattr(e, "kind", None)
    msg = str(e)

    if kind is ErrChunkerMismatch:
        return IngestFailure(
            reason="库内索引由旧版切块器建立，需要重建知识库后才能继续导入。",
            detail=msg,
            retryable=False,
            needs_rebuild=True,
        )
    if kind is ErrQueueFull:
        return IngestFailure(
            reason="本地服务正忙，已暂停扫描。稍后会自动继续。",
            detail=msg,
            retryable=True,
            should_pause=True,
        )
    if kind is ErrResourceLimited:
        return IngestFailure(
            reason="可用内存不足，已暂停扫描。关闭部分程序后会自动继续。",
            detail=msg,
            retryable=True,
            should_pause=True,
        )
    if kind is ErrModelUnavailable:
        return IngestFailure(
            reason="向量模型未配置，无法完成入库。请到管理后台配置后重启服务。",
            detail=msg,
            retryable=False,
        )
    if isinstance(e, APIError):
        return IngestFailure(reason=f"入库失败：{msg}", detail=msg, retryable=True)
    return IngestFailure(
        reason="入库时发生意外错误。",
        detail=f"{type(e).__name__}: {msg}",
        retryable=True,
    )


def ingest(
    conn,
    kb_id: str,
    req: IngestChunksRequest,
) -> tuple[Optional[IngestOutcome], Optional[IngestFailure]]:
    """提交一次入库。返回 `(结果, 失败)`，两者必有且仅有一个非 None。

    ## 为什么返回元组而不是抛异常

    见 `IngestFailure` 的说明。这里额外的好处是调用方的循环能写成：

        ok, fail = ingest(...)
        if fail:
            handle(fail)
            if fail.should_pause: break
            continue

    而不需要在循环里套 try/except（那会让"单篇失败继续"的逻辑变得难读）。
    """
    if len(req.chunks) > MAX_CHUNKS_PER_DOC:
        return None, IngestFailure(
            reason=(
                f"这个文件切出 {len(req.chunks)} 个片段，"
                f"超过单文档上限 {MAX_CHUNKS_PER_DOC}。请拆分后重新扫描。"
            ),
            retryable=False,
        )

    try:
        res = conn.call(lambda c: c.ingest_chunks(kb_id, req))
    except Exception as e:  # noqa: BLE001 - 边界收敛，见 classify_error
        return None, classify_error(e)

    diff = res.diff or {}
    return IngestOutcome(
        document_id=res.document_id,
        chunk_count=res.chunk_count,
        duplicate=res.duplicate,
        existing_id=res.existing_id,
        added=int(diff.get("added", 0) or 0),
        updated=int(diff.get("updated", 0) or 0),
        deleted=int(diff.get("deleted", 0) or 0),
        unchanged=int(diff.get("unchanged", 0) or 0),
    ), None


def vectorize(conn, kb_id: str) -> tuple[bool, str]:
    """触发整个知识库的向量化（异步）。返回 `(是否已触发, 说明)`。

    ## 为什么按整个库触发一次，而不是逐篇

    `vectorize` 的语义是"为该 KB 内所有缺向量的切片补向量"。逐篇调用
    每次都要遍历全库统计，反而更慢。一次触发后轮询 `kb_stat` 即可。

    ## 为什么用 async

    同步 `vectorize` 在几千块时要跑几分钟，HTTP 请求会超时。
    `is_async=True` 会返回一个 task_id，进度从 `kb_stat().vectorize` 读。
    """
    try:
        res = conn.call(lambda c: c.vectorize(kb_id, is_async=True))
    except Exception as e:  # noqa: BLE001
        f = classify_error(e)
        return False, f.reason

    # 服务端在已有任务在跑时会返回 started=False，这不是错误
    return True, getattr(res, "note", "") or "已触发向量化"
