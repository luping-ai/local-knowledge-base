"""GUI 冒烟测试：驱动真实的 MainWindow 跑一次启动 + 扫描。

## 为什么不用 pytest-qt

这个项目只需要"能起窗口、能连服务、能扫描一次"的验证。pytest-qt 会
引入一个只在测试里用的依赖，而它解决的问题（事件循环夹具、signal spy）
在这里用 `QApplication.processEvents()` + 轮询就能覆盖。

## 为什么必须有这个测试

M0 的三条验收（A15~A18）都是"能不能起来、能不能恢复"这类问题，
单元测试测不到。而人工点一遍又不可重复——每次改动都要手动验证一遍
"窗口能开吗"是团队会跳过的步骤。

## 前提

需要本机 LocalAIHub 正在运行（A17 的场景）。服务不可用时本测试会
**跳过**而不是失败——"服务没起"不是代码缺陷。
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from localkb.runtime_conn import RuntimeConnector
from localkb.store.db import StateDB
from localkb.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 QApplication。

    ## 为什么必须共享

    Qt 不允许在同一进程里创建两个 QApplication，而不销毁就直接再建会
    崩（"A QApplication instance already exists" 之后是段错误）。所以
    用 module 作用域，且不主动 quit。
    """
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def live_conn():
    """需要 Runtime 在线。不在线就跳过。"""
    conn = RuntimeConnector()
    if not conn.alive():
        pytest.skip("本机 LocalAIHub 未运行，跳过 GUI 联调测试")
    return conn


def _pump(app, seconds: float) -> None:
    """跑事件循环 N 秒（让 worker 的信号有机会被处理）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_main_window_boots_and_fills_status(qapp, live_conn, tmp_path) -> None:
    """窗口能起来，并在拿到 resources() 后把状态栏填上。

    覆盖 A17 的"能连上就进入可用状态"。
    """
    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        # 启动探测在 worker 线程里，等它回来
        _pump(qapp, 8)

        # 状态栏的三项都必须有内容（不能停在初始的"—"）
        assert "模型" in win._status._model.text()
        assert "资源" in win._status._resource.text()
        assert "知识库" in win._status._kb.text()
        assert "—" not in win._status._resource.text(), "资源状态没有更新"
        win.close()
    finally:
        db.close()


def test_kb_list_is_loaded(qapp, live_conn, tmp_path) -> None:
    """知识库列表要么有内容、要么给出可读原因（不能是空白）。

    这条对应方案 §8.5："知识库端点不存在"与"空列表"必须能区分。
    """
    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        _pump(qapp, 8)
        page = win._kb_page
        has_items = page._kb_list.count() > 0
        has_hint = bool(page._kb_hint.text().strip())
        assert has_items or has_hint, "既没有知识库列表也没有提示文案"
        win.close()
    finally:
        db.close()


def test_scan_runs_through_ui(qapp, live_conn, tmp_path) -> None:
    """点"开始扫描"后：进度上报 → 完成回调 → 界面显示汇总。

    覆盖 A1 的 M0 部分（枚举 + diff + 原因展示）。
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# a\n\n内容", encoding="utf-8")
    (docs / "b.txt").write_text("b", encoding="utf-8")

    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        _pump(qapp, 8)

        # 选一个有内容的库（避免用空库时"没有知识库"分支）
        kbs = live_conn.call(lambda c: c.list_kb())
        if not kbs:
            pytest.skip("服务端没有知识库，跳过扫描联调")
        kb_id = kbs[0].id

        db.add_root(kb_id, str(docs))
        win._kb_page.set_kbs(kbs)
        # 手动选中该库并刷新目录列表
        for i in range(win._kb_page._kb_list.count()):
            if win._kb_page._kb_list.item(i).data(0x0100) == kb_id:
                win._kb_page._kb_list.setCurrentRow(i)
                break
        _pump(qapp, 0.2)
        assert win._kb_page._roots_list.count() == 1

        # 走真实的按钮回调（不是直接调 worker）
        win._kb_page._on_scan()
        # 扫描 2 个文件很快，但要等线程结束 + 信号回来
        _pump(qapp, 10)

        phase_text = win._kb_page._phase.text()
        assert "完成" in phase_text, f"扫描未完成，当前状态：{phase_text}"
        assert "新增" in phase_text, f"应识别为新增文件：{phase_text}"
        win.close()
    finally:
        db.close()


def test_scan_cancel_button_state(qapp, live_conn, tmp_path) -> None:
    """空闲时"取消"必须禁用、"开始扫描"必须可用。

    这条看着琐碎，但它是"按钮状态机"的唯一自动化保护——之前手写 UI
    最容易出的 bug 就是"按钮永远灰着"或"点了两次跑两个线程"。
    """
    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        _pump(qapp, 8)
        page = win._kb_page
        if page._kb_list.count() == 0:
            pytest.skip("没有知识库可选中")
        assert page._btn_cancel.isEnabled() is False
        assert page._btn_scan.isEnabled() is True
        win.close()
    finally:
        db.close()


# ------------------------------------------------------------ 启动前提非阻塞

def test_startup_prereq_does_not_block(qapp, live_conn, tmp_path) -> None:
    """启动流程**不能**弹模态对话框。

    ## 这条测试为什么必须存在

    第一版 `_check_startup_prereq()` 是
    `while True: chk = evaluate_startup(conn); dlg.exec()`。
    在没有加载模型的环境里，`dlg.exec()` 永远不返回，于是：

      · 整个 `pytest tests` 卡了 11 分钟没有任何输出
      · CPU 时间停在 16 秒（它在等事件，不在算）
      · 真实使用中用户在窗口刚出现时看到"程序卡死"

    三个现象都指向同一行代码。而**手工点一遍是发现不了的**——如果那台
    机器的模型已加载，这段代码永远不会进入模态分支，一切看起来正常。

    所以这条测试断言的是"构造窗口能在有限时间内返回"，并且不管模型
    加载没加载都必须成立（不依赖 live 环境的模型状态）。
    """
    import time as _time

    db = StateDB(tmp_path / "state.db")
    try:
        t0 = _time.monotonic()
        win = MainWindow(db, live_conn)
        _pump(qapp, 3)
        elapsed = _time.monotonic() - t0
        # 3 秒的事件泵 + 构造，超过这个数就说明有地方在阻塞等用户输入。
        # 上限给得宽松（15 秒），因为慢机器上 resources() 可能要几秒。
        assert elapsed < 15, f"启动流程耗时 {elapsed:.1f}s，疑似有阻塞等待"
        win.close()
    finally:
        db.close()


def test_startup_banner_explains_missing_model(qapp, live_conn, tmp_path) -> None:
    """没有模型时，对话页黄条必须写出"原因 + 该去哪里解决"。

    只在模型确实未加载时验证（模型已加载时这条无意义，跳过）。
    """
    from localkb.ui.startup_check import describe_startup_problem, evaluate_startup

    chk = evaluate_startup(live_conn)
    if not chk.has_problem:
        pytest.skip("本机模型已加载且 Runtime 在线，无问题场景")

    text = describe_startup_problem(chk)
    assert text, "有问题却给不出提示文案"
    assert "重新检查" in text, "提示里必须告诉用户可以点「重新检查」"
    if not chk.runtime_ok:
        assert "Local AI Hub" in text
    else:
        assert "模型" in text
        assert "管理后台" in text, "缺模型时必须指向管理后台"

        # 窗口里黄条真的显示了
        db = StateDB(tmp_path / "state.db")
        try:
            win = MainWindow(db, live_conn)
            _pump(qapp, 6)
            # ## 为什么用 isHidden 而不是 isVisible / isVisibleTo
            #
            # `isVisible()` 要求整条父链都真实显示在屏幕上。测试里没有
            # `win.show()`（那会真的弹一个窗口出来干扰开发机），所以
            # 永远是 False。
            #
            # `isVisibleTo(win)` 实测也不可用：探针里 `_banner`（子控件）
            # 返回 True，而它的父控件 `_banner_box` 返回 False——同一个
            # 层级链上两种结果，说明它判断的是"显式隐藏标记的传播"而不是
            # "逻辑上该不该显示"。用它写断言会得到自相矛盾的结论。
            #
            # `isHidden()` 问的是"这个控件自己有没有被 setVisible(False)"，
            # 正是 `show_banner` 做的事，语义最贴近被测行为。
            assert not win._chat_page._banner_box.isHidden(), "黄条没有显示"
            assert win._chat_page._banner.text().strip(), "黄条没有文字"
            win.close()
        finally:
            db.close()


def test_no_problem_means_no_banner(qapp, live_conn, tmp_path) -> None:
    """没有**任何**问题时不该有黄条——常驻提示会变成噪音。

    ## 为什么要同时检查 preflight 警告（实测踩到的脆弱点）

    第一版只判断 `evaluate_startup().has_problem`。那次全量测试失败在这里：

      · `evaluate_startup` 只看"Runtime 在不在、模型有没有加载"
      · 而 `_run_preflight` 还会报**内存/RAG 警告**（如"可用内存不足，
        RAG 已禁用"）
      · 当时正好有一个 10 万块的向量化在后台跑，吃掉大量内存 →
        preflight 报了警告 → 黄条按设计显示

    测试于是误判为失败。**这不是产品缺陷，是测试对环境敏感。**
    判据必须覆盖两条黄条来源，否则换个内存状态就会假失败。
    """
    from localkb.preflight import check_install
    from localkb.ui.startup_check import evaluate_startup

    chk = evaluate_startup(live_conn)
    if chk.has_problem:
        pytest.skip("本机有启动前提问题，这条只验无问题场景")

    pf = check_install(live_conn)
    if pf.warnings or not pf.ok:
        pytest.skip(f"本机 preflight 有警告/失败（{pf.warnings or pf.fatal}），"
                    "这条只验完全干净的场景")

    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        _pump(qapp, 6)
        assert win._chat_page._banner_box.isHidden(), (
            f"无任何问题却显示了黄条：{win._chat_page._banner.text()!r}"
        )
        win.close()
    finally:
        db.close()


def test_status_bar_shows_loaded_model_not_capability(qapp, live_conn, tmp_path) -> None:
    """状态栏必须显示**实际加载**的模型，不能显示"能力上能跑"的。

    ## 这个缺陷是怎么发现的

    离屏渲染截图（`tools/shot-localkb.py`）里同一屏出现了两句话：

      · 黄条："本地服务已连接，但没有加载对话模型，暂时无法提问"
      · 状态栏："模型：qwen2.5-3b-instruct-q4"

    直接矛盾。原因是 `_on_bootstrap_ready` 用了 `cap.rag_model`
    ——它回答"这台机器能跑哪个"，不是"现在跑着哪个"。

    用户看到这种矛盾的第一个结论是"这软件在胡说"，然后不再相信任何提示。

    ## 断言方式

    在"未加载模型"的环境下（本测试的常见情形），状态栏必须自认未加载。
    如果模型已加载，则必须与 `active_model()` 一致。
    """
    from localkb.ui.startup_check import evaluate_startup

    chk = evaluate_startup(live_conn)
    db = StateDB(tmp_path / "state.db")
    try:
        win = MainWindow(db, live_conn)
        _pump(qapp, 8)
        text = win._status._model.text()
        assert "模型" in text

        if not chk.model_loaded:
            assert "未加载" in text, (
                f"服务端没加载模型，状态栏却显示 {text!r}——"
                "这里传了 cap.rag_model（能力）而不是 active_model（实况）"
            )
        else:
            loaded = live_conn.call(lambda c: c.active_model())
            assert loaded in text, f"状态栏 {text!r} 与 active_model {loaded!r} 不一致"
        win.close()
    finally:
        db.close()


def test_preflight_warnings_survive_prereq(qapp, live_conn, tmp_path) -> None:
    """preflight 的警告不能被启动前提检查清掉，两者要同时显示。

    ## 这条测试保护的是"两块代码写同一个控件"

    黄条只有一块，两个地方会写它：

      · `_run_preflight()` —— 版本警告（如"没有配置 Embedding，只能关键词检索"）
      · `_check_startup_prereq()` —— 缺 Runtime / 缺模型

    第一版 `_check_startup_prereq()` 在**没有问题**时也调 `show_banner("")`，
    把刚写进去的 preflight 警告清掉了。用户看到的现象是"启动时黄条闪一下
    就没了"——而那条警告正是"语义检索为什么不好用"的唯一解释。

    第二版改成"无问题时不碰黄条"，但两个问题**同时存在**时仍是后写的覆盖
    先写的——这条测试正是发现那个的。

    正确行为：两段都在，用空行分隔。它们回答不同的问题
    （"为什么语义检索不好用" vs "为什么问不了"），缺一条都会让用户误判。
    """
    from localkb.ui import main_window as mw

    real = mw.MainWindow._run_preflight
    injected = "测试注入：没有配置 Embedding，只能关键词检索"

    def fake_run_preflight(self) -> None:
        # 模拟 preflight 走到"有警告"分支，然后走真实的后继流程
        self._preflight_warnings = injected
        self._chat_page.show_banner(injected)
        self._check_startup_prereq()

    mw.MainWindow._run_preflight = fake_run_preflight
    db = StateDB(tmp_path / "state.db")
    try:
        win = mw.MainWindow(db, live_conn)
        _pump(qapp, 6)
        text = win._chat_page._banner.text()
        assert injected in text, f"警告被清掉了，当前黄条：{text!r}"
        win.close()
    finally:
        mw.MainWindow._run_preflight = real
        db.close()
