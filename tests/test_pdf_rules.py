"""结构修复规则的专项测试（方案 §5.8.2 的六类）。

## 为什么这些要单独一个文件

它们验证的不是"能解析"，而是"解析出来的东西**能用**"。这六条规则没有
写好的话，PDF 的检索质量会静默变差——用户只会觉得"搜不准"，而不会
报错。所以每一条都要有独立、可复现的断言。

## 测试数据的构造思路

不用真实 PDF（无法在 CI 里稳定复现），而是直接构造 `_PageInfo` 结构，
绕过解析层只测规则本身。这样每条规则的问题都能被精确定位——若混在
"从 PDF 读出来"的流程里，失败时分不清是读取问题还是规则问题。
"""

from __future__ import annotations

from localkb.parse.pdf import (
    _body_font_size,
    _build_blocks,
    _clean_line,
    _continues,
    _detect_repeated_text,
    _heading_level,
    _Line,
    _norm_for_repeat,
    _order_by_columns,
    _PageInfo,
)
from localkb.parse.types import BlockKind


def line(text: str, *, page: int = 1, size: float = 11.0, x0: float = 72.0,
         y0: float = 100.0) -> _Line:
    return _Line(text=text, page=page, size=size, x0=x0, y0=y0,
                 x1=x0 + len(text) * size * 0.5, y1=y0 + size, block_no=0)


def page(no: int, lines: list[_Line], *, height: float = 842.0,
         width: float = 595.0) -> _PageInfo:
    for ln in lines:
        ln.page = no
    return _PageInfo(page=no, height=height, width=width, lines=lines)


# ------------------------------------------------ 规则 1：页眉页脚丢弃

def test_header_repeated_across_pages_is_dropped() -> None:
    pages = [
        page(i, [line("某某公司 内部资料", y0=40), line(f"正文第{i}页内容。", y0=400)])
        for i in range(1, 7)
    ]
    repeated = _detect_repeated_text(pages)
    assert repeated, "重复页眉没有被识别"
    blocks = _build_blocks(pages, repeated, _body_font_size(pages), [])
    joined = "\n".join(b.text for b in blocks)
    assert "内部资料" not in joined
    assert "正文第1页内容" in joined


def test_footer_with_page_number_is_dropped() -> None:
    """页脚常是"第 N 页 / 共 M 页"，数字每页不同，必须归一化后才能识别。"""
    pages = [
        page(i, [line(f"正文{i}。", y0=400), line(f"第 {i} 页 共 6 页", y0=810)])
        for i in range(1, 7)
    ]
    repeated = _detect_repeated_text(pages)
    blocks = _build_blocks(pages, repeated, _body_font_size(pages), [])
    joined = "\n".join(b.text for b in blocks)
    assert "共 6 页" not in joined


def test_single_line_page_text_is_not_dropped() -> None:
    """回归：只有一行的页面，那一行是正文，不是页眉。

    ## 这个缺陷的实际表现

    6 页、每页一行的短文档，那一行 y 坐标在顶部 8% 内，归一化后各页
    "重复"（数字变 #）→ 被判为页眉全部丢弃 → 最终报"几乎没有可提取
    的文字"，用户以为 PDF 是坏的。
    """
    pages = [page(i, [line(f"第{i}页的正文内容在这里，足够长。", y0=60)])
             for i in range(1, 7)]
    repeated = _detect_repeated_text(pages)
    assert repeated == set(), f"单行页面不该识别出页眉：{repeated}"


def test_body_repeated_phrase_not_dropped() -> None:
    """正文中部的重复短语不能被当页眉丢掉（位置是判据）。"""
    pages = [
        page(i, [line("注意事项：请仔细阅读。", y0=400),
                 line(f"内容{i}。", y0=450)])
        for i in range(1, 7)
    ]
    repeated = _detect_repeated_text(pages)
    assert "注意事项：请仔细阅读。" not in repeated


def test_repeat_norm_replaces_digits() -> None:
    assert _norm_for_repeat("第 3 页") == _norm_for_repeat("第 12 页")
    assert _norm_for_repeat("") == ""


def test_repeat_needs_enough_pages() -> None:
    """3 页以下没有统计意义，不该判页眉。"""
    pages = [page(i, [line("页眉", y0=40), line(f"内容{i}", y0=400)])
             for i in range(1, 3)]
    assert _detect_repeated_text(pages) == set()


# ------------------------------------------------ 规则 2：硬换行合并

def test_wrapped_line_is_merged() -> None:
    """被硬换行切断的句子要合并（否则半句被切块器切断）。"""
    pages = [page(1, [
        line("甲方应在收到发票后三十日内支付全部", y0=400),
        line("款项，逾期按日万分之五计息。", y0=420),
    ])]
    blocks = _build_blocks(pages, set(), 11.0, [])
    assert len(blocks) == 1
    assert "三十日内支付全部款项" in blocks[0].text


def test_sentence_end_prevents_merge() -> None:
    pages = [page(1, [
        line("第一句话结束了。", y0=400),
        line("第二句话开始。", y0=420),
    ])]
    blocks = _build_blocks(pages, set(), 11.0, [])
    assert len(blocks) == 2


def test_list_item_starts_new_paragraph() -> None:
    """列表项不能与上一行合并（它是新条目）。"""
    assert _continues("上一段的结尾没有标点", "• 第一条") is False
    assert _continues("上一段结尾", "1. 第一条") is False


def test_cross_page_continuation_merged() -> None:
    """跨页被切断的句子也要合并，且结束页码要更新。

    ## 这条测试保护的是一个静默缺陷

    `Block` 早期没有 `page_end` 字段，而 pdf.py 里写了
    `blocks[-1].page_end = p.page`——普通 dataclass 允许动态加属性，
    所以这行不报错、但也不生效。结果是跨页段落的结束页码永远是 0，
    来源卡片显示的页码比实际内容少一页。
    """
    pages = [
        page(1, [line("这句话在页面底部没有说完，", y0=800)]),
        page(2, [line("下一页接着说完这句话。", y0=80)]),
    ]
    blocks = _build_blocks(pages, set(), 11.0, [])
    assert len(blocks) == 1
    assert blocks[0].page == 1, "起始页应为 1"
    assert blocks[0].page_end == 2, "结束页应为 2（跨页）"
    assert blocks[0].page_span == (1, 2)


def test_hyphenated_word_is_joined() -> None:
    """英文单词被连字符断行时要拼回（否则检索不到原词）。"""
    assert _clean_line("infor-") == "infor"
    assert "information" not in _clean_line("infor-")


def test_control_chars_removed() -> None:
    assert _clean_line("正常\x00文本\x07") == "正常文本"


# ------------------------------------------------ 规则 3：多栏重排

def test_two_column_layout_is_reordered() -> None:
    """两栏排版要按"左栏读完再读右栏"排序。

    ## 构造

    左栏三行 x0=72，右栏三行 x0=320（页宽 595，间隔 248 > 25%）。
    绘制顺序故意交替，模拟 PDF 内部的乱序。
    """
    lines = [
        line("左一", x0=72, y0=100), line("右一", x0=320, y0=100),
        line("左二", x0=72, y0=130), line("右二", x0=320, y0=130),
        line("左三", x0=72, y0=160), line("右三", x0=320, y0=160),
    ]
    ordered = _order_by_columns(lines, page_width=595.0)
    texts = [ln.text for ln in ordered]
    assert texts == ["左一", "左二", "左三", "右一", "右二", "右三"]


def test_single_column_keeps_reading_order() -> None:
    """单栏时按 y 排序，不做任何分栏处理（避免把表格左右列当两栏）。"""
    lines = [
        line("第三行", y0=160), line("第一行", y0=100), line("第二行", y0=130),
    ]
    ordered = _order_by_columns(lines, page_width=595.0)
    assert [ln.text for ln in ordered] == ["第一行", "第二行", "第三行"]


def test_narrow_gap_is_not_a_column_break() -> None:
    """x 间隔小于页宽 25% 时不当作分栏。"""
    lines = [line(f"行{i}", x0=72 + i * 5, y0=100 + i * 30) for i in range(6)]
    ordered = _order_by_columns(lines, page_width=595.0)
    assert [ln.text for ln in ordered] == [f"行{i}" for i in range(6)]


# ------------------------------------------------ 规则 5：标题识别

def test_heading_detected_by_size() -> None:
    ln = line("第一章 总则", size=20)
    assert _heading_level(ln, "第一章 总则", body_size=11) == 1


def test_heading_level_scales_with_size() -> None:
    assert _heading_level(line("A", size=20), "A", 11) == 1
    assert _heading_level(line("B", size=16), "B", 11) == 2
    assert _heading_level(line("C", size=13.5), "C", 11) == 3


def test_small_size_difference_is_not_heading() -> None:
    """字号只大一点点不能判标题（正文里有大量轻微字号差异）。"""
    assert _heading_level(line("x", size=11.5), "x", 11) == 0


def test_long_line_is_not_heading() -> None:
    """长行即使是字号大也不当标题（标题很少超过 60 字）。"""
    text = "很长的文字" * 20
    assert _heading_level(line(text, size=20), text, 11) == 0


def test_line_ending_with_period_is_not_heading() -> None:
    """以句末标点结尾的行不当标题。"""
    for end in ("。", "，", "；", "：", ".", ",", ";"):
        t = f"这句话以标点结尾{end}"
        assert _heading_level(line(t, size=20), t, 11) == 0, f"{end} 没被拦住"


def test_body_size_uses_char_weighted_median() -> None:
    """字号估计用中位数：标题很多时也不能把标题当正文基准。"""
    pages = [page(1, [line("大标题", size=24, y0=100)]
                  + [line("正文内容" * 10, size=11, y0=200 + i * 20)
                     for i in range(10)])]
    size = _body_font_size(pages)
    assert 10 <= size <= 12, f"正文字号估计偏差：{size}"


# ------------------------------------------------ 规则 4：目录页

def test_toc_page_is_skipped() -> None:
    """目录页会被命中"目录"而不是正文，必须跳过并提示。"""
    toc_lines = [line(f"第{i}章 某章节名称 ........ {i}", y0=100 + i * 20)
                 for i in range(1, 15)]
    body = [line("真正的正文内容在这里。", y0=400)]
    pages = [page(1, toc_lines), page(2, body)]

    warnings: list[str] = []
    blocks = _build_blocks(pages, set(), 11.0, warnings)
    joined = "\n".join(b.text for b in blocks)
    assert "........" not in joined, "目录行仍在结果里"
    assert "真正的正文" in joined
    assert any("目录" in w for w in warnings)


def test_toc_only_checked_in_first_pages() -> None:
    """文档中部的类似内容不当目录（避免误伤附录清单）。"""
    toc_like = [line(f"项目{i} ........ {i}", y0=100 + i * 20) for i in range(1, 15)]
    pages = [page(1, [line("正文。", y0=400)]),
             page(2, [line("更多正文。", y0=400)])]
    for i in range(3, 9):
        pages.append(page(i, toc_like if i == 8 else [line(f"正文{i}", y0=400)]))

    blocks = _build_blocks(pages, set(), 11.0, [])
    joined = "\n".join(b.text for b in blocks)
    assert "........" in joined, "第 8 页不该被当成目录"


# ------------------------------------------------ 综合：标题 + 正文结构

def test_heading_and_body_produce_correct_structure() -> None:
    pages = [page(1, [
        line("第一章 总则", size=20, y0=100),
        line("本文件规定了售后服务的基本要求。", size=11, y0=150),
        line("适用于所有产品线。", size=11, y0=170),
    ])]
    blocks = _build_blocks(pages, set(), 11.0, [])
    heads = [b for b in blocks if b.kind is BlockKind.HEADING]
    paras = [b for b in blocks if b.kind is BlockKind.PARAGRAPH]
    assert len(heads) == 1
    assert heads[0].level == 1
    assert len(paras) >= 1
    assert all(b.page == 1 for b in blocks)
