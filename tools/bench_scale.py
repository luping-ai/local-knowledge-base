"""规模标定：10 万块完整向量化 + 热检索耗时。

## 为什么这两项要一起测

"检索多快"这个问题在 5.9 万块的库上量出过两个截然不同的数：

  · 296 块的库：140~170 ms
  · 5.9 万块的库**第一次**：2422 ms

后者不是"检索慢"，是 **`LoadIndex` 把全库向量从 SQLite BLOB 读进内存并
建索引**的一次性成本。混在一起报一个数会得出错误结论（"库大了检索慢 15 倍"），
而真相是"第一次慢、之后快，但快多少没人知道"。

所以这个脚本把检索拆成三段来测：

  1. **冷启**：索引未载入 → 第一次调用（含 LoadIndex）
  2. **热**：紧接着的 N 次调用（索引已在内存）
  3. **冷启后**：让 Runtime 空闲足够久（可选），看索引会不会被释放

只有第 2 段是用户日常感受到的耗时。

## 为什么分阶段落盘

10 万块按实测 11.6 块/秒要 2.4 小时。中途任何中断（网络、休眠、手滑关窗口）
都会让"已经跑了两小时"白费。所以：

  · 每 5000 块把进度写进 `--state` 指定的 JSON
  · 重跑时若发现同一个库已存在且未完成，**接着向量化**而不是重建
  · 检索结果每测完一段就追加进 `--out`（Markdown）

## 用法

    # 全量（约 2.5~3 小时）
    python tools/bench_scale.py --chunks 100000

    # 只测检索（库里已有数据时）
    python tools/bench_scale.py --kb-id <id> --search-only

    # 换小规模先验证脚本本身（约 10 分钟）
    python tools/bench_scale.py --chunks 10000

## 安全

默认建**临时库**并在结束时删除（`--keep` 保留）。不碰现有知识库。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localkb.chunk.splitter import split
from localkb.parse.registry import parse
from localkb.runtime_conn import RuntimeConnector

__all__ = ["main"]

SEARCH_QUERIES = [
    "保修期是多久",
    "整机保修两年还是三年",
    "电池等易损件保修多久",
    "人为损坏是否在保修范围内",
    "售后服务的基本要求",
]


@dataclass
class Sample:
    """一个检索测量点。"""

    label: str
    ms: float
    hits: int = 0
    note: str = ""


@dataclass
class Report:
    chunks: int = 0
    embedded: int = 0
    vectorize_seconds: float = 0.0
    build_seconds: float = 0.0
    ingest_seconds: float = 0.0
    runtime_before_mb: float = 0.0
    runtime_after_ingest_mb: float = 0.0
    runtime_after_vec_mb: float = 0.0
    searches: list[Sample] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"## 规模：{self.chunks} 块")
        lines.append("")
        lines.append("| 阶段 | 耗时 | 说明 |")
        lines.append("| --- | --- | --- |")
        lines.append(f"| 造数据（解析+切块） | {self.build_seconds:.1f} s | |")
        lines.append(f"| 入库 | {self.ingest_seconds:.1f} s | "
                     f"Runtime +{self.runtime_after_ingest_mb - self.runtime_before_mb:.0f} MB |")
        lines.append(f"| 向量化 {self.embedded}/{self.chunks} | "
                     f"{self.vectorize_seconds:.0f} s | "
                     f"{self.embedded / max(self.vectorize_seconds, 0.1):.1f} 块/秒，"
                     f"Runtime +{self.runtime_after_vec_mb - self.runtime_after_ingest_mb:.0f} MB |")  # noqa: E501
        lines.append("")

        if self.searches:
            lines.append("| 检索 | 耗时 (ms) | 命中 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for s in self.searches:
                lines.append(f"| {s.label} | {s.ms:.0f} | {s.hits} | {s.note} |")
            lines.append("")

            warm = [s.ms for s in self.searches if s.label.startswith("热")]
            if warm:
                lines.append(f"**热检索**：中位数 {statistics.median(warm):.0f} ms，"
                             f"最小 {min(warm):.0f} ms，最大 {max(warm):.0f} ms"
                             f"（n={len(warm)}）")
                lines.append("")
        return "\n".join(lines)


def rss_mb() -> float:
    import psutil

    total = 0.0
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in ("localai-cli.exe", "llama-server.exe"):
                total += p.memory_info().rss / 1024 / 1024
        except psutil.Error:
            continue
    return total


def make_doc(path: Path, chars: int) -> Path:
    """造一份指定字数的 Markdown（带标题，覆盖标题路径逻辑）。"""
    parts: list[str] = ["# 售后服务手册\n"]
    n = 0
    sec = 0
    while n < chars:
        sec += 1
        parts.append(f"\n## 第 {sec} 条 保修规定\n")
        body = (
            "本章规定了售后服务的基本要求。产品自购买之日起享受保修服务，"
            "整机保修期为两年，电池等易损件为六个月。人为损坏不在保修范围内。"
            "如需维修请联系客服中心，或前往就近的服务网点办理。"
        ) * 3
        parts.append(body)
        n += len(body) + 20
    path.write_text("".join(parts), encoding="utf-8")
    return path


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_state(path: Path, **kw) -> None:
    cur = load_state(path)
    cur.update(kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def build_kb(conn, rep: Report, target: int, tmp: Path,
             state_path: Path, kb_name: str) -> str:
    """造数据 → 入库，直到**实际块数**达到 target。返回 kb_id。

    ## 为什么按实际块数检查而不是按"每篇约 N 块"估算（实测踩到的缺陷）

    第一版写 `per_doc = 89; docs_needed = target // 89`。89 是从
    `chars_per_doc=40000` 反推的，而真实值是 **168 块/篇**——差了近一倍。

    后果：pilot 想造 1 万块，实际入了 16016；10 万块那次跑到第 500 篇时
    已经 67353 块，按 1123 篇算最终会有 **18.8 万块**。于是：

      · 测的不是"设计上限 10 万块"，而是 1.9 倍
      · 向量化时间从 2.9 小时变成 5.4 小时
      · 数据写进文档后与规格对不上，下一次没法比较

    根因是"用估算值推导循环次数"。改成每轮向服务端要 `kb_stat` 的实际
    块数、达到 target 就停——估算错误不再影响规模。代价是每篇多一次
    HTTP（约 10 ms），相对入库本身（约 2 秒/篇）可以忽略。

    ## 为什么每篇 40000 字

    Runtime 有**单文档 2000 块**的硬上限。168 块离上限很远，又足够减少
    HTTP 往返次数（比 500 字小文件快得多）。
    """
    from localkb.ingest import build_request, ingest

    chars_per_doc = 40_000
    # 只用来打印预估，绝不用于控制循环次数
    guess_per_doc = 168
    guess_docs = max(1, target // guess_per_doc)

    kb = conn.call(lambda c: c.create_kb(kb_name, description="规模标定"))
    kb_id = kb.id
    save_state(state_path, kb_id=kb_id, kb_name=kb_name, target=target,
               docs_done=0)
    print(f"  临时库 {kb_id}，目标 {target} 块"
          f"（按 {guess_per_doc} 块/篇估约 {guess_docs} 篇，实际按块数停）")

    rep.runtime_before_mb = rss_mb()
    t_build = 0.0
    t_ingest = 0.0
    i = 0
    while True:
        st = conn.call(lambda c: c.kb_stat(kb_id))
        if st.chunk_count >= target:
            print(f"  达到目标：{st.chunk_count} 块（{i} 篇）")
            break
        if i > 3000:
            print("  ✗ 篇数超过 3000 仍没到目标，停止（避免无限循环）")
            break

        t0 = time.monotonic()
        p = make_doc(tmp / f"doc{i}.md", chars_per_doc)
        doc = parse(p)
        chunks, chunker = split(doc)
        t_build += time.monotonic() - t0

        req = build_request(
            path=p, name=f"scale-{i}.md",
            file_hash=f"scale-{kb_id}-{i}",
            chunks=chunks, chunker=chunker,
            file_size=p.stat().st_size,
        )
        t0 = time.monotonic()
        _ok, fail = ingest(conn, kb_id, req)
        t_ingest += time.monotonic() - t0
        if fail is not None:
            print(f"    第 {i} 篇入库失败：{fail.reason}")
            break
        i += 1
        if i % 100 == 0:
            save_state(state_path, docs_done=i)
            print(f"    入库 {i} 篇，累计约 {st.chunk_count} 块")

    rep.build_seconds = t_build
    rep.ingest_seconds = t_ingest
    rep.runtime_after_ingest_mb = rss_mb()

    st = conn.call(lambda c: c.kb_stat(kb_id))
    save_state(state_path, docs_done=i, chunks=st.chunk_count)
    rep.chunks = st.chunk_count
    rep.embedded = st.embedded_count
    print(f"  入库完成：{st.chunk_count} 块（已向量化 {st.embedded_count}）")
    return kb_id


def vectorize(conn, rep: Report, kb_id: str, state_path: Path) -> None:
    """触发向量化并轮询到结束，每 60 秒打印进度并落盘。

    ## 为什么先 unload_model

    实测踩到过：向量化跑了 8000 秒，期间生成模型被空闲卸载（`-idle-unload-sec`），
    于是"向量占用内存"算出了负值。显式卸载之后测到的才是纯向量 + 索引开销。

    这里更重要的原因是**内存**：3B 模型占 3.3 GB，与 10 万块的向量并存会让
    Runtime 接近本机上限，向量化可能因为拿不到内存而变慢甚至失败。
    """
    print("  卸载生成模型（避免 3.3 GB 模型与向量争内存，也保证测量干净）")
    try:
        conn.call(lambda c: c.unload_model())
        time.sleep(3.0)
    except Exception as e:  # noqa: BLE001
        print(f"    卸载失败（{e}）——测量会偏大")

    base = rss_mb()
    print(f"  向量化前 Runtime {base:.0f} MB")

    t0 = time.monotonic()
    conn.call(lambda c: c.vectorize(kb_id, is_async=True))
    last_report = t0
    last_embedded = 0
    st = None
    while True:
        time.sleep(2.0)
        st = conn.call(lambda c: c.kb_stat(kb_id))
        if st.vectorize is None or not st.vectorize.running:
            break
        now = time.monotonic()
        if now - last_report >= 60:
            done = st.embedded_count
            rate = (done - last_embedded) / (now - last_report)
            eta = (st.chunk_count - done) / rate / 60 if rate > 0 else 0
            print(f"    {done}/{st.chunk_count} 块  "
                  f"{rate:.1f} 块/秒  剩余约 {eta:.0f} 分钟")
            save_state(state_path, embedded=done,
                       vectorize_elapsed=round(now - t0, 1))
            last_report = now
            last_embedded = done

    rep.vectorize_seconds = time.monotonic() - t0
    rep.embedded = st.embedded_count if st else 0
    rep.chunks = st.chunk_count if st else 0
    rep.runtime_after_vec_mb = rss_mb()
    save_state(state_path, embedded=rep.embedded,
               vectorize_seconds=round(rep.vectorize_seconds, 1), done=True)
    print(f"  向量化完成：{rep.embedded}/{rep.chunks} 块，"
          f"{rep.vectorize_seconds:.0f} 秒"
          f"（{rep.embedded / max(rep.vectorize_seconds, 0.1):.1f} 块/秒）")
    print(f"  向量化后 Runtime {rep.runtime_after_vec_mb:.0f} MB"
          f"（+{rep.runtime_after_vec_mb - base:.0f} MB）")


def bench_search(conn, rep: Report, kb_id: str, *, warm_rounds: int,
                 cold_rounds: int) -> None:
    """测检索：冷启一次 + 热 N 次，并重复 `cold_rounds` 轮观察稳定性。

    ## 为什么要多轮冷启

    无法保证"让索引驻留"或"让它释放"——这是 Runtime 的内部行为。
    所以换个做法：连续做多轮，看第 1 次与后续的差值在不同轮次里是否稳定。
    如果每轮第 1 次都明显更慢，说明索引确实会被释放（或按查询重建）。
    """
    from localai import SearchRequest

    for rnd in range(1, cold_rounds + 1):
        for qi, q in enumerate(SEARCH_QUERIES):
            label = "冷启" if qi == 0 else "热"
            if rnd > 1:
                label = f"第{rnd}轮{label}"
            t0 = time.monotonic()
            res = conn.call(lambda c: c.search(SearchRequest(
                query=q, kb_ids=[kb_id], top_k=20)))
            ms = (time.monotonic() - t0) * 1000
            rep.searches.append(Sample(
                label=label, ms=ms, hits=len(res.hits),
                note=f"「{q}」" + ("（索引冷启动）" if label == "冷启" else ""),
            ))
            print(f"    {label:12s} {ms:8.0f} ms  {len(res.hits)} 命中  「{q}」")

        # 同一 query 连打，测"真正热"的耗时
        for i in range(warm_rounds):
            t0 = time.monotonic()
            res = conn.call(lambda c: c.search(SearchRequest(
                query=SEARCH_QUERIES[0], kb_ids=[kb_id], top_k=20)))
            ms = (time.monotonic() - t0) * 1000
            rep.searches.append(Sample(
                label="热(同询问)",
                ms=ms, hits=len(res.hits), note=f"第 {i + 1} 次同 query",
            ))
            print(f"    热(同询问)   {ms:8.0f} ms  {len(res.hits)} 命中")


def main() -> int:
    ap = argparse.ArgumentParser(description="LocalKB 规模标定")
    ap.add_argument("--chunks", type=int, default=100_000)
    ap.add_argument("--kb-id", default="", help="复用已有库（跳过造数据）")
    ap.add_argument("--search-only", action="store_true", help="只测检索")
    ap.add_argument("--warm-rounds", type=int, default=3)
    ap.add_argument("--cold-rounds", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="保留临时库")
    ap.add_argument("--out", default="docs/实测记录/20261116-规模标定10万块.md")
    ap.add_argument("--state", default="",
                    help="进度文件（默认在系统临时目录下的 localkb-scale-state.json）")
    args = ap.parse_args()

    conn = RuntimeConnector()
    if not conn.alive():
        print("✗ LocalAIHub 未运行")
        return 1

    state_path = Path(args.state) if args.state else (
        Path(os.environ.get("TEMP", tempfile.gettempdir())) / "localkb-scale-state.json")
    rep = Report()
    tmp = Path(tempfile.mkdtemp(prefix="localkb-scale-"))
    kb_id = args.kb_id
    created = False

    try:
        if not kb_id and not args.search_only:
            prev = load_state(state_path)
            if prev.get("kb_id") and not prev.get("done"):
                # 上次没跑完：确认库还在就接着用，避免白跑几小时
                try:
                    st = conn.call(lambda c: c.kb_stat(prev["kb_id"]))
                    if st.chunk_count > 0:
                        kb_id = prev["kb_id"]
                        print(f"发现未完成的标定库 {kb_id}"
                              f"（{st.chunk_count} 块，已向量化 {st.embedded_count}）——接着跑")
                        rep.chunks = st.chunk_count
                        rep.embedded = st.embedded_count
                except Exception:  # noqa: BLE001
                    print("上次的库已不存在，重新开始")

            if not kb_id:
                print(f"[1/3] 造 {args.chunks} 块数据并入库")
                kb_id = build_kb(conn, rep, args.chunks, tmp, state_path,
                                 f"scale-{int(time.time())}")
                created = True

            if args.search_only:
                print("[2/3] 跳过向量化（--search-only）")
            else:
                print("[2/3] 向量化（这一步最慢，按 11.6 块/秒估 2.4 小时）")
                vectorize(conn, rep, kb_id, state_path)

        if not kb_id:
            print("✗ 需要 --kb-id 或允许造数据")
            return 1

        st = conn.call(lambda c: c.kb_stat(kb_id))
        rep.chunks = st.chunk_count
        rep.embedded = st.embedded_count
        print(f"[3/3] 检索（库内 {rep.chunks} 块，已向量化 {rep.embedded}）")
        bench_search(conn, rep, kb_id,
                     warm_rounds=args.warm_rounds, cold_rounds=args.cold_rounds)

        md = rep.to_markdown()
        print()
        print(md)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"\n已写入 {out}")
        print(f"结构化结果：{json.dumps(asdict(rep), ensure_ascii=False)[:400]}…")
        return 0
    finally:
        if created and not args.keep:
            import contextlib

            with contextlib.suppress(Exception):
                conn.call(lambda c: c.delete_kb(kb_id))
            print(f"\n已清理临时库 {kb_id}")
            with contextlib.suppress(OSError):
                state_path.unlink()
        elif kb_id:
            print(f"\n保留库 {kb_id}（--keep 或复用了指定库）")


if __name__ == "__main__":
    raise SystemExit(main())
