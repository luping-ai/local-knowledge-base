"""M3 验收脚本：A2（有据可查 + 页码）与 A3（拒答）。

## 为什么单独一个脚本而不是 pytest

这两项需要"干净的事实型语料"，而 pytest 里的临时库构造起来啰嗦
（要写文件、扫描、入库、向量化）。这个脚本一次跑完并打印人类可读的
结果，便于人工确认答案质量——A2/A3 的判定本质上是"答案对不对"，
不适合用字符串包含硬断言（模型表述会变）。
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

from localkb.chat import (
    KB_MODE_SELECTED,
    KbScope,
    build_chat_request,
    stream_turn,
)
from localkb.runtime_conn import RuntimeConnector
from localkb.scan.job import ScanJob
from localkb.scan.walker import Walker
from localkb.store.db import StateDB

DOC_AFTERSALES = """# 售后服务手册

## 第三章 保修条款

### 3.2 保修期限

整机保修期为两年，自购买之日起计算。电池等易损件保修期为六个月。

### 3.3 保修范围

保修范围包括硬件故障与制造缺陷。人为损坏不在保修范围内。
"""

DOC_COMPANY = """# 我们公司

本公司成立于 2015 年，总部位于杭州，目前有员工 320 人。

主营业务是企业级存储设备。
"""

A2_CASES = [
    ("整机保修期多久？", "两年"),
    ("电池保修多久？", "六个月"),
    ("公司有多少员工？", "320"),
]

A3_CASES = [
    "这家公司的年营业额是多少？",
    "CEO 叫什么名字？",
    "保修期能延长到五年吗？",
]

REFUSAL_MARKERS = ("没有", "未提及", "未提供", "无法", "不包含", "资料中", "未见")


def ask(conn, kb_id: str, q: str, max_tokens: int = 120) -> tuple[str, object]:
    req = build_chat_request(
        question=q, history=[],
        scope=KbScope(KB_MODE_SELECTED, [kb_id]),
        max_tokens=max_tokens,
    )
    ans = ""
    meta = None
    for ev in stream_turn(conn, req):
        if ev.kind == "meta":
            meta = ev
        elif ev.kind == "delta":
            ans += ev.text
    return ans.strip(), meta


def main() -> int:
    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ 本机 LocalAIHub 未运行")
        return 1

    name = f"localkb-verify-{uuid.uuid4().hex[:6]}"
    kb = conn.call(lambda c: c.create_kb(name, description="M3 验收"))
    print(f"验收库: {kb.id}")

    tmp = Path(tempfile.mkdtemp())
    docs = tmp / "docs"
    docs.mkdir()
    (docs / "售后手册.md").write_text(DOC_AFTERSALES, encoding="utf-8")
    (docs / "公司简介.md").write_text(DOC_COMPANY, encoding="utf-8")

    db = StateDB(tmp / "state.db")
    events = []
    job = ScanJob(db, kb.id, [docs], on_progress=events.append, conn=conn,
                  walker_factory=lambda **k: Walker(use_default_excludes=False, **k))
    plan = job.build_plan()
    plan = job.parse_all(plan)
    plan = job.ingest_all(plan)
    vec_ok, vec_note = job.vectorize(timeout_sec=120)
    print(f"入库: {events[-1].message}")
    print(f"向量化: {vec_ok} {vec_note}")

    passed = 0
    total = 0

    print("\n=== A2 有据可查（答案 + 页码 + 章节）===")
    for q, expect in A2_CASES:
        total += 1
        ans, meta = ask(conn, kb.id, q)
        hit = expect in ans
        passed += 1 if hit else 0
        mark = "OK  " if hit else "FAIL"
        print(f"  [{mark}] {q}")
        print(f"         答案: {ans[:80]}")
        srcs = meta.knowledge.sources if (meta and meta.knowledge) else []
        if srcs:
            s = srcs[0]
            print(f"         来源: {s.source}  第 {s.page_start} 页  "
                  f"{s.heading_path[:50]}")
        else:
            print("         来源: （无）")

    print("\n=== A3 拒答（资料里没有的必须说没有）===")
    for q in A3_CASES:
        total += 1
        ans, _ = ask(conn, kb.id, q)
        refused = any(m in ans for m in REFUSAL_MARKERS)
        passed += 1 if refused else 0
        mark = "OK  " if refused else "FAIL"
        print(f"  [{mark}] {q}")
        print(f"         答案: {ans[:80]}")

    print(f"\n合计: {passed}/{total}")

    # 清理
    import contextlib

    with contextlib.suppress(Exception):
        conn.call(lambda c: c.delete_kb(kb.id))
    db.close()
    print(f"已清理验收库 {kb.id}")
    return 0 if passed == total else 2


if __name__ == "__main__":
    sys.exit(main())
