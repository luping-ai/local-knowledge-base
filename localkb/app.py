"""程序入口。

## 启动顺序

    1. 单实例检查（用 state.db 的文件锁，不抢占 Runtime 的锁）
    2. 打开 state.db（必要时迁移）
    3. 建 RuntimeConnector（只构造，不连接——连接在 worker 里做）
    4. 建主窗口，进入事件循环

## 为什么单实例用 state.db 旁边的一个锁文件

不能用 `discovery.EnsureSingleInstance`：那是 Runtime 的锁，抢它会让
Runtime 以为已有实例在跑。也不能用"窗口标题找窗口"：那要求窗口已经
建好，而我们要在更早的阶段阻止第二个实例。

用 `%LOCALAPPDATA%\\LocalKB\\.lock` 上的 `msvcrt.locking`：Windows 上
文件锁随进程退出自动释放（包括崩溃），没有"残留锁"问题。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from localkb import __version__
from localkb.runtime_conn import RuntimeConnector
from localkb.store.db import open_state_db
from localkb.ui.main_window import MainWindow

__all__ = ["main"]


def _acquire_single_instance(lock_path: Path):
    """尝试拿单实例锁。成功返回文件句柄，失败返回 None。

    ## 为什么不用 pid 文件

    pid 文件需要判断"那个 pid 还活着吗"，而 pid 会被复用——判错的后果是
    "明明没开却提示已经开了"。文件锁由内核管理，进程一死就释放，没有这个问题。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 不用 with：这个句柄要活到进程退出（锁靠它持有）。用 with 会在
    # 函数返回时关闭文件、释放锁，单实例保护立刻失效。
    f = open(lock_path, "a+")  # noqa: SIM115
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def write_selfcheck_report(report: str, data_dir: Path | None = None) -> Path | None:
    """把自检报告写成 `selfcheck.txt`，返回路径；失败返回 None。

    ## 为什么用 utf-8-sig（带 BOM）而不是 utf-8

    这份文件的读者是"遇到问题来找我们的人"，他会用记事本打开。

    无 BOM 的 UTF-8 在 Windows 上会被一些工具按系统 ANSI 解码：

      · PowerShell 5.1 的 `Get-Content -Raw` → 中文全乱码
      · 旧版记事本 → 同样乱码

    于是用户看到 `[OK] Runtime ??` 这种反而更困惑的东西。而**乱码只在
    "另一个进程/另一个工具打开时"出现**，Python 自己读回来是对的——
    这正是它容易被忽略的原因：`tools/verify-installer.ps1` 第一版就在
    这里误判过 2 项（同一份文件、Python 断言通过、PowerShell 断言失败）。

    加 BOM 后记事本、PowerShell、VS Code 都能正确识别。
    """
    try:
        import os as _os

        if data_dir is None:
            base = _os.environ.get("LOCALAPPDATA")
            data_dir = (Path(base) / "LocalKB") if base else (Path.home() / ".localkb")
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "selfcheck.txt"
        path.write_text(report, encoding="utf-8-sig")
        return path
    except OSError:
        return None


def _self_check() -> int:
    """跑一遍关键路径并报告结果。返回 0 表示全部通过。

    ## 检查什么

    | 项 | 为什么 |
    | --- | --- |
    | 依赖可导入 | PySide6 / pymupdf / docx / openpyxl / pptx / localai |
    | 解析器可用 | 真造一个 PDF/DOCX/XLSX/PPTX 并解析 —— 只 `import` 不够，
    |            | PyMuPDF 缺 mupdf 后端时 import 成功但 open 失败；
    |            | `import pptx` 还要能带上 PIL（打包时曾被 excludes 裁掉） |
    | 切块器可用 | 走一遍 split |
    | state.db 可用 | 建表 + 读写 |
    | Runtime 连接 | 探活（失败不算错误，用户可能还没装） |

    ## 结果往哪儿输出（关键）

    打包后的 exe 是 **GUI 子系统，没有 stdout**——`print` 的内容会直接
    丢掉。所以：

      1. 始终写一份到 `<数据目录>\\selfcheck.txt`（这是唯一可靠的通道）
      2. 若有 stdout（开发期/重定向），同时打印
      3. 最后弹一个消息框显示结果（用户双击 `LocalKB.exe --self-check`
         也能看到，不需要开 cmd）

    第 3 条是为了"用户报问题"的场景：让他在快捷方式后面加一个参数、
    双击、然后截图发过来。比让他开 cmd 打命令现实得多。
    """
    import tempfile
    import traceback

    lines: list[str] = []
    ok = True

    def say(s: str = "") -> None:
        lines.append(s)
        # 有 stdout 就打印；没有（GUI 子系统时 sys.stdout 为 None）静默跳过
        import contextlib

        with contextlib.suppress(Exception):
            print(s)

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            say(f"  [OK  ] {name}" + (f"  {detail}" if detail else ""))
        except Exception as e:  # noqa: BLE001 - 自检要报告而不是中断
            ok = False
            say(f"  [FAIL] {name}  {type(e).__name__}: {e}")
            for ln in traceback.format_exc().splitlines()[-6:]:
                say(f"         {ln}")

    say("LocalKB 自检")
    say(f"  版本  {__version__}")
    say(f"  Python {sys.version.split()[0]}")
    say(f"  冻结  {getattr(sys, 'frozen', False)}")
    say(f"  可执行 {sys.executable}")
    say()

    tmp = Path(tempfile.mkdtemp(prefix="localkb-selfcheck-"))

    def _imports() -> str:
        import docx  # noqa: F401 - 只验证可导入
        import openpyxl
        import pymupdf  # noqa: F401 - 只验证可导入
        from PySide6 import QtCore

        return f"PySide6 {QtCore.__version__} / openpyxl {openpyxl.__version__}"

    def _pdf() -> str:
        import pymupdf

        from localkb.parse.registry import parse

        p = tmp / "t.pdf"
        doc = pymupdf.open()
        for i in range(2):
            pg = doc.new_page()
            pg.insert_text((72, 72), f"第{i + 1}页内容。", fontsize=12,
                           fontname="china-s")
        doc.save(str(p))
        doc.close()
        parsed = parse(p)
        pages = {b.page for b in parsed.blocks}
        if pages != {1, 2}:
            raise RuntimeError(f"页码不对：{pages}")
        return f"{len(parsed.blocks)} 块，页码 {sorted(pages)}"

    def _docx() -> str:
        from docx import Document

        from localkb.parse.registry import parse

        p = tmp / "t.docx"
        d = Document()
        d.add_heading("第一章", level=1)
        d.add_paragraph("正文内容。")
        d.save(str(p))
        parsed = parse(p)
        heads = [b for b in parsed.blocks if b.kind.value == "heading"]
        if not heads:
            raise RuntimeError("标题没被识别")
        return f"{len(parsed.blocks)} 块（含标题）"

    def _xlsx() -> str:
        import openpyxl

        from localkb.parse.registry import parse

        p = tmp / "t.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["列A", "列B"])
        ws.append(["值1", "值2"])
        wb.save(str(p))
        parsed = parse(p)
        return f"{len(parsed.blocks)} 块"

    def _pptx() -> str:
        """PPTX 自检（M6）。

        ## 为什么自检必须覆盖 PPTX，而不只是"依赖导入 OK"

        `import pptx` 会连带加载 **PIL**（`pptx.parts` 用它判断嵌入图片
        的格式）。而打包 spec 一度把 `PIL` 放进了 excludes——开发环境装着
        Pillow、单测全绿，打包后却会在**解析 PPTX 时**才
        `ModuleNotFoundError`。

        所以自检要真造一个 pptx 并解析，才能发现"依赖被裁掉了"这类问题。
        只 import 一下不够：`import pptx` 成功但缺 Pillow 的情况不会出现，
        反过来缺 lxml 时 import 就炸——两种都要覆盖，所以造文件最稳。
        """
        from pptx import Presentation

        from localkb.parse.registry import parse

        p = tmp / "t.pptx"
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "自检标题"
        slide.placeholders[1].text = "自检正文"
        prs.save(str(p))

        parsed = parse(p)
        heads = [b for b in parsed.blocks if b.kind.value == "heading"]
        if not heads:
            raise RuntimeError("PPT 标题没被识别")
        if not any(b.page == 1 for b in parsed.blocks):
            raise RuntimeError("PPT 页码不是 1")
        return f"{len(parsed.blocks)} 块（含标题，页码 {heads[0].page}）"

    def _chunk() -> str:
        from localkb.chunk.splitter import split
        from localkb.parse.types import Block, ParsedDoc

        doc = ParsedDoc(blocks=[Block(text="内容。" * 200, page=1)])
        chunks, info = split(doc)
        if not chunks:
            raise RuntimeError("切不出块")
        return f"{len(chunks)} 块，chunker {info.name}@{info.version}"

    def _db() -> str:
        from localkb.store.db import StateDB

        db = StateDB(tmp / "state.db")
        try:
            v = db.version
            db.set_setting("k", "v")
            if db.get_setting("k") != "v":
                raise RuntimeError("读写不一致")
            return f"schema v{v}"
        finally:
            db.close()

    def _runtime() -> str:
        from localkb.runtime_conn import RuntimeConnector

        conn = RuntimeConnector()
        if not conn.alive():
            # 不算失败：用户可能还没装 LocalAIHub
            return "未连接（Local AI Hub 未运行，不算错误）"
        cap = conn.call(lambda c: c.capability())
        return f"已连接，level={cap.level}"

    check("依赖导入", _imports)
    check("PDF 解析", _pdf)
    check("DOCX 解析", _docx)
    check("XLSX 解析", _xlsx)
    check("PPTX 解析", _pptx)
    check("切块器", _chunk)
    check("state.db", _db)
    check("Runtime 连接", _runtime)

    def _preflight() -> str:
        from localkb.preflight import check_install, format_check
        from localkb.runtime_conn import RuntimeConnector

        r = check_install(RuntimeConnector())
        if not r.ok:
            # 致命问题要让自检失败——"装的东西版本不对"是最常见的
            # "用户说不能用"的根因（方案 §13.4 记录过一次真实事故）。
            raise RuntimeError(format_check(r))
        return "版本检查通过"

    check("版本检查（SDK + 契约）", _preflight)

    say()
    say("全部通过" if ok else "有失败项（见上面的 FAIL）")

    report = "\n".join(lines)

    report_path = write_selfcheck_report(report)

    # 弹窗：让用户双击 `LocalKB.exe --self-check` 时也能看到结果
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        box = QMessageBox()
        box.setWindowTitle("LocalKB 自检")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText("全部通过" if ok else "有失败项")
        box.setDetailedText(report)
        if report_path is not None:
            box.setInformativeText(f"完整结果已保存到：\n{report_path}")
        box.exec()
        del app
    except Exception:  # noqa: BLE001 - 弹窗失败不影响退出码
        pass

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)

    # ---- 自检模式（`--self-check`）----
    #
    # ## 为什么需要它
    #
    # 冻结后的 exe 是个黑盒：解析器缺 DLL、PyMuPDF 少了模型文件、某个
    # 纯 Python 模块没被 PyInstaller 收进来——这些都只在**用户机器上**
    # 才暴露，而 GUI 崩溃时用户只能看到"闪一下就没了"。
    #
    # 这个模式不开窗口，直接在控制台跑一遍关键路径并打印结果。用途：
    #   1. 打包后的冒烟验证（package-localkb.ps1 会调）
    #   2. 用户报"用不了"时的第一诊断手段（让他在 cmd 里跑一次，把输出发过来）
    #
    # 用 `--self-check` 而不是 `--help`：GUI 程序没有控制台，"帮助"
    # 没有意义；而自检有明确的输出与退出码。
    if "--self-check" in argv:
        return _self_check()

    app = QApplication(argv)
    app.setApplicationName("LocalKB")
    app.setApplicationVersion(__version__)

    # 1. 单实例
    import os

    base = os.environ.get("LOCALAPPDATA")
    data_dir = Path(base) / "LocalKB" if base else Path.home() / ".localkb"
    lock = _acquire_single_instance(data_dir / ".lock")
    if lock is None:
        QMessageBox.information(
            None, "LocalKB 已在运行",
            "已经有一个 LocalKB 窗口打开了。\n"
            "请检查任务栏，或先在已打开的窗口里操作。",
        )
        return 0

    # 打开本地库：失败要明确说，不能静默进一个空界面。
    #
    # 这里刻意捕获所有异常：state.db 打不开的原因很多（磁盘满、文件被
    # 占用、schema 迁移脚本出错、磁盘只读）。每一种都不该让程序"静默
    # 进入一个空界面"——用户会以为知识库丢了。把堆栈原样给他，至少
    # 他能贴出来求助。
    try:
        db = open_state_db(data_dir)
    except Exception:  # noqa: BLE001
        QMessageBox.critical(
            None, "无法打开本地数据库",
            f"{data_dir}\n\n{traceback.format_exc()}",
        )
        return 1

    # 3. 连接器：只构造。真正的连接在窗口的 worker 里做（可能等 20 秒）
    conn = RuntimeConnector()

    # 4. 主窗口
    win = MainWindow(db, conn)
    win.show()

    try:
        return app.exec()
    finally:
        db.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
