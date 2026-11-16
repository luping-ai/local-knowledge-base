"""DOCX：靠样式拿标题层级（Go 侧做不到的那件事）。

## 为什么 DOCX 的标题最容易做对

Word 把"这是几级标题"**显式**存在段落样式里（`Heading 1/2/3` 或
`outlineLvl`）。这不是猜测，是作者本来的意图。所以：

    paragraph.style.name == "Heading 2"  →  level 2
    paragraph.paragraph_format.outline_level == 1 → level 2（0 基）

## 两种来源都要看

- **样式名**：中文版 Word 可能存成 "标题 2"（本地化）。所以除了英文
  `Heading N`，还要匹配 `标题 N`。
- **outlineLvl**：有些文档用了自定义样式但设了轮廓级别。只读样式名会
  漏掉这批。

两者优先级：outlineLvl 更可靠（它是语义，样式名只是外观约定），但
python-docx 对 outlineLvl 的暴露是 `paragraph.paragraph_format.
outline_level`，在某些文件上返回 None。所以先看样式名、再回退 outlineLvl。

## 表格

Word 表格在 `doc.tables` 里，与段落**不在同一个序列**。直接遍历
`doc.paragraphs` 会完全丢掉表格内容。必须按 body 的 XML 顺序遍历，
否则表格内容会全部落到文档末尾，页码/顺序全乱。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from localkb.parse.types import Block, BlockKind, ParsedDoc, ParseError, UnsupportedError

__all__ = ["parse_docx"]

# "Heading 2" / "标题 2" / "h2"
_HEADING_RE = re.compile(r"^(?:heading|标题|h)\s*(\d)$", re.IGNORECASE)

# docx 的 XML 命名空间
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def parse_docx(path: Path) -> ParsedDoc:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        raise ParseError(
            "缺少 python-docx 依赖，无法解析 Word 文档。",
        ) from None

    if path.suffix.lower() in (".doc", ".xls", ".ppt"):
        raise UnsupportedError(
            "这是旧版 Office 格式，请另存为 .docx / .xlsx 后重新扫描。",
        )

    try:
        doc = Document(str(path))
    except zipfile.BadZipFile as e:
        raise ParseError("文件已损坏，无法读取。", detail=str(e)) from None
    except KeyError as e:
        # 缺 word/document.xml：zip 能打开但不是合法 docx
        raise ParseError(
            "文件已损坏或不是有效的 Word 文档。", detail=str(e),
        ) from None
    except Exception as e:  # noqa: BLE001 - python-docx 的异常类型不统一
        # python-docx 对"不是 zip"抛的是 `PackageNotFoundError`
        # （python-docx 自定义，继承 Exception），而不是 zipfile.BadZipFile。
        # 实测坑：只捕 BadZipFile 会让损坏文件落到"无法打开"这种
        # 指向不明的文案上。这里按消息判断，给出"已损坏"这个可行动的结论。
        msg = str(e)
        if "package not found" in msg.lower() or "not a zip" in msg.lower():
            raise ParseError("文件已损坏，无法读取。", detail=msg) from None
        raise ParseError("无法打开这个 Word 文档。", detail=msg) from None

    blocks: list[Block] = []
    warnings: list[str] = []
    total_chars = 0

    # 按 body 的 XML 顺序遍历：段落与表格交替出现，顺序必须保持
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            para = Paragraph(child, doc)
            blk = _paragraph_block(para, warnings)
            if blk is not None:
                blocks.append(blk)
                total_chars += len(blk.text)
        elif tag == qn("w:tbl"):
            table = Table(child, doc)
            blocks.extend(_table_blocks(table))
            total_chars += sum(len(b.text) for b in _table_blocks(table))

    if not blocks:
        raise ParseError(
            "这个 Word 文档里没有可提取的文字（可能全是图片或文本框）。",
        )

    return ParsedDoc(blocks=blocks, parser="docx", total_chars=total_chars,
                     warnings=warnings)


def _heading_level(para) -> int:
    """返回标题级别（1 起），非标题返回 0。"""
    # 1) 样式名
    try:
        name = para.style.name or ""
    except (AttributeError, KeyError):
        name = ""
    m = _HEADING_RE.match(name.strip())
    if m:
        return int(m.group(1))
    # 中文版 Word 常见 "标题 1"；上面已覆盖，这里处理 "标题1"（无空格）
    m2 = re.match(r"^(?:heading|标题)\s*(\d)$", name.strip(), re.IGNORECASE)
    if m2:
        return int(m2.group(1))

    # 2) outlineLvl（0 基 → 1 基）
    try:
        lvl = para.paragraph_format.outline_level
    except (AttributeError, KeyError):
        lvl = None
    if lvl is not None:
        try:
            return int(lvl) + 1
        except (TypeError, ValueError):
            pass
    return 0


def _paragraph_block(para, warnings: list[str]) -> Block | None:
    text = (para.text or "").strip()
    level = _heading_level(para)
    if not text:
        return None
    if level:
        return Block(text=text, kind=BlockKind.HEADING, level=level)
    # 列表项：Word 的编号是段落属性，python-docx 不给"是否列表"的便捷接口，
    # 只能看样式名里有没有 List
    try:
        style = (para.style.name or "").lower()
    except (AttributeError, KeyError):
        style = ""
    kind = BlockKind.LIST_ITEM if "list" in style or "列表" in style \
        else BlockKind.PARAGRAPH
    return Block(text=text, kind=kind)


def _table_blocks(table) -> list[Block]:
    """把一个 Word 表格变成"表头 + 行"的块序列。

    ## 为什么第一行当表头

    Word 表格没有"表头"标记（除非设了 `tblHeader` 属性，但极少用）。
    第一行当表头是行业惯例，也是"每块重复表头"（§5.3）唯一可用的启发式。

    代价：第一行不是表头的表格（比如整表都是数据）会多一个冗余前缀。
    这个代价可以接受——多一个前缀不影响检索，而缺列名会让整块失效。
    """
    out: list[Block] = []
    header_text = ""
    for r_idx, row in enumerate(table.rows):
        cells = []
        for cell in row.cells:
            t = (cell.text or "").strip().replace("\n", " ")
            cells.append(t)
        # 合并单元格会让 python-docx 返回重复的 cell 引用，去重相邻重复
        deduped: list[str] = []
        for c in cells:
            if not deduped or deduped[-1] != c:
                deduped.append(c)
        line = "\t".join(deduped).strip()
        if not line:
            continue
        if r_idx == 0:
            header_text = line
            out.append(Block(text=line, kind=BlockKind.TABLE_ROW, is_header=True))
        else:
            out.append(Block(text=line, kind=BlockKind.TABLE_ROW,
                             header_text=header_text))
    return out
