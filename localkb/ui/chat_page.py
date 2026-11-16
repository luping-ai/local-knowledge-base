"""对话页：消息流、知识库开关、来源卡片、降级提示。

## 为什么"知识库开关"要有三态

方案 §8.1.1 定的三态（none / selected / all）不是过度设计：

- **none**：普通对话。用户问"你好"时不该去查库（既慢又可能引入无关引用）。
- **selected**：只查勾选的库。这是默认——用户建了多个库时，"这次问的是工作还是学习"只有他知道。
- **all**：显式要求查全部。**必须有这个选项**，否则用户有 20 个库时要勾 20 次。

**不能把"一个都没勾"当成 all**：那是把"不用知识库"误解成"查全部"，
用户会看到答案带上他完全没想到的来源。

## 为什么来源卡片在 `meta` 就渲染

`meta` 一定在第一个 delta 之前到达，而 `done` 可能永远不来（用户取消、
网络断）。若等 `done` 才显示来源，中途失败时用户看不到任何依据——而那时
他更需要知道"系统本来打算引用什么"。

## 降级提示的位置

一律放在**输入框上方**（常驻黄条），不是弹窗：

- 弹窗会被关掉，之后用户就忘了"这次没查库"
- 常驻黄条在每次提问时都能看到，与"答案有没有依据"直接相关
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from localkb.chat import (
    KB_MODE_ALL,
    KB_MODE_NONE,
    KB_MODE_SELECTED,
    KbScope,
)
from localkb.ui.source_card import SourceCard, SourceView

__all__ = ["ChatPage"]

# 会话列表里显示的消息预览长度
_PREVIEW_LEN = 30


@dataclass
class _Bubble:
    """一条消息气泡。"""

    role: str           # user / assistant
    text: str = ""
    card_holder: Optional[QWidget] = None
    label: Optional[QLabel] = None
    sources_area: Optional[QWidget] = None


class ChatPage(QWidget):
    """对话页。"""

    # 请求发起一次提问：(question, scope_mode, kb_ids)
    ask_requested = Signal(str, str, list)
    # 请求取消当前生成
    cancel_requested = Signal()
    # 请求切换会话（conv_id）
    conversation_selected = Signal(str)
    # 请求新建会话
    new_conversation_requested = Signal()
    # 请求打开文件（path, page）
    open_file_requested = Signal(str, int)
    # 请求重新检查启动前提（用户点黄条上的"重新检查"）
    recheck_requested = Signal()

    def __init__(self, db, *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._conv_id: str = ""
        self._generating = False
        self._current_scope = KbScope()
        self._kbs: list = []
        self._bubbles: dict[str, _Bubble] = {}
        self._snippets: dict[str, str] = {}
        self._build()

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---- 左：会话列表 ----
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("会话"))
        head.addStretch(1)
        self._btn_new = QPushButton("新对话")
        self._btn_new.clicked.connect(self.new_conversation_requested.emit)
        head.addWidget(self._btn_new)
        lv.addLayout(head)

        self._conv_list = QListWidget()
        self._conv_list.currentItemChanged.connect(self._on_conv_selected)
        lv.addWidget(self._conv_list, 1)
        splitter.addWidget(left)

        # ---- 右：消息流 + 输入 ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        # 知识库范围选择器
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("知识库"))
        self._combo_scope = QComboBox()
        # 三项与 KbScope 的三个 mode 一一对应。
        #
        # ## 为什么"指定库"也要作为一个选项存在（踩过的坑）
        #
        # 早期版本下拉框只有"不使用"与"全部"两项，"指定几个库"只能通过
        # 旁边的按钮进入。后果是：用代码 `set_scope(SELECTED, [...])` 设置
        # 范围时，`findData(SELECTED)` 返回 -1 → 下拉框停在不使用，
        # 而右侧标签显示"localkb-dev"——**界面自相矛盾**。
        #
        # 实测症状：截图里下拉框写"不使用知识库"，旁边标签写"localkb-dev"。
        self._combo_scope.addItem("不使用知识库", KB_MODE_NONE)
        self._combo_scope.addItem("指定的知识库…", KB_MODE_SELECTED)
        self._combo_scope.addItem("全部知识库", KB_MODE_ALL)
        self._combo_scope.currentIndexChanged.connect(self._on_scope_changed)
        scope_row.addWidget(self._combo_scope, 1)
        self._btn_pick = QPushButton("选择…")
        self._btn_pick.setToolTip("勾选要检索的知识库")
        self._btn_pick.clicked.connect(self._on_pick_bases)
        scope_row.addWidget(self._btn_pick)
        self._scope_label = QLabel("")
        self._scope_label.setStyleSheet("color: #909399; font-size: 12px;")
        scope_row.addWidget(self._scope_label)
        rv.addLayout(scope_row)

        # 降级提示区（常驻，方案 §8.5）
        #
        # ## 为什么是"容器 + 标签 + 动作行"而不是一个 QLabel
        #
        # "没有加载模型"这类提示必须给出**可执行动作**（打开管理后台）。
        # 原来的纯 QLabel 写得出文字、放不了按钮，于是只能把动作写进正文
        # 让人自己去开浏览器——而用户不知道该往哪个地址去。
        #
        # 用容器之后，提示与按钮同处一块，视觉上是一个整体。
        self._banner_box = QWidget()
        banner_l = QVBoxLayout(self._banner_box)
        banner_l.setContentsMargins(0, 0, 0, 0)
        banner_l.setSpacing(4)

        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "background: #fdf6ec; border: 1px solid #f5dab1;"
            " border-radius: 4px; padding: 6px 8px; color: #b88230;"
        )
        banner_l.addWidget(self._banner)

        self._banner_actions = QWidget()
        ba = QHBoxLayout(self._banner_actions)
        ba.setContentsMargins(0, 0, 0, 0)
        ba.addStretch(1)

        # "重新检查"：用户在管理后台加载完模型后，不必重启程序。
        # 这是黄条里唯一必须存在的按钮——没有它，用户只能关掉再打开，
        # 而"关掉再打开"看起来像是重装才能解决的问题。
        self._banner_recheck_btn = QPushButton("重新检查")
        self._banner_recheck_btn.clicked.connect(self.recheck_requested)
        ba.addWidget(self._banner_recheck_btn)

        self._banner_action_btn = QPushButton("")
        self._banner_action_btn.setVisible(False)
        ba.addWidget(self._banner_action_btn)
        # 当前"打开链接"按钮的连接句柄（见 show_banner_action 的说明）
        self._banner_action_conn = None
        banner_l.addWidget(self._banner_actions)

        self._banner_box.setVisible(False)
        rv.addWidget(self._banner_box)

        # 消息流（滚动区）。用 QScrollArea + 竖排布局而不是 QTextBrowser：
        # 气泡里要放可点击的按钮（来源卡片），富文本做不到。
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._stream = QWidget()
        self._stream_layout = QVBoxLayout(self._stream)
        self._stream_layout.setContentsMargins(8, 8, 8, 8)
        self._stream_layout.setSpacing(10)
        self._stream_layout.addStretch(1)
        self._scroll.setWidget(self._stream)
        rv.addWidget(self._scroll, 1)

        # 状态行（排队 / 正在准备模型 / 生成中）
        self._status = QLabel("")
        self._status.setStyleSheet("color: #909399; font-size: 12px;")
        rv.addWidget(self._status)

        # 输入区
        input_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入问题…（Enter 发送，Shift+Enter 换行）")
        self._input.returnPressed.connect(self._on_send)
        input_row.addWidget(self._input, 1)
        self._btn_send = QPushButton("发送")
        self._btn_send.clicked.connect(self._on_send)
        input_row.addWidget(self._btn_send)
        self._btn_cancel = QPushButton("停止")
        self._btn_cancel.setVisible(False)
        self._btn_cancel.clicked.connect(self.cancel_requested.emit)
        input_row.addWidget(self._btn_cancel)
        rv.addLayout(input_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 800])

    # ------------------------------------------------------------- 知识库范围

    def set_kbs(self, kbs: list) -> None:
        """服务端知识库列表（SDK 的 KBSummary）。"""
        self._kbs = list(kbs)
        self._refresh_scope_label()

    def set_scope(self, scope: KbScope, *, notify: bool = False) -> None:
        """设置当前范围（来自会话历史或用户选择）。"""
        self._current_scope = scope
        mode = scope.mode
        # 选中"指定库"但一个都没勾 → 退回 none。
        #
        # 为什么不能留着一个空列表：`KbScope.to_request_kb_ids()` 对
        # selected+空 会返回 None（不查库），但界面上显示"指定的知识库"
        # 却什么都没选，用户会以为查过了。UI 状态要诚实。
        if mode == KB_MODE_SELECTED and not scope.kb_ids:
            self._current_scope = KbScope(KB_MODE_NONE)
            mode = KB_MODE_NONE

        idx = self._combo_scope.findData(mode)
        self._combo_scope.blockSignals(True)
        self._combo_scope.setCurrentIndex(idx if idx >= 0 else 0)
        self._combo_scope.blockSignals(False)
        self._refresh_scope_label()
        if notify and self._conv_id:
            self._db.update_conversation_scope(
                self._conv_id, self._current_scope.mode,
                json.dumps(self._current_scope.kb_ids, ensure_ascii=False),
            )

    def _on_scope_changed(self, _idx: int) -> None:
        mode = self._combo_scope.currentData()
        if mode == KB_MODE_SELECTED:
            # 切到"指定的知识库"时：若已有勾选就沿用，否则弹选择器。
            #
            # ## 为什么"已有勾选就沿用"
            #
            # 用户可能在"不使用"与"指定"之间来回切。每次都弹框会让
            # 这个动作变得很烦；而已选的库是有意义的上下文。
            if self._current_scope.mode == KB_MODE_SELECTED \
                    and self._current_scope.kb_ids:
                self._refresh_scope_label()
                return
            self._on_pick_bases()
            return
        self.set_scope(KbScope(mode=mode or KB_MODE_NONE), notify=True)

    def _on_pick_bases(self) -> None:
        """弹出多选对话框选知识库。"""
        if not self._kbs:
            QMessageBox.information(
                self, "没有可用的知识库",
                "服务端还没有知识库。请先到知识库页创建并导入文档。",
            )
            return

        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("选择要检索的知识库")
        dlg.resize(420, 360)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("勾选后，提问会只在这些库里检索："))

        lst = QListWidget()
        chosen = set(self._current_scope.kb_ids)
        for kb in self._kbs:
            item = QListWidgetItem(f"{kb.name}  ·  {kb.document_count} 篇")
            item.setData(Qt.UserRole, kb.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if kb.id in chosen else Qt.Unchecked)
            lst.addItem(item)
        v.addWidget(lst, 1)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        v.addWidget(box)

        if dlg.exec() != QDialog.Accepted:
            # 取消时不改动当前范围
            self._combo_scope.blockSignals(True)
            idx = self._combo_scope.findData(self._current_scope.mode)
            self._combo_scope.setCurrentIndex(idx if idx >= 0 else 0)
            self._combo_scope.blockSignals(False)
            return

        picked = [
            lst.item(i).data(Qt.UserRole)
            for i in range(lst.count())
            if lst.item(i).checkState() == Qt.Checked
        ]
        if not picked:
            # 一个都没勾 = 不用知识库（不是"查全部"）
            self.set_scope(KbScope(KB_MODE_NONE), notify=True)
            return
        self.set_scope(KbScope(KB_MODE_SELECTED, picked), notify=True)

    def _refresh_scope_label(self) -> None:
        sc = self._current_scope
        if sc.mode == KB_MODE_ALL:
            self._scope_label.setText(f"已选择全部 {len(self._kbs)} 个库")
        elif sc.mode == KB_MODE_SELECTED:
            names = {k.id: k.name for k in self._kbs}
            picked = [names.get(i, i) for i in sc.kb_ids]
            text = "、".join(picked[:3])
            if len(picked) > 3:
                text += f" 等 {len(picked)} 个"
            self._scope_label.setText(text)
        else:
            self._scope_label.setText("")

    # ------------------------------------------------------------------ 会话

    def set_conversations(self, convs: list) -> None:
        self._conv_list.blockSignals(True)
        self._conv_list.clear()
        for c in convs:
            title = c.title or "（未命名会话）"
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, c.id)
            self._conv_list.addItem(item)
        self._conv_list.blockSignals(False)

    def set_conversation(self, conv_id: str) -> None:
        self._conv_id = conv_id
        for i in range(self._conv_list.count()):
            if self._conv_list.item(i).data(Qt.UserRole) == conv_id:
                self._conv_list.blockSignals(True)
                self._conv_list.setCurrentRow(i)
                self._conv_list.blockSignals(False)
                break

    def _on_conv_selected(self, cur, _prev) -> None:
        if cur is None:
            return
        cid = cur.data(Qt.UserRole)
        if cid and cid != self._conv_id:
            self.conversation_selected.emit(cid)

    def clear_stream(self) -> None:
        """清空消息流（切换会话或新建时）。"""
        while self._stream_layout.count() > 1:
            item = self._stream_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._bubbles.clear()

    # ------------------------------------------------------------------ 渲染

    def add_user_message(self, text: str) -> None:
        self._add_bubble("user", text)

    def begin_assistant_message(self) -> None:
        """开始一条空的助手消息（准备接流）。"""
        self._add_bubble("assistant", "")
        self._status.setText("正在准备模型…")

    def append_delta(self, text: str) -> None:
        b = self._last_assistant()
        if b is None or b.label is None:
            return
        b.text += text
        b.label.setText(b.text)
        self._scroll_to_bottom()

    def finish_assistant_message(self, *, sources: list[SourceView],
                                 warnings: list[str],
                                 finish_reason: str = "",
                                 used_chunks: int = 0,
                                 all_bases: bool = False,
                                 base_count: int = 0,
                                 degraded: bool = False,
                                 model: str = "") -> None:
        b = self._last_assistant()
        if b is not None and b.sources_area is not None:
            self._render_sources(b.sources_area, sources, warnings,
                                 used_chunks=used_chunks, all_bases=all_bases,
                                 base_count=base_count, degraded=degraded,
                                 model=model, finish_reason=finish_reason)
        self.set_generating(False)
        self._status.setText("")
        self._scroll_to_bottom()

    def show_error(self, message: str) -> None:
        """把最后一条助手消息标为失败/提示。

        ## 为什么文案也写进气泡

        `_status` 是底部一行小字，容易被忽略。当"这次提问没法进行"时
        （模型未加载、库不可用），原因必须出现在**回答该出现的位置**——
        用户的视线就在那里。
        """
        b = self._last_assistant()
        if b is not None and b.label is not None:
            b.text = message
            b.label.setText(message)
            b.label.setStyleSheet("color: #f56c6c;")
        self.set_generating(False)
        self._status.setText(message)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def _add_bubble(self, role: str, text: str) -> None:
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)

        bubble = QFrame()
        bubble.setMaximumWidth(680)
        bubble.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(10, 8, 10, 8)
        bv.setSpacing(6)

        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = QFont()
        f.setPointSize(10)
        label.setFont(f)
        bv.addWidget(label)

        # sources_area 只给 assistant：用户消息没有来源
        sources_area = None
        if role == "assistant":
            sources_area = QWidget()
            QVBoxLayout(sources_area).setContentsMargins(0, 0, 0, 0)
            bv.addWidget(sources_area)

        if role == "user":
            bubble.setStyleSheet(
                "background: #ecf5ff; border-radius: 8px;"
            )
            hl.addStretch(1)
            hl.addWidget(bubble)
        else:
            bubble.setStyleSheet(
                "background: #ffffff; border: 1px solid #ebeef5;"
                " border-radius: 8px;"
            )
            hl.addWidget(bubble)
            hl.addStretch(1)

        # 插到 stretch 之前（保持底部对齐）
        self._stream_layout.insertWidget(self._stream_layout.count() - 1, holder)
        self._bubbles[str(id(bubble))] = _Bubble(
            role=role, text=text, label=label,
            sources_area=sources_area, card_holder=bubble,
        )
        self._scroll_to_bottom()

    def _last_assistant(self) -> Optional[_Bubble]:
        """最后一个 assistant 气泡（流式写入目标）。"""
        for b in reversed(list(self._bubbles.values())):
            if b.role == "assistant":
                return b
        return None

    def _render_sources(self, area: QWidget, sources: list[SourceView],
                        warnings: list[str], *, used_chunks: int,
                        all_bases: bool, base_count: int,
                        degraded: bool, model: str,
                        finish_reason: str) -> None:
        lay = area.layout()
        if lay is None:
            return
        # 清掉旧内容（重渲时）
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        # 依据区标题 + 库范围说明（方案 §8.1.1：传 all 时必须如实展示）
        if sources or used_chunks:
            head = QLabel("依据：")
            head.setStyleSheet("color: #909399; font-size: 12px;")
            lay.addWidget(head)

        if all_bases:
            scope_note = QLabel(f"已检索全部 {base_count} 个知识库")
            scope_note.setStyleSheet("color: #909399; font-size: 12px;")
            lay.addWidget(scope_note)

        for src in sources:
            card = SourceCard(src)
            card.open_requested.connect(self.open_file_requested.emit)
            card.snippet_requested.connect(self._on_show_snippet)
            lay.addWidget(card)

        # 未命中知识库 —— 必须明确告知，否则用户以为答案有依据
        if used_chunks == 0 and not sources:
            warn = QLabel("知识库中未找到相关资料，以下为通用回答。")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #e6a23c; font-size: 12px;")
            lay.addWidget(warn)

        # 降级
        if degraded:
            d = QLabel(f"本次使用 {model}（服务端按内存情况降级）")
            d.setWordWrap(True)
            d.setStyleSheet("color: #909399; font-size: 12px;")
            lay.addWidget(d)

        # 截断
        if finish_reason == "length":
            t = QLabel("回答达到长度上限，已截断。")
            t.setWordWrap(True)
            t.setStyleSheet("color: #e6a23c; font-size: 12px;")
            lay.addWidget(t)

        # 服务端警告（预算裁剪、语义不可用等）—— 折叠为一行，不丢弃
        if warnings:
            ws = QLabel("；".join(warnings))
            ws.setWordWrap(True)
            ws.setStyleSheet("color: #909399; font-size: 12px;")
            lay.addWidget(ws)

    def _on_show_snippet(self, src: SourceView) -> None:
        """文件不在时的退路：显示引用片段。"""
        dlg = QMessageBox(self)
        dlg.setWindowTitle("引用的片段")
        dlg.setText(f"{src.name}　{src.page_label}" if src.page_label else src.name)
        dlg.setInformativeText(src.snippet or "（没有片段记录）")
        if src.path:
            dlg.setDetailedText(f"原路径：{src.path}\n该文件已不在原位置。")
        dlg.exec()

    def show_banner(self, text: str) -> None:
        """常驻提示（降级、库不可用等）。空字符串隐藏。

        会同时清掉动作按钮——换了提示还挂着上一个动作会让人误点
        （比如"打开管理后台"配着"磁盘空间不足"的文案）。
        """
        self._banner.setText(text)
        self._banner_box.setVisible(bool(text))
        if not text:
            self.show_banner_action("", "")

    def show_banner_action(self, label: str, url: str) -> None:
        """在提示下方挂一个打开链接的按钮。空 label 表示移除。

        ## 为什么用 URL 而不是信号

        目前的动作只有"打开某个地址"这一种（管理后台）。走信号需要每个
        调用点都连一次，而地址本身是数据、不是行为。
        """
        if not label or not url:
            self._banner_action_btn.setVisible(False)
            self._banner_action_btn.setText("")
            self._banner_actions.setVisible(False)
            self._banner_action_conn = None
            return

        self._banner_action_btn.setText(label)
        # ## 为什么用一个连接对象而不是反复 disconnect()
        #
        # `QPushButton.clicked` 会被反复 connect 累积——换几次提示之后
        # 一次点击会打开好几个页面。
        #
        # 但 `disconnect()` 在"从未连接过"时**不抛 RuntimeError，只发一条
        # RuntimeWarning**（`libpyside: Failed to disconnect (None)`）。
        # 所以 try/except 捕不到，测试里会看到 4 条警告而不知道要不要管。
        #
        # 持有 `clicked.connect(...)` 返回的连接对象，用 `disconnect(conn)`
        # 精确断开，未连接时什么都不做。
        if self._banner_action_conn is not None:
            with contextlib.suppress(RuntimeError):
                self._banner_action_btn.clicked.disconnect(self._banner_action_conn)
            self._banner_action_conn = None
        self._banner_action_conn = self._banner_action_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(url))
        self._banner_action_btn.setVisible(True)
        self._banner_actions.setVisible(True)

    # ------------------------------------------------------------------ 生成

    def set_generating(self, on: bool) -> None:
        self._generating = on
        self._btn_send.setEnabled(not on)
        self._btn_cancel.setVisible(on)
        self._input.setEnabled(not on)

    def _on_send(self) -> None:
        if self._generating:
            return
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.set_generating(True)
        sc = self._current_scope
        # 先把消息与气泡加进流里，让用户看到"我说了什么"。
        # 若前置检查失败，main_window 会调 show_banner + set_generating(False)，
        # 气泡停在"正在准备模型…"——这比"点了发送什么反应都没有"要好。
        self.add_user_message(text)
        self.begin_assistant_message()
        self.ask_requested.emit(text, sc.mode, list(sc.kb_ids))

    # ------------------------------------------------------------------ 滚动

    def _scroll_to_bottom(self) -> None:
        """滚到底部。

        ## 为什么要 QTimer.singleShot(0)

        直接设 value 时布局还没算完（气泡刚 addWidget），
        `verticalScrollBar().maximum()` 仍是旧值，滚不到底。
        延迟到下一轮事件循环，布局已经完成。
        """
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))
