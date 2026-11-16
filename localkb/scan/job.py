"""扫描任务的编排：遍历 → diff → 上报进度 → 取消。

## 这一层目前只做"枚举 + 与本地表比对"

解析、切块、入库都在 M1/M2 才接上。但 diff 逻辑现在就要定型，因为它
决定了三件事，而这三件事在后续阶段不会变：

  1. **哪些文件要处理**（新增 / 内容变了 / 未变跳过 / 已失效）
  2. **进度怎么算**（分母是"要处理的文件数"，不是"扫到的文件数"——
     后者会让用户看到 5000/5000 却还在等，因为其中 4900 个是跳过的）
  3. **取消后留下什么状态**（已处理的保留，未处理的回到 pending）

## 为什么 diff 要在这里而不是 walker 里

walker 只认识磁盘，不认识数据库。把"查 local_files"塞进 walker 会让它
依赖 StateDB，从而无法在纯文件系统测试里使用（现在的 walker 测试
一个 sqlite 连接都不用建）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from localkb.scan.hasher import file_stat, sha256_file
from localkb.scan.walker import Walker, WalkResult
from localkb.store.db import LocalFile, StateDB

__all__ = ["ScanPlan", "ScanPhase", "ScanJob", "FileAction"]


class ScanPhase(str, Enum):
    """扫描阶段。UI 据此显示不同的进度语义。

    ## 为什么继承 str

    这些值会进日志与测试断言（`p.phase.value == "hashing"`）。继承 str
    让 `str(ScanPhase.DONE)` 与 f-string 输出人类可读的名字，而不是
    `<ScanPhase.DONE: 'done'>`。ruff 的 UP042 建议改用 StrEnum（3.11+），
    但那会让 `phase == "done"` 这类比较在旧版本上行为不一致——本项目
    `requires-python >= 3.11` 虽然满足，但保持与 SDK 的写法一致更重要
    （SDK 里也是 `class X(str, Enum)`）。
    """

    IDLE = "idle"
    WALKING = "walking"
    """枚举磁盘文件（分母未知，显示"已发现 N 个"）。"""

    HASHING = "hashing"
    """计算 sha256 并比对本地表（分母已知）。"""

    PARSING = "parsing"
    """解析 + 切块。这一阶段最慢（PDF 每页 15~30ms），且内存峰值最高。"""

    INGESTING = "ingesting"
    """提交给 Runtime（含增量的 update 路径）。"""

    VECTORIZING = "vectorizing"
    """等向量化完成。关键词检索在入库后立即可用，语义检索要等这一步。"""

    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FileAction(str, Enum):
    """一个文件在这次扫描里该怎么处理。

    与 `ScanPhase` 同理用 `str, Enum`（理由见上）。
    """

    NEW = "new"
    """库里没有，要导入。"""

    CHANGED = "changed"
    """库里有但内容变了（sha256 不同），要更新。"""

    UNCHANGED = "unchanged"
    """库里有且内容未变，跳过。"""

    RETRY = "retry"
    """上次失败或跳过，本次重试。"""

    MISSING = "missing"
    """本地表有、磁盘上没有（文件被移动或删除，见方案 §7.7.1）。"""

    DUPLICATE = "duplicate"
    """与本次扫描中另一份文件内容完全相同（sha256 相同），不入库。

    ## 为什么这个状态必须存在，而不是"合并到 UNCHANGED"

    两者对用户的含义完全不同：UNCHANGED 是"这份已经入库了，不用再做"，
    DUPLICATE 是"这份内容和另一份一模一样，所以没导它"。前者用户不用
    关心，后者必须被告知——否则他会问"我这份文件怎么搜不到"。

    方案 §12.2：完全重复**不需要用户判断**（内容一模一样），所以自动
    只导一份，但要告诉用户"另有 N 个副本未入库"。
    """


@dataclass
class ScanItem:
    path: Path
    action: FileAction
    sha256: str = ""
    size: int = 0
    mtime: int = 0
    # doc_id 仅在 CHANGED / UNCHANGED / RETRY 时非空
    doc_id: Optional[str] = None
    # previous 是本地表里原来那行（用于判断"是改名还是内容变了"）
    previous: Optional[LocalFile] = None
    # 解析结果。M1 阶段只填充"块数"与"提示"，M2 才会真的提交给 Runtime。
    # 单独放一个 dict 而不是完整 ParsedDoc，是为了让计划可以序列化/
    # 打印（调试时能直接看每篇切出多少块）。
    chunk_count: int = 0
    parse_error: str = ""
    warnings: list[str] = field(default_factory=list)
    # 入库结果（M2 起填充）。ingested=False 且无 ingest_error 表示"尚未尝试"。
    ingested: bool = False
    ingest_error: str = ""
    # 这次入库是新增还是更新（增量路径用 update_document_id）
    is_update: bool = False
    # 解析 + 切块的结果，供 ingest_all 复用。
    #
    # ## 为什么缓存而不是重新解析
    #
    # 一篇 500 页 PDF 的解析要 8~15 秒。若入库阶段再解析一遍，等于把
    # 扫描时间翻倍，而且第二遍的结果可能与第一遍不同（文件在两次之间
    # 被改了）——那种不一致极难排查：入库的块与报告的块数对不上。
    #
    # 代价是内存：1907 块大约占几 MB。与"重新解析一遍"相比值得。
    _chunks: list = field(default_factory=list, repr=False)
    _chunker: object = field(default=None, repr=False)
    # 内容完全相同的"主副本"路径（DUPLICATE 时非空）。
    #
    # ## 为什么要记下是"跟谁重复"
    #
    # 只告诉用户"另有 N 个副本"没有用——他需要知道**哪一份进了库**，
    # 才能判断"我想搜的其实是另一份"。方案 §12.7 的提示条虽然只显示
    # 聚合数字，但设置页/详情的分组视图要用这个字段。
    duplicate_of: str = ""


@dataclass
class ScanPlan:
    """一次扫描的计划：要处理什么、跳过了什么。"""

    items: list[ScanItem] = field(default_factory=list)
    walk: Optional[WalkResult] = None
    # missing 是本地表有、磁盘上没了的文件路径
    missing: list[LocalFile] = field(default_factory=list)

    @property
    def to_process(self) -> list[ScanItem]:
        """需要真正处理（解析+入库）的条目。"""
        return [i for i in self.items
                if i.action in (FileAction.NEW, FileAction.CHANGED, FileAction.RETRY)]

    @property
    def unchanged(self) -> list[ScanItem]:
        return [i for i in self.items if i.action is FileAction.UNCHANGED]


@dataclass
class ScanProgress:
    """一次进度上报。UI 只读这个结构，不直接读 job 内部状态。"""

    phase: ScanPhase
    # done / total 在 WALKING 阶段 total 为 0（分母未知）
    done: int = 0
    total: int = 0
    # current 是正在处理的文件（给"当前：xxx.pdf"用）
    current: str = ""
    # counts 按 FileAction 计数，给"新增 12 / 变更 3 / 跳过 480"用
    counts: dict[str, int] = field(default_factory=dict)
    # message 是给用户的一句话（失败/暂停时非空）
    message: str = ""

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(self.done * 100 / self.total))


class ScanJob:
    """一次扫描的执行体。**不碰 Qt**——它可以在测试里同步跑。

    ## 线程模型

    UI 应该把它放进 QThread，用 `on_progress` 回调上抛。回调在**工作线程**
    被调用，Qt 侧必须用 signal（自动跨线程排队），不能直接更新 widget。

    ## 为什么分两阶段上报

    WALKING 阶段分母未知（要扫完才知道有多少文件），HASHING 阶段分母已知。
    合成一个进度条会让"卡在 30%"变成常态——实际上那是在算 sha256。

    ## 为什么 hash 也算进度

    sha256 是这一阶段最慢的部分（大文件每 MB 都要读）。不算进度的话，
    100 篇 PDF 会在"正在扫描"上停十几秒没有任何反馈。
    """

    def __init__(
        self,
        db: StateDB,
        kb_id: str,
        roots: list[Path],
        *,
        on_progress: Optional[Callable[[ScanProgress], None]] = None,
        cancel: Optional[threading.Event] = None,
        walker_factory: Callable[..., Walker] = Walker,
        hash_workers: int = 1,
        conn=None,
    ) -> None:
        self._db = db
        self._kb_id = kb_id
        self._roots = roots
        self._on_progress = on_progress
        self._cancel = cancel or threading.Event()
        self._walker_factory = walker_factory
        # hash_workers 刻意默认 1：机械盘上并发读会让整体更慢，且会与用户
        # 正在用的 Word 抢 IO。M5 的极限测试会给出更合适的值。
        self._hash_workers = hash_workers
        # conn 为 None 时只能做 build_plan / parse_all（M1 行为）。
        #
        # 为什么不在构造时就要求 conn：M1 的三个阶段（遍历/hash/解析）
        # 不需要 Runtime，保持"不传也能用"让测试与"扫描前估算"这类
        # 场景不必拉起服务。真正入库时才由 ingest_all 检查。
        self._conn = conn
        self._phase = ScanPhase.IDLE
        # 完全重复判定的状态（方案 §12.2）。每次 build_plan 开头重置。
        #   _seen_hashes:  sha256 → 主副本路径
        #   _imported_by_hash: sha256 → 已入库的路径（一次性建索引）
        #
        # ## 为什么 _imported_by_hash 要一次性建好
        #
        # 第一版在 _mark_duplicate 里对每个文件都调一次
        # `list_files(statuses=["imported"])`。5000 篇的库会让这变成
        # 5000 次全表查询 × 5000 行 = 2500 万次比较——扫描会慢到不可用。
        # 改成开头建一次字典，之后都是 O(1)。
        self._seen_hashes: dict[str, str] = {}
        self._imported_by_hash: dict[str, str] = {}

    # ------------------------------------------------------------------ 控制

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------ 构建计划

    def build_plan(self) -> ScanPlan:
        """遍历 + hash + 与本地表比对，得到计划。不写库、不入库。

        单独抽出来的理由：调用方可能只想"看看要处理多少东西"（扫描前
        估算、或给用户的确认对话框），而不想真的开始。也让测试可以断言
        计划而不需要跑完整流程。
        """
        self._emit(ScanPhase.WALKING, message="正在枚举文件…")

        wk = self._walker_factory(cancel=self._cancel)
        walk = wk.walk(self._roots)
        if self.cancelled:
            self._emit(ScanPhase.CANCELLED, message="已取消")
            return ScanPlan(walk=walk)

        plan = ScanPlan(walk=walk)
        total = len(walk.files)
        self._emit(ScanPhase.HASHING, done=0, total=total,
                   counts={"discovered": total, "skipped": len(walk.skipped)})

        # 重置判重状态并建"已入库 sha256"索引（见 __init__ 的说明）
        self._seen_hashes = {}
        self._imported_by_hash = {
            row.sha256: row.path
            for row in self._db.list_files(self._kb_id, statuses=["imported"])
            if row.sha256
        }

        for i, path in enumerate(walk.files):
            if self.cancelled:
                self._emit(ScanPhase.CANCELLED, done=i, total=total,
                           message="已取消")
                return plan

            try:
                size, mtime = file_stat(path)
                digest = sha256_file(path)
            except OSError as e:
                # 遍历后到算 hash 之间文件可能被删/被锁。这不是扫描失败，
                # 记一条 skipped 继续——与 walker 里的处理保持同一语义。
                from localkb.scan.walker import SkipReason

                reason = (SkipReason.LOCKED
                          if isinstance(e, PermissionError)
                          else SkipReason.UNREADABLE)
                if walk is not None:
                    walk.skipped.append((path, reason))
                self._emit(ScanPhase.HASHING, done=i + 1, total=total,
                           current=path.name)
                continue

            item = self._classify(path, digest, size, mtime)
            self._mark_duplicate(item, plan)
            plan.items.append(item)
            self._emit(
                ScanPhase.HASHING, done=i + 1, total=total,
                current=path.name,
                counts=self._counts(plan),
            )
            # 让出 CPU：UI 线程需要机会渲染进度。50ms 太小会拖慢扫描，
            # 1 秒太大会让"取消"不灵敏。这里用极短 sleep，实际节流交给
            # 调用方的回调实现（Qt 侧可以做 100ms 合并）。
            if i % 20 == 19:
                time.sleep(0.001)

        plan.missing = self._find_missing({i.path for i in plan.items})
        self._emit(ScanPhase.DONE, done=total, total=total,
                   counts=self._counts(plan),
                   message=self._summary(plan))
        return plan

    # ------------------------------------------------------------------ 解析

    def parse_all(self, plan: ScanPlan, *,
                  parser: Optional[Callable[[Path], object]] = None
                  ) -> ScanPlan:
        """对计划中"需要处理"的条目做解析 + 切块，填充块数与提示。

        ## 为什么与 build_plan 分开

        M1 阶段只要"能解析、能切块、能看到块数"就够验收；M2 才会把块提交
        给 Runtime。分成两步让 M1 可以独立验证，也让"扫描前估算"这种
        只想知道工作量的场景不必真的解析。

        ## 单篇失败必须继续（§10.5）

        "单篇失败不影响整体"是硬要求。所以这里逐篇 try/except，把错误
        记在 `item.parse_error` 上，而不是让它冒泡中断整次扫描。

        ## 为什么不并发

        PyMuPDF 解析一份 100 页 PDF 约 1~3 秒，且内存峰值可达数百 MB。
        并发解析会让峰值叠加，与"解析必须与推理错开峰值"（§10.4）直接
        冲突。串行换来的是可预测的内存曲线。
        """
        from localkb.chunk.splitter import split
        from localkb.parse.registry import parse as default_parse
        from localkb.parse.types import ParseError

        p = parser or default_parse
        targets = plan.to_process
        total = len(targets)
        if not total:
            self._emit(ScanPhase.DONE, done=0, total=0,
                       message=self._summary(plan))
            return plan

        self._emit(ScanPhase.PARSING, done=0, total=total,
                   counts=self._counts(plan))

        for i, item in enumerate(targets):
            if self.cancelled:
                self._emit(ScanPhase.CANCELLED, done=i, total=total,
                           message="已取消")
                return plan
            try:
                doc = p(item.path)
                file_chunks, chunker_info = split(doc)
                item.chunk_count = len(file_chunks)
                item.warnings = list(getattr(doc, "warnings", []) or [])
                # 缓存切块结果供入库复用（见 ScanItem._chunks 的说明）
                item._chunks = file_chunks
                item._chunker = chunker_info
                if not file_chunks:
                    item.parse_error = (
                        f"解析成功但切不出任何块（{len(doc.blocks)} 个文本块）。"
                    )
            except ParseError as e:
                # ParseError 的 reason 已经是给用户看的话（§5.6）
                item.parse_error = e.reason
            except Exception as e:  # noqa: BLE001 - 单篇失败不能中断整体
                item.parse_error = f"解析时出错：{type(e).__name__}: {e}"

            self._emit(ScanPhase.PARSING, done=i + 1, total=total,
                       current=item.path.name,
                       counts=self._counts(plan))

        self._persist(plan)
        self._emit(ScanPhase.DONE, done=total, total=total,
                   counts=self._counts(plan),
                   message=self._summary(plan))
        return plan

    def ingest_all(self, plan: ScanPlan) -> ScanPlan:
        """把 `parse_all` 切好的块提交给 Runtime，并把结果写回本地表。

        ## 为什么要求先跑 parse_all

        入库需要"切好的块"。重新解析一遍会让扫描时间翻倍，而且文件在两次
        之间被改动时，入库的块与报告的块数会对不上——那种不一致极难排查。
        所以这里**复用** `parse_all` 缓存的结果，而不是重新解析。

        ## 单篇失败继续，但两类错误要暂停整次扫描

        - `resource_limited` / `queue_full`：是**系统状态**，不是这一篇
          的问题。继续提交只会堆队列。
        - `chunker_mismatch`：库内版本不一致，后面每一篇都会失败。
          必须停下来让用户决定是否重建。
        """
        from localkb.ingest import build_request, ingest

        if self._conn is None:
            raise RuntimeError(
                "ingest_all 需要 Runtime 连接（构造 ScanJob 时传 conn）"
            )

        targets = [
            i for i in plan.to_process
            if i._chunks and not i.parse_error and not i.ingested
        ]
        total = len(targets)
        if not total:
            self._persist(plan)
            return plan

        self._emit(ScanPhase.INGESTING, done=0, total=total,
                   counts=self._counts(plan))

        for i, item in enumerate(targets):
            if self.cancelled:
                self._emit(ScanPhase.CANCELLED, done=i, total=total,
                           message="已取消")
                self._persist(plan)
                return plan

            # 增量更新必须带上旧 doc_id，否则会新建一份重复文档（§7.2）
            update_id = None
            if item.action is FileAction.CHANGED and item.doc_id:
                update_id = item.doc_id
                item.is_update = True

            req = build_request(
                path=item.path,
                name=item.path.name,
                file_hash=item.sha256,
                chunks=item._chunks,
                chunker=item._chunker,
                file_size=item.size,
                modified_at=item.mtime,
                update_document_id=update_id,
            )
            ok, fail = ingest(self._conn, self._kb_id, req)

            if fail is not None:
                item.ingest_error = fail.reason
                if fail.needs_rebuild or fail.should_pause:
                    # 后续每一篇都会同样失败，立刻停下并保留已完成的
                    self._persist(plan)
                    self._emit(
                        ScanPhase.PAUSED, done=i + 1, total=total,
                        counts=self._counts(plan),
                        message=fail.reason,
                    )
                    return plan
            else:
                item.ingested = True
                item.doc_id = ok.existing_id if ok.duplicate else ok.document_id
                # 缓存用完了，及时释放（一篇 200 块的文本可能有几百 KB）
                item._chunks = []
                item._chunker = None

            self._emit(ScanPhase.INGESTING, done=i + 1, total=total,
                       current=item.path.name,
                       counts=self._counts(plan))

        self._persist(plan)
        self._emit(ScanPhase.DONE, done=total, total=total,
                   counts=self._counts(plan),
                   message=self._summary(plan))
        return plan

    def vectorize(self, timeout_sec: float = 600.0) -> tuple[bool, str]:
        """触发并等待向量化完成。

        ## 为什么要"等"

        关键词检索在入库后立即可用，语义检索要等向量补齐。若不等就宣告
        "完成"，用户第一次提问会得到"语义检索不可用"的降级提示——
        而实际上只是还在跑。

        ## 超时不算失败

        5000 篇的向量化可能超过 10 分钟。超时后返回 False 并说明"仍在后台
        运行"，让 UI 显示"语义检索准备中"而不是"失败"。
        """
        import time as _time

        from localkb.ingest import vectorize as trigger

        if self._conn is None:
            return False, "未连接本地服务"

        ok, note = trigger(self._conn, self._kb_id)
        if not ok:
            return False, note

        self._emit(ScanPhase.VECTORIZING, message="正在生成向量…")
        deadline = _time.monotonic() + timeout_sec
        while _time.monotonic() < deadline:
            if self.cancelled:
                return False, "已取消（向量化仍在后台继续）"
            try:
                st = self._conn.call(lambda c: c.kb_stat(self._kb_id))
            except Exception:  # noqa: BLE001 - 查询失败不该中断等待
                _time.sleep(1.0)
                continue
            vp = st.vectorize
            if vp is None or not vp.running:
                # ## 判据是"计数相等"，不是"任务不跑了"
                #
                # 第一版写成 `if not vp.running: return True, "向量已就绪(...)"`。
                # 但 `running=false` 有五种可能：正常完成、用户取消、Runtime
                # 重启、任务失败、引擎崩溃。只有第一种是成功。
                #
                # 后果是用户取消之后 UI 显示"**向量已就绪（16000/100000）**"
                # ——一句话自相矛盾，而且会让他以为"这个库就是没这段内容"，
                # 而不是"这段内容没被索引"。语义检索实际只覆盖 16%，
                # 却没有任何地方能让人察觉。
                #
                # 未完成时返回 False 但**不是失败**：已完成的切片可用，
                # 再次触发会从断点继续（服务端 pendingChunks 只看缺向量的）。
                # 调用方（ScanWorker）会把 note 送到 UI，文案必须说清这一点。
                if st.embedded_count < st.chunk_count:
                    return False, (
                        f"语义索引未完成（{st.embedded_count}/{st.chunk_count}）。"
                        "已完成的切片可用于语义检索；再次扫描会继续补齐剩余部分。"
                    )
                return True, f"向量已就绪（{st.embedded_count}/{st.chunk_count}）"
            self._emit(
                ScanPhase.VECTORIZING,
                done=vp.embedded_chunks, total=vp.total_chunks,
                message=f"正在生成向量 {vp.embedded_chunks}/{vp.total_chunks}",
            )
            _time.sleep(0.5)
        return False, "向量化仍在后台运行，语义检索会在稍后可用。"

    def check_similar(self, plan: ScanPlan) -> str:
        """对本次新入库的文档做内容相似检测（方案 §12.4 / §12.5）。返回说明文案。

        ## 为什么只传本次新入库的 doc_id

        §12.5：增量扫描时只涉及改动的几个文件，而**这正是用户最需要确认
        的时刻**（"我刚存了一版，是不是和旧版重复了"）。全库重算在 10 万
        块时要几十秒，而其中 99% 上次已经确认过了。

        传空列表的语义是"该库全部文档"（服务端），所以空时要直接跳过——
        `dedup.check_similar` 里有这条保护。

        ## 为什么失败不算扫描失败

        入库已经完成，关键词与语义检索都可用。相似检测只是"额外提醒"，
        它失败不该让用户以为整次扫描白做了。所以这里返回一句说明，
        由 UI 决定显示与否。

        ## 为什么在 vectorize 之后

        指纹算法要读**块文本**（不是向量），理论上入库完就能算。但
        `dedup_check` 的 `recompute=True` 会遍历文档并读块——与向量化
        同时跑会争 CPU，让两件事都变慢。放在向量化之后串行更稳妥。
        """
        from localkb.dedup import check_similar as run_check

        if self._conn is None:
            return ""
        doc_ids = [i.doc_id for i in plan.items
                   if i.ingested and i.doc_id and not i.ingest_error]
        if not doc_ids:
            return ""

        res = run_check(self._conn, self._db, self._kb_id, doc_ids,
                        recompute=True)
        if res.has_groups:
            n = sum(len(g.members) for g in res.groups)
            msg = f"发现 {len(res.groups)} 组共 {n} 个文件内容相似，需要确认入库版本"
            if res.incomplete:
                msg += f"（另有 {len(res.pending)} 篇尚未完成比对）"
            return msg
        if res.incomplete:
            return res.note
        return ""

    def _persist(self, plan: ScanPlan) -> None:
        """把本次扫描的结论写进 `local_files`。

        ## 为什么 M1 就要做（而不是等 M2）

        不落库的后果不是"下次再解析一遍"这么简单：

          - 每次都显示"新增 95 篇"，看起来像在重复导入
          - `missing` 检测失效（本地表没有旧行，永远查不出"文件没了"）
          - 完全重复分组（§12.3）没有数据源

        这三件事都属于"扫描器自己的正确性"，与入库无关。

        ## status 怎么定

          - 解析成功 → `pending`（等 M2 真正入库后才变 `imported`）。
            这里**不能**写 `imported`——那会让下一次 diff 认为"已经在库里"，
            于是一篇从未入库的文档被永久跳过。
          - 解析失败 → `failed` + 可读原因（下次会重试）
          - 切不出块 → `skipped`
        """
        for item in plan.items:
            if item.action is FileAction.UNCHANGED:
                continue   # 未变的不重写（保留 imported_at 与其他状态）
            # 用户主动排除的文件：不重写状态，否则会被改回 pending 又被导入。
            # 判据看本地表里的原状态（上一行记录），不是 item.action。
            prev = item.previous
            if prev is not None and prev.status == "excluded":
                continue
            if item.parse_error:
                status, err = "failed", item.parse_error
            elif item.ingested:
                status, err = "imported", ""
            elif item.ingest_error:
                status, err = "failed", item.ingest_error
            elif item.action is FileAction.DUPLICATE:
                # ## 为什么 DUPLICATE 要单独一个状态
                #
                # 写成 `skipped` 会让下次扫描把它当成"上次跳过了、可以重试"
                # （`_classify` 里 RETRY 的条件包含 skipped）→ 于是每扫描一次
                # 就重新尝试导入一份明知重复的文件，白费解析时间。
                #
                # 写成 `imported` 又不对：它确实没进库，`doc_id` 是空的，
                # 文档列表里会出现一行点不动的记录。
                #
                # 所以用独立状态，并在 _classify 里让它与 `excluded` 一样
                # 直接返回 UNCHANGED（不重试）。
                status, err = "duplicate", f"与 {item.duplicate_of} 内容相同"
            elif item.chunk_count:
                status, err = "pending", ""
            else:
                status, err = "skipped", "没有可切分的文本"
            self._db.upsert_file(LocalFile(
                kb_id=self._kb_id,
                path=str(item.path),
                doc_id=item.doc_id,
                sha256=item.sha256,
                size=item.size,
                mtime=item.mtime,
                status=status,
                error=err,
                name_sent=item.path.name,
            ))

    def _mark_duplicate(self, item: ScanItem, plan: ScanPlan) -> None:
        """完全重复判定（方案 §12.2 / §12.3）。命中就把 action 改成 DUPLICATE。

        ## 判据是 sha256 相同，不需要用户判断

        内容一模一样，选哪一份入库对检索结果没有影响。所以自动只导一份，
        其余标 DUPLICATE——但要**告知用户**（"另有 N 个副本未入库"），
        否则他会问"我这份文件怎么搜不到"。

        ## 两个来源都要看

        1. **本批已见过这个 sha256**：用户把同一份文件复制到 5 个目录，
           一次扫描全扫到。
        2. **库里已有这个 sha256**（`imported` 状态）：分批扫描时会遇到——
           先扫了 A 目录，后来又加 B 目录，B 里那份与 A 相同。

        只看本批会漏掉第 2 种，只看库会漏掉"同批内 5 份都没有导过"的情况
        （那时库是空的，得靠第 1 条）。

        ## 为什么"主副本"优先选已入库的

        已入库的那份有 doc_id、能被检索到、来源卡片能跳回原文。保留它
        意味着**不需要任何重导入动作**。若反过来选新的、把旧的标重复，
        就得删掉已入库的文档再导一次——白费几小时向量化。

        同批内都没有 doc_id 时，选**最先遇到的那份**（遍历顺序稳定，
        结果可复现）。
        """
        if not item.sha256:
            return
        # 已入库/未变的条目不用判重：它们本来就不会再导入
        if item.action not in (FileAction.NEW, FileAction.CHANGED,
                               FileAction.RETRY):
            return

        canonical = self._seen_hashes.get(item.sha256)

        if canonical is None:
            # 库里已有同 sha256 且已入库 → 以库里那份为主副本
            existing_path = self._imported_by_hash.get(item.sha256)
            if existing_path:
                item.action = FileAction.DUPLICATE
                item.duplicate_of = existing_path
                self._seen_hashes[item.sha256] = existing_path
                return
            # 本批第一份，作为主副本
            self._seen_hashes[item.sha256] = str(item.path)
            return

        # 本批或库内已有同内容的一份 → 这份是副本
        item.action = FileAction.DUPLICATE
        item.duplicate_of = canonical

    def _classify(self, path: Path, digest: str, size: int, mtime: int) -> ScanItem:
        prev = self._db.get_file(self._kb_id, str(path))
        if prev is None:
            return ScanItem(path=path, action=FileAction.NEW,
                            sha256=digest, size=size, mtime=mtime)

        # 用户明确排除的文件：不管内容变没变都不导入。
        #
        # ## 为什么这条必须在这里（而不是靠"删了记录"）
        #
        # 早期实现里"从库里移除"就是删掉 local_files 行，而文件还在磁盘上
        # → 下次扫描按"新增"又导了一遍。用户看到"我删了它，它又回来了"。
        #
        # 判据是"用户说过不要它"。内容变了也不导——他可能只是编辑了这个
        # 他不想入库的文件。要恢复必须在设置里显式操作。
        if prev.status == "excluded":
            return ScanItem(path=path, action=FileAction.UNCHANGED,
                            sha256=digest, size=size, mtime=mtime,
                            previous=prev)

        # 上次判定为完全重复：不重试。
        #
        # ## 与 excluded 的区别
        #
        # excluded 是"用户说了不要它"，重复是"内容与另一份相同所以没导"。
        # 两者都不该重试，但**重复是可逆的**：如果主副本被删了，下次扫描
        # 时这份应当被重新考虑。
        #
        # 所以这里是"内容没变就不重试"，内容变了会落到下面的 CHANGED 分支
        # 正常处理——那时它已经不是重复了。
        if prev.status == "duplicate" and prev.sha256 == digest:
            return ScanItem(path=path, action=FileAction.UNCHANGED,
                            sha256=digest, size=size, mtime=mtime,
                            previous=prev)

        if prev.sha256 == digest:
            # 内容未变。但上次可能是失败/跳过状态——那种要重试，
            # 否则用户修好权限后永远等不到第二次机会。
            if prev.status in ("failed", "skipped", "locked", "too_long",
                               "cloud_only", "too_large"):
                return ScanItem(path=path, action=FileAction.RETRY,
                                sha256=digest, size=size, mtime=mtime,
                                doc_id=prev.doc_id, previous=prev)
            return ScanItem(path=path, action=FileAction.UNCHANGED,
                            sha256=digest, size=size, mtime=mtime,
                            doc_id=prev.doc_id, previous=prev)

        return ScanItem(path=path, action=FileAction.CHANGED,
                        sha256=digest, size=size, mtime=mtime,
                        doc_id=prev.doc_id, previous=prev)

    def _find_missing(self, seen: set[Path]) -> list[LocalFile]:
        """本地表里有、这次没扫到的文件。

        ## 为什么必须单独算而不是"扫到才算"

        用户移动了整个目录后，磁盘上所有旧路径都不存在。若只在遍历结果
        里比对，这些文件会永远停在 imported 状态，UI 显示"已入库"但
        点"打开原文"会失败。

        **不自动删除**（方案 §7.7.1）：移动与删除在磁盘上看是同一件事，
        自动删索引会让用户移动一次目录就丢掉几小时的解析成果。这里只
        标记，等用户决定。
        """
        seen_norm = {str(p).replace("\\", "/").lower() for p in seen}
        missing: list[LocalFile] = []
        for row in self._db.list_files(self._kb_id):
            if row.status in ("deleted", "missing"):
                continue
            if row.path.replace("\\", "/").lower() in seen_norm:
                continue
            if Path(row.path).exists():
                # 存在但没被扫到：说明它不在当前扫描根下（用户移除了目录），
                # 这不是 missing，是"不在范围内"。
                continue
            missing.append(row)
        return missing

    # ------------------------------------------------------------------ 上报

    def _counts(self, plan: ScanPlan) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in plan.items:
            out[item.action.value] = out.get(item.action.value, 0) + 1
        if plan.walk is not None:
            out["skipped"] = len(plan.walk.skipped)
            out["discovered"] = plan.walk.total_seen
        return out

    @staticmethod
    def _summary(plan: ScanPlan) -> str:
        parts: list[str] = []
        c = {}
        for item in plan.items:
            c[item.action.value] = c.get(item.action.value, 0) + 1
        if c.get("new"):
            parts.append(f"新增 {c['new']} 篇")
        if c.get("changed"):
            parts.append(f"变更 {c['changed']} 篇")
        if c.get("retry"):
            parts.append(f"重试 {c['retry']} 篇")
        if c.get("unchanged"):
            parts.append(f"未变 {c['unchanged']} 篇")
        # 完全重复：必须说出来（方案 §12.7）。不说的话用户会问
        # "我这份文件怎么搜不到"，而那正是他最需要知道的一条。
        if c.get("duplicate"):
            parts.append(f"内容重复跳过 {c['duplicate']} 篇")
        if plan.walk is not None and plan.walk.skipped:
            parts.append(f"跳过 {len(plan.walk.skipped)} 个文件")
        if plan.missing:
            parts.append(f"{len(plan.missing)} 篇源文件已不在")

        # 解析阶段的统计（parse_all 跑过才有意义）
        parsed = [i for i in plan.items if i.chunk_count or i.parse_error]
        if parsed:
            ok = [i for i in parsed if not i.parse_error]
            total_chunks = sum(i.chunk_count for i in ok)
            parts.append(f"已解析 {len(ok)} 篇共 {total_chunks} 块")
            bad = len(parsed) - len(ok)
            if bad:
                parts.append(f"{bad} 篇解析失败")

        return "，".join(parts) if parts else "没有需要处理的文件"

    def _emit(self, phase: ScanPhase, **kw) -> None:
        self._phase = phase
        if self._on_progress is None:
            return
        self._on_progress(ScanProgress(phase=phase, **kw))
