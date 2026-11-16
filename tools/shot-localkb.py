"""离屏渲染主窗口并截图：验证黄条实际长什么样。

## 为什么不用系统截屏

系统截屏要求窗口真的显示在屏幕上并处于前台。实测遇到过：
`MainWindowHandle` 拿到句柄后调用 `SetForegroundWindow` 成功，
几秒后再查句柄变成 0（窗口被最小化/失去主窗口身份），
`GetWindowRect` 返回 0x0，截出来的图是空的。

而 `QWidget.grab()` 是 Qt 自己渲染到 QPixmap——不依赖窗口是否可见、
是否在前台、是否被别的窗口遮挡。验证"布局对不对、文字有没有截断、
按钮在不在"这类问题足够了。

## 用法

    python tools/shot-localkb.py            # 默认截主窗口（对话页）
    python tools/shot-localkb.py --tab 0    # 截知识库页
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from localkb.runtime_conn import RuntimeConnector
from localkb.store.db import StateDB
from localkb.ui.main_window import MainWindow

__all__ = ["main"]


def main() -> int:
    ap = argparse.ArgumentParser(description="离屏渲染 LocalKB 主窗口")
    ap.add_argument("--tab", type=int, default=1, help="0=知识库 1=对话（默认 1）")
    ap.add_argument("--out", default="")
    ap.add_argument("--seconds", type=float, default=6.0, help="先跑多久事件循环")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication([])
    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ LocalAIHub 未运行——截出来的会是「连不上」的界面")
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="localkb-shot-"))
    db = StateDB(tmp / "state.db")
    win = MainWindow(db, conn)
    # 必须先 show：否则 QWidget.grab() 对未显示的控件拿到的是空白
    win.show()
    try:
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

        win._tabs.setCurrentIndex(args.tab)
        for _ in range(60):
            app.processEvents()
            time.sleep(0.01)

        out = pathlib.Path(args.out) if args.out else (
            pathlib.Path("output") / f"localkb-shot-tab{args.tab}.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        ok = win.grab().save(str(out), "PNG")
        print(f"{'已保存' if ok else '保存失败'} {out}")
        print(f"  黄条文本：{win._chat_page._banner.text()[:80]!r}")
        print(f"  黄条隐藏：{win._chat_page._banner_box.isHidden()}")
        return 0 if ok else 1
    finally:
        win.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
