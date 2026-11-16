"""XLSX：按"表头 + N 行"切块（Go 侧把整表拼成一片的那个缺陷）。

## 问题的本质

Go 的 `ooxml` 把整个 sheet 拼成一片文本，表头是第一行。450 字切下来，
第一块有表头，后面的块只有一堆没有列名的数字。用户问"某公司上个月销售额
多少"，后面的块里根本没有"销售额"这个词——语义通道也救不了，因为那串
数字的向量里不含任何列语义。

## 这条路径的两个关键决定

1. **每个 sheet 独立处理**：sheet 名本身就是重要的结构信息
   （"1月报表" vs "2月报表"），混在一个 chunk 里会让两块内容互相污染。
2. **表头行不消耗行预算**：表头每块都要重复，若把它算进"N 行"里，
   列多的表会切成大量只有两三行的块。

## 公式与缓存值

`data_only=True` 读的是 Excel **上次计算后缓存**的值，而不是公式。
这与 Go 侧一致，也是唯一可行的选择：解析器无法重算公式（那需要一个
公式引擎）。代价是"改了公式但没在 Excel 里打开过"的文件会读到 None，
所以要给出可读的提示，而不是让列里出现一片空白。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from localkb.parse.types import Block, BlockKind, ParsedDoc, ParseError, UnsupportedError

__all__ = ["parse_xlsx"]

# 每个块大致容纳多少行（列多时减少）
_ROWS_PER_CHUNK_MANY_COLS = 8
_ROWS_PER_CHUNK_FEW_COLS = 20
# 列数超过这个值就按"多列"处理
_MANY_COLS = 6


def parse_xlsx(path: Path) -> ParsedDoc:
    try:
        import openpyxl
    except ImportError:
        raise ParseError("缺少 openpyxl 依赖，无法解析 Excel。") from None

    if path.suffix.lower() == ".xls":
        raise UnsupportedError(
            "这是旧版 Excel 格式（.xls），请另存为 .xlsx 后重新扫描。",
        )

    try:
        wb = openpyxl.load_workbook(str(path), data_only=True,
                                    read_only=True)
    except zipfile.BadZipFile as e:
        raise ParseError("文件已损坏，无法读取。", detail=str(e)) from None
    except Exception as e:  # noqa: BLE001
        raise ParseError("无法打开这个 Excel 文件。", detail=str(e)) from None

    blocks: list[Block] = []
    warnings: list[str] = []
    total_chars = 0
    empty_sheets: list[str] = []
    all_none_sheets: list[str] = []

    try:
        for ws in wb.worksheets:
            sheet_blocks, stats = _sheet_blocks(ws)
            if not sheet_blocks:
                empty_sheets.append(ws.title)
                continue
            if stats["all_none"]:
                all_none_sheets.append(ws.title)
            blocks.extend(sheet_blocks)
            total_chars += sum(len(b.text) for b in sheet_blocks)
    finally:
        wb.close()

    if not blocks:
        raise ParseError(
            "这个 Excel 里没有可提取的文字（所有工作表都是空的）。",
        )

    if all_none_sheets:
        # 这是"公式没算过"的典型症状：单元格有公式但缓存值为空。
        # 不提示的话用户会看到某些列整片空白，以为是导入出了问题。
        warnings.append(
            "这些工作表里有公式但还没有计算结果（"
            + "、".join(all_none_sheets[:3])
            + "）。请用 Excel 打开并保存一次后重新扫描。"
        )
    if empty_sheets:
        warnings.append(
            f"跳过了 {len(empty_sheets)} 个空工作表："
            + "、".join(empty_sheets[:5])
        )

    return ParsedDoc(blocks=blocks, parser="xlsx", total_chars=total_chars,
                     warnings=warnings)


def _sheet_blocks(ws) -> tuple[list[Block], dict]:
    """把一个工作表变成"sheet 标题 + 表头 + 行组"的块序列。"""
    rows: list[list[str]] = []
    all_none = True
    for row in ws.iter_rows(values_only=True):
        cells = ["" if v is None else str(v).strip() for v in row]
        if any(cells):
            all_none = False
        if not any(cells):
            continue  # 全空行跳过（Excel 里常见大量空行）
        rows.append(cells)

    stats = {"all_none": all_none and bool(rows)}
    if not rows:
        return [], stats

    # 去掉右侧整列为空的部分（避免大量无意义的 \t）
    width = max(len(r) for r in rows)
    while width > 1 and all(len(r) < width or not r[width - 1] for r in rows):
        width -= 1
    rows = [(r + [""] * width)[:width] for r in rows]

    header = rows[0]
    header_text = "\t".join(header).strip()
    ncols = sum(1 for c in header if c) or 1
    rows_per_chunk = (_ROWS_PER_CHUNK_MANY_COLS if ncols >= _MANY_COLS
                      else _ROWS_PER_CHUNK_FEW_COLS)

    blocks: list[Block] = []
    # sheet 名作为一级标题：它是最强的结构信息
    blocks.append(Block(text=ws.title, kind=BlockKind.HEADING, level=1))
    blocks.append(Block(text=header_text, kind=BlockKind.TABLE_ROW,
                        is_header=True))

    for row in rows[1:]:
        line = "\t".join(row).strip()
        if not line:
            continue
        blocks.append(Block(text=line, kind=BlockKind.TABLE_ROW,
                            header_text=header_text))

    # 用 `header_text` 让切块器知道每块要重复什么；行数预算由切块器的
    # 字符数目标间接控制。这里额外给一个提示性分组，便于将来按行限流。
    stats["rows_per_chunk"] = rows_per_chunk
    return blocks, stats
