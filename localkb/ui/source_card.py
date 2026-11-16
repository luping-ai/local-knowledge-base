"""来源卡片：显示"来自哪个文件、第几页、哪一段"，并支持跳转。

## 三种终态（方案 §7.7.2）

引用卡片必须能处理"文件不在"，不能只做前两种：

| 情况 | 按钮 | 为什么 |
| --- | --- | --- |
| 文件在且能跳页 | "跳到第 N 页" | PDF 有页码参数时最精确 |
| 文件在但不能跳页 | "打开文件" | Word/Excel 不接受页码参数 |
| **文件不在** | "查看片段" | 库内内容仍可检索，但不能打开 |

第三种是最容易被忽略的：用户移动了一次目录，之后每次看历史消息都会遇到。

## 为什么页码要判 0

`page_start=0` 表示"未知"（旧数据或 txt/md）。显示成"第 0 页"会让用户
以为跳转坏了——而实际上只是这份格式没有页码。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = ["SourceCard", "SourceView"]


@dataclass
class SourceView:
    """渲染一张卡片需要的全部信息。

    与 SDK 的 `KnowledgeSource` 分开：这里多了 `path`（本地路径，来自
    `local_files`，不经 API）与 `snippet`（补查的片段正文）。
    """

    index: int = 0
    doc_id: str = ""
    name: str = ""
    page_start: int = 0
    page_end: int = 0
    heading_path: str = ""
    snippet: str = ""
    # 本地可定位的路径；空表示"源文件已不在"或未映射
    path: str = ""

    @property
    def page_label(self) -> str:
        if not self.page_start:
            return ""
        if self.page_end and self.page_end != self.page_start:
            return f"第 {self.page_start}–{self.page_end} 页"
        return f"第 {self.page_start} 页"

    @property
    def file_available(self) -> bool:
        return bool(self.path) and Path(self.path).exists()


class SourceCard(QFrame):
    """一张来源卡片。"""

    # 请求打开文件（path, page）。page=0 表示只打开不跳页。
    open_requested = Signal(str, int)
    # 请求查看片段（文件不可用时的退路）
    snippet_requested = Signal(object)

    def __init__(self, src: SourceView, *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._src = src
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setStyleSheet(
            "SourceCard { background: #fafafa; border: 1px solid #ebeef5;"
            " border-radius: 6px; }"
        )
        self._build()

    def _build(self) -> None:
        src = self._src
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # 标题行：[1] 文件名 · 第 3 页
        head = QHBoxLayout()
        head.setSpacing(6)
        idx = QLabel(f"[{src.index}]")
        idx.setStyleSheet("color: #409eff; font-weight: 600;")
        head.addWidget(idx)

        name = QLabel(src.name or "（未知文件）")
        name.setStyleSheet("font-weight: 500;")
        name.setWordWrap(True)
        head.addWidget(name, 1)

        if src.page_label:
            page = QLabel(src.page_label)
            page.setStyleSheet("color: #606266;")
            head.addWidget(page)
        v.addLayout(head)

        # 章节路径
        if src.heading_path:
            hp = QLabel(src.heading_path)
            hp.setStyleSheet("color: #909399; font-size: 12px;")
            hp.setWordWrap(True)
            v.addWidget(hp)

        # 片段
        if src.snippet:
            sn = QLabel(f"“{src.snippet}”")
            sn.setStyleSheet("color: #606266; font-size: 12px;")
            sn.setWordWrap(True)
            v.addWidget(sn)

        # 动作行
        act = QHBoxLayout()
        act.addStretch(1)
        if src.file_available:
            label, page = self._jump_label()
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda: self.open_requested.emit(src.path, page))
            act.addWidget(btn)
        else:
            # 文件不在：提供"查看片段"而不是灰掉按钮。
            # 库内内容仍可检索，用户需要能看到"当时引用了什么"。
            btn = QPushButton("查看片段" if src.snippet else "文件已不在")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setEnabled(bool(src.snippet))
            btn.setToolTip(
                f"源文件已不在 {src.path}" if src.path else "源文件路径未知"
            )
            btn.clicked.connect(lambda: self.snippet_requested.emit(src))
            act.addWidget(btn)
        v.addLayout(act)

    def _jump_label(self) -> tuple[str, int]:
        """决定按钮文案与实际传给打开文件的页码。

        ## 为什么按扩展名判断而不是"试一下"

        `AcroRd32.exe /A "page=3"` 只对 Acrobat/福昕有效，其他 PDF 阅读器
        会忽略参数（不报错，只是停在第一页）。所以：
          - PDF：给页码，文案"跳到第 N 页"
          - 其他：不给页码，文案"打开文件"

        方案 §8.3 的要求是"必须诚实"——不能承诺一个做不到的跳转。
        """
        src = self._src
        suffix = Path(src.path).suffix.lower()
        if suffix == ".pdf" and src.page_start:
            return f"跳到第 {src.page_start} 页", src.page_start
        if suffix in (".txt", ".md", ".markdown"):
            # 文本有内置预览器，可以精确定位（见 platform/open_file）
            return "打开并定位", src.page_start
        return "打开文件", 0
