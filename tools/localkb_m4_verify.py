"""M4 验收脚本：A4（增量更新）、A5（删除）、A19（后台删库）、A20（重命名）。

## 为什么用脚本而不是 pytest

这四项验的是"端到端行为 + 响应时间"，判定包含"30 秒内"这类时间条件，
且需要人看输出确认。pytest 里写时间断言会因为 CI 负载而偶发失败。

脚本跑完会清理自己建的库，不污染现有数据。
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from pathlib import Path

from localkb.maintain import apply_rename, delete_local_document, reconcile
from localkb.runtime_conn import RuntimeConnector
from localkb.scan.job import ScanJob
from localkb.scan.walker import Walker
from localkb.store.db import StateDB
from localkb.watch.watcher import Watcher

DOC_V1 = """# 售后手册

## 保修条款

整机保修期为两年。
"""
DOC_V2 = """# 售后手册

## 保修条款

整机保修期为三年。电池保修六个月。
"""


def scan_once(conn, db, kb_id, docs: Path, *, parse=True) -> object:
    job = ScanJob(db, kb_id, [docs], conn=conn,
                  walker_factory=lambda **k: Walker(use_default_excludes=False, **k))
    plan = job.build_plan()
    if parse:
        plan = job.parse_all(plan)
        plan = job.ingest_all(plan)
    return plan


def count_docs(conn, kb_id: str) -> int:
    return conn.call(lambda c: c.kb_stat(kb_id)).document_count


def count_chunks(conn, kb_id: str) -> int:
    return conn.call(lambda c: c.kb_stat(kb_id)).chunk_count


def main() -> int:
    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ 本机 LocalAIHub 未运行")
        return 1

    tmp = Path(tempfile.mkdtemp())
    docs = tmp / "docs"
    docs.mkdir()
    db = StateDB(tmp / "state.db")

    kb = conn.call(lambda c: c.create_kb(f"localkb-m4-{uuid.uuid4().hex[:6]}",
                                         description="M4 验收"))
    print(f"验收库: {kb.id}\n")
    results: list[tuple[str, bool, str]] = []

    try:
        # -------- 基线：导入一版 --------
        f = docs / "售后手册.md"
        f.write_text(DOC_V1, encoding="utf-8")
        scan_once(conn, db, kb.id, docs)
        d0, c0 = count_docs(conn, kb.id), count_chunks(conn, kb.id)
        print(f"[基线] 文档={d0} 切片={c0}")

        # -------- A4: 改文件 → 30 秒内索引更新 --------
        print("\n=== A4 修改文件后增量更新 ===")
        t0 = time.monotonic()
        f.write_text(DOC_V2, encoding="utf-8")
        scan_once(conn, db, kb.id, docs)
        elapsed = time.monotonic() - t0
        d1, c1 = count_docs(conn, kb.id), count_chunks(conn, kb.id)
        ok = d1 == 1 and c1 >= c0 and elapsed < 30
        print(f"  耗时 {elapsed:.1f}s  文档={d1} 切片={c1}"
              f"（文档数应仍为 1，切片应 ≥ {c0}）")
        results.append(("A4 增量更新", ok, f"{elapsed:.1f}s, docs={d1}, chunks={c1}"))

        # 内容真的变了吗
        from localai import SearchRequest

        hits = conn.call(lambda c: c.search(SearchRequest(
            query="三年", kb_ids=[kb.id], top_k=3))).hits
        has_new = any("三年" in h.text for h in hits)
        print(f"  检索「三年」命中: {has_new}")
        results.append(("A4 内容已更新", has_new, f"hits={len(hits)}"))

        # -------- A20: 重命名 --------
        print("\n=== A20 重命名同步服务端名字 ===")
        new_path = docs / "售后手册-终稿.md"
        f.rename(new_path)
        ok_r, note = apply_rename(db, kb.id, conn,
                                  old_path=str(f), new_path=str(new_path))
        docs_remote = conn.call(lambda c: c.list_documents(kb.id))
        remote_name = docs_remote[0].name if docs_remote else ""
        ok = ok_r and remote_name == new_path.name
        print(f"  结果: {note}")
        print(f"  服务端名字: {remote_name!r}（应为 {new_path.name!r}）")
        results.append(("A20 重命名", ok, remote_name))

        # -------- A5: 删除文件 --------
        print("\n=== A5 删除文件（只移出库，文件保留）===")
        row = db.get_file(kb.id, str(new_path))
        ok_d, note = delete_local_document(
            db, kb.id, conn, path=str(new_path),
            doc_id=row.doc_id if row else "", remove_file=False)
        d2 = count_docs(conn, kb.id)
        ok = ok_d and d2 == 0 and new_path.exists()
        print(f"  {note}")
        print(f"  库内文档={d2}（应为 0）  文件仍在={new_path.exists()}（应为 True）")
        results.append(("A5 删除（保留文件）", ok, f"docs={d2}"))

        # -------- A4b: 文件删掉后保留库内容（missing）--------
        print("\n=== missing: 文件没了但库内容保留 ===")
        f2 = docs / "另一个.md"
        f2.write_text("# 另一个\n\n一些内容。", encoding="utf-8")
        scan_once(conn, db, kb.id, docs)
        d_before = count_docs(conn, kb.id)
        f2.unlink()
        rep = reconcile(db, kb.id, conn)
        d_after = count_docs(conn, kb.id)
        ok = d_before == d_after == 1 and len(rep.missing) == 1
        print(f"  对账: {rep.summary()}")
        print(f"  库内文档 {d_before} → {d_after}（应不变）")
        results.append(("missing 不删索引", ok, rep.summary()))

        # -------- A19: 后台删库后对账 --------
        print("\n=== A19 后台删库后的对账 ===")
        conn.call(lambda c: c.delete_kb(kb.id))
        rep2 = reconcile(db, kb.id, conn)
        # 库没了，list_documents 会 404 或返回空；本地行应被标 orphaned
        print(f"  对账: {rep2.summary()}")
        print(f"  失败项: {len(rep2.errors)}")
        # 判定：要么识别出 orphaned，要么记了错误（库不存在）
        ok = bool(rep2.orphaned or rep2.errors)
        results.append(("A19 删库后对账", ok,
                        f"orphaned={len(rep2.orphaned)} errors={len(rep2.errors)}"))

        # -------- 监听器端到端 --------
        print("\n=== 监听器（改文件后能收到事件）===")
        kb2 = conn.call(lambda c: c.create_kb(f"localkb-m4w-{uuid.uuid4().hex[:6]}"))
        try:
            w = Watcher(debounce_sec=0.3)
            ok_w, msg = w.start([docs])
            if not ok_w:
                print(f"  跳过：{msg}")
                results.append(("监听器", False, msg))
            else:
                target = docs / "监听测试.md"
                target.write_text("# 监听\n\n内容。", encoding="utf-8")
                got = []
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    got = w.pop_ready()
                    if got:
                        break
                    time.sleep(0.2)
                w.stop()
                ok = any("监听测试" in c.path.name for c in got)
                print(f"  收到事件: {[c.path.name for c in got]}")
                results.append(("监听器", ok, f"{len(got)} 个事件"))
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                conn.call(lambda c: c.delete_kb(kb2.id))

    finally:
        import contextlib

        with contextlib.suppress(Exception):
            conn.call(lambda c: c.delete_kb(kb.id))
        db.close()

    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}  —  {detail}")
    print(f"\n合计: {passed}/{len(results)}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
