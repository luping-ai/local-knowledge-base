"""PPTX 解析器测试：真造 pptx 文件，不 mock。

## 重点测什么

PPTX 的解析难点不在"取文字"，而在**结构**：

  1. 页码必须是幻灯片序号（`page = slide_number`），否则溯源跳错页
  2. 标题占位符要识别成 HEADING（方案 §5.2 标"高"可靠性）
  3. 表格内容不能丢（`shape.text_frame` 对表格不存在，只读它会静默丢内容）
  4. 纯图片 PPT 要报 `UnsupportedError` 而不是"损坏"

第 3 条是这类解析器最典型的静默失效：不报错、不抛异常，内容就是不在了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")

from localkb.parse.registry import parse  # noqa: E402
from localkb.parse.types import BlockKind, ParseError, UnsupportedError  # noqa: E402


def write_pptx(p: Path, slides: list[dict]) -> Path:
    """造 pptx。

    `slides` 每项：{"title": str, "bullets": [str], "table": [[...]]}
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    # 用 "标题和内容" 版式（有标题占位符 + 正文占位符）
    layout = prs.slide_layouts[1]

    for spec in slides:
        slide = prs.slides.add_slide(layout)
        if spec.get("title"):
            slide.shapes.title.text = spec["title"]

        bullets = spec.get("bullets") or []
        if bullets:
            body = slide.placeholders[1]
            body.text = bullets[0]
            for extra in bullets[1:]:
                body.text_frame.add_paragraph().text = extra

        if spec.get("table"):
            rows = spec["table"]
            n_rows, n_cols = len(rows), len(rows[0])
            shape = slide.shapes.add_table(
                n_rows, n_cols, Inches(1), Inches(3),
                Inches(4), Inches(1),
            )
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    shape.table.cell(r, c).text = str(val)

    prs.save(str(p))
    return p


# ------------------------------------------------------------ 基本解析

def test_one_block_per_slide_with_page_number(tmp_path: Path) -> None:
    """每页一块，页码 = 幻灯片序号（1 基）。"""
    p = write_pptx(tmp_path / "a.pptx", [
        {"title": "第一页标题", "bullets": ["第一页要点一", "第一页要点二"]},
        {"title": "第二页标题", "bullets": ["第二页要点"]},
        {"title": "第三页标题", "bullets": ["第三页要点"]},
    ])
    doc = parse(p)

    pages = {b.page for b in doc.blocks}
    assert pages == {1, 2, 3}, f"页码应覆盖 1/2/3，实际 {pages}"

    # 每页的正文应带上本页的文字
    texts = "\n".join(b.text for b in doc.blocks)
    assert "第一页要点一" in texts
    assert "第三页要点" in texts


def test_title_becomes_heading(tmp_path: Path) -> None:
    """标题占位符 → HEADING，并成为同页正文的 heading。"""
    p = write_pptx(tmp_path / "b.pptx", [
        {"title": "季度总结", "bullets": ["营收增长 20%"]},
    ])
    doc = parse(p)

    heads = [b for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert heads, "标题占位符没被识别成 HEADING"
    assert heads[0].text == "季度总结"
    assert heads[0].page == 1
    assert heads[0].level == 1

    bodies = [b for b in doc.blocks if b.kind is BlockKind.PARAGRAPH]
    assert bodies[0].heading == "季度总结", "正文块没带上标题路径"
    assert "营收增长" in bodies[0].text


def test_parser_name_and_total_chars(tmp_path: Path) -> None:
    p = write_pptx(tmp_path / "c.pptx", [
        {"title": "标题", "bullets": ["正文内容"]},
    ])
    doc = parse(p)
    assert doc.parser == "pptx"
    assert doc.total_chars > 0


# ------------------------------------------------------------ 表格（最容易静默丢内容）

def test_table_content_is_extracted(tmp_path: Path) -> None:
    """表格内容必须出现——`shape.text_frame` 对表格不存在，只读它会全丢。"""
    p = write_pptx(tmp_path / "d.pptx", [
        {"title": "数据页", "table": [
            ["公司", "销售额"],
            ["A 公司", "120"],
            ["B 公司", "95"],
        ]},
    ])
    doc = parse(p)
    texts = "\n".join(b.text for b in doc.blocks)

    for cell in ("公司", "销售额", "A 公司", "120", "B 公司", "95"):
        assert cell in texts, f"表格单元格「{cell}」丢失，实际：{texts!r}"


def test_table_rows_keep_line_breaks(tmp_path: Path) -> None:
    """表格按行拼接（每行独立），不是把整表粘成一行。"""
    p = write_pptx(tmp_path / "e.pptx", [
        {"title": "T", "table": [["列1", "列2"], ["v1", "v2"]]},
    ])
    doc = parse(p)
    body = "\n".join(b.text for b in doc.blocks if b.kind is BlockKind.PARAGRAPH)
    assert "列1\t列2" in body or "列1" in body
    assert "\n" in body, f"表格行之间应有换行，实际 {body!r}"


# ------------------------------------------------------------ 异常路径

def test_image_only_slides_are_unsupported_not_broken(tmp_path: Path) -> None:
    """纯图片 PPT → UnsupportedError（不是 ParseError）。

    ## 为什么区分这两种

    "没坏，只是我们做不到"要给用户"转文本"的动作；"坏了"要给"检查文件"。
    报成 ParseError 会让用户去找"文件哪里损坏了"，而文件其实完好。
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式，无占位符
    # 造一个极小的 png（1x1 透明）
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
           b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc"
           b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    img = tmp_path / "x.png"
    img.write_bytes(png)
    slide.shapes.add_picture(str(img), Inches(1), Inches(1))

    p = tmp_path / "f.pptx"
    prs.save(str(p))

    with pytest.raises(UnsupportedError) as ei:
        parse(p)
    assert "OCR" in ei.value.reason, f"文案要说明为什么不支持，实际 {ei.value.reason!r}"


def test_corrupt_file_raises_parse_error(tmp_path: Path) -> None:
    """不是 zip 的文件 → ParseError（可行动：检查文件）。"""
    p = tmp_path / "g.pptx"
    p.write_bytes(b"this is definitely not a pptx")
    with pytest.raises(ParseError) as ei:
        parse(p)
    assert not isinstance(ei.value, UnsupportedError)


def test_legacy_ppt_hint(tmp_path: Path) -> None:
    """`.ppt` 旧格式 → 提示另存为 `.pptx`。"""
    p = tmp_path / "h.ppt"
    p.write_bytes(b"anything")
    with pytest.raises(UnsupportedError) as ei:
        parse(p)
    assert ".pptx" in ei.value.reason


def test_empty_slides_are_skipped_with_warning(tmp_path: Path) -> None:
    """有空白页时应跳过并给出提示，而不是静默丢掉页号。"""
    p = write_pptx(tmp_path / "i.pptx", [
        {"title": "有内容", "bullets": ["正文"]},
        {},  # 空白页
        {"title": "又有了", "bullets": ["更多"]},
    ])
    doc = parse(p)
    assert any("跳过" in w for w in doc.warnings), f"应提示跳过空白页，实际 {doc.warnings}"
    # 空白页不应产生块，但页码仍是真实序号
    pages = {b.page for b in doc.blocks}
    assert 1 in pages and 3 in pages


# ------------------------------------------------------------ 与切块器的配合

def test_slides_do_not_get_merged_by_chunker(tmp_path: Path) -> None:
    """短页不能被切块器合并——否则页码精度丢失。

    这正是 `splitter` 的 `cross_page` 规则要保证的：不同页的短句各自成块。
    """
    from localkb.chunk.splitter import split

    p = write_pptx(tmp_path / "j.pptx", [
        {"title": "第一页", "bullets": ["短"]},
        {"title": "第二页", "bullets": ["也很短"]},
        {"title": "第三页", "bullets": ["同样短"]},
    ])
    doc = parse(p)
    chunks, _info = split(doc)

    chunk_pages = {c.page_start for c in chunks if c.page_start}
    assert len(chunk_pages) >= 3, (
        f"3 页短内容被合并了，页码只剩 {chunk_pages}——"
        "来源卡片会指错页"
    )
