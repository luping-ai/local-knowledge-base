"""纯文本与 Markdown。

## 为什么先做这个

它是唯一"解析不复杂、但结构信息全靠自己推断"的格式。Markdown 的标题
（`#`）是显式的，所以标题栈能做得比 PDF 精确得多；而纯文本没有任何
标记，只能整篇按段落切。两者共用一条代码路径，用 `suffix` 区分。

## 编码：为什么优先 UTF-8、失败才猜 GBK

方案的 §5.6 要求"非 UTF-8 编码要给出可操作的原因"。但直接拒绝 GBK 文件
对中文用户很不友好——大量老文件就是 GBK。折中：

    1. 先按 UTF-8 严格解码
    2. 失败则尝试 GBK（覆盖绝大多数中文老文件）
    3. 都失败才报错，并说明编码

不引入 `chardet` 之类的探测库：它会为了几 MB 的文件把整个内容读两遍，
而且猜错的概率不低（GBK 与 GB18030 的短文本极易混淆）。
"""

from __future__ import annotations

import re
from pathlib import Path

from localkb.parse.types import Block, BlockKind, ParsedDoc, ParseError

__all__ = ["parse_text", "parse_markdown"]

# Markdown 标题：1~6 个 # 后跟空格
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# 代码围栏
_FENCE = re.compile(r"^```")
# 列表项：- * + 或 1. 2)
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# 单个段落超过这个长度时按句切分（避免切块器收到"一整篇无换行"的巨型块）
_MAX_PARA = 2000


def _decode(raw: bytes) -> tuple[str, str]:
    """解码。返回 (文本, 使用的编码)。

    ## 为什么 BOM 要单独处理

    带 BOM 的 UTF-8 文件（Windows 记事本默认）用 `utf-8` 解码会在开头
    留下 `\\ufeff`，它跟着第一块进向量，等于给所有中文查询加了个噪声字符。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk"), "gbk"
    except UnicodeDecodeError as e:
        # GB18030 是 GBK 的超集，再试一次（部分生僻字只有它有）
        try:
            return raw.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            raise ParseError(
                "文件不是 UTF-8 编码（可能是 GBK），请转换后重试。",
                detail=str(e),
            ) from None


def parse_text(path: Path) -> ParsedDoc:
    """纯文本。没有结构，整篇按空行分段。"""
    raw = path.read_bytes()
    if not raw.strip():
        raise ParseError("文件是空的，没有内容可索引。")
    text, enc = _decode(raw)

    blocks: list[Block] = []
    for para in _split_paragraphs(text):
        for piece in _split_oversized(para):
            blocks.append(Block(text=piece, kind=BlockKind.PARAGRAPH))

    doc = ParsedDoc(blocks=blocks, parser=f"text/{enc}", total_chars=len(text))
    if enc not in ("utf-8", "utf-8-sig"):
        # 用非 UTF-8 解码成功时提示用户（内容可能有个别字错乱），
        # 但不阻止入库——老文件能读出来比读不出来有用。
        doc.warnings.append(
            f"文件用 {enc} 编码读取。若发现个别字符错乱，请转为 UTF-8 后重新扫描。"
        )
    return doc


def parse_markdown(path: Path) -> ParsedDoc:
    """Markdown。`#` 标题映射为 heading 块（级别来自 # 的数量）。"""
    raw = path.read_bytes()
    if not raw.strip():
        raise ParseError("文件是空的，没有内容可索引。")
    text, enc = _decode(raw)

    blocks: list[Block] = []
    buf: list[str] = []
    in_code = False

    def flush(kind: BlockKind = BlockKind.PARAGRAPH) -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if not body:
            return
        for piece in _split_oversized(body):
            blocks.append(Block(text=piece, kind=kind))

    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            if in_code:
                # 代码块结束：整块作为一个 CODE 块
                flush(BlockKind.CODE)
                in_code = False
            else:
                flush()
                in_code = True
            continue

        if in_code:
            buf.append(line)
            continue

        m = _MD_HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            if title:
                blocks.append(Block(text=title, kind=BlockKind.HEADING,
                                    level=level))
            continue

        if not line.strip():
            flush()
            continue

        if _LIST.match(line):
            # 列表项单独成块会导致大量 20 字的碎块，所以先攒着，
            # 遇到空行再一起 flush。kind 记为 LIST_ITEM 保留语义。
            buf.append(line.strip())
            continue

        buf.append(line)

    flush(BlockKind.CODE if in_code else BlockKind.PARAGRAPH)

    doc = ParsedDoc(blocks=blocks, parser=f"markdown/{enc}",
                    total_chars=len(text))
    if enc not in ("utf-8", "utf-8-sig"):
        doc.warnings.append(
            f"文件用 {enc} 编码读取。若发现个别字符错乱，请转为 UTF-8 后重新扫描。"
        )
    return doc


def _split_paragraphs(text: str) -> list[str]:
    """按空行分段。单个换行不分段（那是 PDF 复制或硬换行，不是新段落）。"""
    out = []
    for chunk in re.split(r"\n\s*\n", text):
        s = chunk.strip()
        if s:
            out.append(s)
    return out


def _split_oversized(para: str, limit: int = _MAX_PARA) -> list[str]:
    """段落本身超长时按句末标点切开。

    ## 为什么在解析层就切

    切块器能处理超长块，但它只有一个"硬上限 600"的约束，会按 600 硬切。
    而一段 5000 字、内有句号的文本，按句号切比按 600 字切更合理——这个
    信息（哪里是句子）只有解析阶段最清楚。
    """
    if len(para) <= limit:
        return [para]
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    for sent in re.split(r"(?<=[。！？；.!?;])", para):
        if not sent:
            continue
        if size + len(sent) > limit and cur:
            parts.append("".join(cur).strip())
            cur, size = [], 0
        cur.append(sent)
        size += len(sent)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]
