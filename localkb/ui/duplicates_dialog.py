"""完全重复副本的详情对话框（方案 §12.1 / §12.7）。

## 这个对话框只做一件事：告知

方案 §12.1 明确了三个"不做"，这里逐条对应：

| 不做 | 这里怎么体现 |
| --- | --- |
| 不提供删除／归档／移动 | **没有任何**这类按钮。唯一的动作是"在文件夹中显示" |
| 不做处置动作状态机 | 没有 keep／archive／ignore 状态。每份文件只有"已入库"或"未入库" |
| 不把"可释放空间"做成行动号召 | 只在汇总里显示"共 N MB"，不写"可释放"、不给"一键清理" |

## 为什么这很重要，而不只是"少做几个按钮"

用户看到"重复文件"这个词，第一反应是"程序要帮我删"。一旦界面上出现
"清理"按钮，他就得停下来判断"这个删了会不会有问题"——而这个判断
**不该由他在这里做**。

本产品的边界很清楚：重复检测的产物只是"这些副本没入库"。想清理磁盘
的用户会自己去资源管理器——那里有回收站、有撤销、有他熟悉的一切。

## 为什么按组展示而不是平铺列表

用户要判断的是"这 5 个文件是不是同一份"。平铺成 5 行看不出分组，
而按组分块一目了然（每组的第一行是"进了库的那份"，其余是副本）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from localkb.platform.open_file import reveal_in_explorer

__all__ = ["DuplicatesDialog"]


class DuplicatesDialog(QDialog):
    """列出各组"内容完全相同"的文件。**只读**。"""

    def __init__(self, groups: list[list], *, parent: Optional[QWidget] = None,
                 kb_name: str = "") -> None:
        """`groups` 是若干组，每组是一个 `LocalFile` 列表。

        每组内部约定：**第一份是进了库的那份**（调用方保证）。
        """
        super().__init__(parent)
        self.setWindowTitle("内容相同的文件")
        self.resize(720, 480)
        self._groups = groups
        self._kb_name = kb_name
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setSpacing(10)

        total = sum(len(g) for g in self._groups)
        copies = total - len(self._groups)
        total_bytes = sum(f.size for g in self._groups for f in g)
        mb = total_bytes / 1024 / 1024
        size_text = f"{mb:.1f} MB" if mb >= 1 else f"{total_bytes / 1024:.0f} KB"

        head = QLabel(
            f"共 {len(self._groups)} 组、{copies} 个副本与其他文件内容完全相同。"
            f"这些副本**没有加入知识库**，占用 {size_text}。"
        )
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 13px;")
        v.addWidget(head)

        # ## 这句是消除顾虑的关键
        #
        # 用户看到"重复"会担心文件被动了。必须明确说"文件本身不动"，
        # 并解释"没入库"意味着什么。
        #
        # ## 为什么不再写"如果你要搜的是某一副本…"
        #
        # 第一版写了"可以把要用的那份改成不同内容，或移到别的库"——
        # 但**完全重复的文件内容一模一样**，搜哪一份的结果都相同，
        # 换哪份入库对检索没有任何影响。给一个"怎么换"的指引反而让
        # 用户以为这里有需要他决定的事。
        #
        # 真正需要用户判断的是**内容相似**（相似组），那个有独立入口。
        note = QLabel(
            "文件本身仍在原位置，本程序不会移动或删除它们。"
            "这些副本与已入库的那份内容完全相同，所以不入库也不会让"
            "你搜不到——检索命中的就是同一段内容。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setSpacing(12)
        for idx, group in enumerate(self._groups, start=1):
            iv.addWidget(self._group_box(idx, group))
        iv.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        v.addWidget(box)

    def _group_box(self, idx: int, group: list) -> QWidget:
        """一组重复文件的展示块。"""
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(8, 6, 8, 6)
        bv.setSpacing(4)
        box.setStyleSheet(
            "background: #fafafa; border: 1px solid #ebeef5; border-radius: 4px;"
        )

        title = QLabel(f"第 {idx} 组 · {len(group)} 个文件内容相同")
        title.setStyleSheet("font-weight: 600; border: none;")
        bv.addWidget(title)

        for pos, f in enumerate(group):
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)

            # 第一份是进了库的（调用方保证）。标出来，因为用户最想知道
            # "到底是哪一份在库里"。
            tag = "已入库" if pos == 0 else "未入库"
            tag_label = QLabel(tag)
            tag_label.setFixedWidth(48)
            tag_label.setAlignment(Qt.AlignCenter)
            tag_label.setStyleSheet(
                "color: #67c23a; font-size: 11px; border: none;"
                if pos == 0 else
                "color: #909399; font-size: 11px; border: none;"
            )
            rl.addWidget(tag_label)

            path_label = QLabel(f.path)
            path_label.setWordWrap(True)
            path_label.setStyleSheet("border: none; font-size: 12px;")
            path_label.setToolTip(f.path)
            rl.addWidget(path_label, 1)

            btn = QPushButton("在文件夹中显示")
            btn.setFixedWidth(120)
            btn.clicked.connect(
                lambda _checked=False, p=f.path: reveal_in_explorer(p))
            rl.addWidget(btn)

            bv.addWidget(row)
        return box


def group_duplicates(rows: list, *, masters: dict[str, str]) -> list[list]:
    """把 `local_files` 的重复行按 sha256 分组，并把主副本排到每组第一位。

    ## 为什么需要 `masters`

    `duplicate` 状态的行只记了"与谁相同"（存在 `error` 里）。要把
    "已入库的那份"排到组首，必须知道每个 sha256 对应的主副本路径——
    这正是 `masters` 提供的（{sha256: path}）。

    ## 为什么主副本可能不在 rows 里

    `list_duplicate_files` 只返回 `status='duplicate'` 的行，而主副本
    是 `imported`。所以它通常不在 `rows` 中——这时组内只有副本，
    调用方应把主副本单独查出来放进 `masters`。

    这里的行为：按 sha256 分组，若 `masters` 里有该 sha256 的路径，
    就把它作为"虚拟首项"插到最前（用一个最小的占位对象，只带 path
    与 size）。调用方不需要自己拼。
    """
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[r.sha256].append(r)

    out: list[list] = []
    for digest, members in buckets.items():
        members.sort(key=lambda r: r.path)
        master_path = masters.get(digest, "")
        if master_path:
            # 找出主副本对应的成员（若它在 rows 里），否则造一个占位
            hit = next((m for m in members if m.path == master_path), None)
            if hit is not None:
                members.remove(hit)
                members.insert(0, hit)
            else:
                members.insert(0, _MasterStub(path=master_path))
        out.append(members)
    out.sort(key=lambda g: -len(g))
    return out


class _MasterStub:
    """主副本的占位对象（只带展示需要的字段）。

    ## 为什么不用真实的 LocalFile

    主副本是 `imported` 状态，`list_duplicate_files` 不返回它。查一次
    `get_file` 也行，但那样调用方要为每组多查一次库。这里只需要 path
    与 size 用于展示。
    """

    __slots__ = ("path", "size", "status")

    def __init__(self, path: str, size: int = 0) -> None:
        self.path = path
        self.size = size
        self.status = "imported"
