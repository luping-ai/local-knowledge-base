"""主窗口：装配页面、状态栏、后台任务。

## 职责

这个类只做三件事，其余都委托给页面与 worker：

  1. **装配**：建页面、连信号
  2. **启动流程**：探活 Runtime → 取能力与资源 → 填状态栏 → 拉知识库列表
  3. **协调**：把页面的请求转给 worker，把 worker 的结果转回页面

## 为什么启动流程要单独一个 `_bootstrap`

"连不上服务端"与"连上了但知识库端点没注册"是完全不同的两种状态，
后者仍然要进入主界面（只是禁用相关功能）。把这套判断塞进
`__init__` 会让人以为"构造失败 = 程序不能用"，而实际上大部分情况下
程序是可用的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from localkb import __version__
from localkb.chat import KB_MODE_ALL, KB_MODE_NONE, KB_MODE_SELECTED, ChatTurn, KbScope
from localkb.platform.open_file import copy_to_clipboard, open_file, reveal_in_explorer
from localkb.runtime_conn import RuntimeConnector
from localkb.store.db import StateDB
from localkb.ui.chat_page import ChatPage
from localkb.ui.kb_page import KbPage
from localkb.ui.source_card import SourceView
from localkb.ui.status_bar import StatusBar
from localkb.ui.workers import CallWorker, ChatWorker, ConnectionProbeWorker, ScanWorker

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, db: StateDB, conn: RuntimeConnector,
                 *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._db = db
        self._conn = conn
        # 持有 worker 的引用：QThread 被 GC 会连带销毁正在跑的线程
        # （表现为"程序启动后立刻崩"或"信号永远不来"）。
        self._workers: list = []
        self._scan_worker: Optional[ScanWorker] = None
        self._chat_worker: Optional[ChatWorker] = None
        # 知识库列表缓存（doc_id → path 的查询需要 kb_id）
        self._kb_ids_cache: list = []
        # preflight 报的警告文本（黄条是共享的，"重新检查"后要能恢复它）
        self._preflight_warnings: str = ""

        self.setWindowTitle(f"LocalKB {__version__}")
        self.resize(1100, 720)
        self._build()
        self._bootstrap()

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._kb_page = KbPage(self._db)
        self._kb_page.refresh_requested.connect(self._load_kbs)
        self._kb_page.scan_requested.connect(self._start_scan)
        self._kb_page.cancel_requested.connect(self._cancel_scan)
        self._kb_page.docs_refresh_requested.connect(self._load_documents)
        self._kb_page.delete_requested.connect(self._on_delete_document)
        self._kb_page.missing_manage_requested.connect(self._show_missing_dialog)
        self._kb_page.duplicates_requested.connect(self._show_duplicates_dialog)
        self._kb_page.similar_confirm_requested.connect(self._confirm_similar)
        self._kb_page.open_file_requested.connect(self._open_file)
        self._tabs.addTab(self._kb_page, "知识库")

        self._chat_page = ChatPage(self._db)
        self._chat_page.ask_requested.connect(self._on_ask)
        self._chat_page.cancel_requested.connect(self._cancel_chat)
        self._chat_page.new_conversation_requested.connect(self._new_conversation)
        self._chat_page.conversation_selected.connect(self._open_conversation)
        self._chat_page.open_file_requested.connect(self._open_file)
        self._chat_page.recheck_requested.connect(self._recheck_startup)
        self._tabs.addTab(self._chat_page, "对话")

        layout.addWidget(self._tabs)
        self.setCentralWidget(central)

        # 菜单栏：只有"设置"一项。
        #
        # ## 为什么用菜单而不是一个显眼的按钮
        #
        # 设置页里只有一条选项（相似组默认选中策略），它是**可选优化**
        # 而非必经流程。放成主界面按钮会引导用户去改它，而默认值
        # （不预选）本来就是推荐值。
        #
        # 菜单栏也让将来加"关于/日志/导出"有地方去，不需要重排界面。
        mb = self.menuBar()
        m_file = mb.addMenu("文件")
        act_settings = m_file.addAction("设置…")
        act_settings.triggered.connect(self._show_settings)
        m_file.addSeparator()
        act_quit = m_file.addAction("退出")
        act_quit.triggered.connect(self.close)

        m_help = mb.addMenu("帮助")
        act_about = m_help.addAction("关于 LocalKB")
        act_about.triggered.connect(self._show_about)

        self._status = StatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("正在连接本地服务…")

    # ------------------------------------------------------------------ 设置

    def _show_settings(self) -> None:
        from localkb.ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self._db, parent=self)
        dlg.exec()
        if dlg.saved:
            self._status.showMessage("设置已保存", 5000)

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "关于 LocalKB",
            f"<b>LocalKB {__version__}</b><br><br>"
            "个人本地知识库桌面端。<br>"
            "复用本机已安装的 Local AI Hub 提供推理与向量化能力，"
            "自带不带运行环境（同一台机器只能有一份 Runtime）。<br><br>"
            f"数据目录：{self._db.path.parent}",
        )

    # ---------------------------------------------------------------- 启动流程

    def _bootstrap(self) -> None:
        w = ConnectionProbeWorker(self._conn)
        w.ready.connect(self._on_bootstrap_ready)
        w.failed.connect(self._on_bootstrap_failed)
        self._track(w)
        w.start()

    def _on_bootstrap_ready(self, wake, st) -> None:
        if not wake.ok:
            # 服务连不上：这是"程序现在不能用"，必须明确告诉用户并给出动作。
            # 不退出程序——用户可能刚去装 LocalAIHub，回来点"重试"就行。
            self._status.set_kb(rag_enabled=False, kb_endpoints_ok=False)
            self._status.set_model("")
            self._status.set_resource(None)
            self._show_connection_problem(wake)
            return

        if st is not None:
            self._status.set_resource(st)
            cap = st.capability
            if cap is not None:
                # ## 为什么这里显示 active_model 而不是 cap.rag_model
                #
                # `cap.rag_model` 回答的是"**这台机器能跑哪个模型**"（能力评估），
                # 不是"现在跑着哪个"。实测踩到过：状态栏显示
                # "模型：qwen2.5-3b-instruct-q4"，而同一窗口的黄条写着
                # "没有加载对话模型，暂时无法提问"——两句话互相矛盾，
                # 用户第一反应是"这软件在胡说"。
                #
                # 状态栏是"当前实况"，必须显示实际加载的模型。
                # 能力评估（能跑什么）在黄条里以"本机能力评估"标注，两者不混。
                loaded = ""
                try:
                    loaded = self._conn.call(lambda c: c.active_model()) or ""
                except Exception:  # noqa: BLE001 - 读不到就显示"未加载"
                    loaded = ""
                self._status.set_model(loaded)
                self._status.set_capability_model(cap.rag_model or "")
                self._status.set_kb(
                    rag_enabled=cap.rag_enabled,
                    kb_endpoints_ok=True,
                    reason=cap.reason,
                )
        self._status.showMessage("已连接本地服务", 3000)

        # 版本检查（方案 §13.4）
        #
        # ## 为什么在连上之后、拉列表之前
        #
        # 版本不对时后面每一个请求都可能以奇怪的方式失败（缺字段、
        # 404）。先检查能给出"一句话原因 + 明确动作"，而不是让用户
        # 看着一堆功能逐个失效。
        self._run_preflight()

    def _run_preflight(self) -> None:
        """检查 SDK 与契约版本。致命问题时弹窗并进入只读模式。"""
        from localkb.preflight import check_install, format_check

        try:
            res = check_install(self._conn)
        except Exception:  # noqa: BLE001 - 检查本身失败不该阻止启动
            self._load_kbs()
            return

        if not res.ok:
            self._status.showMessage("版本不匹配，部分功能不可用", 10000)
            QMessageBox.critical(
                self, "版本不匹配",
                format_check(res) + "\n\n程序会以只读模式启动："
                "可以浏览已有知识库，但无法导入或提问。",
            )
        elif res.warnings:
            # 警告不打断：写进对话页的黄条（那是用户真正会看的地方）
            #
            # 同时缓存下来：黄条是共享的，"重新检查"成功后要能把警告放回来。
            self._preflight_warnings = "\n".join(res.warnings)
            self._chat_page.show_banner(self._preflight_warnings)

        # 启动前提（Runtime 在、模型已加载）。有模型时不打扰用户。
        #
        # ## 顺序要求：必须在警告之后就位（实测踩到的覆盖缺陷）
        #
        # 黄条只有一块，两个来源会写它：preflight 的警告、启动前提的问题。
        # 第一版 `_check_startup_prereq()` 在"没有问题"时调 `show_banner("")`
        # ——把刚刚写进去的 preflight 警告**清掉了**。
        #
        # 后果：`check_install` 报的警告（比如"没有配置 Embedding，只能关键词
        # 检索"）在启动后一闪就没了，用户永远看不到。而这条正是"语义检索
        # 为什么不好用"的唯一解释。
        #
        # 现在 `_check_startup_prereq()` 只在自己**有问题时**写黄条，无问题时
        # 不碰——见下。
        if res.ok:
            self._check_startup_prereq()

        self._load_kbs()

    def _check_startup_prereq(self) -> None:
        """检查"能不能提问"，有问题就在对话页显示黄条。

        ## 为什么不用模态对话框（实测踩到的缺陷）

        第一版是 `while True: chk = evaluate_startup(conn); dlg.exec()` ——
        **模态循环**。三个后果，前两个是真实的用户可感知问题：

        1. **窗口还没画出来就卡住**。这段代码被 `_bootstrap` 的 ready 回调
           同步调用，此时窗口刚 `show()`、事件循环还没跑完一轮。弹模态框会
           让窗口停在"半画出来"的状态，用户看到的是"程序启动就卡死"。

        2. **UI 线程上做同步 HTTP**。`evaluate_startup()` 内部有 3 次
           `conn.call(...)`（capability / active_model / discovery）。Runtime
           忙的时候每次几百毫秒到几秒，全程主线程冻结——连对话框都画不出来。

        3. **无法自动化测试**。全量测试卡了 11 分钟没有任何输出，CPU 时间
           停在 16 秒——`dlg.exec()` 永远不会自己返回，`processEvents()`
           里也没有"人来点按钮"。这一点本身不是产品问题，但它说明这段
           代码**没有任何自动保护**，改动引入回归不会被发现。

        现在：**非阻塞**。检查结果变成对话页的常驻黄条 + 状态栏提示，
        用户随时能继续整理目录、看已有库，想提问时黄条已经写清了原因和
        该去哪儿解决。`evaluate_startup` 仍在主线程（它已经在 bootstrap
        回调之后，且 Runtime 刚探活成功，通常很快），但不再有循环与
        模态等待——最坏情况是黄条晚几百毫秒出现，而不是窗口冻结。
        """
        from localkb.ui.startup_check import describe_startup_problem, evaluate_startup

        try:
            chk = evaluate_startup(self._conn)
        except Exception:  # noqa: BLE001 - 检查失败不该阻止启动
            return

        if not chk.has_problem:
            # ## 不碰黄条（重要）
            #
            # 这里**不能**调 `show_banner("")`。黄条是共享的：`_run_preflight`
            # 可能刚把"没有配置 Embedding"这类警告写进去，清掉它等于让用户
            # 永远看不到"语义检索为什么不好用"的唯一解释。
            #
            # 没问题就什么都不做——黄条的状态由最后一个"有问题"的写入者决定。
            return

        self._chat_page.show_banner(self._banner_text(describe_startup_problem(chk)))
        self._status.showMessage("本地服务未就绪，提问不可用（详见对话页提示）", 10000)
        # 把管理后台按钮挂到对话页黄条上，而不是拦住启动
        self._chat_page.show_banner_action("打开管理后台", chk.admin_url)

    def _banner_text(self, problem: str) -> str:
        """把"启动前提问题"与"preflight 警告"合成一段黄条文本。

        ## 为什么必须合并而不是后写的覆盖先写的

        两个来源会在启动时同时成立：

          · preflight：内存紧张 → RAG 被禁用、Embedding 没配（警告）
          · 启动前提：模型没加载（问题）

        这两条**不重叠**，各自回答不同的问题（"为什么语义检索不好用"
        与"为什么问不了"）。只显示后者会让用户以为"配好模型就全好了"，
        然后在提问时发现检索质量很差，却找不到任何解释。

        用空行分隔：`describe_startup_problem` 自己已经用了换行，直接
        拼接会挤成一团。
        """
        if not self._preflight_warnings:
            return problem
        if not problem:
            return self._preflight_warnings
        return f"{problem}\n\n{self._preflight_warnings}"

    def _recheck_startup(self) -> None:
        """用户点黄条上的"重新检查"：后台重查一次启动前提。

        ## 为什么走线程

        用户刚在管理后台点了"加载模型"，此时 Runtime 正在读 1 GB 的
        weights，响应会慢 1~3 秒。在主线程做这三次调用会让窗口在用户
        **刚点完按钮**的瞬间白掉——正好落在他最没耐心的时候。

        用 `CallWorker` 而不是新写一个 worker：它已经处理了异常分流
        （APIError 只给消息、意外异常带堆栈），这里不需要额外语义。
        """
        from localkb.ui.startup_check import evaluate_startup

        w = CallWorker(self._conn, lambda _c: evaluate_startup(self._conn))
        w.done.connect(self._on_recheck_done)
        w.failed.connect(lambda msg: self._status.showMessage(f"重新检查失败：{msg}", 8000))
        self._track(w)
        w.start()
        self._status.showMessage("正在重新检查本地服务…", 5000)

    def _on_recheck_done(self, chk) -> None:
        from localkb.ui.startup_check import describe_startup_problem

        if not chk.has_problem:
            # 问题解决了：先清掉"未就绪"提示，再把 preflight 警告放回来。
            #
            # ## 为什么需要恢复（否则警告会被永久丢掉）
            #
            # 黄条只有一块。用户点"重新检查"成功时，如果直接 `show_banner("")`，
            # 启动时 preflight 报的警告（如"没有配置 Embedding，只能关键词
            # 检索"）就再也回不来了——而用户刚解决了模型问题，正好开始关心
            # 检索质量。所以缓存一份，恢复它。
            self._chat_page.show_banner(self._preflight_warnings)
            self._chat_page.show_banner_action("", "")
            self._status.showMessage("本地服务已就绪，可以提问了", 8000)
            return

        self._chat_page.show_banner(self._banner_text(describe_startup_problem(chk)))
        self._chat_page.show_banner_action("打开管理后台", chk.admin_url)
        self._status.showMessage("仍然未就绪（详见对话页提示）", 8000)

    def _after_kbs_loaded(self) -> None:
        """知识库列表到位后：启动对账 + 文件监听。

        ## 为什么在这之后而不是在 bootstrap 里

        对账与监听都需要"当前是哪个库"。库的选中是由 `_on_kbs_loaded`
        决定的（恢复上次会话/选中第一个），所以必须等它完成。
        """
        kb_id = self._kb_page._current_kb
        if kb_id:
            self._run_reconcile(kb_id)
            self._start_watch(kb_id)

    def _start_watch(self, kb_id: str) -> None:
        """为当前知识库的扫描目录启动文件监听（方案 §7.2）。

        ## 为什么只监听"当前选中的库"

        同时监听所有库的目录会让事件量翻倍，且用户切库时仍会收到别的库
        的事件。切库时重启监听（`_on_kb_selected` 会触发）。

        ## 监听失败不是错误

        网络盘、权限、watchdog 缺失都可能失败。用户仍能手工点"开始扫描"，
        所以只在状态栏提示，不弹窗。
        """
        from localkb.watch.watcher import Watcher

        roots = [Path(r.path) for r in self._db.list_roots(kb_id)]
        if not roots:
            return

        # 先停旧的（切库时）
        old = getattr(self, "_watcher", None)
        if old is not None:
            old.stop()

        w = Watcher(debounce_sec=2.0)
        ok, msg = w.start(roots)
        if not ok:
            self._status.showMessage(f"文件监听未启动：{msg}", 6000)
            self._watcher = None
            return
        self._watcher = w

        # 定时 pull：每 2 秒看一次有没有"稳定下来的变更"
        if not hasattr(self, "_watch_timer") or self._watch_timer is None:
            self._watch_timer = QTimer(self)
            self._watch_timer.timeout.connect(self._poll_watch)
            self._watch_timer.start(2000)

    def _poll_watch(self) -> None:
        """消费已稳定的变更，触发一次增量扫描。

        ## 为什么"有变更就整库重扫"而不是逐文件精确处理

        逐文件处理需要把"哪个文件变了"传进 ScanJob，而 ScanJob 的 diff
        逻辑本来就是"遍历 + 比对 sha256"——它自然会跳过未变的文件。

        代价是每次变更都要遍历一遍目录（几百到几千个 stat，几十毫秒到
        几秒）。收益是不需要维护"变更 → 动作"的第二套逻辑，也就不会出现
        "监听路径的判断与扫描路径的判断不一致"。

        真到"几万文件 + 频繁变更"时再优化（方案 §10.5 的性能目标是
        "改一个文件 30 秒内更新"，遍历完全够用）。
        """
        w = getattr(self, "_watcher", None)
        if w is None:
            return
        ready = w.pop_ready()
        if not ready:
            return
        if self._worker_alive(self._scan_worker):
            # 正在扫描：变更留到下一轮（debouncer 会继续攒）
            return
        kb_id = self._kb_page._current_kb
        if not kb_id:
            return
        roots = [Path(r.path) for r in self._db.list_roots(kb_id)]
        if not roots:
            return
        names = "、".join(sorted({c.path.name for c in ready})[:3])
        more = f" 等 {len(ready)} 个文件" if len(ready) > 3 else ""
        self._status.showMessage(f"检测到变化：{names}{more}，正在更新…", 4000)
        self._start_scan(kb_id, roots)

    def _on_bootstrap_failed(self, tb: str) -> None:
        self._status.showMessage("连接本地服务失败", 6000)
        first = tb.strip().splitlines()[-1] if tb.strip() else "未知错误"
        QMessageBox.critical(
            self, "无法连接本地服务",
            f"{first}\n\n请确认 Local AI Hub 正在运行。",
        )

    def _show_connection_problem(self, wake) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("本地服务未就绪")
        box.setText(wake.message or "无法连接本地服务。")
        if wake.detail:
            box.setInformativeText(f"详情：{wake.detail}")
        retry = box.addButton("重试", QMessageBox.AcceptRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is retry:
            self._bootstrap()

    # ---------------------------------------------------------------- 知识库列表

    def _on_kbs_failed(self, msg: str) -> None:
        # 这里最常见的不是网络错误，而是"知识库未启用"。文案要指向动作，
        # 不要显示 APIError 的原文（用户看不懂 capability_unmet）。
        if "404" in msg or "not found" in msg.lower() or "resource_not_found" in msg:
            self._kb_page.set_kb_error(
                "本地服务没有启用知识库功能（通常是可用内存不足）。"
                "关闭部分程序后重启本地服务可恢复。"
            )
            self._status.set_kb(rag_enabled=False, kb_endpoints_ok=False)
        else:
            self._kb_page.set_kb_error(f"读取知识库失败：{msg}")

    # ------------------------------------------------------------------ 扫描

    def _start_scan(self, kb_id: str, roots: list) -> None:
        if self._worker_alive(self._scan_worker):
            return
        w = ScanWorker(self._db, kb_id, roots, conn=self._conn)
        w.progress.connect(self._kb_page.on_progress)
        w.finished_ok.connect(self._on_scan_done)
        w.failed.connect(self._on_scan_failed)
        self._scan_worker = w
        self._track(w)
        w.start()
        self._status.showMessage("正在扫描…")

    def _cancel_scan(self) -> None:
        w = self._scan_worker
        if self._worker_alive(w):
            w.cancel()
            self._status.showMessage("正在取消…", 2000)

    def _on_scan_done(self, plan) -> None:
        self._kb_page.on_finished(plan)
        self._status.showMessage("扫描完成", 3000)
        # 扫描后刷新文档列表（新增的文档要出现在列表里）
        kb_id = self._kb_page._current_kb
        if kb_id:
            self._load_documents(kb_id)
        self._load_kbs()

    def _on_scan_failed(self, tb: str) -> None:
        self._kb_page.on_failed(tb)
        self._status.showMessage("扫描失败", 5000)

    # -------------------------------------------------------------- 文档与删除

    def _load_documents(self, kb_id: str) -> None:
        """拉服务端文档列表 + 本地映射，填充文档表。"""
        w = CallWorker(self._conn, lambda c: c.list_documents(kb_id))
        w.done.connect(lambda docs: self._on_docs_loaded(kb_id, docs))
        w.failed.connect(self._on_docs_failed)
        self._track(w)
        w.start()

    def _on_docs_loaded(self, kb_id: str, docs: list) -> None:
        local = {r.doc_id: r for r in self._db.list_files(kb_id) if r.doc_id}
        self._kb_page.set_documents(docs, local)
        self._refresh_missing_banner(kb_id)
        # 切库时重启监听（只监听当前库的目录）
        if kb_id == self._kb_page._current_kb:
            self._start_watch(kb_id)

    def _refresh_missing_banner(self, kb_id: str) -> None:
        """按本地表状态刷新 missing 提示条。

        ## 为什么判断"磁盘上是否还在"而不是只看 status

        status 可能是 `imported` 但文件已经被删（用户没走本程序删）。
        真正的事实是"磁盘上有没有这个文件"，`Path.exists()` 才是最直接的
        判据——status 只是缓存。
        """
        missing = [
            r for r in self._db.list_files(kb_id)
            if r.doc_id and r.status not in ("deleted",)
            and not Path(r.path).exists()
        ]
        if missing:
            self._kb_page.show_missing(len(missing), Path(missing[0].path).name)
        else:
            self._kb_page.show_missing(0)

    def _run_reconcile(self, kb_id: str) -> None:
        """启动时做一次四向对账（方案 §7.2.2）。

        ## 为什么放在后台线程

        `list_documents` 是网络请求；几千篇文档时返回体可能几 MB。
        主线程做会让启动卡一下，而这段逻辑完全是"修补性质"的，
        晚一两秒完成没有影响。
        """
        from localkb.maintain import reconcile

        def _do():
            return reconcile(self._db, kb_id, self._conn)

        w = CallWorker(self._conn, lambda _c: _do())
        w.done.connect(lambda rep: self._on_reconcile_done(kb_id, rep))
        w.failed.connect(lambda tb: self._status.showMessage(
            "启动对账失败（不影响使用）", 4000))
        self._track(w)
        w.start()

    def _on_reconcile_done(self, kb_id: str, rep) -> None:
        if not rep.clean:
            self._status.showMessage(f"启动对账：{rep.summary()}", 8000)
        self._refresh_missing_banner(kb_id)
        if kb_id == self._kb_page._current_kb:
            self._load_documents(kb_id)

    def _on_docs_failed(self, msg: str) -> None:
        self._kb_page.set_documents([], {})
        self._kb_page._docs_hint.setText(f"读取文档列表失败：{msg}")

    def _on_delete_document(self, kb_id: str, path: str, doc_id: str,
                            remove_file: bool) -> None:
        """执行删除。**先删库记录，再动文件**（方案 §7.7）。"""
        from localkb.maintain import delete_local_document

        def _do():
            return delete_local_document(
                self._db, kb_id, self._conn,
                path=path, doc_id=doc_id, remove_file=remove_file,
            )

        w = CallWorker(self._conn, lambda _c: _do())
        w.done.connect(self._on_deleted)
        w.failed.connect(lambda tb: self._status.showMessage(
            f"删除失败：{tb.strip().splitlines()[-1]}", 6000))
        self._track(w)
        w.start()

    def _on_deleted(self, result) -> None:
        ok, note = result
        self._status.showMessage(note, 8000)
        if not ok:
            QMessageBox.warning(self, "删除未完成", note)
        kb_id = self._kb_page._current_kb
        if kb_id:
            self._load_documents(kb_id)
        self._load_kbs()

    def _confirm_similar(self, kb_id: str) -> None:
        """逐组确认相似文档的入库版本（方案 §12.5 / B7 / B10）。

        ## 为什么逐组弹而不是一个大列表

        用户要做的判断是"这一组里哪个版本对"。把 3 组塞进一个对话框，
        他得在 9 个单选按钮里来回跳，而每组的选择互相独立。逐组确认
        每次只需看一段内容。

        组数通常是个位数（§12.5 的例子是 6 组），逐组不会烦人；
        而如果真有几十组，那说明这个库的组织方式需要调整，
        逐组反而能让用户意识到这一点。

        ## 为什么"稍后再说"立即停止后续组

        用户点"稍后"表达的通常是"现在不想处理这些"。继续弹下一组
        等于不尊重这个选择。剩余组留在 `pending_dups` 里，提示条
        仍然在——他随时可以回来。
        """
        from localkb.dedup import (
            format_diff,
            load_pending_groups,
            resolve_pending_group,
            similar_default_choice,
        )
        from localkb.ui.similar_dialog import SimilarDialog

        if not kb_id:
            return
        groups = load_pending_groups(self._db, kb_id)
        if not groups:
            self._kb_page.refresh_similar_banner()
            QMessageBox.information(self, "确认入库版本",
                                    "这个知识库没有待确认的相似文件。")
            return

        strategy = similar_default_choice(self._db)
        handled = 0
        for g in groups:
            dlg = SimilarDialog(
                g,
                default_choice=strategy,
                diff_loader=lambda a, b: format_diff(self._conn, kb_id, a, b),
                parent=self,
            )
            if dlg.exec() != SimilarDialog.Accepted:
                break   # 用户选了"稍后再说"：剩余组保留
            chosen_doc = dlg.chosen_doc_id
            chosen_path = dlg.chosen_path
            if not chosen_doc and not chosen_path:
                continue

            self._apply_similar_choice(kb_id, g, chosen_doc, chosen_path)
            resolve_pending_group(self._db, kb_id, g.group_id, chosen_path)
            handled += 1

        self._kb_page.refresh_similar_banner()
        if handled:
            self._status.showMessage(f"已确认 {handled} 组入库版本", 8000)
            self._load_documents(kb_id)
            self._load_kbs()

    def _apply_similar_choice(self, kb_id: str, group, chosen_doc: str,
                              chosen_path: str) -> None:
        """执行一组的确认：其余成员不入库；若已入库的版本被换下则切换。

        ## 三种情况

        1. **选中的已经入库了**（`imported`）：什么都不用做，只需把
           其余成员的本地状态标成"未入库"。
        2. **选中的没入库，另一个成员入了库**：换版本（先导后删）。
        3. **都没入库**（相似组里全是待导入的）：导入选中的那份。

        ## 为什么"其余成员标 similar_replaced"而不是删记录

        它们的 sha256 要保留——下次扫描才能认出"文件没变"，不会重复
        解析。这也是"换回来"能工作的前提。
        """
        from localkb.maintain import switch_kb_version
        from localkb.store.db import LocalFile

        # 找出组内已入库的成员（最多一个：Runtime 同库内按内容去重，
        # 但相似组的成员内容不同，理论上可能各入库一份——那是"两份都
        # 在库里"，也属于要收敛的情况）
        imported = []
        for m in group.members:
            if m.doc_id and self._db.get_file_by_doc_id(kb_id, m.doc_id):
                row = self._db.get_file_by_doc_id(kb_id, m.doc_id)
                if row is not None and row.status == "imported":
                    imported.append(m)

        chosen_is_imported = any(m.doc_id == chosen_doc for m in imported)

        if not chosen_is_imported:
            # 需要把选中的那份变成库内版本
            old = next((m for m in imported if m.doc_id != chosen_doc), None)
            if old is not None and old.path and chosen_path:
                ok, note = switch_kb_version(
                    self._db, kb_id, self._conn,
                    old_doc_id=old.doc_id, old_path=old.path,
                    new_path=chosen_path,
                )
                self._status.showMessage(note, 10000)
                if not ok:
                    QMessageBox.warning(self, "换版本未完成", note)
                    return
            # 若没有已入库的旧版，说明这一组都还没入库 —— 交给下一次
            # 扫描正常处理（用户在对话框里选中的那份会被优先导入，
            # 其余标为未入库）。这里只做标记，不重复走一遍导入。

        # 其余成员标记为"未入库"（不删记录，保留 sha256）
        for m in group.members:
            if m.doc_id == chosen_doc:
                continue
            row = self._db.get_file(kb_id, m.path) if m.path else None
            if row is None:
                continue
            if row.status in ("imported", "pending"):
                self._db.upsert_file(LocalFile(
                    id=row.id, kb_id=kb_id, path=row.path, doc_id=None,
                    sha256=row.sha256, size=row.size, mtime=row.mtime,
                    status="similar_replaced",
                    error=f"已确认入库版本为 {chosen_path or chosen_doc}",
                    name_sent=row.name_sent, imported_at=row.imported_at,
                ))

    def _show_duplicates_dialog(self, kb_id: str) -> None:
        """显示"内容完全相同的文件"详情（方案 §12.1 / §12.7）。

        ## 只读，没有任何处置动作

        §12.1 明确：不为重复组提供删除/归档/移动。所以这个对话框里
        唯一的按钮是"在文件夹中显示"——让用户自己去资源管理器处理。
        """
        from localkb.ui.duplicates_dialog import DuplicatesDialog, group_duplicates

        if not kb_id:
            return
        rows = self._db.list_duplicate_files(kb_id)
        if not rows:
            self._kb_page.refresh_duplicate_banner()
            QMessageBox.information(self, "内容相同的文件",
                                    "这个知识库里没有内容完全相同的副本。")
            return

        masters = self._db.duplicate_masters(kb_id)
        groups = group_duplicates(rows, masters=masters)
        kb_name = ""
        for _i in range(self._kb_page._kb_list.count()):
            item = self._kb_page._kb_list.item(_i)
            if item.data(0x0100) == kb_id:
                kb_name = item.text()
                break

        dlg = DuplicatesDialog(groups, parent=self, kb_name=kb_name)
        dlg.exec()

    def _show_missing_dialog(self) -> None:
        """missing 处理：逐条给三个动作（保留 / 移除 / 重绑）。

        ## 为什么先给汇总而不是逐条弹窗

        用户移动一次目录会有几十上百个 missing。逐条弹会让这个功能
        不可用（点 100 次"保留"）。所以先汇总，再让用户选择"全部保留"
        或"逐个处理"。
        """
        kb_id = self._kb_page._current_kb
        if not kb_id:
            return
        rows = [r for r in self._db.list_files(kb_id)
                if r.status == "missing"]
        if not rows:
            self._kb_page.show_missing(0)
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("源文件已不在")
        box.setText(f"有 {len(rows)} 篇文档的源文件已不在原位置。")
        box.setInformativeText(
            "可能原因：目录被移动、文件被改名或删除。\n\n"
            "库内内容仍可检索——「保留」表示继续用现有索引；"
            "「从库中移除」会删掉这些文档的索引。\n\n"
            "如果整个目录被移动了，建议先把新位置加为扫描目录并扫描一次，"
            "本程序会按内容哈希自动匹配上，不需要手工处理。"
        )
        keep = box.addButton("保留库内内容", QMessageBox.AcceptRole)
        remove = box.addButton("从库中移除", QMessageBox.DestructiveRole)
        box.addButton("稍后处理", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is keep:
            # 保留：把状态改回 imported，只是不再显示提示
            for r in rows:
                self._db.set_status(kb_id, r.path, "imported",
                                    "源文件已不在（保留库内内容）")
            self._kb_page.show_missing(0)
            self._status.showMessage("已保留库内内容", 4000)
        elif box.clickedButton() is remove:
            self._status.showMessage("正在移除…")
            for r in rows:
                self._on_delete_document(kb_id, r.path, r.doc_id or "", False)

    # ------------------------------------------------------------------ 对话

    def _load_kbs(self) -> None:
        """拉知识库列表。404 说明端点未注册（低内存），要区别于"空列表"。"""
        w = CallWorker(self._conn, lambda c: c.list_kb())
        w.done.connect(self._on_kbs_loaded)
        w.failed.connect(self._on_kbs_failed)
        self._track(w)
        w.start()

    def _on_kbs_loaded(self, kbs) -> None:
        self._kb_page.set_kbs(kbs)
        # 对话页也要这份列表（知识库开关需要它）
        self._chat_page.set_kbs(kbs)
        # 缓存给 _path_of_doc 用（避免每次来源卡片都全表扫）
        self._kb_ids_cache = list(kbs)
        if not self._chat_page._conv_id:
            self._resume_or_new_conversation()
        # 启动对账 + 文件监听（要等库选中之后）
        self._after_kbs_loaded()

    def _resume_or_new_conversation(self) -> None:
        """启动时回到最近一次会话；没有会话才新建。

        ## 为什么不是"每次启动都新建"

        早期实现是 `_new_conversation()`，后果是用户每开一次程序就多一个
        空会话。用几次之后列表里全是"（未命名会话）"，真正有内容的会话
        被挤到下面——而这个列表是用户找回历史问答的唯一入口。

        ## 为什么要跳过空会话

        上次可能是"点开程序但没提问就关了"，留下一个空会话。恢复它没有
        价值（用户看不到任何内容），反而要清一次流。所以只看有消息的会话。
        """
        for conv in self._db.list_conversations(limit=20):
            if self._db.list_messages(conv.id, limit=1):
                self._open_conversation(conv.id)
                return
        self._new_conversation()

    def _new_conversation(self) -> None:
        """建一个新会话并切过去。

        ## 为什么标题留空

        标题由第一次提问的内容生成（截断），这比"新对话 2026-11-16"
        更有辨识度。空标题时列表显示"（未命名会话）"。
        """
        import uuid

        conv_id = f"conv_{uuid.uuid4().hex[:16]}"
        self._db.create_conversation(conv_id)
        self._chat_page.clear_stream()
        self._chat_page.set_conversation(conv_id)
        self._chat_page.set_scope(KbScope(KB_MODE_NONE))
        self._refresh_conversations()

    def _refresh_conversations(self) -> None:
        self._chat_page.set_conversations(self._db.list_conversations())

    def _open_conversation(self, conv_id: str) -> None:
        """加载历史会话：消息流、库范围都要还原。"""
        import json

        conv = self._db.get_conversation(conv_id)
        if conv is None:
            return
        self._chat_page.clear_stream()
        self._chat_page.set_conversation(conv_id)

        mode = conv.kb_mode or KB_MODE_NONE
        try:
            kb_ids = json.loads(conv.kb_ids or "[]")
        except ValueError:
            kb_ids = []
        self._chat_page.set_scope(KbScope(mode=mode, kb_ids=list(kb_ids)))

        for row in self._db.list_messages(conv_id):
            if row.role == "user":
                self._chat_page.add_user_message(row.content)
                continue
            # assistant：先建空气泡再填内容与来源
            self._chat_page.begin_assistant_message()
            self._chat_page.append_delta(row.content)
            try:
                snap = json.loads(row.sources or "[]")
            except ValueError:
                snap = []
            try:
                warns = json.loads(row.warnings or "[]")
            except ValueError:
                warns = []
            views = [
                SourceView(
                    index=int(s.get("index", 0)),
                    doc_id=str(s.get("doc_id", "")),
                    name=str(s.get("name", "")),
                    page_start=int(s.get("page_start", 0) or 0),
                    page_end=int(s.get("page_end", 0) or 0),
                    heading_path=str(s.get("heading_path", "")),
                    snippet=str(s.get("snippet", "")),
                    path=self._path_of_doc(str(s.get("doc_id", ""))),
                )
                for s in snap
            ]
            self._chat_page.finish_assistant_message(
                sources=views, warnings=list(warns),
                used_chunks=row.used_chunks, all_bases=row.all_bases,
                base_count=row.base_count, degraded=row.degraded,
                model=row.model,
            )

    def _path_of_doc(self, doc_id: str) -> str:
        """doc_id → 本地路径（查 `local_files`，不经 API）。

        ## 为什么可能返回空

        文档可能是别的客户端导入的（管理后台、CLI），本机没有它的路径。
        这时来源卡片显示"文件已不在"是**诚实**的——我们确实不知道它在哪。
        """
        if not doc_id:
            return ""
        for kb_id in [k.id for k in getattr(self, "_kb_ids_cache", [])]:
            row = self._db.get_file_by_doc_id(kb_id, doc_id)
            if row is not None:
                return row.path
        # 缓存没命中时全表扫（local_files 通常几千行，可接受）
        return self._db.find_path_by_doc_id(doc_id) or ""

    def _on_ask(self, question: str, scope_mode: str, kb_ids: list) -> None:
        """发起提问。"""
        from localkb.chat import check_chat_ready

        # 先确认"现在能不能提问"。不做这一步的话，用户会看到一个空回答
        # 加一行原始错误（model_unavailable: 没有已加载的模型）。
        reason = check_chat_ready(self._conn)
        if reason:
            self._chat_page.show_banner(reason)
            # 空气泡要改成"提示"状态，否则会一直显示"正在准备模型…"
            self._chat_page.show_error(reason)
            return

        scope = KbScope(mode=scope_mode, kb_ids=list(kb_ids))
        self._chat_page.show_banner("")

        # 历史从库里取（最近若干轮），让服务端按 history_tokens 裁剪
        history: list[tuple[str, str]] = []
        if self._chat_page._conv_id:
            rows = self._db.list_messages(self._chat_page._conv_id, limit=40)
            history = [(r.role, r.content) for r in rows if r.content]

            # 首条消息时用问题当标题
            conv = self._db.get_conversation(self._chat_page._conv_id)
            if conv is not None and not conv.title:
                self._db.update_conversation_scope(
                    conv.id, scope.mode,
                    __import__("json").dumps(scope.kb_ids, ensure_ascii=False),
                )
                self._set_conversation_title(conv.id, question)

        turn = ChatTurn(question=question, kb_ids=list(kb_ids))
        turn.kb_mode = scope.mode
        w = ChatWorker(self._conn, question=question, history=history,
                       scope=scope, turn=turn)
        w.event.connect(self._on_chat_event)
        w.finished_turn.connect(self._on_turn_finished)
        w.failed.connect(self._on_chat_failed)
        self._chat_worker = w
        self._track(w)
        w.start()

    def _set_conversation_title(self, conv_id: str, question: str) -> None:
        title = question.strip().splitlines()[0][:28] if question.strip() else ""
        if not title:
            return
        with self._db.tx() as c:
            c.execute("update conversations set title=? where id=?",
                      (title, conv_id))
        self._refresh_conversations()

    def _on_chat_event(self, ev) -> None:
        page = self._chat_page
        if ev.kind == "queued":
            if ev.ahead > 0:
                page.set_status(f"正在排队，前面还有 {ev.ahead} 个任务…")
            else:
                page.set_status("正在准备模型…")
        elif ev.kind == "meta":
            page.set_status("正在生成…")
            if ev.degraded:
                self._status.set_model(ev.model, degraded=True)
            # 记住本次实际用的库范围，落库时要用（turn 里也要有）
            if ev.knowledge is not None:
                self._last_kb_mode = (
                    KB_MODE_ALL if ev.knowledge.all_bases else
                    (KB_MODE_SELECTED if ev.knowledge.kb_ids else KB_MODE_NONE)
                )
        elif ev.kind == "delta":
            page.append_delta(ev.text)
        elif ev.kind == "warning":
            # 警告不打断生成，累积展示由 finish 统一处理；这里只记日志级提示
            if ev.warnings:
                page.set_status("；".join(ev.warnings[:1]))
        elif ev.kind == "error":
            page.show_error(ev.error)

    def _on_turn_finished(self, turn: ChatTurn) -> None:
        page = self._chat_page
        if turn.error == "已停止":
            page.set_status("已停止生成")
            page.set_generating(False)

        # 渲染来源卡片
        views = [
            SourceView(
                index=getattr(s, "index", 0),
                doc_id=getattr(s, "document_id", ""),
                name=getattr(s, "source", "") or "（未知文件）",
                page_start=getattr(s, "page_start", 0),
                page_end=getattr(s, "page_end", 0),
                heading_path=getattr(s, "heading_path", ""),
                snippet=turn.snippets.get(getattr(s, "document_id", ""), ""),
                path=self._path_of_doc(getattr(s, "document_id", "")),
            )
            for s in turn.sources
        ]
        page.finish_assistant_message(
            sources=views,
            warnings=turn.warnings,
            finish_reason=turn.finish_reason,
            used_chunks=turn.used_chunks,
            all_bases=turn.all_bases,
            base_count=turn.base_count,
            degraded=turn.degraded,
            model=turn.model,
        )

        # 落库：用户消息 + 助手消息
        conv_id = page._conv_id
        if conv_id:
            import json

            self._db.add_message(conv_id, "user", turn.question)
            self._db.add_message(
                conv_id, "assistant",
                turn.answer or "（未生成内容）",
                sources=turn.source_snapshot or "[]",
                kb_mode=turn.kb_mode or KB_MODE_NONE,
                kb_ids=json.dumps(turn.kb_ids, ensure_ascii=False),
                all_bases=turn.all_bases, base_count=turn.base_count,
                model=turn.model, degraded=turn.degraded,
                used_chunks=turn.used_chunks,
                warnings=json.dumps(turn.warnings, ensure_ascii=False),
            )
            self._refresh_conversations()

    def _on_chat_failed(self, tb: str) -> None:
        first = tb.strip().splitlines()[-1] if tb.strip() else "未知错误"
        self._chat_page.show_error(first)
        self._status.showMessage("对话失败", 5000)

    def _cancel_chat(self) -> None:
        w = getattr(self, "_chat_worker", None)
        if self._worker_alive(w):
            w.cancel()
            self._chat_page.set_status("正在停止…")

    def _open_file(self, path: str, page: int) -> None:
        ok, note = open_file(path, page)
        self._status.showMessage(note, 6000)
        if not ok:
            # 打不开时提供两个可行动作，而不是只报错
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("无法打开文件")
            box.setText(note)
            copy_btn = box.addButton("复制路径", QMessageBox.AcceptRole)
            reveal = box.addButton("在文件夹中显示", QMessageBox.ActionRole)
            box.addButton("关闭", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is copy_btn:
                copy_to_clipboard(path)
            elif box.clickedButton() is reveal:
                reveal_in_explorer(path)

    # ------------------------------------------------------------------ 收尾

    def _track(self, worker) -> None:
        """持有引用并在结束时清理。

        ## 为什么必须持有引用

        `QThread` 对象被 Python GC 回收时，若线程还在跑，Qt 会直接
        终止进程（"QThread: Destroyed while thread is still running"）。
        局部变量在函数返回后就会被回收——这是最典型的崩溃原因。

        ## 为什么清理时要清掉 self._scan_worker

        `deleteLater()` 销毁的是 C++ 对象，Python 包装器仍在。之后任何
        对它的方法调用（如 `closeEvent` 里的 `isRunning()`）都会抛
        `RuntimeError: Internal C++ object already deleted`。

        实测症状：跑完一次扫描后关窗口，程序崩溃在 closeEvent 里。
        所以清理时必须把持有点置空，而不只是从列表里移除。
        """
        self._workers.append(worker)

        def _cleanup() -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            # 关键：先解除持有点，再 deleteLater。顺序反过来会让下一行
            # 读到已删除的对象。
            if self._scan_worker is worker:
                self._scan_worker = None
            worker.deleteLater()

        worker.finished.connect(_cleanup)

    def _worker_alive(self, worker) -> bool:
        """worker 是否还在跑。

        ## 为什么要包成函数

        `deleteLater()` 之后 Python 侧的对象还在，调 `isRunning()` 会抛
        `RuntimeError`。这个函数把"可能已销毁"的判断收在一处，避免每个
        调用点各写一遍 try/except。
        """
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def closeEvent(self, event) -> None:
        """关窗时取消正在跑的扫描并等它退出。

        ## 为什么不直接退出

        `ScanWorker` 正在写 state.db。进程被直接杀掉时 SQLite 的 WAL
        会让数据一致（不会损坏），但"当前正在导入的那个文件"会停在
        importing 状态，下次启动会显示一个永远不动的进度。等它退出
        能把它正确标成 pending。

        ## 文件监听也必须停

        `watchdog` 的 Observer 是非守护线程。不停的话进程会挂在那里
        不退出——用户看到"窗口关了但任务管理器里还有 LocalKB"。
        """
        w = self._scan_worker
        if self._worker_alive(w):
            w.cancel()
            w.wait(3000)

        watcher = getattr(self, "_watcher", None)
        if watcher is not None:
            watcher.stop()
            self._watcher = None

        timer = getattr(self, "_watch_timer", None)
        if timer is not None:
            timer.stop()

        super().closeEvent(event)
