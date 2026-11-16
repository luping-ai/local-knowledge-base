"""解析结果的中间表示：解析器只负责产出这个，切块器只消费它。

## 为什么要先定义这个结构，而不是各解析器直接吐字符串

方案的 §5.1 要求每个块带页码、标题路径、块类型。这三样信息在解析阶段就
必须拿到（切块之后再也补不出来——尤其页码）。所以解析器的输出必须**保留
结构**，不能是"整篇拼成一段文本"。

这正是 Go 侧的老毛病：`ooxml` 把 `.xlsx` 整表拼成一片、`pdftext` 在
`normalizeText` 后丢掉页边界。定义这个结构就是为了从根上避免重蹈覆辙。

## 三个字段的语义边界

    page      1 基页码。0 表示"这份格式没有页码"（txt/md）。
    heading   该段所属的标题路径（只存最近一级，拼装在 chunker 里做）。
    kind      段落 / 标题 / 表格行 / 列表项 / 代码块。

`heading` 只存**一级**而不是完整路径：解析器天然知道自己"当前在哪个标题下"，
但"整条路径"需要维护一个栈——那是切块器的职责（`chunk/heading.py`）。
让解析器维护栈会让每个格式各写一遍，且容易与切块器不一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "BlockKind",
    "Block",
    "ParsedDoc",
    "ParseError",
    "UnsupportedError",
]


class BlockKind(str, Enum):
    """块的类型。

    ## 为什么要区分 heading 与 paragraph

    切块时标题要**粘在下一段前面**（不是独立成块），否则"2.1 付款方式"
    单独成块时它的向量里只有标题、正文块里没有"付款方式"四个字——
    问"付款方式是什么"两个块都命中不了。

    `table_row` 要特殊处理：Excel 每块都要重复表头（§5.3）。
    """

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE_ROW = "table_row"
    LIST_ITEM = "list"
    CODE = "code"


@dataclass
class Block:
    """一个结构单元（通常对应原文的一个段落）。

    `text` 应是**已清理**的文本（去页眉页脚、合并硬换行、去控制字符）。
    清理规则在各自的解析器里，因为它们依赖格式特点（PDF 看字号、
    DOCX 看样式、XLSX 看行列）。
    """

    text: str
    kind: BlockKind = BlockKind.PARAGRAPH
    # 1 基页码；0 表示该格式无页码
    page: int = 0
    # 该块结束的页码。段落跨页（上一页底部没说完、下一页继续）时
    # page < page_end，切块器据此给出准确的页码范围。
    #
    # ## 为什么需要单独一个字段（踩过的坑）
    #
    # 早期只有 `page`，而 pdf.py 里写了 `blocks[-1].page_end = p.page`。
    # 因为 Block 是普通 dataclass、不是 frozen，这行**静默创建了一个
    # 新属性**而不是报错——于是跨页段落的结束页码永远是 0，
    # 来源卡片会显示"第 3 页"而实际内容跨到第 4 页。
    #
    # 0 表示"与 page 相同"（不跨页）。
    page_end: int = 0
    # 所属标题（最近一级的原文），空表示不在任何标题下
    heading: str = ""
    # 标题级别（1=H1, 2=H2…）；非 heading 块用于知道自己在几级标题下
    level: int = 0
    # Excel 专用：该行是否为表头
    is_header: bool = False
    # Excel 专用：表头行的文本（供每块重复）
    header_text: str = ""

    @property
    def page_span(self) -> tuple[int, int]:
        """实际页码范围。`page_end=0` 视为与 `page` 相同。"""
        return self.page, (self.page_end or self.page)


@dataclass
class ParsedDoc:
    """一份文档的解析结果。"""

    blocks: list[Block] = field(default_factory=list)
    # 解析器名与版本（进 chunker 标识，用于事后排查"这批块是谁切的"）
    parser: str = ""
    # 原文总字符数（用于校验"切块后覆盖了 90% 以上"，§11.3）
    total_chars: int = 0
    # 非致命的提示（如"跳过了 3 页空白页"），会展示给用户
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.blocks)


class ParseError(Exception):
    """解析失败。

    ## 为什么带 `reason` 而不是只用 message

    UI 要直接把这个 `reason` 展示给用户，而它必须**指向下一步动作**
    （方案的 §5.6：不能出现"解析失败"这种无信息量的文案）。

    `reason` 是给用户看的；`detail` 是给排障看的（异常原文）。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}（{detail}）")
        self.reason = reason
        self.detail = detail


class UnsupportedError(ParseError):
    """不是"坏了"，而是"这份文件当前不支持"。

    分开的理由：两者的用户动作不同。坏文件让用户去修文件；不支持的文件
    让用户去转换格式（比如扫描件转文本、`.doc` 另存为 `.docx`）。
    """
