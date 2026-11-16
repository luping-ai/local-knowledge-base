"""知识库页：知识库列表、文档列表、扫描目录、扫描进度、跳过原因。

## 这个页面的三块职责

1. **知识库与文档列表**：用户看到"库里有什么"，并能逐篇移除（§7.7）
2. **扫描目录与进度**：把本机文件导进库（M0~M2）
3. **跳过 / 缺失 / 失败原因**：回答"我明明选了 3000 个文件，为什么只导了 2000 个"

## 为什么"跳过原因"要单独一块而不是塞进列表

用户最容易问的问题就是上面那句。一个按原因分组的汇总（"87 个文件
正被其他程序占用"）能直接回答，而混在文件列表里的 87 行红字只会
让人关掉这个页面（方案 §5.6 / §7.5 的同一原则）。

## missing 为什么单独一个提示条

"源文件已不在"不是跳过、不是失败，而是"库内内容还在、磁盘上没了"。
这个状态需要用户决定（保留 / 移除 / 重绑），所以用提示条 + 计数，
不用列表行——列表里塞 37 行"文件已不在"会把真正要看的文档挤走。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from localkb.scan.job import ScanPhase, ScanProgress
from localkb.scan.walker import SKIP_MESSAGES
from localkb.store.db import StateDB

__all__ = ["KbPage"]

_PHASE_LABEL = {
    ScanPhase.IDLE: "空闲",
    ScanPhase.WALKING: "枚举文件",
    ScanPhase.HASHING: "计算指纹",
    ScanPhase.PARSING: "解析与切块",
    ScanPhase.INGESTING: "写入知识库",
    ScanPhase.VECTORIZING: "生成向量",
    ScanPhase.DONE: "完成",
    ScanPhase.CANCELLED: "已取消",
    ScanPhase.FAILED: "失败",
    ScanPhase.PAUSED: "已暂停",
}


class KbPage(QWidget):
    """知识库页。"""

    # 请求主窗口跑一次扫描（参数是 kb_id 与目录列表）
    scan_requested = Signal(str, list)
    # 请求取消
    cancel_requested = Signal()
    # 请求刷新知识库列表
    refresh_requested = Signal()
    # 请求删除一篇文档：(kb_id, path, doc_id, remove_file)
    delete_requested = Signal(str, str, str, bool)
    # 请求刷新文档列表（切换知识库或对账后）
    docs_refresh_requested = Signal(str)
    # 请求处理 missing 项
    missing_manage_requested = Signal()
    # 请求打开文件（path, page）
    open_file_requested = Signal(str, int)
    # 请求查看"内容完全相同的副本"详情（方案 §12.7）
    duplicates_requested = Signal(str)
    # 请求确认相似组（方案 §12.5）
    similar_confirm_requested = Signal(str)

    def __init__(self, db: StateDB, *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._current_kb: str = ""
        self._scanning = False
        self._build()

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        root = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---- 左：知识库列表 ----
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("知识库"))
        head.addStretch(1)
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.clicked.connect(self.refresh_requested.emit)
        head.addWidget(self._btn_refresh)
        lv.addLayout(head)

        self._kb_list = QListWidget()
        self._kb_list.currentItemChanged.connect(self._on_kb_selected)
        lv.addWidget(self._kb_list, 1)
        self._kb_hint = QLabel("")
        self._kb_hint.setWordWrap(True)
        self._kb_hint.setStyleSheet("color: #909399; font-size: 12px;")
        lv.addWidget(self._kb_hint)
        splitter.addWidget(left)

        # ---- 右：文档列表 + 扫描目录 + 进度 + 结果 ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)

        # missing 提示条（默认隐藏，有 missing 才显示）
        self._missing_banner = QWidget()
        mb = QHBoxLayout(self._missing_banner)
        mb.setContentsMargins(8, 6, 8, 6)
        self._missing_label = QLabel("")
        self._missing_label.setWordWrap(True)
        self._missing_label.setStyleSheet("color: #b88230;")
        mb.addWidget(self._missing_label, 1)
        self._btn_missing = QPushButton("处理…")
        self._btn_missing.clicked.connect(self.missing_manage_requested.emit)
        mb.addWidget(self._btn_missing)
        self._missing_banner.setStyleSheet(
            "background: #fdf6ec; border: 1px solid #f5dab1; border-radius: 4px;"
        )
        self._missing_banner.setVisible(False)
        rv.addWidget(self._missing_banner)

        # 完全重复提示条（方案 §12.7）
        #
        # ## 为什么只给一行 + 一个"详情"，不做确认对话框
        #
        # §12.2：完全重复**不需要用户判断**——内容一模一样，选哪一份入库
        # 对检索结果没有影响。所以这里只告知"另有 N 个副本未入库"，
        # 不打断流程、不要求任何操作。
        #
        # "去确认"那种交互只属于**内容相似**（相似组），因为那一层需要
        # 用户判断"哪个版本对"。把两者混在一个对话框里会让用户以为
        # "连一模一样的文件也要我逐个确认"，那是纯粹的负担。
        #
        # ## 为什么措辞里绝不能出现"清理/释放空间/删除"
        #
        # §12.1 明确禁止。用户看到"清理"会以为程序要动他的文件，而这里
        # 只是"没入库"。文件原样留在磁盘上。
        self._dup_banner = QWidget()
        db_l = QHBoxLayout(self._dup_banner)
        db_l.setContentsMargins(8, 6, 8, 6)
        self._dup_label = QLabel("")
        self._dup_label.setWordWrap(True)
        self._dup_label.setStyleSheet("color: #606266;")
        db_l.addWidget(self._dup_label, 1)
        self._btn_dup = QPushButton("详情")
        self._btn_dup.clicked.connect(
            lambda: self.duplicates_requested.emit(self._current_kb))
        db_l.addWidget(self._btn_dup)
        self._dup_banner.setStyleSheet(
            "background: #f4f4f5; border: 1px solid #e9e9eb; border-radius: 4px;"
        )
        self._dup_banner.setVisible(False)
        rv.addWidget(self._dup_banner)

        # 相似组待确认提示条（方案 §12.5 / §12.7）
        #
        # ## 为什么这条要放在文档列表上方、且带"去确认"
        #
        # §12.5：内容相似**需要用户判断**（选哪一版），所以它是真正的
        # 待办，不是纯告知。用户点"稍后"后，下次打开这个库还要能看到
        # ——这就是 B8 的验收点（关程序再打开仍有提示）。
        #
        # 与"完全重复"分两条：那条不需要任何操作，这条必须有人做决定。
        # 混成一条会让用户以为"一模一样的东西也要我确认"。
        self._similar_banner = QWidget()
        sb = QHBoxLayout(self._similar_banner)
        sb.setContentsMargins(8, 6, 8, 6)
        self._similar_label = QLabel("")
        self._similar_label.setWordWrap(True)
        self._similar_label.setStyleSheet("color: #b88230;")
        sb.addWidget(self._similar_label, 1)
        self._btn_similar = QPushButton("去确认")
        self._btn_similar.clicked.connect(
            lambda: self.similar_confirm_requested.emit(self._current_kb))
        sb.addWidget(self._btn_similar)
        self._similar_banner.setStyleSheet(
            "background: #fdf6ec; border: 1px solid #f5dab1; border-radius: 4px;"
        )
        self._similar_banner.setVisible(False)
        rv.addWidget(self._similar_banner)

        # 文档列表
        self._grp_docs = QGroupBox("知识库文档")
        dl = QVBoxLayout(self._grp_docs)
        self._docs_table = QTableWidget(0, 4)
        self._docs_table.setHorizontalHeaderLabels(["名称", "状态", "切片", "操作"])
        self._docs_table.verticalHeader().setVisible(False)
        self._docs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._docs_table.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self._docs_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        dl.addWidget(self._docs_table, 1)
        self._docs_hint = QLabel("")
        self._docs_hint.setStyleSheet("color: #909399; font-size: 12px;")
        self._docs_hint.setWordWrap(True)
        dl.addWidget(self._docs_hint)
        rv.addWidget(self._grp_docs, 2)

        # 扫描目录
        self._grp_roots = QGroupBox("扫描目录")
        rvl = QVBoxLayout(self._grp_roots)
        self._roots_list = QListWidget()
        rvl.addWidget(self._roots_list, 1)
        btns = QHBoxLayout()
        self._btn_add = QPushButton("添加目录…")
        self._btn_add.clicked.connect(self._on_add_root)
        self._btn_remove = QPushButton("移除")
        self._btn_remove.clicked.connect(self._on_remove_root)
        self._btn_scan = QPushButton("开始扫描")
        self._btn_scan.clicked.connect(self._on_scan)
        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self.cancel_requested.emit)
        for b in (self._btn_add, self._btn_remove, self._btn_scan, self._btn_cancel):
            btns.addWidget(b)
        btns.addStretch(1)
        rvl.addLayout(btns)
        rv.addWidget(self._grp_roots, 1)

        # 进度
        prog = QGroupBox("扫描进度")
        pv = QVBoxLayout(prog)
        self._phase = QLabel("空闲")
        pv.addWidget(self._phase)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        pv.addWidget(self._bar)
        self._current = QLabel("")
        self._current.setStyleSheet("color: #606266; font-size: 12px;")
        pv.addWidget(self._current)
        rv.addWidget(prog)

        # 跳过原因
        self._grp_skip = QGroupBox("已跳过的文件")
        sv = QVBoxLayout(self._grp_skip)
        self._skip_list = QListWidget()
        sv.addWidget(self._skip_list, 1)
        self._skip_detail = QLabel("")
        self._skip_detail.setWordWrap(True)
        self._skip_detail.setStyleSheet("color: #606266; font-size: 12px;")
        sv.addWidget(self._skip_detail)
        rv.addWidget(self._grp_skip, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

    # ------------------------------------------------------------------ 知识库

    def set_kbs(self, kbs: list) -> None:
        """填充服务端知识库列表（SDK 的 KBSummary 列表）。"""
        prev = self._current_kb
        self._kb_list.clear()
        for kb in kbs:
            label = f"{kb.name}  ·  {kb.document_count} 篇"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, kb.id)
            item.setToolTip(f"id: {kb.id}\n切片数: {kb.chunk_count}")
            self._kb_list.addItem(item)
        if not kbs:
            self._kb_hint.setText(
                "服务端还没有知识库。可以到管理后台新建，或稍后由本程序创建。"
            )
        else:
            self._kb_hint.setText("")

        # 恢复上次选中
        for i in range(self._kb_list.count()):
            if self._kb_list.item(i).data(Qt.UserRole) == prev:
                self._kb_list.setCurrentRow(i)
                break
        else:
            if self._kb_list.count():
                self._kb_list.setCurrentRow(0)

    def set_kb_error(self, message: str) -> None:
        """拉取知识库失败时显示（通常是端点未启用）。"""
        self._kb_list.clear()
        self._current_kb = ""
        self._kb_hint.setText(message)
        self._update_buttons()

    def _on_kb_selected(self, cur, _prev) -> None:
        self._current_kb = cur.data(Qt.UserRole) if cur else ""
        self._reload_roots()
        self._update_buttons()
        # 切换库时刷新两条提示条：B8 的"关程序再打开仍有提示"靠的就是
        # 这里——状态来自 local_files / pending_dups 表，不是内存变量。
        self.refresh_duplicate_banner()
        self.refresh_similar_banner()
        if self._current_kb:
            self.docs_refresh_requested.emit(self._current_kb)

    # -------------------------------------------------------------- 文档列表

    def set_documents(self, docs: list, local: dict) -> None:
        """填充文档列表。

        `docs` 是服务端的 `DocumentSummary` 列表；`local` 是
        `{doc_id: LocalFile}`，用于拿本地路径与状态。

        ## 为什么两者都要

        服务端给"名称、状态、切片数"；本地给"磁盘路径与是否还在"。
        来源卡片的"打开原文"需要路径，而服务端不返回它（方案 §6.3）。
        """
        self._docs_table.setRowCount(0)
        for d in docs:
            row = self._docs_table.rowCount()
            self._docs_table.insertRow(row)

            name_item = QTableWidgetItem(getattr(d, "name", "") or getattr(d, "id", ""))
            name_item.setData(Qt.UserRole, getattr(d, "id", ""))
            loc = local.get(getattr(d, "id", ""))
            if loc is not None:
                name_item.setToolTip(loc.path)
            self._docs_table.setItem(row, 0, name_item)

            status = getattr(d, "status", "") or ""
            idx = getattr(d, "index_status", "") or ""
            status_text = status + (f" / {idx}" if idx else "")
            st_item = QTableWidgetItem(status_text)
            if status == "MISSING" or (loc is not None and loc.status == "missing"):
                st_item.setForeground(Qt.darkYellow)
            elif status == "FAILED":
                st_item.setForeground(Qt.red)
            self._docs_table.setItem(row, 1, st_item)

            self._docs_table.setItem(
                row, 2, QTableWidgetItem(str(getattr(d, "chunk_count", 0) or 0)))

            self._add_row_buttons(row, d, loc)

        if not docs:
            self._docs_hint.setText("这个知识库还没有文档。选一个目录并点「开始扫描」。")
        else:
            # 服务端有、本机没有映射的文档无法"打开原文"（不知道路径）。
            # 这不是错误——它可能是管理后台或 CLI 导入的——但要在界面上
            # 说明，否则用户点"移除"时看不到"打开"按钮会以为程序坏了。
            no_local = [d for d in docs
                        if getattr(d, "id", "") not in local]
            if no_local:
                self._docs_hint.setText(
                    f"共 {len(docs)} 篇，其中 {len(no_local)} 篇不是本机导入的"
                    f"（无法「打开原文」，但可以移除）"
                )
            else:
                self._docs_hint.setText(f"共 {len(docs)} 篇")

    def _add_row_buttons(self, row: int, doc, loc) -> None:
        """每行的操作按钮：打开原文 / 从库中移除。"""
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(2, 0, 2, 0)
        hl.setSpacing(4)

        if loc is not None and Path(loc.path).exists():
            btn_open = QPushButton("打开")
            btn_open.setToolTip(loc.path)
            btn_open.clicked.connect(
                lambda _=False, p=loc.path: self.open_file_requested.emit(p, 0))
            hl.addWidget(btn_open)

        btn_del = QPushButton("移除")
        btn_del.setToolTip("从知识库移除（可选择是否同时删除文件）")
        doc_id = getattr(doc, "id", "")
        name = getattr(doc, "name", "") or doc_id
        size = int(getattr(doc, "file_size", 0) or 0)
        path = loc.path if loc is not None else ""
        btn_del.clicked.connect(
            lambda _=False, i=doc_id, n=name, p=path, s=size:
            self._on_delete_clicked(i, n, p, s))
        hl.addWidget(btn_del)

        hl.addStretch(1)
        self._docs_table.setCellWidget(row, 3, holder)

    def _on_delete_clicked(self, doc_id: str, name: str, path: str,
                           size: int) -> None:
        from localkb.ui.delete_dialog import DeleteDialog

        if not self._current_kb:
            return
        dlg = DeleteDialog(name=name, path=path, size_bytes=size, parent=self)
        if dlg.exec() != DeleteDialog.Accepted:
            return
        ch = dlg.choice
        self.delete_requested.emit(
            self._current_kb, path, doc_id, ch.remove_file)

    def show_missing(self, count: int, sample: str = "") -> None:
        """显示 missing 提示条。count=0 时隐藏。"""
        if count <= 0:
            self._missing_banner.setVisible(False)
            return
        text = f"⚠ 有 {count} 篇文档的源文件已不在原位置（可能被移动或删除）。"
        if sample:
            text += f" 例如：{sample}"
        self._missing_label.setText(text)
        self._missing_banner.setVisible(True)

    def refresh_duplicate_banner(self) -> None:
        """刷新"内容完全相同的副本"提示条（方案 §12.7）。

        ## 文案说"未入库"，不说"重复"

        §12.1 明确禁止"清理/释放空间/删除"这类措辞。中性表述是
        "已跳过未入库"——文件还在磁盘上，只是没进知识库。

        也不能说"与库内文档相同"：B1 场景（同一文件复制到 5 个目录、
        一次扫描全扫到）里，4 份副本重复的对象是**同批刚被导入的那一份**，
        不是"库内已有的"。说成"库内文档"会让用户困惑"我库里什么时候
        有这个文件了"。

        所以用"与其他文件内容完全相同"。
        """
        if not self._current_kb:
            self._dup_banner.setVisible(False)
            return

        groups = self._db.find_duplicate_hashes(self._current_kb)
        if not groups:
            self._dup_banner.setVisible(False)
            return

        # 统计副本数量与占用空间。"共 N MB"只是信息展示，不是行动号召
        # （方案 §12.1：不把"可释放空间"做成 CTA，不提供"一键释放"）。
        dups = self._db.list_duplicate_files(self._current_kb)
        total_bytes = sum(f.size for f in dups)
        mb = total_bytes / 1024 / 1024
        size_text = f"{mb:.1f} MB" if mb >= 1 else f"{total_bytes / 1024:.0f} KB"

        self._dup_label.setText(
            f"另有 {len(dups)} 个文件与其他文件内容完全相同，已跳过未入库"
            f"（共 {size_text}）。文件本身仍在原位置。"
        )
        self._btn_dup.setToolTip(f"涉及 {len(groups)} 组重复内容")
        self._dup_banner.setVisible(True)

    def refresh_similar_banner(self) -> None:
        """刷新"相似组待确认"提示条（方案 §12.5 / B8）。

        ## 为什么读数据库而不是内存

        B8 的验收是"用户点稍后再说，关程序再打开，知识库页仍有待确认
        提示"。所以待办状态必须落在 `pending_dups` 表里，每次刷新时
        从库里读。用内存变量会在重启后丢失——那比不提示更糟，因为用户
        会以为确认过了（§12.10 的"产品底线"）。
        """
        if not self._current_kb:
            self._similar_banner.setVisible(False)
            return

        n = self._db.count_pending_groups(self._current_kb)
        if n <= 0:
            self._similar_banner.setVisible(False)
            return
        self._similar_label.setText(
            f"⚠ 有 {n} 组文件内容相似，需要确认入库版本。"
        )
        self._similar_banner.setVisible(True)

    # ------------------------------------------------------------------ 目录

    def _reload_roots(self) -> None:
        self._roots_list.clear()
        if not self._current_kb:
            return
        for r in self._db.list_roots(self._current_kb, enabled_only=False):
            item = QListWidgetItem(r.path)
            item.setData(Qt.UserRole, r.id)
            self._roots_list.addItem(item)

    def _on_add_root(self) -> None:
        if not self._current_kb:
            QMessageBox.information(self, "还没有选择知识库",
                                    "请先在左侧选择一个知识库。")
            return
        d = QFileDialog.getExistingDirectory(self, "选择要扫描的目录")
        if not d:
            return
        self._db.add_root(self._current_kb, d)
        self._reload_roots()

    def _on_remove_root(self) -> None:
        item = self._roots_list.currentItem()
        if item is None:
            return
        rid = item.data(Qt.UserRole)
        # 移除目录只改配置，不删任何文件、不删任何索引（方案 §7.7 的第四种情形）
        self._db.remove_root(rid)
        self._reload_roots()

    def _on_scan(self) -> None:
        if not self._current_kb:
            return
        roots = [Path(r.path) for r in self._db.list_roots(self._current_kb)]
        if not roots:
            QMessageBox.information(self, "没有扫描目录",
                                    "请先添加至少一个目录。")
            return
        self._scanning = True
        self._update_buttons()
        self._skip_list.clear()
        self._skip_detail.setText("")
        self.scan_requested.emit(self._current_kb, roots)

    # ------------------------------------------------------------------ 进度

    def on_progress(self, p: ScanProgress) -> None:
        label = _PHASE_LABEL.get(p.phase, p.phase.value)
        if p.phase is ScanPhase.WALKING:
            # 枚举阶段分母未知：显示"已发现 N 个"，进度条走不确定动画
            self._phase.setText(f"{label} · 已发现 {p.counts.get('discovered', p.done)} 个")
            self._bar.setRange(0, 0)
        elif p.phase is ScanPhase.VECTORIZING and p.total <= 0:
            # 向量化只报状态、没有可量化进度时（如"仍在后台运行"），
            # 不要用 0% 卡住进度条——那看起来像卡死了。
            self._phase.setText(f"{label} · {p.message or '进行中'}")
        else:
            self._phase.setText(label)
            self._bar.setRange(0, 100)
            self._bar.setValue(p.percent)
        self._current.setText(p.current or "")
        if p.message:
            self._current.setText(p.message)

    def on_finished(self, plan) -> None:
        self._scanning = False
        self._update_buttons()
        self._bar.setRange(0, 100)
        self._bar.setValue(100)
        # 扫描结束后刷新两条提示条（方案 §12.7）。
        # 放在这里而不是 on_progress 里：重复判定与相似检测都在扫描末尾
        # 才完成，扫描中反复刷新只会让界面抖动。
        self.refresh_duplicate_banner()
        self.refresh_similar_banner()

        # 跳过原因按类型分组
        counts: dict[str, int] = {}
        if plan.walk is not None:
            for _path, reason in plan.walk.skipped:
                counts[reason] = counts.get(reason, 0) + 1
        self._skip_list.clear()
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            msg = SKIP_MESSAGES.get(reason, reason)
            self._skip_list.addItem(f"{n} 个 · {msg}")

        # 解析失败的逐条列出（每条都带可读原因，方案 §5.6）
        failed = [i for i in plan.items if i.parse_error]
        for item in failed:
            self._skip_list.addItem(f"解析失败 · {item.path.name}：{item.parse_error}")

        parts = []
        c: dict[str, int] = {}
        for item in plan.items:
            c[item.action.value] = c.get(item.action.value, 0) + 1
        if c.get("new"):
            parts.append(f"新增 {c['new']}")
        if c.get("changed"):
            parts.append(f"变更 {c['changed']}")
        if c.get("retry"):
            parts.append(f"待重试 {c['retry']}")
        if c.get("unchanged"):
            parts.append(f"未变 {c['unchanged']}")
        # 完全重复要出现在汇总里（方案 §12.7）。用户扫完看到"新增 1"时
        # 必须能同时看到"内容重复跳过 4"，否则他会以为另外 4 个文件丢了。
        if c.get("duplicate"):
            parts.append(f"内容重复跳过 {c['duplicate']}")
        if plan.missing:
            parts.append(f"源文件已不在 {len(plan.missing)}")

        ok = [i for i in plan.items if i.chunk_count and not i.parse_error]
        ingested = [i for i in ok if i.ingested]
        if ingested:
            total_chunks = sum(i.chunk_count for i in ingested)
            parts.append(f"已入库 {len(ingested)} 篇共 {total_chunks} 块")
        elif ok:
            total_chunks = sum(i.chunk_count for i in ok)
            parts.append(f"已切出 {total_chunks} 块")

        # 入库失败要与解析失败分开数：两者的原因与处理完全不同
        ingest_failed = [i for i in ok if i.ingest_error]
        if ingest_failed:
            parts.append(f"{len(ingest_failed)} 篇入库失败")

        self._phase.setText("完成 · " + ("，".join(parts) if parts else "没有需要处理的文件"))

        notes = []
        if plan.missing:
            notes.append(
                f"有 {len(plan.missing)} 篇文档的源文件已不在原位置"
                "（可能被移动或删除）。库内内容仍可检索。"
            )
        for item in ok:
            if item.warnings:
                notes.append(f"{item.path.name}：{item.warnings[0]}")
                break
        self._skip_detail.setText("\n".join(notes))

        # 入库失败的逐条列出（与解析失败同样处理：每条都要能指向下一步）
        for item in ingest_failed:
            self._skip_list.addItem(f"入库失败 · {item.path.name}：{item.ingest_error}")

    def on_failed(self, message: str) -> None:
        self._scanning = False
        self._update_buttons()
        self._bar.setRange(0, 100)
        self._phase.setText("失败")
        first = message.strip().splitlines()[0] if message.strip() else "未知错误"
        self._current.setText(first)
        QMessageBox.warning(self, "扫描失败", first)

    def _update_buttons(self) -> None:
        has_kb = bool(self._current_kb)
        self._btn_add.setEnabled(has_kb and not self._scanning)
        self._btn_remove.setEnabled(has_kb and not self._scanning)
        self._btn_scan.setEnabled(has_kb and not self._scanning)
        self._btn_cancel.setEnabled(self._scanning)
        self._grp_roots.setEnabled(has_kb)
