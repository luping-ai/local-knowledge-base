"""离屏渲染 M7 的两个对话框与提示条，用于肉眼检查布局。

## 为什么要专门截图

提示条与对话框是"文案即产品表现"的地方：文字被截断、按钮挤在一起、
"请选择要入库的版本"这种灰字没显示出来——这些都只有在渲染后才能发现，
而单元测试断言的是逻辑（`isHidden()`、`chosen_doc_id`），看不到排版。

`QWidget.grab()` 不依赖窗口是否在前台，比系统截屏可靠。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from localkb.dedup import SimilarGroup, SimilarMember
from localkb.store.db import LocalFile, StateDB
from localkb.ui.duplicates_dialog import (
    DuplicatesDialog,
    group_duplicates,
)
from localkb.ui.settings_dialog import SettingsDialog
from localkb.ui.similar_dialog import SimilarDialog

OUT = pathlib.Path("output")


def pump(app, seconds: float = 0.6) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="m7shot-"))
    db = StateDB(tmp / "state.db")

    # 造：1 组 5 份完全重复 + 1 组 3 份相似
    for i in range(5):
        db.upsert_file(LocalFile(
            kb_id="kb1", path=f"D:/工作/项目报告/副本{i + 1}/项目报告_v2.docx",
            sha256="dup_hash", size=4_200_000,
            status="imported" if i == 0 else "duplicate",
            doc_id="doc_master" if i == 0 else None,
        ))
    db.upsert_pending_group(
        kb_id="kb1", group_id="g1", kind="similar",
        paths='[{"doc_id":"d1","name":"报告_v2","path":"D:/工作/项目报告_v2.docx",'
              '"mtime":1740000000,"hamming":0},'
              '{"doc_id":"d2","name":"报告_final","path":"D:/工作/项目报告_final.docx",'
              '"mtime":1741000000,"hamming":2},'
              '{"doc_id":"d3","name":"报告","path":"D:/备份/项目报告.docx",'
              '"mtime":1738000000,"hamming":3}]',
    )

    OUT.mkdir(exist_ok=True)

    # ---- 1. 完全重复详情 ----
    rows = db.list_duplicate_files("kb1")
    masters = db.duplicate_masters("kb1")
    groups = group_duplicates(rows, masters=masters)
    dlg = DuplicatesDialog(groups, kb_name="我的文档")
    dlg.show()
    pump(app)
    ok1 = dlg.grab().save(str(OUT / "m7-duplicates-dialog.png"), "PNG")
    dlg.close()
    print(f"{'已保存' if ok1 else '失败'} output/m7-duplicates-dialog.png")

    # ---- 2. 相似确认（默认不预选） ----
    g = SimilarGroup(
        group_id="g1", kind="similar",
        members=[
            SimilarMember(doc_id="d1", name="报告_v2",
                          path="D:/工作/项目报告_v2.docx", mtime=1740000000),
            SimilarMember(doc_id="d2", name="报告_final",
                          path="D:/工作/项目报告_final.docx", mtime=1741000000,
                          hamming=2),
            SimilarMember(doc_id="d3", name="报告",
                          path="D:/备份/项目报告.docx", mtime=1738000000,
                          hamming=3),
        ],
        similarity_min=95, similarity_max=100,
    )

    def fake_diff(_a: str, _b: str) -> str:
        return ("· 新增 1 段（例如：本合同自 2025 年 3 月起生效…）\n"
                "· 修改 2 段（第 5 段改为「服务费人民币 12000 元」…）\n"
                "· 未变 14 段")

    dlg2 = SimilarDialog(g, default_choice="none", diff_loader=fake_diff,
                         kb_name="我的文档")
    dlg2.show()
    pump(app)
    ok2 = dlg2.grab().save(str(OUT / "m7-similar-none.png"), "PNG")
    print(f"{'已保存' if ok2 else '失败'} output/m7-similar-none.png")
    print(f"  确定按钮可用：{dlg2._btn_ok.isEnabled()}"
          f"（应为 False），提示：{dlg2._hint.text()!r}")
    dlg2.close()

    # ---- 3. 相似确认（newest 预选） ----
    dlg3 = SimilarDialog(g, default_choice="newest", diff_loader=fake_diff)
    dlg3.show()
    pump(app)
    ok3 = dlg3.grab().save(str(OUT / "m7-similar-newest.png"), "PNG")
    print(f"{'已保存' if ok3 else '失败'} output/m7-similar-newest.png")
    print(f"  预选 doc_id：{dlg3.chosen_doc_id}（应为 d2 = 最新）")
    print(f"  确定按钮可用：{dlg3._btn_ok.isEnabled()}（应为 True）")
    dlg3.close()

    # ---- 4. 设置 ----
    dlg4 = SettingsDialog(db)
    dlg4.show()
    pump(app)
    ok4 = dlg4.grab().save(str(OUT / "m7-settings.png"), "PNG")
    dlg4.close()
    print(f"{'已保存' if ok4 else '失败'} output/m7-settings.png")

    db.close()
    return 0 if all([ok1, ok2, ok3, ok4]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
