"""内存标定：测"内容规模 → 内存占用"的对应关系。

## 这个脚本回答什么

不是"8GB 够不够"，而是**每一类负载吃多少内存**：

  - 桌面端空转 / 扫描 / 解析 / 入库各阶段各占多少
  - 一篇 N 页 PDF 解析时桌面端峰值多少
  - 库里有 N 个 chunk 时 Runtime 与桌面端各占多少
  - 一次提问（检索 + 生成）时两端各占多少

有这些数据之后才能做"输入规模控制"：比如"单文件超过 200 页就提示拆分"、
"单库超过 8 万块就提示拆库"——判据来自实测而不是估算。

## 为什么在本机（16GB）测就能用

内存占用分两类：

  **绝对量**（与这台机器无关，可直接用于任何机器）
    · 桌面端各阶段 RSS
    · 一篇 PDF 的解析峰值
    · 每个 chunk 的向量与索引开销
    · 模型的权重驻留

  **相对量**（受机器影响）
    · 模型能否装下（取决于当前可用内存）
    · KV cache 随 ctx 增长的部分

本脚本只测**绝对量**。那些数字在任何机器上都成立，所以 16GB 机器上
测出的"10 万 chunk 占 147 MB 向量"在 8GB 机器上也是 147 MB。

## 怎么测

`runtime` 组的指标来自 `/api/v1/resources`（Runtime 自己报的）；
`desktop` 组的指标来自 psutil（本进程 RSS）。

## 用法

    # 全量（会往一个临时库里导入数据，慢，约 10 分钟）
    python tools/bench_memory.py --full

    # 快速（只测桌面端解析与空转，约 1 分钟）
    python tools/bench_memory.py

    # 指定样本规模
    python tools/bench_memory.py --chunks 50000

## 安全

所有数据写进**临时知识库**，测完删除。不会碰现有的库。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# 让脚本能 import localkb（在仓库根跑时）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localkb.chunk.splitter import split
from localkb.parse.registry import parse
from localkb.runtime_conn import RuntimeConnector

__all__ = ["main"]


def rss_mb(proc: psutil.Process | None = None) -> float:
    """进程 RSS（MB）。默认取本进程。"""
    p = proc or psutil.Process()
    try:
        return p.memory_info().rss / 1024 / 1024
    except psutil.Error:
        return 0.0


def find_runtime_processes() -> list[psutil.Process]:
    """找到 Runtime 与引擎进程。

    ## 为什么要按名字找而不是按 PID

    Runtime 会重启、引擎会换 PID。按名字 + 可执行文件路径匹配更稳。
    """
    out: list[psutil.Process] = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name in ("localai-cli.exe", "llama-server.exe"):
                out.append(p)
        except psutil.Error:
            continue
    return out


def runtime_rss_mb() -> float:
    """Runtime 全家（localai-cli + 所有 llama-server）的 RSS 之和。"""
    total = 0.0
    for p in find_runtime_processes():
        try:
            total += p.memory_info().rss / 1024 / 1024
        except psutil.Error:
            continue
    return total


def system_available_mb() -> float:
    return psutil.virtual_memory().available / 1024 / 1024


@dataclass
class Sample:
    """一个测量点。"""

    label: str
    desktop_mb: float = 0.0
    runtime_mb: float = 0.0
    available_mb: float = 0.0
    elapsed_s: float = 0.0
    note: str = ""

    def to_row(self) -> str:
        return (
            f"| {self.label} | {self.desktop_mb:.0f} | {self.runtime_mb:.0f} "
            f"| {self.available_mb:.0f} | {self.elapsed_s:.2f} | {self.note} |"
        )


@dataclass
class Report:
    samples: list[Sample] = field(default_factory=list)
    # 关键派生量
    derived: dict[str, float] = field(default_factory=dict)

    def add(self, s: Sample) -> None:
        self.samples.append(s)
        print(f"  {s.label:38s} desktop={s.desktop_mb:6.0f}MB "
              f"runtime={s.runtime_mb:6.0f}MB avail={s.available_mb:6.0f}MB "
              f"{s.elapsed_s:6.2f}s")

    def to_markdown(self) -> str:
        lines = [
            "| 场景 | 桌面端 RSS (MB) | Runtime RSS (MB) | 系统可用 (MB) | 耗时 (s) | 说明 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(s.to_row() for s in self.samples)
        if self.derived:
            lines.append("")
            lines.append("| 派生量 | 值 |")
            lines.append("| --- | --- |")
            for k, v in self.derived.items():
                lines.append(f"| {k} | {v:.3f} |")
        return "\n".join(lines)


# ------------------------------------------------------------------ 样本生成

def make_pdf(path: Path, pages: int, *, chars_per_page: int = 2000) -> Path:
    """造一份指定页数、指定每页字数的中文 PDF。"""
    import pymupdf

    doc = pymupdf.open()
    para = ("本章规定了售后服务的基本要求。产品自购买之日起享受保修服务，"
            "整机保修期为两年，电池等易损件为六个月。人为损坏不在保修范围内。")
    for i in range(pages):
        page = doc.new_page()
        y = 60.0
        text = (para * ((chars_per_page // len(para)) + 1))[:chars_per_page]
        # 每行约 40 字，逐行写入
        for j in range(0, len(text), 40):
            if y > 780:
                break
            page.insert_text((50, y), text[j:j + 40], fontsize=10,
                             fontname="china-s")
            y += 14
    doc.save(str(path))
    doc.close()
    return path


def make_markdown(path: Path, chars: int) -> Path:
    """造一份指定字数的 Markdown（带标题，覆盖标题路径逻辑）。"""
    parts: list[str] = []
    n = 0
    sec = 0
    while n < chars:
        sec += 1
        parts.append(f"\n## 第 {sec} 节\n")
        body = ("本节说明售后服务的具体要求。产品保修期为两年，"
                "自购买之日起计算。如需维修请联系客服中心。") * 3
        parts.append(body)
        n += len(body) + 20
    path.write_text("# 内存测试文档\n" + "".join(parts), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 各组测量

def bench_desktop_idle(rep: Report) -> None:
    """基线：桌面端空转（不解析、不入库）。"""
    # 强制 gc，避免上一轮的临时对象影响基线
    import gc

    gc.collect()
    time.sleep(0.5)
    rep.add(Sample(
        label="桌面端空转（基线）",
        desktop_mb=rss_mb(), runtime_mb=runtime_rss_mb(),
        available_mb=system_available_mb(),
        note="只 import 依赖，未解析文件",
    ))


def bench_parse(rep: Report, tmp: Path) -> None:
    """解析峰值：不同页数的 PDF。

    ## 为什么要分档测

    PyMuPDF 的内存不是线性的——它按"当前页的对象图"分配，但整份文档
    的 xref 表与字体缓存会随页数增长。分档能看出拐点。
    """
    for pages in (10, 50, 200, 500):
        p = make_pdf(tmp / f"p{pages}.pdf", pages)
        size_mb = p.stat().st_size / 1024 / 1024
        baseline = rss_mb()
        t0 = time.monotonic()
        doc = parse(p)
        elapsed = time.monotonic() - t0
        peak = rss_mb()
        rep.add(Sample(
            label=f"解析 PDF {pages} 页",
            desktop_mb=peak, runtime_mb=runtime_rss_mb(),
            available_mb=system_available_mb(), elapsed_s=elapsed,
            note=f"文件 {size_mb:.1f} MB，{len(doc.blocks)} 块，"
                 f"增量 {peak - baseline:.0f} MB",
        ))
        rep.derived[f"PDF 每页解析增量 (MB/页, {pages} 页)"] = (
            (peak - baseline) / pages if pages else 0)


def bench_chunk(rep: Report, tmp: Path) -> None:
    """切块峰值：不同字数的文档。"""
    for chars in (50_000, 200_000, 1_000_000):
        p = make_markdown(tmp / f"c{chars}.md", chars)
        baseline = rss_mb()
        t0 = time.monotonic()
        doc = parse(p)
        chunks, _info = split(doc)
        elapsed = time.monotonic() - t0
        peak = rss_mb()
        rep.add(Sample(
            label=f"解析+切块 {chars // 1000}k 字",
            desktop_mb=peak, runtime_mb=runtime_rss_mb(),
            available_mb=system_available_mb(), elapsed_s=elapsed,
            note=f"{len(chunks)} 块，增量 {peak - baseline:.0f} MB",
        ))
        if chunks:
            rep.derived[f"每块内存增量 (MB/块, {chars // 1000}k 字)"] = (
                (peak - baseline) / len(chunks))


def bench_ingest(rep: Report, tmp: Path, kb_id: str, conn,
                 target_chunks: int) -> None:
    """入库 + 向量化：测"库规模 → 两端内存"。

    ## 这是最重要的组

    它回答"10 万 chunk 的库要多少内存"。

    ## 为什么向量化前要先卸载生成模型（第一版在这里测错了）

    第一版顺序是"入库 → 向量化 → 测 RSS"，结果向量化跑了 760 秒，
    期间**模型被空闲卸载**（`-idle-unload-sec 90`），Runtime RSS 从
    3407 MB 掉到 302 MB —— 于是"向量+索引内存"算出了 **-0.052 MB/块**
    这种无意义的负值，而且那次提问返回 0 字（模型没了）。

    现在改成：
      1. 入库（此时模型可能还加载着，只测"入库"这一步的增量）
      2. **显式卸载模型** → 测基线（只有 Runtime + SQLite + Embedding）
      3. 向量化（此时没有生成模型，空闲卸载不影响 Embedding）
      4. 再测 → 增量就是纯粹的向量 + FTS 索引开销
    """
    from localkb.ingest import build_request, ingest

    # 按"每块约 450 字"造文档，凑到 target_chunks
    chars_per_doc = 40_000        # 约 89 块
    per_doc = 89
    docs_needed = max(1, target_chunks // per_doc)

    before_desktop = rss_mb()
    before_runtime = runtime_rss_mb()

    t0 = time.monotonic()
    for i in range(docs_needed):
        p = make_markdown(tmp / f"ing{i}.md", chars_per_doc)
        doc = parse(p)
        chunks, chunker = split(doc)
        req = build_request(
            path=p, name=f"mem-test-{i}.md",
            file_hash=f"membench-{kb_id}-{i}",
            chunks=chunks, chunker=chunker,
            file_size=p.stat().st_size,
        )
        _ok, fail = ingest(conn, kb_id, req)
        if fail is not None:
            print(f"    入库失败（{i}）：{fail.reason}")
            break
    ingest_elapsed = time.monotonic() - t0

    st = conn.call(lambda c: c.kb_stat(kb_id))
    rep.add(Sample(
        label=f"入库 {st.chunk_count} 块",
        desktop_mb=rss_mb(), runtime_mb=runtime_rss_mb(),
        available_mb=system_available_mb(), elapsed_s=ingest_elapsed,
        note=f"Runtime 增量 {runtime_rss_mb() - before_runtime:.0f} MB，"
             f"桌面端增量 {rss_mb() - before_desktop:.0f} MB",
    ))

    # ---- 卸载模型，拿干净的基线 ----
    try:
        conn.call(lambda c: c.unload_model())
        time.sleep(3.0)     # 等引擎退出、内存归还
    except Exception as e:  # noqa: BLE001
        print(f"    卸载模型失败（{e}）——向量内存的测量会不准")
    base_runtime = runtime_rss_mb()
    rep.add(Sample(
        label="卸载模型后（基线）",
        desktop_mb=rss_mb(), runtime_mb=base_runtime,
        available_mb=system_available_mb(),
        note=f"只有 Runtime + SQLite + Embedding，库内 {st.chunk_count} 块",
    ))

    # ---- 向量化 ----
    print("    触发向量化…")
    t0 = time.monotonic()
    conn.call(lambda c: c.vectorize(kb_id, is_async=True))
    st2 = st
    for _ in range(1800):        # 最多 30 分钟
        time.sleep(1.0)
        st2 = conn.call(lambda c: c.kb_stat(kb_id))
        if st2.vectorize is None or not st2.vectorize.running:
            break
    vec_elapsed = time.monotonic() - t0
    after_vec = runtime_rss_mb()

    rep.add(Sample(
        label=f"向量化 {st2.chunk_count} 块",
        desktop_mb=rss_mb(), runtime_mb=after_vec,
        available_mb=system_available_mb(), elapsed_s=vec_elapsed,
        note=f"相对卸载后基线增量 {after_vec - base_runtime:.0f} MB，"
             f"已向量化 {st2.embedded_count}",
    ))

    if st2.chunk_count:
        rep.derived["每块向量+索引内存 (MB/块)"] = (
            (after_vec - base_runtime) / st2.chunk_count)
        rep.derived["向量化速度 (块/秒)"] = st2.chunk_count / max(vec_elapsed, 0.1)

    # ---- 检索（模型未加载，纯检索开销）----
    from localai import SearchRequest

    t0 = time.monotonic()
    res = conn.call(lambda c: c.search(SearchRequest(
        query="保修期是多久", kb_ids=[kb_id], top_k=20)))
    search_ms = (time.monotonic() - t0) * 1000
    rep.add(Sample(
        label=f"检索（{st2.chunk_count} 块）",
        desktop_mb=rss_mb(), runtime_mb=runtime_rss_mb(),
        available_mb=system_available_mb(), elapsed_s=search_ms / 1000,
        note=f"{len(res.hits)} 条命中，{search_ms:.0f} ms",
    ))
    rep.derived["检索耗时 (ms)"] = search_ms


def bench_query(rep: Report, kb_id: str, conn) -> None:
    """一次完整提问（检索 + 生成）。需要模型重新加载。"""
    from localkb.chat import KB_MODE_SELECTED, KbScope, build_chat_request, stream_turn

    # 重新加载模型（上一个测量把它卸了）
    try:
        cap = conn.call(lambda c: c.capability())
        conn.call(lambda c: c.load_model(cap.rag_model or "qwen2.5-1.5b-instruct-q4"))
    except Exception as e:  # noqa: BLE001
        print(f"    无法加载模型（{e}），跳过提问测量")
        return

    before = rss_mb()
    t0 = time.monotonic()
    req = build_chat_request(
        question="保修期是多久？", history=[],
        scope=KbScope(KB_MODE_SELECTED, [kb_id]), max_tokens=128,
    )
    text = ""
    for ev in stream_turn(conn, req):
        if ev.kind == "delta":
            text += ev.text
    elapsed = time.monotonic() - t0
    rep.add(Sample(
        label="一次提问（模型已加载）",
        desktop_mb=rss_mb(), runtime_mb=runtime_rss_mb(),
        available_mb=system_available_mb(), elapsed_s=elapsed,
        note=f"回答 {len(text)} 字，桌面端增量 {rss_mb() - before:.0f} MB",
    ))


# ------------------------------------------------------------------ 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="LocalKB 内存标定")
    ap.add_argument("--full", action="store_true",
                    help="含入库与向量化（慢，约 10 分钟）")
    ap.add_argument("--chunks", type=int, default=20000,
                    help="入库组的 chunk 目标数（默认 20000）")
    ap.add_argument("--out", default="", help="Markdown 输出路径")
    args = ap.parse_args()

    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ 本机 LocalAIHub 未运行，无法测 Runtime 侧指标")
        return 1

    vm = psutil.virtual_memory()
    print("LocalKB 内存标定")
    print(f"  机器   {vm.total / 1024 ** 3:.1f} GiB 总内存，"
          f"当前可用 {vm.available / 1024 ** 3:.1f} GiB")
    print(f"  Python {sys.version.split()[0]}")
    print()

    rep = Report()
    tmp = Path(tempfile.mkdtemp(prefix="localkb-membench-"))
    kb_id = ""
    try:
        bench_desktop_idle(rep)
        print("\n[1/3] 解析峰值")
        bench_parse(rep, tmp)
        print("\n[2/3] 切块峰值")
        bench_chunk(rep, tmp)

        if args.full:
            print(f"\n[3/3] 入库与向量化（目标 {args.chunks} 块）")
            kb = conn.call(lambda c: c.create_kb(
                f"membench-{uuid.uuid4().hex[:6]}", description="内存标定"))
            kb_id = kb.id
            bench_ingest(rep, tmp, kb_id, conn, args.chunks)
            bench_query(rep, kb_id, conn)
        else:
            print("\n[3/3] 跳过入库（加 --full 才跑）")

    finally:
        if kb_id:
            import contextlib

            # 清理失败不该让标定失败（服务可能已不可用）
            with contextlib.suppress(Exception):
                conn.call(lambda c: c.delete_kb(kb_id))
            print(f"\n已清理临时库 {kb_id}")

    md = rep.to_markdown()
    print("\n" + "=" * 72)
    print(md)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
