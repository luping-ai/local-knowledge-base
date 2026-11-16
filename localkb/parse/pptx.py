"""PPTX：按页取文本框内容，一页一块。

## 为什么一页一块，而不是把整个 PPT 拼成一片

页码是溯源的核心。用户点"来源"要能跳到**那一页幻灯片**，而 PPT 一页
通常就是一屏内容（标题 + 几条要点），本身就是天然的语义单元。

拼成一片的后果与 Go 侧 Excel 的问题同源：450 字切下来，中间那些块既
没有标题也不知道自己是第几页，来源卡片只能显示"第 3 页"而实际内容可能
来自第 7 页。

## 为什么每页最多一块（而不是一页内再切）

单页幻灯片的文字量通常 50~300 字，远低于切块目标（450 字）。硬要
"凑满 450 字"就得把相邻几页合并——那会立刻丢掉页码精度。

方案 §5.4 明确"一页一块（页内文字合并）"。切块器的 `cross_page` 规则
（不同页的短句各自成块）正好保证它不会被合并掉。

## 标题占位符是可靠的（方案 §5.2 标"高"）

PPT 的版式（layout）把"标题"这个角色**显式**定义为
`PP_PLACEHOLDER.TITLE` / `CENTER_TITLE` / `SUBTITLE`，不是靠字号猜。
所以标题可以标成 `BlockKind.HEADING`，让切块器把它拼到正文前面。

这与 PDF 的标题识别完全不同：PDF 只能按字号猜（方案里标"中"、建议后置），
PPTX 是读结构（标"高"）。

## 为什么按形状顺序而不是文本框顺序

`slide.shapes` 的顺序就是 PowerPoint 里绘制的顺序（z-order 从底到顶），
与视觉阅读顺序大体一致。python-pptx 没有提供"阅读顺序"。
不排序的话标题可能落在正文后面，标题路径就拼错了。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from localkb.parse.types import Block, BlockKind, ParsedDoc, ParseError, UnsupportedError

__all__ = ["parse_pptx"]

# 每页最多取多少个形状的文本（防御异常文件：见过 800 个形状的页面）
_MAX_SHAPES_PER_SLIDE = 400
# 单页文本上限。超过说明这不是正常幻灯片（可能是"把文档粘进 PPT"），
# 截断并给出提示，避免一个块远超 hard_max 被切碎后页码全同
_MAX_CHARS_PER_SLIDE = 20_000


def parse_pptx(path: Path) -> ParsedDoc:
    try:
        from pptx import Presentation
        from pptx.enum.shapes import PP_PLACEHOLDER
    except ImportError:
        raise ParseError(
            "缺少 python-pptx 依赖，无法解析 PowerPoint 文档。",
        ) from None

    try:
        prs = Presentation(str(path))
    except zipfile.BadZipFile as e:
        raise ParseError("文件已损坏，无法读取。", detail=str(e)) from None
    except KeyError as e:
        # zip 能打开但缺 ppt/presentation.xml：不是合法 pptx
        raise ParseError(
            "文件已损坏或不是有效的 PowerPoint 文档。", detail=str(e),
        ) from None
    except Exception as e:  # noqa: BLE001 - python-pptx 异常类型不统一
        raise ParseError(
            "无法打开这个 PowerPoint 文件。", detail=str(e),
        ) from None

    blocks: list[Block] = []
    warnings: list[str] = []
    total_chars = 0
    empty_slides: list[int] = []
    truncated: list[int] = []

    # 幻灯片编号从 1 开始（页码是 1 基，与 PDF 一致）
    for idx, slide in enumerate(prs.slides, start=1):
        slide_blocks, stats = _slide_blocks(
            slide, idx, PP_PLACEHOLDER, warnings,
        )
        if not slide_blocks:
            empty_slides.append(idx)
            continue
        if stats["truncated"]:
            truncated.append(idx)
        blocks.extend(slide_blocks)
        total_chars += sum(len(b.text) for b in slide_blocks)
        if idx > 5000:
            # 防御：超过 5000 页的 PPT 极可能是异常文件或误选了别的格式
            warnings.append("幻灯片超过 5000 页，只处理了前 5000 页。")
            break

    if not blocks:
        # ## 为什么这里用 UnsupportedError 而不是 ParseError
        #
        # "全是图片的 PPT"（把幻灯片做成图片）与"扫描件 PDF"是同一类问题：
        # 文件没坏，是**当前版本做不到**。分开报错才能给出正确动作
        # （"转文本" vs "检查文件是否损坏"）。
        #
        # 文案与 PDF 的扫描件提示保持同一结构：是什么 → 为什么不做 → 怎么办。
        # 两处措辞一致很重要——用户可能同时遇到两种文件，看到两套说法
        # 会以为"是不是两种不同的问题"。
        raise UnsupportedError(
            "这份 PPT 里没有可提取的文字（可能整页都是图片），"
            "当前版本不支持 OCR 识别。\n"
            "为什么不做：OCR 引擎要多占 200MB~1GB 内存，低配机上会挤掉对话模型；"
            "而且识别出错时会出现「检索到了，但内容是乱码」——那比检索不到更难排查。\n"
            "可以这样做：① 用 PowerPoint 的「另存为 → PDF」后配合 OCR 工具转换；"
            "② 把图片里的文字重新整理成文本框；③ 导入这份 PPT 对应的原始文档。"
        )

    if empty_slides:
        warnings.append(
            f"跳过了 {len(empty_slides)} 页无文字的幻灯片："
            + "、".join(f"第 {n} 页" for n in empty_slides[:5])
            + ("…" if len(empty_slides) > 5 else "")
        )
    if truncated:
        warnings.append(
            "这些页面的文字超过上限（2 万字/页）已被截断："
            + "、".join(f"第 {n} 页" for n in truncated[:3])
            + "。请检查是否误把长文档粘进了幻灯片。"
        )

    return ParsedDoc(blocks=blocks, parser="pptx", total_chars=total_chars,
                     warnings=warnings)


def _is_title_placeholder(shape, pp_placeholder) -> bool:
    """判断形状是否是标题占位符。

    ## 为什么要看 placeholder_format 而不是形状名

    形状名（`shape.name`）是"Title 1"这类**可以随便改**的标签，而且中文
    版 PowerPoint 存的是"标题 1"。靠名字匹配会漏。

    `placeholder_format.type` 是 OOXML 里的枚举，与界面语言无关：
      TITLE / CENTER_TITLE → 页面主标题
      SUBTITLE             → 副标题（也是标题语义，作为二级）
    """
    try:
        pf = shape.placeholder_format
    except (AttributeError, ValueError):
        return False
    if pf is None:
        return False
    try:
        return pf.type in (
            pp_placeholder.TITLE,
            pp_placeholder.CENTER_TITLE,
            pp_placeholder.SUBTITLE,
        )
    except (AttributeError, TypeError):
        return False


def _title_level(shape, pp_placeholder) -> int:
    """标题级别：主标题 1，副标题 2。"""
    try:
        t = shape.placeholder_format.type
        if t == pp_placeholder.SUBTITLE:
            return 2
    except (AttributeError, ValueError, TypeError):
        pass
    return 1


def _shape_text(shape) -> str:
    """取形状里的文本；表格按行拼接。

    ## 为什么表格要单独处理

    `shape.text_frame` 对表格（GraphicFrame）**不存在**——表格在
    `shape.has_table` 为真时要用 `shape.table.rows`。只读 text_frame
    会让 PPT 里的表格内容整片丢失，而且不会报错（`has_text_frame` 为
    False 时被跳过）。这是最容易漏的一处。
    """
    if getattr(shape, "has_table", False):
        try:
            rows = []
            for row in shape.table.rows:
                cells = [c.text.strip() for c in row.cells]
                line = "\t".join(cells).strip()
                if line:
                    rows.append(line)
            return "\n".join(rows)
        except (AttributeError, ValueError):
            return ""

    if not getattr(shape, "has_text_frame", False):
        return ""

    try:
        # 逐段落取，保留段落边界（段内的软换行不应变成新块）
        parts = []
        for para in shape.text_frame.paragraphs:
            line = "".join(r.text for r in para.runs).strip()
            if line:
                parts.append(line)
        return "\n".join(parts)
    except (AttributeError, ValueError):
        return ""


def _slide_blocks(slide, slide_no: int, pp_placeholder,
                  warnings: list[str]) -> tuple[list[Block], dict]:
    """把一页幻灯片变成块序列。

    标题在前（作为 HEADING），其余文本合成为一个 PARAGRAPH 块。

    ## 为什么正文合并成一块而不是每段一块

    切块器会把同一页的多个短块**累加**到同一个缓冲区（`cross_page` 只在
    页码变化时拦截），所以拆成多块与合并成一块，最终产出的 chunk 基本
    相同。合并的好处是减少块数量（500 页 PPT × 5 个形状 = 2500 块，
    会明显拖慢入库），且不改变语义。
    """
    body_parts: list[str] = []
    title_text = ""
    title_level = 1
    truncated = False
    chars = 0

    shapes = list(slide.shapes)
    if len(shapes) > _MAX_SHAPES_PER_SLIDE:
        warnings.append(
            f"第 {slide_no} 页有 {len(shapes)} 个形状，只处理前 "
            f"{_MAX_SHAPES_PER_SLIDE} 个。"
        )
        shapes = shapes[:_MAX_SHAPES_PER_SLIDE]

    for shape in shapes:
        text = _shape_text(shape)
        if not text:
            continue

        if not title_text and _is_title_placeholder(shape, pp_placeholder):
            title_text = text
            title_level = _title_level(shape, pp_placeholder)
            chars += len(text)
            continue

        # 表格内容保留换行（每行是独立语义），其余作为一段
        body_parts.append(text)
        chars += len(text)

    blocks: list[Block] = []

    if title_text:
        blocks.append(Block(
            text=title_text, kind=BlockKind.HEADING,
            page=slide_no, level=title_level,
        ))

    if body_parts:
        body = "\n".join(body_parts).strip()
        if chars > _MAX_CHARS_PER_SLIDE:
            body = body[:_MAX_CHARS_PER_SLIDE]
            truncated = True
        if body:
            blocks.append(Block(
                text=body, kind=BlockKind.PARAGRAPH,
                page=slide_no,
                # 让切块器知道这一块属于哪个标题（标题路径拼装在 chunker）
                heading=title_text,
                level=title_level,
            ))

    return blocks, {"truncated": truncated}
