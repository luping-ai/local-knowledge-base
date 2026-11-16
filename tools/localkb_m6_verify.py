"""M6 端到端验证：PPTX 从解析到检索全流程。

## 为什么需要这个脚本而不只是单测

单测证明"解析器能读 pptx"，但产品链路是：
  parse → split（页码/标题路径）→ ingest_chunks → 向量化 → search

其中任何一环对 `page` 语义的处理不一致，都会让"来源卡片跳错页"。
而这只有在真实入库后检索才能发现（`SearchHit` 里的页码来自数据库，
不是解析器的内存对象）。

## 用法

    python tools/localkb_m6_verify.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localkb.chunk.splitter import split
from localkb.ingest import build_request, ingest
from localkb.parse.registry import parse
from localkb.runtime_conn import RuntimeConnector

__all__ = ["main"]

SLIDES = [
    {"title": "2026 年度产品规划", "bullets": ["三条产品线", "重点：知识库与检索"]},
    {"title": "市场分析", "bullets": ["竞品 A 占 35%", "竞品 B 占 28%", "我们占 12%"]},
    {"title": "财务目标", "bullets": ["营收 1200 万", "毛利率 62%"],
     "table": [["季度", "营收"], ["Q1", "250 万"], ["Q2", "310 万"]]},
    {"title": "团队与排期", "bullets": ["新增 3 人", "Q3 完成核心功能"]},
]


def build_pptx(path: Path) -> Path:
    import pptx
    from pptx.util import Inches

    prs = pptx.Presentation()
    layout = prs.slide_layouts[1]
    for spec in SLIDES:
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = spec["title"]
        body = slide.placeholders[1]
        body.text = spec["bullets"][0]
        for extra in spec["bullets"][1:]:
            body.text_frame.add_paragraph().text = extra
        if spec.get("table"):
            rows = spec["table"]
            shape = slide.shapes.add_table(
                len(rows), len(rows[0]), Inches(1), Inches(4.5),
                Inches(4), Inches(1))
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    shape.table.cell(r, c).text = val
    prs.save(str(path))
    return path


def main() -> int:
    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ LocalAIHub 未运行")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="localkb-m6-"))
    ppt = build_pptx(tmp / "年度规划.pptx")
    ok_count = 0
    total = 0
    fb = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok_count, total
        total += 1
        if cond:
            ok_count += 1
            print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
        else:
            fb.append(name)
            print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))

    print(f"[1/4] 解析 {ppt.name}")
    doc = parse(ppt)
    check("解析成功", bool(doc.blocks), f"{len(doc.blocks)} 块")
    pages = sorted({b.page for b in doc.blocks})
    check("页码覆盖 4 页", pages == [1, 2, 3, 4], f"实际 {pages}")
    heads = [b for b in doc.blocks if b.kind.value == "heading"]
    check("标题识别为 HEADING", len(heads) == 4, f"{len(heads)} 个")
    check("标题文本正确", heads[0].text == "2026 年度产品规划", heads[0].text)
    alltext = "\n".join(b.text for b in doc.blocks)
    check("表格内容在里面", "250 万" in alltext and "季度" in alltext)

    print("\n[2/4] 切块")
    chunks, info = split(doc)
    check("切出块", bool(chunks), f"{len(chunks)} 块")
    cp = sorted({c.page_start for c in chunks})
    check("块页码覆盖 4 页", cp == [1, 2, 3, 4], f"实际 {cp}")
    check("块带标题路径",
          all(("规划" in c.text or "分析" in c.text or "目标" in c.text
               or "团队" in c.text) for c in chunks),
          "每块都应含所属标题")

    print("\n[3/4] 入库")
    kb = conn.call(lambda c: c.create_kb(
        f"m6-verify-{uuid.uuid4().hex[:6]}", description="M6 端到端验证"))
    kb_id = kb.id
    try:
        req = build_request(
            path=ppt, name=ppt.name,
            file_hash=f"m6-{kb_id}",
            chunks=chunks, chunker=info,
            file_size=ppt.stat().st_size,
        )
        _ok, fail = ingest(conn, kb_id, req)
        check("入库成功", fail is None,
              fail.reason if fail else f"{len(chunks)} 块")

        st = conn.call(lambda c: c.kb_stat(kb_id))
        check("库内块数一致", st.chunk_count == len(chunks),
              f"{st.chunk_count} vs {len(chunks)}")

        print("\n[4/4] 向量化与检索")
        conn.call(lambda c: c.vectorize(kb_id, is_async=True))
        for _ in range(180):
            time.sleep(1.0)
            st = conn.call(lambda c: c.kb_stat(kb_id))
            if st.vectorize is None or not st.vectorize.running:
                break
        check("向量化完成", st.embedded_count == st.chunk_count,
              f"{st.embedded_count}/{st.chunk_count}")

        from localai import SearchRequest

        hits = conn.call(lambda c: c.search(SearchRequest(
            query="营收目标是多少", kb_ids=[kb_id], top_k=5)))
        check("检索有结果", bool(hits.hits), f"{len(hits.hits)} 条")
        if hits.hits:
            pages_hit = {h.page_start for h in hits.hits if h.page_start}
            check("命中带页码", bool(pages_hit), f"页码 {sorted(pages_hit)}")

            # ## 为什么断言 top-1 而不是"包含第 3 页"
            #
            # 第一版写 `3 in pages_hit`。但样本太小（4 块），top_k=5 会返回
            # 全部 4 页 → 断言恒真，等于没验。
            #
            # 真正要验的是**排序**：问"营收目标"时，第 3 页（财务目标，
            # 含"营收 1200 万"）必须排第一。这才是检索质量。
            top1 = hits.hits[0]
            check("top-1 是第 3 页（财务目标）", top1.page_start == 3,
                  f"实际第 {top1.page_start} 页，分数 {top1.score:.3f}")
            check("top-1 内容含营收",
                  "营收" in (top1.text or ""),
                  (top1.text or "")[:40].replace("\n", " "))
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            conn.call(lambda c: c.delete_kb(kb_id))
        print(f"\n已清理 {kb_id}")

    print()
    if not fb:
        print(f"全部通过（{total} 项）")
        return 0
    print(f"失败 {len(fb)}/{total}：{fb}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
