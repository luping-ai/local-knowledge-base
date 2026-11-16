"""相似组确认：选哪一版入库（方案 §12.2.1 / §12.7，D2 的核心 UI）。

## 这个对话框唯一的任务

让用户在一组"同一文档的不同版本"里指定**一份**入库，其余不入库。
文件本身一个都不动。

## 四条必须做到的设计

| 要求 | 为什么 |
| --- | --- |
| **默认不预选**（`none`） | 预选最新会让用户习惯性点确定，在"新版其实是误存的草稿"时把旧版挤掉 |
| 未选中时 `[确定]` 不可点 + 灰字提示 | 否则用户点确定然后什么都没发生，会以为坏（§12.7） |
| 明确写"文件本身不动" | 消除顾虑的关键——用户确认的是"哪个进知识库"，不是"删掉哪个文件"（§12.7） |
| 差异摘要辅助但**不强制阅读** | 它是帮助，不是关卡（§12.7） |

## 为什么不用 QListWidget 的 checkState

需要"单选且必须选一个"的语义，而 `checkState` 允许全不选或全选。
`QRadioButton` 天然是单选的，配合一个 `QButtonGroup` 就能拿到
"当前选中哪个"，也不必自己维护互斥逻辑。

## 为什么"打开所在位置"是唯一的文件操作

§12.1 明确：不为重复组提供任何删除/归档/移动动作。用户想清理磁盘，
自己去资源管理器——那里有回收站、有撤销、有他熟悉的一切。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from localkb.platform.open_file import reveal_in_explorer

__all__ = ["SimilarDialog"]


class SimilarDialog(QDialog):
    """确认一组相似文档中哪一份入库。

    ## 参数

    `group` 是 `localkb.dedup.SimilarGroup`（含 members / 相似度范围）。
    `default_choice` 是设置项的值（`none` / `newest` / `oldest`）。
    `diff_loader` 是一个可选回调：给定两个 doc_id 返回差异摘要文本。
    传 None 表示不显示差异（服务端不可用时也能用这个对话框）。
    """

    def __init__(self, group, *, default_choice: str = "none",
                 diff_loader: Optional[Callable[[str, str], str]] = None,
                 parent: Optional[QWidget] = None,
                 kb_name: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("确认入库版本")
        self.resize(760, 520)
        self._group = group
        self._diff_loader = diff_loader
        self._kb_name = kb_name
        self._buttons: list[QRadioButton] = []
        self._chosen_doc_id: str = ""
        self._build(default_choice)

    # ------------------------------------------------------------------ 结果

    @property
    def chosen_doc_id(self) -> str:
        """用户选中的 doc_id（空表示没选）。"""
        return self._chosen_doc_id

    @property
    def chosen_path(self) -> str:
        for m in self._group.members:
            if m.doc_id == self._chosen_doc_id:
                return m.path
        return ""

    # ------------------------------------------------------------------ 构建

    def _build(self, default_choice: str) -> None:
        v = QVBoxLayout(self)
        v.setSpacing(10)

        n = len(self._group.members)
        lo, hi = self._group.similarity_min, self._group.similarity_max
        span = f"（相似度 {lo}%~{hi}%）" if lo and hi else ""
        head = QLabel(
            f"这 {n} 个文件内容相似{span}，可能是同一文档的不同版本。"
            "请选一个加入知识库："
        )
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 13px;")
        v.addWidget(head)

        # ---- 成员单选列表 ----
        box = QGroupBox("候选版本")
        bl = QVBoxLayout(box)
        self._bgroup = QButtonGroup(self)
        self._bgroup.setExclusive(True)

        preset = self._group.pick_default(default_choice)
        for m in self._group.members:
            rb = QRadioButton(self._member_text(m))
            rb.setToolTip(m.path or m.name)
            rb.setProperty("doc_id", m.doc_id)
            if m.doc_id == preset:
                rb.setChecked(True)
                self._chosen_doc_id = m.doc_id
            self._bgroup.addButton(rb)
            self._buttons.append(rb)

            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addWidget(rb, 1)
            if m.path:
                btn = QPushButton("在文件夹中显示")
                btn.setFixedWidth(120)
                btn.clicked.connect(
                    lambda _c=False, p=m.path: reveal_in_explorer(p))
                rl.addWidget(btn)
            bl.addWidget(row)

        self._bgroup.buttonClicked.connect(self._on_pick)
        v.addWidget(box)

        # ---- 差异摘要 ----
        self._diff_box = QGroupBox("差异（选中任一文件后显示）")
        dv = QVBoxLayout(self._diff_box)
        self._diff_label = QLabel("选中一个文件后，这里会显示它与组内第一个版本的差异。")
        self._diff_label.setWordWrap(True)
        self._diff_label.setStyleSheet("color: #606266; font-size: 12px;")
        dv.addWidget(self._diff_label)
        v.addWidget(self._diff_box, 1)

        # ---- 提示 ----
        #
        # ## 这句话是消除顾虑的关键（§12.7）
        #
        # 用户看到"确认入库版本"会以为程序要动文件。必须明确说：
        # 文件本身不动，只是其余几份不进知识库。不提这句，用户可能
        # 因为担心而直接取消。
        tip = QLabel(
            "其余版本不会加入知识库，但**文件本身留在原位置不动**。"
            "随时可以在知识库页换用另一版。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(tip)

        # ---- 未选中提示 + 按钮 ----
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(self._hint)

        self._bbox = QDialogButtonBox()
        self._btn_ok = self._bbox.addButton("确定", QDialogButtonBox.AcceptRole)
        self._btn_later = self._bbox.addButton("稍后再说",
                                               QDialogButtonBox.RejectRole)
        self._bbox.accepted.connect(self.accept)
        self._bbox.rejected.connect(self.reject)
        v.addWidget(self._bbox)

        self._sync_ok_state()
        # 预选（newest/oldest）时立刻显示差异（B10）。
        #
        # 第一版这里只调了 `_sync_ok_state()`，于是"预选了一份"但差异区
        # 还是空白占位文字——用户会以为差异功能没生效，或者以为预选的
        # 那份是"组内第一个版本"（提示语正是这么写的）。
        #
        # 预选状态下用户最想看的就是"这份和别的差在哪"，不显示等于
        # 让他必须手动再点一次同一个按钮。
        self._update_diff()

    @staticmethod
    def _member_text(m) -> str:
        """一行成员信息：名称 + 修改时间 + 相似度偏差。"""
        parts = [m.path or m.name]
        if m.mtime:
            import datetime

            try:
                dt = datetime.datetime.fromtimestamp(m.mtime)
                parts.append(dt.strftime("%Y-%m-%d"))
            except (OverflowError, OSError, ValueError):
                pass
        if m.hamming:
            parts.append(f"差异 {m.hamming}/64")
        return "  ·  ".join(parts)

    # ------------------------------------------------------------------ 交互

    def _on_pick(self, btn) -> None:
        doc_id = btn.property("doc_id") or ""
        self._chosen_doc_id = doc_id
        self._sync_ok_state()
        self._update_diff()

    def _sync_ok_state(self) -> None:
        """未选中时确定不可点，并给出说明（§12.7）。"""
        ok = bool(self._chosen_doc_id)
        self._btn_ok.setEnabled(ok)
        if ok:
            self._hint.setText("")
        else:
            # 明确说"请选择"，否则用户会点确定然后什么都没发生
            self._hint.setText("请选择要入库的版本")

    def _update_diff(self) -> None:
        """显示选中版本与组内第一版的差异。"""
        if self._diff_loader is None or not self._chosen_doc_id:
            return
        base = next((m for m in self._group.members
                     if m.doc_id != self._chosen_doc_id), None)
        if base is None:
            return
        try:
            text = self._diff_loader(base.doc_id, self._chosen_doc_id)
        except Exception as e:  # noqa: BLE001 - 差异是辅助信息，失败不该拦住
            text = f"（差异信息获取失败：{type(e).__name__}）"
        self._diff_label.setText(text or "两份文档没有可显示的段落差异。")
