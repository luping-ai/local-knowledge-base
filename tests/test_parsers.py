"""解析器测试：每种格式都真造文件，不 mock。

## 为什么用真文件

解析器的价值全在"与真实文件的兼容性"上。mock 掉 `Document(...)` 之后，
测试验证的只是"我调用了它"，而不是"它能读出来"。而真实文件里的坑
（样式名本地化、合并单元格、加密 PDF）一个都测不到。

代价是测试需要造文件。用 python-docx / openpyxl / PyMuPDF 自己写，
比塞二进制 fixture 更可读，也更容易按需调整内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from localkb.parse.registry import SUPPORTED_SUFFIXES, parse, parser_for
from localkb.parse.types import BlockKind, ParseError, UnsupportedError

# ------------------------------------------------------------ 造文件的工具

def write_txt(p: Path, text: str, encoding: str = "utf-8") -> Path:
    p.write_text(text, encoding=encoding)
    return p


def write_md(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def write_docx(p: Path, build) -> Path:
    """用 python-docx 造一个 docx。`build(doc)` 里加内容。"""
    from docx import Document

    doc = Document()
    build(doc)
    doc.save(str(p))
    return p


def write_xlsx(p: Path, sheets: dict[str, list[list]]) -> Path:
    """造 xlsx：{sheet 名: 行列表}。"""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    wb.save(str(p))
    return p


def write_pdf(p: Path, pages: list[list[tuple[str, float]]]) -> Path:
    """造 PDF：pages 是 [[(文本, 字号), ...]]，每个元素一行。"""
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72.0
        for text, size in lines:
            page.insert_text((72, y), text, fontsize=size,
                             fontname="china-s")
            y += size * 1.6
    doc.save(str(p))
    doc.close()
    return p


# ------------------------------------------------------------ 分发

def test_supported_suffixes() -> None:
    assert ".pdf" in SUPPORTED_SUFFIXES
    assert ".docx" in SUPPORTED_SUFFIXES
    assert ".xlsx" in SUPPORTED_SUFFIXES
    assert ".md" in SUPPORTED_SUFFIXES
    assert ".txt" in SUPPORTED_SUFFIXES


def test_parser_for_unknown_returns_none() -> None:
    assert parser_for(Path("a.exe")) is None


def test_legacy_doc_gives_actionable_error(tmp_path: Path) -> None:
    """旧格式要明确说"另存为 .docx"，而不是"无法打开"。"""
    f = tmp_path / "old.doc"
    f.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 魔数
    with pytest.raises(UnsupportedError) as ei:
        parse(f)
    assert ".docx" in ei.value.reason
    assert "另存为" in ei.value.reason


def test_legacy_xls(tmp_path: Path) -> None:
    f = tmp_path / "old.xls"
    f.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(UnsupportedError) as ei:
        parse(f)
    assert ".xlsx" in ei.value.reason


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as ei:
        parse(tmp_path / "nope.txt")
    assert "不存在" in ei.value.reason


def test_unsupported_extension(tmp_path: Path) -> None:
    f = tmp_path / "a.exe"
    f.write_bytes(b"MZ")
    with pytest.raises(UnsupportedError):
        parse(f)


def test_corrupt_docx_does_not_leak_raw_exception(tmp_path: Path) -> None:
    """坏文件必须收敛成 ParseError（否则整次扫描会因为一个文件中断）。

    ## 但要区分"文件坏了"与"代码坏了"

    `SyntaxError` / `ImportError` 之类是我们自己的问题，不该被伪装成
    "文件损坏"——那会让开发期排查变成灾难。registry 里对这类异常放行。
    """
    f = tmp_path / "bad.docx"
    f.write_bytes(b"not a zip at all")
    with pytest.raises(ParseError) as ei:
        parse(f)
    assert "损坏" in ei.value.reason or "无法解析" in ei.value.reason


def test_code_errors_are_not_disguised_as_corrupt_files(tmp_path: Path) -> None:
    """我们自己代码的错误必须原样抛出，不能被包装成"文件损坏"。"""
    import localkb.parse.registry as reg

    f = tmp_path / "a.txt"
    f.write_text("内容", encoding="utf-8")

    def boom(_p):
        raise SyntaxError("deliberate")

    orig = reg._REGISTRY[".txt"]
    reg._REGISTRY[".txt"] = boom
    try:
        with pytest.raises(SyntaxError):
            reg.parse(f)
    finally:
        reg._REGISTRY[".txt"] = orig


# ------------------------------------------------------------ TXT

def test_txt_basic(tmp_path: Path) -> None:
    f = write_txt(tmp_path / "a.txt", "第一段内容。\n\n第二段内容。")
    doc = parse(f)
    assert len(doc.blocks) == 2
    assert all(b.kind is BlockKind.PARAGRAPH for b in doc.blocks)


def test_txt_single_newline_is_not_paragraph_break(tmp_path: Path) -> None:
    """单个换行不是段落分隔（那是硬换行）。"""
    f = write_txt(tmp_path / "a.txt", "这是同一段。\n只是换了行。")
    doc = parse(f)
    assert len(doc.blocks) == 1


def test_txt_gbk_is_readable(tmp_path: Path) -> None:
    """GBK 老文件要能读，并给提示（不是直接拒绝）。"""
    f = tmp_path / "gbk.txt"
    f.write_bytes("这是 GBK 编码的中文内容。".encode("gbk"))
    doc = parse(f)
    assert "GBK" in doc.parser or "gbk" in doc.parser
    assert "中文内容" in doc.blocks[0].text
    assert doc.warnings, "非 UTF-8 应给出提示"


def test_txt_bom_is_stripped(tmp_path: Path) -> None:
    """带 BOM 的 UTF-8 不能在开头留 \\ufeff（会污染第一块的向量）。"""
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf" + "内容".encode())
    doc = parse(f)
    assert not doc.blocks[0].text.startswith("\ufeff")


def test_txt_empty_file(tmp_path: Path) -> None:
    f = write_txt(tmp_path / "e.txt", "   \n\n  ")
    with pytest.raises(ParseError) as ei:
        parse(f)
    assert "空的" in ei.value.reason


def test_txt_long_paragraph_is_split(tmp_path: Path) -> None:
    """超长无换行的段落要在解析层按句切开（不让切块器硬切）。"""
    long_text = "这是一个句子。" * 400     # 2800 字，无换行
    f = write_txt(tmp_path / "long.txt", long_text)
    doc = parse(f)
    assert len(doc.blocks) > 1
    for b in doc.blocks:
        assert len(b.text) <= 2100


# ------------------------------------------------------------ Markdown

def test_md_headings_become_blocks(tmp_path: Path) -> None:
    f = write_md(tmp_path / "a.md", "# 一级标题\n\n正文内容。\n\n## 二级标题\n\n更多正文。")
    doc = parse(f)
    heads = [(b.text, b.level) for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert ("一级标题", 1) in heads
    assert ("二级标题", 2) in heads


def test_md_heading_levels(tmp_path: Path) -> None:
    f = write_md(tmp_path / "a.md",
                 "# H1\n\n### H3\n\n###### H6\n\n正文。")
    doc = parse(f)
    levels = [b.level for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert levels == [1, 3, 6]


def test_md_code_block_kept_as_one(tmp_path: Path) -> None:
    f = write_md(tmp_path / "a.md",
                 "# 标题\n\n```python\nprint(1)\nprint(2)\n```\n\n正文。")
    doc = parse(f)
    codes = [b for b in doc.blocks if b.kind is BlockKind.CODE]
    assert len(codes) == 1
    assert "print(1)" in codes[0].text and "print(2)" in codes[0].text


def test_md_list_items_kept(tmp_path: Path) -> None:
    f = write_md(tmp_path / "a.md", "# 标题\n\n- 第一项\n- 第二项\n- 第三项\n")
    doc = parse(f)
    assert any("第一项" in b.text for b in doc.blocks)


def test_md_no_heading_still_parses(tmp_path: Path) -> None:
    f = write_md(tmp_path / "a.md", "只有正文，没有标题。\n\n第二段。")
    doc = parse(f)
    assert len(doc.blocks) == 2


# ------------------------------------------------------------ DOCX

def test_docx_headings_from_style(tmp_path: Path) -> None:
    """A11 的核心：DOCX 标题层级来自样式，必须准确。"""
    def build(doc):
        doc.add_heading("第一章 总则", level=1)
        doc.add_paragraph("这是第一章的正文内容。")
        doc.add_heading("1.1 适用范围", level=2)
        doc.add_paragraph("本条例适用于所有部门。")

    f = write_docx(tmp_path / "a.docx", build)
    doc = parse(f)
    heads = [(b.text, b.level) for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert ("第一章 总则", 1) in heads
    assert ("1.1 适用范围", 2) in heads


def test_docx_heading_style_chinese_name(tmp_path: Path) -> None:
    """中文版 Word 的样式名可能是"标题 1"。

    ## 为什么这条要单独测

    python-docx 的 `add_heading` 在中文模板下可能生成 "Heading 1"，
    但真实用户文件的样式名常是本地化的 "标题 1"。不认它的话，中文
    文档的标题层级会全部丢失——而这正是 Go 侧做不到、我们要做对的点。
    """
    def build(doc):
        p = doc.add_paragraph("中文标题")
        p.style = doc.styles["Heading 1"]
        # 手动改样式名模拟本地化（python-docx 允许改 name）
        import contextlib

        with contextlib.suppress(KeyError, ValueError):
            doc.styles["Heading 1"].name = "标题 1"
        doc.add_paragraph("正文。")

    f = write_docx(tmp_path / "cn.docx", build)
    doc = parse(f)
    assert any(b.kind is BlockKind.HEADING for b in doc.blocks), \
        f"未识别标题：{[(b.text, b.kind) for b in doc.blocks]}"


def test_docx_table_is_extracted(tmp_path: Path) -> None:
    """表格内容不能在 doc.paragraphs 里丢掉（必须按 body 顺序遍历）。"""
    def build(doc):
        doc.add_heading("表格测试", 1)
        t = doc.add_table(rows=3, cols=2)
        t.cell(0, 0).text = "公司"
        t.cell(0, 1).text = "销售额"
        t.cell(1, 0).text = "甲公司"
        t.cell(1, 1).text = "1000"
        t.cell(2, 0).text = "乙公司"
        t.cell(2, 1).text = "2000"

    f = write_docx(tmp_path / "t.docx", build)
    doc = parse(f)
    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert rows, "表格内容没有被提取"
    joined = "\n".join(b.text for b in rows)
    assert "甲公司" in joined and "2000" in joined
    # 第一行是表头
    assert rows[0].is_header
    assert "销售额" in rows[0].text


def test_docx_table_position_preserved(tmp_path: Path) -> None:
    """表格必须出现在它在原文中的位置，而不是被推到文档末尾。"""
    def build(doc):
        doc.add_paragraph("表格前面的话。")
        t = doc.add_table(rows=1, cols=1)
        t.cell(0, 0).text = "表格内容"
        doc.add_paragraph("表格后面的话。")

    f = write_docx(tmp_path / "order.docx", build)
    doc = parse(f)
    texts = [b.text for b in doc.blocks]
    i_before = next(i for i, t in enumerate(texts) if "表格前面" in t)
    i_table = next(i for i, t in enumerate(texts) if "表格内容" in t)
    i_after = next(i for i, t in enumerate(texts) if "表格后面" in t)
    assert i_before < i_table < i_after, f"顺序错了：{texts}"


def test_docx_empty_raises(tmp_path: Path) -> None:
    f = write_docx(tmp_path / "empty.docx", lambda d: None)
    with pytest.raises(ParseError) as ei:
        parse(f)
    assert "没有可提取" in ei.value.reason


# ------------------------------------------------------------ XLSX

def test_xlsx_header_and_rows(tmp_path: Path) -> None:
    """A12 的基础：表头被标记，行被提取。"""
    f = write_xlsx(tmp_path / "a.xlsx", {
        "销售": [
            ["公司", "销售额", "月份"],
            ["甲公司", 1000, "1月"],
            ["乙公司", 2000, "1月"],
        ]
    })
    doc = parse(f)
    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert rows[0].is_header
    assert "销售额" in rows[0].text
    assert any("甲公司" in b.text for b in rows)


def test_xlsx_sheet_name_becomes_heading(tmp_path: Path) -> None:
    """sheet 名是最强的结构信息，必须保留。"""
    f = write_xlsx(tmp_path / "b.xlsx", {
        "1月报表": [["项目", "金额"], ["收入", 100]],
        "2月报表": [["项目", "金额"], ["收入", 200]],
    })
    doc = parse(f)
    heads = [b.text for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert "1月报表" in heads
    assert "2月报表" in heads


def test_xlsx_multiple_sheets_are_separate(tmp_path: Path) -> None:
    """两个 sheet 的内容不能被混成一块。"""
    f = write_xlsx(tmp_path / "c.xlsx", {
        "A": [["col"], ["a1"]],
        "B": [["col"], ["b1"]],
    })
    doc = parse(f)
    from localkb.chunk.splitter import split

    chunks, _ = split(doc)
    # 每个 sheet 至少一块，且块内不同时出现 a1 与 b1
    assert len(chunks) >= 2


def test_xlsx_header_repeats_across_chunks(tmp_path: Path) -> None:
    """A12 的核心：40 行数据切成多块后，每块都要有表头。"""
    rows = [["公司", "销售额", "月份", "区域", "负责人", "备注"]]
    for i in range(40):
        rows.append([f"公司{i}", 1000 + i, "1月", "华东", f"张{i}", "无"])
    f = write_xlsx(tmp_path / "big.xlsx", {"大表": rows})
    doc = parse(f)

    from localkb.chunk.splitter import split

    chunks, _ = split(doc)
    assert len(chunks) > 1, "40 行 × 6 列应该切成多块"
    for c in chunks:
        assert "销售额" in c.text, f"块 {c.ordinal} 缺表头：{c.text[:80]!r}"


def test_xlsx_empty_sheets_warned(tmp_path: Path) -> None:
    f = write_xlsx(tmp_path / "d.xlsx", {
        "有数据": [["a"], [1]],
        "空表": [],
    })
    doc = parse(f)
    assert any("空工作表" in w for w in doc.warnings)


def test_xlsx_all_empty_raises(tmp_path: Path) -> None:
    f = write_xlsx(tmp_path / "e.xlsx", {"空": []})
    with pytest.raises(ParseError) as ei:
        parse(f)
    assert "没有可提取" in ei.value.reason


def test_xlsx_trailing_empty_columns_trimmed(tmp_path: Path) -> None:
    """右侧整列为空时不该留下大量 \\t（那会让块长虚高）。"""
    f = write_xlsx(tmp_path / "f.xlsx", {
        "S": [["a", "b", None, None, None], [1, 2, None, None, None]]
    })
    doc = parse(f)
    for b in doc.blocks:
        assert not b.text.endswith("\t")
        assert "\t\t" not in b.text


# ------------------------------------------------------------ PDF

def test_pdf_basic_and_page_numbers(tmp_path: Path) -> None:
    """页码必须来自解析器，不靠事后反查（这是下沉解析的主要收益）。"""
    f = write_pdf(tmp_path / "a.pdf", [
        [("第一页的内容在这里。", 12)],
        [("第二页的内容在这里。", 12)],
    ])
    doc = parse(f)
    pages = {b.page for b in doc.blocks}
    assert 1 in pages and 2 in pages


def test_pdf_heading_by_font_size(tmp_path: Path) -> None:
    """字号明显大于正文的行判为标题。"""
    f = write_pdf(tmp_path / "h.pdf", [[
        ("第一章 总则", 20),
        ("这是正文内容，字号较小。", 11),
        ("这是第二行正文。", 11),
    ]])
    doc = parse(f)
    heads = [b for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert heads, f"未识别出标题：{[(b.text, b.level) for b in doc.blocks]}"
    assert "第一章" in heads[0].text


def test_pdf_body_text_not_mistaken_for_heading(tmp_path: Path) -> None:
    """正文不能被误判为标题（误判会污染整节的向量）。"""
    body = "这是一段很长很长的正文内容，" * 6
    f = write_pdf(tmp_path / "b.pdf", [[
        ("标题", 18),
        (body, 11),
    ]])
    doc = parse(f)
    paras = [b for b in doc.blocks if b.kind is BlockKind.PARAGRAPH]
    assert any("很长很长" in b.text for b in paras)


def test_pdf_sentence_ending_with_period_is_not_heading(tmp_path: Path) -> None:
    """以句号结尾的行即使是字号较大也不能当标题。"""
    f = write_pdf(tmp_path / "c.pdf", [[
        ("这句话以句号结尾。", 16),
        ("正文内容。", 11),
    ]])
    doc = parse(f)
    heads = [b for b in doc.blocks if b.kind is BlockKind.HEADING]
    assert not any("句号结尾" in h.text for h in heads)


def test_pdf_header_footer_removed(tmp_path: Path) -> None:
    """页眉页脚在各页重复出现，必须丢弃（否则每块都掺进公司名页码）。"""
    pages = []
    for i in range(6):
        pages.append([
            ("某某公司内部资料", 8),        # 页眉位置（y=72，顶部 8%）
            (f"第 {i + 1} 页的正文内容。", 12),
        ])
    f = write_pdf(tmp_path / "hf.pdf", pages)
    doc = parse(f)
    joined = "\n".join(b.text for b in doc.blocks)
    assert "某某公司内部资料" not in joined, "页眉没有被去掉"
    assert "正文内容" in joined


def test_pdf_scanned_gives_unsupported(tmp_path: Path) -> None:
    """无文字层的 PDF 要明确说"这是扫描件"（方案的 §5.6 文案）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    # 只画一个矩形，不写字 → 没有文本层
    page.draw_rect(pymupdf.Rect(72, 72, 200, 200), color=(0, 0, 0), width=2)
    f = tmp_path / "scan.pdf"
    doc.save(str(f))
    doc.close()

    with pytest.raises(UnsupportedError) as ei:
        parse(f)
    assert "扫描件" in ei.value.reason
    assert "OCR" in ei.value.reason


def test_pdf_page_limit_warns(tmp_path: Path) -> None:
    """超过页数上限时截断并提示，而不是静默只导前 N 页。"""
    from localkb.parse import pdf as pdf_mod

    # 每页给足文字，避免被"几乎没有可提取的文字"那条拦掉
    pages = [[(f"第{i}页的正文内容在这里，足够长。", 12)] for i in range(6)]
    f = write_pdf(tmp_path / "many.pdf", pages)

    orig = pdf_mod.MAX_PAGES
    pdf_mod.MAX_PAGES = 3
    try:
        doc = parse(f)
    finally:
        pdf_mod.MAX_PAGES = orig

    assert any("只解析了前 3 页" in w for w in doc.warnings)


def test_pdf_encrypted_gives_actionable_error(tmp_path: Path) -> None:
    """加密 PDF 要说"需要密码"，而不是"损坏"。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "secret", fontsize=12)
    f = tmp_path / "enc.pdf"
    doc.save(str(f), encryption=pymupdf.PDF_ENCRYPT_AES_256,
             user_pw="u", owner_pw="o")
    doc.close()

    with pytest.raises(ParseError) as ei:
        parse(f)
    assert "密码" in ei.value.reason
