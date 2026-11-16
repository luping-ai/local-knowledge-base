"""打开原文件与定位。三个平台差异极大，都收在这个文件里。

## 为什么分扩展名处理跳转

方案 §8.3 的要求是"必须诚实"——不能承诺一个做不到的跳转：

| 类型 | 方式 | 能跳页吗 |
| --- | --- | --- |
| PDF | `AcroRd32.exe /A "page=N"` | 有 Acrobat/福昕时能；否则不能 |
| DOCX/XLSX/PPTX | `os.startfile` | 不能（Office 不接受命令行页码/行号） |
| TXT/MD | 内置预览器 | 能（自己在文本里找） |

## PDF 的一个现实

Windows 上"默认 PDF 阅读器"可能是 Edge、Chrome、SumatraPDF、Acrobat…
只有 Acrobat 与福昕支持 `/A "page=N"`。不检测就无法承诺，所以这里
**先找 Acrobat，找不到就退化为普通打开**，并把页码复制到剪贴板作为
补偿（用户可以在阅读器里 Ctrl+G 粘贴）。

不做"注册表查默认阅读器再猜参数"：各家的命令行语法不同，猜错会静默
停在第一页——用户以为跳转失败，实际是我们给错了参数。

## TXT/MD 的定位方式

不用 `start_rune`（该字段没有透出，且与原文偏移不一致），改为
在文本里搜片段前 40 字。见方案 §8.3 的说明。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "open_file",
    "reveal_in_explorer",
    "copy_to_clipboard",
    "find_snippet_offset",
]

# Acrobat 的常见安装路径。按顺序找，第一个存在的就用。
_ACROBAT_CANDIDATES = (
    r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
    r"C:\Program Files (x86)\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
    r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
    r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
)


def _no_window_flags() -> int:
    """子进程不要弹控制台窗口。

    Windows 上 `subprocess` 拉起控制台程序会闪一个黑框。`os.startfile`
    与 ShellExecute 不经过控制台，不需要这个；但调 AcroRd32.exe 时要用。
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _find_acrobat() -> Optional[str]:
    for p in _ACROBAT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def open_file(path: str | Path, page: int = 0,
              snippet: str = "") -> tuple[bool, str]:
    """打开文件。返回 `(是否成功, 说明)`。

    ## 返回说明而不是抛异常

    调用方是 UI 的按钮回调。打不开文件时最合适的反馈是"复制路径 + 提示"，
    而不是让异常冒泡到 Qt 的事件循环（那会在控制台打一堆堆栈，用户看不到）。

    ## page > 0 时尽力跳页

    - PDF：有 Acrobat 就带 `/A "page=N"`；没有就普通打开 + 复制页码
    - TXT/MD：由调用方用内置预览器（本函数不处理）
    - 其他：忽略 page
    """
    p = Path(path)
    if not p.exists():
        return False, f"文件已不在：{p}"

    if page > 0 and p.suffix.lower() == ".pdf":
        acro = _find_acrobat()
        if acro:
            try:
                subprocess.Popen(
                    [acro, "/A", f"page={page}", str(p)],
                    creationflags=_no_window_flags(),
                )
                return True, f"已在 {p.name} 中打开第 {page} 页"
            except OSError:
                pass   # 落到普通打开
        # 没有 Acrobat：普通打开，并把页码放到剪贴板作为补偿
        _shell_open(p)
        copy_to_clipboard(str(page))
        return True, (
            f"已打开 {p.name}。本机没有支持页码参数的 PDF 阅读器，"
            f"页码 {page} 已复制到剪贴板（可在阅读器里用跳转功能粘贴）。"
        )

    try:
        _shell_open(p)
    except OSError as e:
        return False, f"无法打开文件：{e}"
    return True, f"已打开 {p.name}"


def _shell_open(p: Path) -> None:
    """用系统默认程序打开（Windows: os.startfile；其他: xdg-open/open）。"""
    if sys.platform == "win32":
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def reveal_in_explorer(path: str | Path) -> tuple[bool, str]:
    """在资源管理器里选中该文件（不打开它）。

    ## 为什么需要这个

    "打开原文"对未知格式（比如 .log、.xml）可能弹出用户不想要的程序。
    提供"在文件夹中显示"让用户自己决定用什么打开。
    """
    p = Path(path)
    if not p.exists():
        return False, f"文件已不在：{p}"
    if sys.platform != "win32":
        # 非 Windows 打开所在目录即可，不做"选中"（各桌面环境实现不同）
        _shell_open(p.parent)
        return True, f"已打开所在目录：{p.parent}"

    # explorer /select, 是唯一可靠的"选中"方式。
    # 注意：explorer 的退出码不可靠（成功也常返回 1），所以不检查 returncode。
    try:
        subprocess.Popen(["explorer", "/select,", str(p)])
    except OSError as e:
        return False, f"无法打开资源管理器：{e}"
    return True, "已在资源管理器中定位"


def copy_to_clipboard(text: str) -> bool:
    """复制到剪贴板。失败返回 False（不抛异常）。

    ## 为什么要兼容 Qt 不可用的情况

    这个模块被 worker 线程调用，而 Qt 的剪贴板只能在主线程用。所以优先
    用 Win32 API（线程安全），Qt 退化为兜底。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            # 打开剪贴板：可能被别的程序占用，重试几次
            user32 = ctypes.windll.user32
            for _ in range(5):
                if user32.OpenClipboard(None):
                    break
                import time

                time.sleep(0.05)
            else:
                return False
            try:
                user32.EmptyClipboard()
                CF_UNICODETEXT = 13
                GMEM_MOVEABLE = 0x0002
                data = text.encode("utf-16-le") + b"\x00\x00"
                kernel32 = ctypes.windll.kernel32
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h:
                    return False
                ptr = kernel32.GlobalLock(h)
                if not ptr:
                    kernel32.GlobalFree(h)
                    return False
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    kernel32.GlobalUnlock(h)
                if not user32.SetClipboardData(CF_UNICODETEXT, h):
                    kernel32.GlobalFree(h)
                    return False
                # SetClipboardData 成功后所有权归系统，不要 GlobalFree
                return True
            finally:
                user32.CloseClipboard()
        except Exception:  # noqa: BLE001 - 剪贴板失败不该影响主流程
            return False

    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return False
        app.clipboard().setText(text)
        return True
    except Exception:  # noqa: BLE001
        return False


def find_snippet_offset(file_text: str, snippet: str,
                        probe: int = 40) -> int:
    """在文本里找片段的位置，返回字符偏移（-1 表示没找到）。

    ## 为什么用"搜前 40 字"而不是 start_rune

    `start_rune` 是相对**解析后纯文本**的偏移，而用户打开的是**原文件**
    （带 Markdown 标记、YAML front matter、CRLF）。两者本来就不一致，
    即便服务端透出该字段也定位不准。

    搜片段反而更贴近用户看到的字符串。代价是片段被改写/折叠空白时可能
    找不到——那种情况返回 -1，UI 显示"已打开文件（未能定位）"。
    """
    if not snippet or not file_text:
        return -1
    head = " ".join(snippet.split())[:probe]
    if not head:
        return -1
    idx = file_text.find(head)
    if idx >= 0:
        return idx
    # 逐段退让：前 40 字找不到时试前 20、前 10
    for n in (20, 10):
        if len(head) > n:
            idx = file_text.find(head[:n])
            if idx >= 0:
                return idx
    return -1
