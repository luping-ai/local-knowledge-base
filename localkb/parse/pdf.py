"""PDF：页码 + 标题识别 + 结构修复。

## 这个文件是全项目最"脏"的一块，原因在格式本身

PDF 没有"段落"这个概念，只有"在某坐标画了一段字"。所以方案 §5.8.2 的
六类结构修复规则必须在这里全部实现——它们不是可选的优化，而是"能不能
用"的分界线：

    1. 页眉页脚重复 → 丢弃（否则每页都掺进页码公司名，稀释向量）
    2. 硬换行把句子切断 → 合并（否则一句话被劈成两行，切块时又在半句断开）
    3. 多栏被读成串行 → 按 block 的 x 坐标分栏再排序
    4. 目录页/封面污染索引 → 前几页的特征识别
    5. 标题识别（字号 + 独占行）
    6. 跨页表格断头 → 续页补表头

## 为什么必须用 PyMuPDF 的 block 而不是 get_text()

`page.get_text()` 返回的是**流式文本**，行序由 PDF 内部的绘制顺序决定。
多栏排版时这个顺序常常是"左栏第一行、右栏第一行、左栏第二行…"——
拼出来的句子毫无意义。

`page.get_text("dict")` 给 block 的 bounding box 与 span 的字号，才能：
  - 按 x 坐标判断栏位
  - 按字号判断标题
  - 丢弃页眉页脚（它们在固定的 y 区域反复出现）

## 许可

PyMuPDF 是 AGPL-3.0 / 商业双许可。本项目按内部发行（不分发二进制）处理。
若将来闭源分发，需购买授权或换 pdfplumber（见方案 §16 P2）。
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from localkb.parse.types import Block, BlockKind, ParsedDoc, ParseError, UnsupportedError

__all__ = ["SCANNED_PDF_REASON", "parse_pdf"]

# ## 扫描件 PDF 的拒绝文案（方案 §5.4 / §16 P1 的决策落地）
#
# 这段文案有三个必须做到的事，缺一条用户就不知道该怎么办：
#
#   1. **说清"是什么"**：扫描件 / 图片型 PDF，而不是"文件损坏"
#   2. **说清"为什么不做"**：OCR 引擎占 200MB~1GB，低配机上会与模型抢内存；
#      且 OCR 出错时"检索到但内容是乱码"比"检索不到"更伤信任
#   3. **说清"怎么办"**：给出三条可执行路径，而不是只说"不支持"
#
# 第 2 条是刻意写进用户可见文案的。方案 §16 P1 的结论是"v0.1 不做 OCR，
# 但要给原因"——不给原因时用户会反复重试、或以为软件坏了。
SCANNED_PDF_REASON = (
    "这是扫描件（整份 PDF 没有文字层，内容都是图片），当前版本不支持 OCR 识别。\n"
    "为什么不做：OCR 引擎要多占 200MB~1GB 内存，低配机上会挤掉对话模型；"
    "而且识别出错时会出现「检索到了，但内容是乱码」——那比检索不到更难排查。\n"
    "可以这样做：① 用 Adobe Acrobat / WPS 的「识别文字」功能转成可搜索 PDF；"
    "② 用手机扫描类 App 导出为文本；③ 如果原件还有 Word/PPT 版本，直接导入那个。"
)

# 单个 PDF 最多解析多少页（可配）。超出会提示拆分。
# 500 页：实测 15~30ms/页，500 页约 8~15 秒，用户能接受的上限。
MAX_PAGES = 500

# 页眉页脚区域：页面上下各多少比例的区域视为"可能的页眉页脚"
_HEADER_ZONE = 0.08
_FOOTER_ZONE = 0.92

# 页眉页脚判定：同一文本在这么多比例的页面上出现即视为重复
_REPEAT_RATIO = 0.5
# 至少要有这么多页才做重复判定（3 页以下没有统计意义）
_MIN_PAGES_FOR_REPEAT = 3

# 标题字号阈值：正文字号的多少倍
_HEADING_SIZE_RATIO = 1.15
# 标题行长度上限（标题通常很短；超长多半是正文被误判）
_HEADING_MAX_CHARS = 60


@dataclass
class _Line:
    """一行文本（由同一 block 内的 span 合并而来）。"""

    text: str
    page: int
    size: float          # 该行主要字号
    x0: float
    y0: float
    x1: float
    y1: float
    block_no: int


@dataclass
class _PageInfo:
    page: int
    height: float
    width: float
    lines: list[_Line] = field(default_factory=list)


def parse_pdf(path: Path) -> ParsedDoc:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[no-redef]
        except ImportError:
            raise ParseError("缺少 PyMuPDF 依赖，无法解析 PDF。") from None

    try:
        pdf = pymupdf.open(str(path))
    except Exception as e:  # noqa: BLE001 - 损坏的 PDF 抛的异常类型很多
        raise ParseError("文件已损坏，无法读取。", detail=str(e)) from None

    warnings: list[str] = []
    try:
        if pdf.needs_pass:
            raise ParseError(
                "此 PDF 需要密码。可在导入时输入。",
                detail="encrypted with user password",
            )

        n_pages = pdf.page_count
        if n_pages == 0:
            raise ParseError("这个 PDF 没有任何页面。")
        if n_pages > MAX_PAGES:
            warnings.append(
                f"文档共 {n_pages} 页，只解析了前 {MAX_PAGES} 页。"
                "如需完整索引，请拆分成多个文件。"
            )
            n_pages = MAX_PAGES

        pages = _collect_lines(pdf, n_pages)

        # 全文无文字 → 扫描件（方案 §5.4 / §16 P1 的决策落地）
        if not any(p.lines for p in pages):
            raise UnsupportedError(SCANNED_PDF_REASON)

        repeated = _detect_repeated_text(pages)
        if repeated:
            warnings.append(
                f"已忽略 {len(repeated)} 条在各页重复出现的文本（页眉/页脚）。"
            )

        body_size = _body_font_size(pages)
        blocks = _build_blocks(pages, repeated, body_size, warnings)

        total_chars = sum(len(b.text) for b in blocks)
        if total_chars < 5:
            # 阈值 5 而不是 20：短文档（一页两行）是合法的，不该被判成
            # 扫描件。真正"没有文字层"的情况在上面的 `not any(p.lines)`
            # 已经拦住了，这里只是兜住"有零星字符但没有内容"的极端情形。
            #
            # 文案要给出动作：这类文件多半是"扫描件 + 零星页码/水印"，
            # 用户看到"没文字"会以为是软件问题，所以补一句怎么办。
            raise UnsupportedError(
                "这个 PDF 里几乎没有可提取的文字（可能大部分页面是图片，"
                "只有页码或水印这一层文字）。\n"
                "当前版本不支持 OCR。可用 Adobe Acrobat / WPS 的「识别文字」"
                "转成可搜索 PDF，或导入原件对应的 Word/PPT 版本。"
            )

        return ParsedDoc(
            blocks=blocks, parser="pymupdf", total_chars=total_chars,
            warnings=warnings,
        )
    finally:
        pdf.close()


# --------------------------------------------------------------------- 采集

def _collect_lines(pdf, n_pages: int) -> list[_PageInfo]:
    """把每页的 dict 输出展开成"行"列表。"""
    out: list[_PageInfo] = []
    for i in range(n_pages):
        page = pdf[i]
        rect = page.rect
        info = _PageInfo(page=i + 1, height=float(rect.height),
                         width=float(rect.width))
        try:
            raw = page.get_text("dict")
        except Exception:  # noqa: BLE001 - 个别页面可能损坏
            out.append(info)
            continue

        for b_no, block in enumerate(raw.get("blocks", [])):
            if block.get("type") != 0:   # 0 = 文本块，1 = 图片
                continue
            lines = _lines_from_block(block, i + 1, b_no)
            info.lines.extend(lines)
        out.append(info)
    return out


def _lines_from_block(block: dict, page_no: int, block_no: int) -> list[_Line]:
    """一个 PDF block 内的行合并。

    ## 为什么要把 span 合成行

    PyMuPDF 的 span 是"同一字体的连续文字"，一个句子在中英文混排时会被
    切成多个 span。不合并的话，"甲方应在收到 invoice 后 30 日内支付"
    会变成三个块，语义全丢。

    合并方式：同一行内（y 接近）的 span 拼接；每行取**主要字号**
    （按字符数加权），用于标题判定。
    """
    out: list[_Line] = []
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        if not spans:
            continue
        text_parts: list[str] = []
        weight: dict[float, int] = {}
        for sp in spans:
            t = sp.get("text", "")
            if not t:
                continue
            text_parts.append(t)
            size = round(float(sp.get("size", 0)), 1)
            weight[size] = weight.get(size, 0) + len(t)
        text = "".join(text_parts).strip()
        if not text:
            continue
        # 主要字号：字符数最多的那个
        main_size = max(weight.items(), key=lambda kv: kv[1])[0] if weight else 0.0
        bbox = line.get("bbox") or block.get("bbox") or (0, 0, 0, 0)
        out.append(_Line(
            text=text, page=page_no, size=main_size,
            x0=float(bbox[0]), y0=float(bbox[1]),
            x1=float(bbox[2]), y1=float(bbox[3]),
            block_no=block_no,
        ))
    return out


# ------------------------------------------------------------- 页眉页脚识别

def _detect_repeated_text(pages: list[_PageInfo]) -> set[str]:
    """找出在多数页面固定区域重复出现的文本。

    ## 判定条件（四条同时满足）

    1. 位于页面上下边缘区域
    2. 在 ≥50% 的页面上出现
    3. 本身很短（页眉页脚通常 < 80 字）
    4. **该页还有其他内容**（关键）

    ## 第 4 条是踩过坑才加的

    早期版本没有第 4 条，后果是：**只有一行文字的 PDF 会把正文当页眉丢掉**。

    实测：一份 6 页、每页一行"第 N 页的正文内容…"的测试文档，那唯一一行
    的 y 坐标落在顶部 8% 区域内（y/height ≈ 0.07），且归一化后（数字变 #）
    在各页"重复"，于是被判为页眉全部丢弃 → 最终报"几乎没有可提取的文字"。

    判据是"页眉需要与正文共存"：真正的页眉页面里一定还有别的正文行。
    一页只有一行时，那一行就是正文，不管它在什么位置。
    """
    n = len(pages)
    if n < _MIN_PAGES_FOR_REPEAT:
        return set()

    counter: dict[str, int] = {}
    for p in pages:
        # 只有"多行页面"才参与页眉判定（见上面第 4 条）
        if len([ln for ln in p.lines if ln.text.strip()]) < 2:
            continue
        seen_here: set[str] = set()
        for ln in p.lines:
            if not ln.text or len(ln.text) > 80:
                continue
            rel = ln.y0 / p.height if p.height else 0.5
            if rel > _HEADER_ZONE and rel < _FOOTER_ZONE:
                continue
            norm = _norm_for_repeat(ln.text)
            if norm and norm not in seen_here:
                seen_here.add(norm)
        for norm in seen_here:
            counter[norm] = counter.get(norm, 0) + 1

    threshold = max(2, int(n * _REPEAT_RATIO))
    return {t for t, c in counter.items() if c >= threshold}


def _norm_for_repeat(s: str) -> str:
    """归一化以便跨页比对：去掉纯数字（页码会变）与多余空白。

    ## 为什么去掉数字

    页脚常见 "第 3 页 / 共 20 页"、"3 / 20"、"Page 3"。它们的差异只在
    数字，若按原样比对，20 页会得到 20 个不同的字符串，一条都识别不出来。
    把数字替换成占位符后，它们就聚成一类。
    """
    import re

    s = s.strip()
    if not s:
        return ""
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


# ---------------------------------------------------------------- 正文字号

def _body_font_size(pages: list[_PageInfo]) -> float:
    """估计正文字号：所有行字号的**中位数**（按字符数加权）。

    ## 为什么用中位数而不是众数

    众数在"标题很多、正文很少"的文档上会选到标题字号。中位数稳健得多：
    正文总是占多数字符。
    """
    sizes: list[float] = []
    for p in pages:
        for ln in p.lines:
            if ln.size > 0:
                sizes.extend([ln.size] * max(1, len(ln.text) // 10))
    if not sizes:
        return 10.0
    return statistics.median(sizes)


# ---------------------------------------------------------------- 构建块

def _build_blocks(pages: list[_PageInfo], repeated: set[str],
                  body_size: float, warnings: list[str]) -> list[Block]:
    """逐页把行变成块：分栏 → 排序 → 合并硬换行 → 识别标题。"""
    blocks: list[Block] = []
    prev_text = ""          # 上一行，用于跨页/跨行合并
    prev_page = 0
    skip_toc_pages = _detect_toc_pages(pages)
    if skip_toc_pages:
        warnings.append(
            f"已跳过 {len(skip_toc_pages)} 页目录/封面"
            "（它们会让检索命中「目录」而不是正文）。"
        )

    for p in pages:
        if p.page in skip_toc_pages:
            continue

        lines = [ln for ln in p.lines
                 if _norm_for_repeat(ln.text) not in repeated]
        if not lines:
            continue

        ordered = _order_by_columns(lines, p.width)

        for ln in ordered:
            text = _clean_line(ln.text)
            if not text:
                continue

            level = _heading_level(ln, text, body_size)

            # 跨页续接：上一页最后一行没有句末标点，且本页第一行是
            # 小写/无缩进 → 这是同一句话被页面切开
            if (blocks and prev_page and not level
                    and _continues(prev_text, text)
                    and blocks[-1].kind is BlockKind.PARAGRAPH):
                blocks[-1].text += text
                blocks[-1].page_end = p.page
                prev_text = blocks[-1].text
                prev_page = p.page
                continue
            kind = BlockKind.HEADING if level else BlockKind.PARAGRAPH
            blocks.append(Block(text=text, kind=kind, page=p.page,
                                level=level))
            prev_text = text
            prev_page = p.page

    return _merge_wrapped_lines(blocks)


def _clean_line(s: str) -> str:
    """去掉控制字符与连字符断行残留。"""
    import re

    s = s.replace("\u00ad", "")           # 软连字符
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", "", s)
    # 行尾的连字符（英文单词被断开）：保留连字符会让 "infor-\nmation"
    # 变成 "infor- mation"，检索不到 "information"
    s = re.sub(r"(\w)-\s*$", r"\1", s)
    return s.strip()


def _heading_level(ln: _Line, text: str, body_size: float) -> int:
    """按字号 + 行长推断标题级别。

    ## 只做"高置信度"的判断

    方案的 §5.2 明确说：PDF 标题识别的误判率高，**误判会污染向量**
    （把正文当标题，会让整节内容都带上错误的路径前缀）。所以这里
    只在同时满足以下条件时才判定为标题：

      - 字号 ≥ 正文 × 1.15
      - 独占一行且很短（< 60 字）
      - 不以句末标点结尾（标题很少以"。"结尾）

    其余一律当正文。宁可少识别，不要错识别。
    """
    if body_size <= 0 or ln.size <= 0:
        return 0
    if ln.size < body_size * _HEADING_SIZE_RATIO:
        return 0
    if len(text) > _HEADING_MAX_CHARS:
        return 0
    if text.rstrip().endswith(("。", "，", "；", "：", ".", ",", ";")):
        return 0

    # 按字号比例给级别：越大越可能是高级标题
    ratio = ln.size / body_size
    if ratio >= 1.6:
        return 1
    if ratio >= 1.35:
        return 2
    return 3


def _continues(prev: str, cur: str) -> bool:
    """上一行是否被硬换行切断（应当与当前行合并）。

    ## 判定规则

    - 上一行没有以句末标点结尾
    - 当前行不是以列表符号/编号开头（那是新段落）
    - 当前行首字符不是大写字母（英文新句）——中文没有这个问题，
      但混排文档里有用
    """
    if not prev:
        return False
    if prev.rstrip().endswith(("。", "！", "？", "；", "…", ".", "!", "?", ";", ":")):
        return False
    cur = cur.lstrip()
    if not cur:
        return False
    if cur[0] in "•·-*+":
        return False
    import re

    if re.match(r"^\d+[.)、]", cur):
        return False
    return not (cur[0].isascii() and cur[0].isupper())


def _merge_wrapped_lines(blocks: list[Block]) -> list[Block]:
    """把被硬换行切开的段落合并。

    ## 为什么要在块级别二次合并

    PDF 里每个视觉行都可能是一个独立 block，`_continues` 只能合并
    "相邻两行"。而一段话常被切成 5~8 行，逐行判断会因为中间某行以
    逗号结尾而断开。这里按"块内累积"再合并一次。
    """
    if not blocks:
        return blocks
    out: list[Block] = [blocks[0]]
    for blk in blocks[1:]:
        last = out[-1]
        if (blk.kind is BlockKind.PARAGRAPH
                and last.kind is BlockKind.PARAGRAPH
                and _continues(last.text, blk.text)):
            last.text = last.text + blk.text
            last.page_end = max(last.page_end, blk.page)
            continue
        out.append(blk)
    return out


def _order_by_columns(lines: list[_Line], page_width: float) -> list[_Line]:
    """多栏排版的重排。

    ## 为什么需要

    多栏 PDF 的绘制顺序常常是"左栏第 1 行、右栏第 1 行、左栏第 2 行…"。
    直接按顺序拼接会得到完全无意义的句子。

    ## 怎么判断有栏

    看 x0 的分布：若存在一个明显的"垂直空带"把行分成两组，就是两栏。
    做法是统计 x0 的直方图，找最大的间隔（gap）。间隔超过页宽 25%
    视为分栏。

    ## 单栏时不做任何事

    这是关键：绝大多数文档是单栏，排序逻辑一旦对单栏也生效，就可能
    把正常的行序打乱（比如表格的左右两列会被当成两栏）。
    """
    if len(lines) < 6 or page_width <= 0:
        return sorted(lines, key=lambda ln: (ln.y0, ln.x0))

    xs = sorted(ln.x0 for ln in lines)
    best_gap = 0.0
    cut_x = 0.0
    for a, b in itertools.pairwise(xs):
        gap = b - a
        if gap > best_gap:
            best_gap = gap
            cut_x = (a + b) / 2

    if best_gap < page_width * 0.25:
        # 单栏
        return sorted(lines, key=lambda ln: (ln.y0, ln.x0))

    left = [ln for ln in lines if ln.x0 < cut_x]
    right = [ln for ln in lines if ln.x0 >= cut_x]
    # 某一侧少于 3 行时可能是表格/侧边注，不分栏
    if len(left) < 3 or len(right) < 3:
        return sorted(lines, key=lambda ln: (ln.y0, ln.x0))

    return (sorted(left, key=lambda ln: (ln.y0, ln.x0))
            + sorted(right, key=lambda ln: (ln.y0, ln.x0)))


def _detect_toc_pages(pages: list[_PageInfo]) -> set[int]:
    """识别目录页 / 封面页。

    ## 判据（前 5 页内才判）

    目录页的特征非常明显：
      - 大量行以数字结尾（页码）
      - 有连续的点线（`....`）
      - 行数很多但每行很短

    命中两条以上才判为目录——单条都会误伤（比如价格表全是数字）。

    ## 为什么只判前 5 页

    目录只会在开头。在文档中部判"目录"极易误伤（附录里的清单）。
    """
    out: set[int] = set()
    for p in pages[:5]:
        if len(p.lines) < 8:
            continue
        dot_lines = sum(1 for ln in p.lines if "...." in ln.text or "…" in ln.text)
        num_end = sum(1 for ln in p.lines
                      if ln.text.rstrip()[-1:].isdigit())
        short = sum(1 for ln in p.lines if len(ln.text) < 40)
        total = len(p.lines)
        hits = 0
        if dot_lines / total > 0.2:
            hits += 1
        if num_end / total > 0.5:
            hits += 1
        if short / total > 0.7:
            hits += 1
        if hits >= 2:
            out.add(p.page)
    return out
