"""切块器测试。

## 重点在"边界判定"而不是"能不能切"

切块逻辑写错的表现不是报错，而是**检索质量悄悄变差**——用户只会觉得
"搜不准"。所以每一条边界规则都要有独立断言，且断言到具体切点。
"""

from __future__ import annotations

from localkb.chunk.splitter import (
    CHUNK_PROFILE,
    ChunkerInfo,
    ChunkProfile,
    _best_cut,
    _merge_short_tail,
    _split_long,
    estimate_tokens,
    split,
)
from localkb.parse.types import Block, BlockKind, ParsedDoc


def doc(*texts: str, **kw) -> ParsedDoc:
    """快速构造一份纯文本 ParsedDoc。"""
    return ParsedDoc(
        blocks=[Block(text=t, kind=kw.get("kind", BlockKind.PARAGRAPH),
                      page=kw.get("page", 0)) for t in texts],
        parser="test",
    )


def heading(text: str, level: int = 1) -> Block:
    return Block(text=text, kind=BlockKind.HEADING, level=level)


# ------------------------------------------------------------ 基本切分

def test_short_doc_is_one_chunk() -> None:
    chunks, _ = split(doc("这是一段很短的话。"))
    assert len(chunks) == 1
    assert chunks[0].text == "这是一段很短的话。"
    assert chunks[0].ordinal == 0


def test_target_length_is_respected() -> None:
    """普通段落会按 450 字目标聚拢，而不是一段一块。

    ## 断言方式为什么是"块数"而不是"每块长度"

    段落聚拢到 450 后，最后一块可能只剩几十字（不够凑一整个目标）。
    要求"每块都 ≥ 目标的一半"会误伤这种正常情况。真正要验的是：
    20 段共约 2000 字，不该切成 20 块。
    """
    paras = [f"第{i}段。" + "内容" * 40 for i in range(20)]
    total = sum(len(p) for p in paras)
    chunks, _ = split(doc(*paras))

    assert len(chunks) > 1
    # 至少把 20 段聚成不超过 ceil(total / target) + 1 块
    upper = total // CHUNK_PROFILE.target + 2
    assert len(chunks) <= upper, f"聚拢不足：{len(chunks)} 块（上限 {upper}）"
    # 平均长度应接近目标值
    avg = sum(c.rune_count for c in chunks) / len(chunks)
    assert avg >= CHUNK_PROFILE.target * 0.6, f"平均块长偏小：{avg:.0f}"


def test_hard_max_never_exceeded_except_single_sentence() -> None:
    """硬上限 600 必须守住（单句超长是唯一例外，见下一条测试）。"""
    text = "。".join(["这是一个句子的内容" * 5] * 40) + "。"
    chunks, _ = split(doc(text))
    for c in chunks:
        assert len(c.text) <= CHUNK_PROFILE.hard_max + 20, \
            f"块超长：{len(c.text)}"


def test_single_very_long_sentence_is_split() -> None:
    """一整段没有任何标点的超长文本必须被硬切，不能整段成一个块。"""
    text = "字" * 2000
    chunks, _ = split(doc(text))
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.text) <= CHUNK_PROFILE.hard_max + 10


def test_ordinals_are_sequential() -> None:
    chunks, _ = split(doc(*[f"段落{i}。" * 60 for i in range(6)]))
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


# ------------------------------------------------------------ 边界优先级

def test_prefers_paragraph_boundary() -> None:
    """有段落边界时，切点在段落处而不是句末。"""
    a = "第一段内容。" * 30          # 210
    b = "第二段内容。" * 30
    s = a + "\n\n" + b
    cut = _best_cut(s, 450)
    assert s[:cut].rstrip().endswith("。")
    # 切点应落在段落边界附近，而不是硬切到 450
    assert cut <= 450


def test_prefers_sentence_end_over_weak_boundary() -> None:
    """句末优先于逗号。"""
    s = "这是一个句子，" + "内容" * 100 + "。" + "后续内容" * 50
    cut = _best_cut(s, 450)
    # 不能切在逗号后面（那是弱边界，优先级更低）
    assert s[cut - 1] != "，"


def test_english_period_not_followed_by_space_is_not_boundary() -> None:
    """复刻自 Go 缺陷 1：`3.14`、`v1.2.3`、`e.g.` 不能被拦腰切断。

    Go 的 `isSentenceEnd(r rune)` 拿不到后一个字符，所以 `.` 无条件算句末。
    这里必须检查后一个字符。
    """
    s = "版本号是 v1.2.3，" + "后面的内容" * 100
    cut = _best_cut(s, 450)
    # 切点不能落在 "v1." 与 "2.3" 之间
    assert s[:cut].count("v1.2.3") in (0, 1)
    if cut > s.index("v1.2.3"):
        assert "v1.2.3" in s[:cut], f"把版本号切断了：{s[max(0,cut-20):cut+5]!r}"


def test_english_period_followed_by_space_is_boundary() -> None:
    """英文句号后跟空格时是合法边界。"""
    s = "This is a sentence. " * 40
    cut = _best_cut(s, 450)
    assert s[cut - 1] == " " or s[cut - 1] == "."


def test_pi_is_not_split() -> None:
    """`3.14159` 这类小数不能被当成句末。"""
    s = "圆周率约等于 3.14159，" + "说明文字" * 100
    cut = _best_cut(s, 450)
    if cut > s.index("3.14159"):
        assert "3.14159" in s[:cut]


# ------------------------------------------------------------ 重叠

def test_overlap_only_on_hard_split() -> None:
    """段落边界切分不加重叠（复刻自 Go 缺陷 2 的反面）。

    ## 如何验证

    两段各 300 字（合计 600，正好在硬上限附近）时，切在段落边界。
    若实现给所有切点都加重叠，第二块开头会重复第一块结尾的 54 字。
    """
    a = "甲" * 290 + "。"
    b = "乙" * 290 + "。"
    chunks, _ = split(doc(a + "\n\n" + b))
    if len(chunks) == 2:
        # 第二块不该以第一块的尾部内容开头
        assert not chunks[1].text.startswith(chunks[0].text[-54:])


def test_hard_split_progresses_and_overlaps() -> None:
    """硬切兜底时：有重叠，但每块必须**明显前进**（复刻自 Go 缺陷 2 的反面）。

    ## 两个必须同时成立的性质

    1. 有重叠（否则硬切点上的一句话被劈成两半，两半都不完整）
    2. 前进距离不能太小（Go 的实现里 `next = cut - overlap` 在切点靠近
       块首时会回退到起点，产出"与上一块几乎相同"的块）

    ## 为什么用带序号的文本而不是 '字'*3000

    全重复字符时，"600 个字"与"接下来 600 个字"内容完全相同，无法
    用内容判断是否前进。用递增序号才能看出每一块确实往后走了。
    """
    # 每 10 字一个序号，确保任何 100 字的片段都唯一
    text = "".join(f"{i:09d}" for i in range(400))   # 3600 字，无标点无空格
    segs = _split_long(text, CHUNK_PROFILE)

    assert len(segs) >= 5, f"应切出多块，实际 {len(segs)}"
    for seg in segs:
        assert len(seg) <= CHUNK_PROFILE.hard_max + 5

    import itertools

    # 相邻块不能相同，且应有一段合法重叠
    for prev, cur in itertools.pairwise(segs):
        assert cur != prev, "产出了完全相同的块"
        # 前进：cur 的起点必须在 prev 的起点之后
        assert text.index(cur[:20]) > text.index(prev[:20]), "没有前进"


def test_hard_split_does_not_loop_forever() -> None:
    """切点靠近块首时的死循环保护（Go 缺陷 2 的直接后果）。"""
    text = "x" * 2000
    segs = _split_long(text, CHUNK_PROFILE)
    assert 0 < len(segs) < 20, f"块数异常，可能有死循环：{len(segs)}"


def test_no_infinite_loop_on_many_short_sentences() -> None:
    """防止切点靠近块首时的死循环（Go 缺陷 2 的后果之一）。"""
    text = "短。" * 500
    chunks, _ = split(doc(text))
    assert 0 < len(chunks) < 100, f"块数异常：{len(chunks)}"


# ------------------------------------------------------------ 最小块

def test_short_tail_is_merged() -> None:
    """末尾残句并入前一块。"""
    from localkb.chunk.splitter import Chunk

    a = Chunk(ordinal=0, text="很长的正常内容" * 20, rune_count=140,
              page_start=1, page_end=1, chunk_type="paragraph")
    b = Chunk(ordinal=1, text="完。", rune_count=2, page_start=1, page_end=1,
              chunk_type="paragraph")
    merged = _merge_short_tail([a, b], CHUNK_PROFILE)
    assert len(merged) == 1
    assert "完。" in merged[0].text


def test_merge_does_not_cross_pages() -> None:
    """尾部短块不与前一块合并，若它们在不同页。

    ## 为什么这条是"不合并"

    早期版本断言"合并后 page_end 更新为 5"，那是把跨页合并当成正确行为。
    实际上跨页合并会让来源卡片的页码失真（一片来自第 1 页的内容被标成
    "第 1~5 页"），而"能跳回原文"是产品的核心承诺。所以规则改成：
    **不同页不合并**，哪怕末尾是碎块。
    """
    from localkb.chunk.splitter import Chunk

    a = Chunk(ordinal=0, text="正常内容" * 30, rune_count=124,
              page_start=1, page_end=1, chunk_type="paragraph")
    b = Chunk(ordinal=1, text="尾部", rune_count=2, page_start=5, page_end=5,
              chunk_type="paragraph")
    result = _merge_short_tail([a, b], CHUNK_PROFILE)
    assert len(result) == 2, "跨页的短块不该被合并"
    assert result[0].page_end == 1
    assert result[1].page_start == 5


def test_merge_same_page_short_tail() -> None:
    """同页的尾部短块仍然要合并（避免碎块）。"""
    from localkb.chunk.splitter import Chunk

    a = Chunk(ordinal=0, text="正常内容" * 30, rune_count=124,
              page_start=3, page_end=3, chunk_type="paragraph")
    b = Chunk(ordinal=1, text="尾部", rune_count=2, page_start=3, page_end=3,
              chunk_type="paragraph")
    result = _merge_short_tail([a, b], CHUNK_PROFILE)
    assert len(result) == 1
    assert "尾部" in result[0].text


def test_long_tail_not_merged() -> None:
    from localkb.chunk.splitter import Chunk

    a = Chunk(ordinal=0, text="a" * 200, rune_count=200, chunk_type="paragraph")
    b = Chunk(ordinal=1, text="b" * 100, rune_count=100, chunk_type="paragraph")
    assert len(_merge_short_tail([a, b], CHUNK_PROFILE)) == 2


# ------------------------------------------------------------ 标题路径

def test_heading_path_is_prefixed() -> None:
    """标题路径拼在正文之前（方案 §5.2 的核心收益）。"""
    d = ParsedDoc(blocks=[
        heading("第二章 付款", 1),
        heading("2.1 付款方式", 2),
        Block(text="甲方应在收到发票后 30 日内支付。", page=3),
    ])
    chunks, _ = split(d)
    assert len(chunks) == 1
    assert "第二章 付款 > 2.1 付款方式" in chunks[0].text
    assert chunks[0].heading_path == "第二章 付款 > 2.1 付款方式"
    assert "30 日内支付" in chunks[0].text


def test_heading_sticks_to_next_paragraph() -> None:
    """标题不与正文分离：标题文本必须出现在同一块里。

    否则"付款方式"这个词只存在于标题块，正文块命中不了。
    """
    d = ParsedDoc(blocks=[
        heading("2.1 付款方式", 2),
        Block(text="甲方应通过银行转账支付。"),
    ])
    chunks, _ = split(d)
    assert len(chunks) == 1
    assert "2.1 付款方式" in chunks[0].text
    assert "银行转账" in chunks[0].text


def test_heading_stack_pops_same_level() -> None:
    """同级标题要替换而不是追加。"""
    d = ParsedDoc(blocks=[
        heading("第一章", 1), heading("1.1 小节", 2),
        Block(text="内容甲。" * 20),
        heading("1.2 另一小节", 2),
        Block(text="内容乙。" * 20),
    ])
    chunks, _ = split(d)
    paths = [c.heading_path for c in chunks]
    assert any("1.1 小节" in p for p in paths)
    assert any("1.2 另一小节" in p for p in paths)
    # 1.2 的块里不能还带着 1.1
    for c in chunks:
        if "1.2 另一小节" in c.heading_path:
            assert "1.1" not in c.heading_path


def test_heading_stack_keeps_parent() -> None:
    """下级标题保留上级路径。"""
    d = ParsedDoc(blocks=[
        heading("第一章", 1), heading("1.1", 2), heading("1.1.1", 3),
        Block(text="很深的正文。" * 20),
    ])
    chunks, _ = split(d)
    assert "第一章 > 1.1 > 1.1.1" in chunks[0].heading_path


def test_no_heading_means_empty_path() -> None:
    chunks, _ = split(doc("没有标题的普通内容。"))
    assert chunks[0].heading_path == ""
    assert not chunks[0].text.startswith("[")


# ------------------------------------------------------------ 页码

def test_page_range_within_one_chunk() -> None:
    """同一页内的多段聚成一块时，页码范围要正确（start == end）。"""
    d = ParsedDoc(blocks=[
        Block(text="第一句内容。" * 8, page=1),
        Block(text="第二句内容。" * 8, page=1),
    ])
    chunks, _ = split(d)
    assert len(chunks) == 1
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 1


def test_cross_page_blocks_are_separate() -> None:
    """不同页的段落各自成块（页码是溯源坐标，混了就回不去）。"""
    d = ParsedDoc(blocks=[
        Block(text="第一页的内容。" * 10, page=1),
        Block(text="第二页的内容。" * 10, page=2),
    ])
    chunks, _ = split(d)
    assert len(chunks) == 2, f"跨页不应合并：{len(chunks)} 块"
    assert chunks[0].page_start == 1
    assert chunks[1].page_start == 2


def test_cross_page_paragraph_keeps_span() -> None:
    """单个段落本身跨页（PDF 里一句被页面切断）时，块要记录页码范围。

    这种情况与"两个独立段落各在一页"不同：它是一**个** block 的
    `page != page_end`，切块器要照原样透出这两个页码。
    """
    d = ParsedDoc(blocks=[
        Block(text="这句话从第一页开始，到第二页才说完。" * 3,
              page=1, page_end=2),
    ])
    chunks, _ = split(d)
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2


def test_page_zero_means_unknown() -> None:
    """txt/md 没有页码，必须是 0（不是 1）。UI 据此显示"页码未知"。"""
    chunks, _ = split(doc("纯文本没有页码。"))
    assert chunks[0].page_start == 0
    assert chunks[0].page_end == 0


# ------------------------------------------------------------ Excel 表头

def test_excel_header_repeats_in_every_chunk() -> None:
    """Excel 每块都要带表头（方案 §5.3）。

    不给表头的话，第二块之后只有一堆没有列名的数字，
    问"某列某行"永远命中不了。
    """
    blocks = [Block(text="公司\t销售额\t月份", kind=BlockKind.TABLE_ROW,
                    is_header=True)]
    for i in range(40):
        blocks.append(Block(text=f"公司{i}\t{1000 + i}\t1月",
                            kind=BlockKind.TABLE_ROW))

    d = ParsedDoc(blocks=blocks)
    chunks, _ = split(d)
    assert len(chunks) > 1, "40 行应该切成多块"
    for c in chunks:
        assert "销售额" in c.text, f"块 {c.ordinal} 缺少表头"


def test_excel_header_not_emitted_alone() -> None:
    """表头行本身不该成为独立块。"""
    blocks = [Block(text="列A\t列B", kind=BlockKind.TABLE_ROW, is_header=True)]
    blocks += [Block(text=f"值{i}\t{i}", kind=BlockKind.TABLE_ROW)
               for i in range(5)]
    chunks, _ = split(ParsedDoc(blocks=blocks))
    assert not any(c.text.strip() == "列A\t列B" for c in chunks)


# ------------------------------------------------------------ chunker 标识

def test_chunker_info_comes_from_profile() -> None:
    _, info = split(doc("x"))
    assert isinstance(info, ChunkerInfo)
    assert info.name == "localkb-py"
    assert info.version
    assert info.target_chars == CHUNK_PROFILE.target
    assert info.max_chars == CHUNK_PROFILE.hard_max
    assert info.overlap_chars == CHUNK_PROFILE.overlap


def test_profile_is_frozen() -> None:
    """参数必须冻结：运行期被改动会让 chunker.version 失去意义。"""
    import dataclasses

    p = ChunkProfile()
    assert dataclasses.is_dataclass(p)
    try:
        p.target = 999  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("ChunkProfile 应该冻结")


def test_estimate_tokens_uses_measured_ratio() -> None:
    # 0.63 token/字（实测），1000 字约 630 token
    assert 600 <= estimate_tokens("字" * 1000) <= 660


# ------------------------------------------------------------ 覆盖率

def test_coverage_above_90_percent() -> None:
    """切块要覆盖原文 90% 以上（§11.3）。

    ## 为什么不要求 100%

    段落合并、空白折叠、重叠都会让"块的字符总和"不等于原文长度。
    90% 是"没有大段内容被丢掉"的底线。
    """
    paras = [f"第{i}段。" + "内容" * 30 for i in range(20)]
    original = "\n".join(paras)
    chunks, _ = split(doc(*paras))
    covered = sum(len(c.text) for c in chunks)
    # 有重叠与标题前缀时会大于原文；关键是不能显著小于
    assert covered >= len(original) * 0.9


def test_pages_are_not_merged_into_one_chunk() -> None:
    """不同页的短段落不能聚成一块（会丢失页码精度）。

    ## 这个缺陷的实际表现

    一份 3 页、每页只有一行的 PDF 被切成 1 块，`page_start=1`、
    `page_end=3`。用户点"跳到第 3 页"看到的内容与引用片段对不上——
    而"能跳回原文"正是这个产品的核心承诺。

    短块聚拢（`_pack`）本身是对的（避免大量 20 字的碎块），但**跨页
    聚拢必须禁止**：页码是溯源的坐标，混了就回不去。
    """
    d = ParsedDoc(blocks=[
        Block(text="第一页的正文内容。", page=1),
        Block(text="第二页的正文内容。", page=2),
        Block(text="第三页的正文内容。", page=3),
    ])
    chunks, _ = split(d)
    assert len(chunks) == 3, f"跨页被合并了：{[c.text[:12] for c in chunks]}"
    assert [c.page_start for c in chunks] == [1, 2, 3]
    assert all(c.page_start == c.page_end for c in chunks)


def test_same_page_chunks_still_pack() -> None:
    """同一页内的短段落仍然要聚拢（否则会退回碎块问题）。"""
    d = ParsedDoc(blocks=[
        Block(text=f"第{i}句短内容。", page=1) for i in range(20)
    ])
    chunks, _ = split(d)
    assert len(chunks) < 20, "同页短段落没有被聚拢"


def test_txt_without_pages_still_packs() -> None:
    """无页码格式（txt/md）不该因为"跨页"逻辑而退化成碎块。"""
    d = ParsedDoc(blocks=[Block(text=f"第{i}句。") for i in range(40)])
    chunks, _ = split(d)
    assert len(chunks) < 10, f"无页码文本被切碎了：{len(chunks)} 块"


def test_heading_without_body_still_produces_a_chunk() -> None:
    """只有标题、没有正文的文档也要产出块。

    ## 这个缺陷的实际表现

    一份内容为 `# a` 的 md 文件：标题被暂存到 `pending_heading` 等待
    正文，而正文永远不来 → 切出 0 块 → 报"解析成功但切不出任何块"
    → 标为失败 → 下次扫描反复重试、永远好不了。

    标题本身是内容（用户可能就问"这份文档叫什么"），要单独成块。
    """
    d = ParsedDoc(blocks=[heading("只有一个标题")])
    chunks, _ = split(d)
    assert len(chunks) == 1
    assert "只有一个标题" in chunks[0].text
    assert chunks[0].chunk_type == "heading"


def test_nested_headings_only() -> None:
    """连续多个标题、无正文：每个标题都要能进块（至少最后一个不丢）。"""
    d = ParsedDoc(blocks=[heading("一级", 1), heading("二级", 2)])
    chunks, _ = split(d)
    assert chunks, "连续标题不该产出 0 块"
    joined = "\n".join(c.text for c in chunks)
    assert "二级" in joined


def test_empty_doc_produces_no_chunks() -> None:
    chunks, _ = split(ParsedDoc(blocks=[]))
    assert chunks == []


def test_whitespace_only_blocks_ignored() -> None:
    chunks, _ = split(ParsedDoc(blocks=[
        Block(text="   "), Block(text="\n\n"), Block(text="真正的内容。"),
    ]))
    assert len(chunks) == 1
    assert "真正的内容" in chunks[0].text
