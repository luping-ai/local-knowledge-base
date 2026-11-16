"""设置对话框（方案 §12.2.1 的 `similar_default_choice` 落地）。

## 为什么现在只有一项设置

方案里有若干"可配置"的项，但真正需要在 v0.1 就暴露给用户的只有这一条：
相似组确认时**默认选中哪一版**。其余（页数上限、排除规则、并发）要么
属于服务端，要么默认值对所有用户都合适。

加一堆"高级选项"会让设置页看起来很重要，而实际上用户不该改它们——
那会把"配置负担"转移给用户。

## 为什么默认是 `none`

§12.2.1 的理由值得重复一遍：同一份文档的两个版本，哪个"应该进知识库"
取决于**内容差异**，而不是时间。预选最新会让用户习惯性直接点确定，
从而在"新版其实是误存的草稿"时把旧版挤掉。

所以默认不预选；愿意省事的用户可以改成 newest/oldest，但**预选之后
仍要用户点确定**——预选只是把单选按钮放在某个位置，不是自动提交。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from localkb.dedup import SETTING_KEY, similar_default_choice
from localkb.store.db import StateDB

__all__ = ["SettingsDialog"]

# 下拉项：(内部值, 显示文本, 说明)
_CHOICES = (
    ("none", "不预选（每次都要自己选）",
     "推荐。同一份文档的两个版本，哪个该入库取决于内容差异，不是时间。"),
    ("newest", "预选修改时间最新的那份",
     "省一次点击。注意：若最新版其实是误存的草稿，容易把旧版挤掉。"),
    ("oldest", "预选最早的那份",
     "适用于「以归档版为准」的用法（比如合同以首签版为准）。"),
)


class SettingsDialog(QDialog):
    """设置。目前只有相似组默认选中策略一项。"""

    def __init__(self, db: StateDB, *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(560, 260)
        self._db = db
        self._saved = False
        self._build()

    @property
    def saved(self) -> bool:
        return self._saved

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setSpacing(10)

        title = QLabel("重复与相似文件")
        title.setStyleSheet("font-size: 14px; font-weight: 600;")
        v.addWidget(title)

        form = QFormLayout()
        self._combo = QComboBox()
        for value, label, tip in _CHOICES:
            self._combo.addItem(label, value)
            idx = self._combo.count() - 1
            self._combo.setItemData(idx, tip, 3)   # 3 = ToolTipRole
        current = similar_default_choice(self._db)
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == current:
                self._combo.setCurrentIndex(i)
                break
        self._combo.currentIndexChanged.connect(self._sync_hint)
        form.addRow("确认相似组时：", self._combo)
        v.addLayout(form)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(self._hint)

        # 明确说"预选不等于自动入库"。用户最担心的就是"程序替我做决定"。
        note = QLabel(
            "无论选哪一项，都不会自动提交——预选只是把单选按钮放在某个"
            "位置，「确定」仍然要你自己点。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #909399; font-size: 12px;")
        v.addWidget(note)

        v.addStretch(1)

        box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_ok)
        box.rejected.connect(self.reject)
        v.addWidget(box)

        self._sync_hint()

    def _sync_hint(self) -> None:
        value = self._combo.currentData()
        for v, _label, tip in _CHOICES:
            if v == value:
                self._hint.setText(tip)
                return

    def _on_ok(self) -> None:
        self._db.set_setting(SETTING_KEY, str(self._combo.currentData()))
        self._saved = True
        self.accept()
