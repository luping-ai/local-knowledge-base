"""删除确认对话框：文件与库记录是两个独立决定。

## 为什么是两个复选框而不是一个

方案的 §7.7 把删除拆成两个维度：

    ┌─ 磁盘上的文件 ──── 保留 / 移到回收站 ─┐
    └─ 知识库里的记录 ─ 移除 / 保留 ────────┘

用户的需求组合都有真实场景：

  - "从库里拿掉，但文件留着"（最常见：误导入了一个不该进库的文件）
  - "库和文件都清掉"（真的不要了）
  - "文件删了，但知识还想留着"（这条**不在这个对话框里**，它是
    `missing` 处理，见 maintain.py）

把两者合成一个"删除"按钮的后果是：用户想"从库里拿掉"却担心点了会
删文件，于是不敢点。

## 为什么默认不勾"删除文件"

误删不可逆。默认值必须是最保守的那个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from localkb.platform.recycle import can_recycle

__all__ = ["DeleteChoice", "DeleteDialog"]


@dataclass
class DeleteChoice:
    remove_record: bool = True
    remove_file: bool = False


class DeleteDialog(QDialog):
    """删除确认。"""

    def __init__(self, *, name: str, path: str, size_bytes: int = 0,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("从知识库移除")
        self.resize(520, 240)
        self._choice = DeleteChoice()
        self._build(name, path, size_bytes)

    def _build(self, name: str, path: str, size_bytes: int) -> None:
        v = QVBoxLayout(self)
        v.setSpacing(10)

        title = QLabel(f"「{name}」")
        title.setStyleSheet("font-weight: 600; font-size: 13px;")
        title.setWordWrap(True)
        v.addWidget(title)

        self._cb_record = QCheckBox("从知识库移除（移除后无法通过提问检索到它）")
        self._cb_record.setChecked(True)
        v.addWidget(self._cb_record)

        # 删除文件是第二个独立决定，默认不勾
        recycle_ok = can_recycle()
        size_text = _fmt_size(size_bytes)
        self._cb_file = QCheckBox("同时把文件移到回收站")
        self._cb_file.setChecked(False)
        self._cb_file.setEnabled(recycle_ok)
        v.addWidget(self._cb_file)

        path_label = QLabel(f"    {path}" + (f"（{size_text}）" if size_text else ""))
        path_label.setStyleSheet("color: #606266; font-size: 12px;")
        path_label.setWordWrap(True)
        path_label.setContentsMargins(24, 0, 0, 0)
        v.addWidget(path_label)

        hint = QLabel(
            "不勾第二项时，文件保持原样。之后想恢复，重新扫描该目录即可。"
            if recycle_ok else
            "当前平台不支持移到回收站，本程序不做永久删除。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(hint)

        v.addStretch(1)

        box = QDialogButtonBox()
        self._btn_ok = box.addButton("确定", QDialogButtonBox.AcceptRole)
        box.addButton("取消", QDialogButtonBox.RejectRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)

        self._cb_record.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        """两个都不勾时禁用"确定"（等于什么都没做）。

        ## 为什么不把"确定"改成"取消"

        用户的意图可能是"先看看，算了"。禁用按钮并给一行说明，比
        静默关闭更清楚。
        """
        has_any = self._cb_record.isChecked() or self._cb_file.isChecked()
        self._btn_ok.setEnabled(has_any)

    @property
    def choice(self) -> DeleteChoice:
        return DeleteChoice(
            remove_record=self._cb_record.isChecked(),
            remove_file=self._cb_file.isChecked(),
        )


def _fmt_size(n: int) -> str:
    if n <= 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"
