"""切块器：把 `ParsedDoc` 的块切成带元数据的检索单元。

## 参数来自实测，不是估算

| 项 | 值 | 依据 |
| --- | --- | --- |
| 目标长度 | 450 字符 | 实测 0.63 token/字，e5-small 上限 512 token ≈ 800 字 |
| 硬上限 | 600 字符 | 长句本身可能超目标 |
| 重叠 | 54 字符（12%） | **仅硬切兜底时用** |
| 最小块 | 50 字符 | 文档末尾常剩一两句，过短的并入前块 |

## 与 Go 实现的两处刻意差异（方案 §5.5）

Go 的 `internal/knowledge/chunk` 有两个已知缺陷，这里**不复刻**：

1. `isSentenceEnd(r rune)` 拿不到后一个字符，所以 `.` 无条件视为句末，
   把 `3.14`、`v1.2.3`、`e.g.` 拦腰切断。这里检查 `.` 后是否紧跟空白/结尾。
2. 重叠在所有切点都应用（含段落边界），且切点靠近块首时会产出"与上一块
   近乎完全相同"的块。这里只在硬切兜底时加重叠，且保证新块不短于上一块
   的一半。

`chunker.version` 就是用来区分这类差异的，所以"与 Go 不一致"是预期行为。

## 边界优先级（为什么是这个顺序）

    段落 > 中文句末标点 > 弱边界（逗号/顿号/空格） > 硬切

段落是最强的语义边界（换段等于换意思）；句末次之；逗号再次。只有整段
超过硬上限时才降级到硬切——那是唯一需要重叠的场景（否则一句话会被切成
两半且两半都不完整）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from localkb.parse.types import Block, BlockKind, ParsedDoc

__all__ = [
    "Chunk",
    "ChunkerInfo",
    "CHUNK_PROFILE",
    "ChunkProfile",
    "split",
    "CHUNKER_NAME",
    "CHUNKER_VERSION",
]

# 切块器标识。改算法时**必须**同时升 VERSION——它进 `kb_index_config`，
# 库内版本不一致时服务端返回 409（防止新旧切法混存导致页码错位）。
CHUNKER_NAME = "localkb-py"
CHUNKER_VERSION = "1.0.0"

# 中文句末标点。ASCII 的 `.` 单独处理（要看后一个字符）。
_CN_SENTENCE_END = "。！？；…"
# 弱边界：句内停顿
_WEAK_BOUNDARY = "，、：,;"
# 段落内允许的"硬切"位置——没有更好的边界时才用
_CLOSERS = "）)」』】》”\"'"


@dataclass(frozen=True)
class ChunkProfile:
    """切块参数。冻结是为了防止运行期被意外改动（会让版本号失去意义）。"""

    target: int = 450
    hard_max: int = 600
    overlap: int = 54
    min_len: int = 50
    # 标题路径拼在正文前的格式
    heading_sep: str = " > "
    # 是否给块加 `[路径]` 前缀
    prefix_heading: bool = True


CHUNK_PROFILE = ChunkProfile()


@dataclass
class ChunkerInfo:
    """切块器标识与参数，提交给 Runtime 存进 `kb_index_config`。"""

    name: str = CHUNKER_NAME
    version: str = CHUNKER_VERSION
    target_chars: int = 0
    max_chars: int = 0
    overlap_chars: int = 0

    @classmethod
    def from_profile(cls, p: ChunkProfile) -> ChunkerInfo:
        return cls(target_chars=p.target, max_chars=p.hard_max,
                   overlap_chars=p.overlap)


@dataclass
class Chunk:
    """一个待入库的块。字段与 Runtime 的 `ChunkInput` 一一对应。"""

    ordinal: int
    text: str
    page_start: int = 0
    page_end: int = 0
    start_rune: int = 0
    rune_count: int = 0
    heading_path: str = ""
    chunk_type: str = "paragraph"

    # 以下仅供本地调试，不提交给 Runtime
    raw_len: int = field(default=0, repr=False)


def split(doc: ParsedDoc, profile: ChunkProfile = CHUNK_PROFILE
          ) -> tuple[list[Chunk], ChunkerInfo]:
    """把解析结果切成块。

    返回 `(chunks, chunker_info)`。第二个要随入库请求一起提交。
    """
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)
    pending_heading: str = ""                  # 粘到下一块前面的标题
    rune_cursor = 0                            # 在原文中的位置（估算）

    # 相邻可合并段落先聚成"段组"。为什么不在块级做合并：
    # 合并后再切块才能让"短段落凑够 450 字"，而逐段切会产生大量 60 字的碎块。
    group: list[Block] = []

    def flush_group(path: str = "") -> None:
        """把当前段组切成块。

        ## 为什么 path 必须是参数而不是读 heading_stack

        标题切换时的顺序是"先 flush 旧段组、再压入新标题"。若 flush 内部
        去读 `heading_stack`，它拿到的是**新**标题——于是上一个 sheet 的
        数据会被打上新 sheet 的名字。

        实测症状：两个 sheet 的数据被合并成一块，`heading_path` 只有最后
        那个 sheet 名。
        """
        nonlocal group, rune_cursor
        if not group:
            return
        for text, pages, kind in _pack(group, profile):
            body = text
            if profile.prefix_heading and path:
                body = f"[{path}]\n{text}"
            chunks.append(Chunk(
                ordinal=len(chunks),
                text=body,
                page_start=min(p for p in pages if p) if any(pages) else 0,
                page_end=max(pages) if any(pages) else 0,
                start_rune=rune_cursor,
                rune_count=len(body),
                heading_path=path,
                chunk_type=kind.value if isinstance(kind, BlockKind) else "paragraph",
                raw_len=len(text),
            ))
            rune_cursor += len(body)
        group = []

    for blk in doc.blocks:
        if blk.kind is BlockKind.HEADING:
            # 换标题 → 上一节的内容要收尾
            #
            # ## 为什么这里必须 flush（踩过的坑）
            #
            # 早期版本只做了"弹出/压入标题栈"，没有 flush。后果是：
            # 两个 sheet（"1月报表"、"2月报表"）的表格行会被聚成同一块，
            # 块里的 heading_path 只有一个（最后压入的那个），于是
            # "1 月的销售额"与"2 月的销售额"混在一起，检索时互相污染。
            #
            # 实测症状：一个 25 字的测试文件（两个 sheet 各一行）被切成
            # 1 块，内容是 "A col a1 B col b1"——两个 sheet 的数据混了。
            flush_group(_path_of(heading_stack))
            _push_heading(heading_stack, blk)
            pending_heading = blk.text
            continue

        # 标题粘在第一段前面：否则"2.1 付款方式"单独成块时，
        # 只含正文的那块永远命中不了"付款方式"这个词。
        #
        # 用一个新变量而不是覆盖 blk：覆盖循环变量会让后面的人
        # 误以为"改的是原始块"，而实际上原始 ParsedDoc 不变。
        cur = blk
        if pending_heading:
            cur = Block(
                text=f"{pending_heading}\n{blk.text}",
                kind=blk.kind,
                page=blk.page,
                page_end=blk.page_end,
                heading=blk.heading or pending_heading,
                level=blk.level,
                is_header=blk.is_header,
                header_text=blk.header_text,
            )
            pending_heading = ""

        group.append(cur)

    flush_group(_path_of(heading_stack))

    # 只有一个标题、没有任何正文的文档（比如只有 `# 标题` 的 md）：
    # 标题被暂存到 pending_heading，而永远等不到正文 → 0 块。
    # 标题本身也是内容（用户可能就问"这份文档叫什么"），要单独成块。
    #
    # 实测症状：一份内容为 `# a` 的 md 报"解析成功但切不出任何块"，
    # 被判为失败并在下次扫描时反复重试。
    if pending_heading:
        path = _path_of(heading_stack)
        body = (f"[{path}]\n{pending_heading}"
                if (profile.prefix_heading and path) else pending_heading)
        chunks.append(Chunk(
            ordinal=len(chunks),
            text=body,
            start_rune=rune_cursor,
            rune_count=len(body),
            heading_path=path,
            chunk_type=BlockKind.HEADING.value,
        ))
        pending_heading = ""

    # 尾部过短的块并入前一块（不单独立块：一句残句检索价值极低，
    # 却会占掉 top_k 的一个位置）
    chunks = _merge_short_tail(chunks, profile)

    # 重新编号：合并后 ordinal 会断
    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks, ChunkerInfo.from_profile(profile)


def _push_heading(stack: list[tuple[int, str]], blk: Block) -> None:
    """维护标题栈：同级或更高级的旧标题要弹出。"""
    level = blk.level or 1
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, blk.text))


def _path_of(stack: list[tuple[int, str]]) -> str:
    return CHUNK_PROFILE.heading_sep.join(t for _, t in stack)


def _pack(blocks: list[Block], p: ChunkProfile
          ) -> list[tuple[str, list[int], BlockKind]]:
    """把一组块装成若干段文本，每段 ≤ hard_max。

    返回 `[(text, pages, kind)]`。`pages` 是该段覆盖到的所有页码
    （跨页段落会贡献两个页码）。

    ## 为什么按"累计长度"而不是"逐块判断"

    逐块判断只能得到"每块一片"，而目标是把短段落凑到 450 字。累计到
    接近 target 就收一次，才是"目标长度"的本意。
    """
    out: list[tuple[str, list[int], BlockKind]] = []
    buf: list[str] = []
    pages: list[int] = []
    kind = blocks[0].kind if blocks else BlockKind.PARAGRAPH
    size = 0
    table_header = ""  # Excel 表头，每块重复

    def add_pages(blk: Block) -> None:
        start, end = blk.page_span
        for pg in (start, end):
            if pg and pg not in pages:
                pages.append(pg)

    def emit() -> None:
        nonlocal buf, pages, size
        if not buf:
            return
        text = "\n".join(buf).strip()
        if text:
            out.append((text, pages[:], kind))
        buf, pages, size = [], [], 0

    for blk in blocks:
        text = blk.text.strip()
        if not text:
            continue

        if blk.kind is BlockKind.TABLE_ROW and blk.is_header:
            table_header = text
            continue

        # Excel：每块前置表头（§5.3）。不给表头的话，第二块之后
        # 只有一堆没有列名的数字，问"某列某行"永远命中不了。
        piece = text
        if table_header and blk.kind is BlockKind.TABLE_ROW:
            piece = f"{table_header}\n{text}"

        need = len(piece) + (1 if buf else 0)

        # 跨页不聚拢：短句在不同页时应各自成块，否则一块会横跨多页，
        # 来源卡片只能说"第 1~3 页"，定位精度直接丢失。
        #
        # 实测症状：一份 3 页、每页一行的 PDF 被切成 1 块
        # （page_start=1, page_end=3）——用户点"跳到第 3 页"看到的
        # 是第 3 页，但引用片段实际来自第 1 页。
        #
        # 注意：判断要用 `pages`（当前缓冲区里已有的页码），不能用
        # "上一块"的页码——emit() 之后 pages 被清空，用上一块会漏判。
        cross_page = bool(buf) and blk.page and pages and blk.page not in pages

        if (size + need > p.hard_max or cross_page) and buf:
            emit()
            # 硬切兜底时给下一块加一点重叠（只有这种情况需要）
            if len(piece) > p.hard_max:
                for seg in _hard_split(piece, p):
                    out.append((seg, [blk.page], blk.kind))
                continue

        if len(piece) > p.hard_max:
            emit()
            for seg in _split_long(piece, p):
                out.append((seg, [blk.page], blk.kind))
            continue

        buf.append(piece)
        size += need
        add_pages(blk)
        kind = blk.kind
        if size >= p.target:
            emit()

    emit()
    return out


def _split_long(text: str, p: ChunkProfile) -> list[str]:
    """切一个超过 hard_max 的长段（通常是整段没有段落分隔的 PDF 文本）。

    边界优先级：句末 → 弱边界 → 硬切。
    """
    out: list[str] = []
    rest = text
    guard = 0
    while len(rest) > p.hard_max and guard < 10_000:
        guard += 1
        cut = _best_cut(rest, p.hard_max)
        seg = rest[:cut].rstrip()
        if seg:
            out.append(seg)
        # 重叠：只在硬切兜底时用。且保证前进——否则切点靠近块首时
        # 会产出"与上一块几乎相同"的块并且原地打转。
        nxt = cut - p.overlap if p.overlap else cut
        min_advance = max(p.min_len, len(seg) // 2)
        nxt = max(nxt, min_advance)
        rest = rest[nxt:].lstrip()
    tail = rest.strip()
    if tail:
        out.append(tail)
    return out


def _hard_split(text: str, p: ChunkProfile) -> list[str]:
    return _split_long(text, p)


def _best_cut(s: str, limit: int) -> int:
    """在 `limit` 以内找最好的切点，返回切点索引。

    ## 必须先 clamp（踩过的坑）

    调用方可能传一个超过 `len(s)` 的 limit（例如"这段只有 408 字，但
    我最多允许 600"）。第一版直接用 `s[:limit]`，当 limit > len(s) 时
    后面的 `return limit` 会返回**越界索引**，调用方 `s[:cut]` 拿到全文、
    `rest = s[cut:]` 拿到空串——表现为"最后一块之后多出一个空循环"，
    而在 `while len(rest) > hard_max` 的循环里则表现为原地踏步。

    实测症状：`_split_long('字'*3000)` 产出两个**完全相同**的块。
    """
    limit = min(limit, len(s))
    if limit <= 0:
        return 0

    window = s[:limit]

    # 1) 段落边界
    for sep in ("\n\n", "\n"):
        i = window.rfind(sep)
        if i > limit // 2:
            return i + len(sep)

    # 2) 中文句末标点
    for i in range(len(window) - 1, limit // 2, -1):
        if window[i] in _CN_SENTENCE_END:
            return i + 1

    # 3) ASCII 句号：必须后跟空白或结尾（修复 Go 的缺陷 1）
    for i in range(len(window) - 2, limit // 2, -1):
        if window[i] == "." and (i + 1 >= len(s) or s[i + 1] in " \n\t\r"):
            return i + 1

    # 4) 弱边界
    for i in range(len(window) - 1, limit // 2, -1):
        if window[i] in _WEAK_BOUNDARY:
            return i + 1

    # 5) 空格
    i = window.rfind(" ")
    if i > limit // 2:
        return i + 1

    return limit


def _merge_short_tail(chunks: list[Chunk], p: ChunkProfile) -> list[Chunk]:
    """把末尾过短的块并入前一块。

    ## 为什么必须做

    文档末尾常剩一句两三个字的残留（"完。"）。它单独成块后：
      - 向量几乎是噪声（没有信息量）
      - 会占掉 top_k 的一个位置，挤掉真正有用的块

    ## 为什么只处理"尾部"

    中间的短块通常有语义（比如一张表格只有一行），合并会破坏结构。
    而尾部短块几乎总是切分残留。

    ## 为什么要检查 heading_path 相同（踩过的坑）

    两个 sheet 各只有一行数据时，各自会产出一个 25 字的短块。若不加
    这条检查，末尾的短块会被并入前一块，结果是"1月报表"与"2月报表"
    的数据混在同一块里，`heading_path` 只剩"1 月报表"——检索时
    2 月的数据会被当成 1 月的。

    判据很简单：**跨标题不合并**。标题不同的两块在语义上属于不同章节，
    合并必然污染。
    """
    if len(chunks) < 2:
        return chunks
    last = chunks[-1]
    if last.rune_count >= p.min_len:
        return chunks
    prev = chunks[-2]
    if prev.heading_path != last.heading_path:
        # 不同章节：保留独立块，哪怕很短。宁可留一个碎块，
        # 也不要让两个章节的内容互相污染。
        return chunks
    if last.page_start and prev.page_start and last.page_start != prev.page_start:
        # 不同页：同样不合并。页码是溯源的坐标，合并后来源卡片只能说
        # "第 2~3 页"，用户点进去看到的内容可能不是引用片段。
        # 实测症状：3 页、每页一行的 PDF 被切成 2 块（第二块跨页）。
        return chunks
    merged = Chunk(
        ordinal=prev.ordinal,
        text=f"{prev.text}\n{last.text}",
        page_start=prev.page_start or last.page_start,
        page_end=last.page_end or prev.page_end,
        start_rune=prev.start_rune,
        rune_count=prev.rune_count + last.rune_count + 1,
        heading_path=prev.heading_path or last.heading_path,
        chunk_type=prev.chunk_type,
    )
    return [*chunks[:-2], merged]


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    0.63 token/字来自实测（`docs/实测记录/`）。只用于日志与"块是否过大"
    的粗略判断——真正的预算由服务端算。
    """
    return int(len(text) * 0.63)


# 中文与英文混排时，`re` 用来判断"这个位置是不是词边界"。
_WORD_CHAR = re.compile(r"[0-9A-Za-z_]")


def is_word_boundary(s: str, i: int) -> bool:
    """位置 `i` 是否是词边界（切在这里不会把一个英文单词劈开）。"""
    if i <= 0 or i >= len(s):
        return True
    return not (_WORD_CHAR.match(s[i - 1]) and _WORD_CHAR.match(s[i]))
