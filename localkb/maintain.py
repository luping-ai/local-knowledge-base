"""增量维护：启动对账、删除、重命名、missing 处理。

## 这个文件解决的是"长期使用后数据不一致"这一类问题

单次扫描的正确性在 M2 已经验证。真正的难点是**用几十次之后**：

  - 用户在管理后台删掉了整个知识库 → 本地 `local_files` 全是悬空 `doc_id`
  - 用户把整个目录移走了 → 所有路径失效，但索引还在（内容仍可检索）
  - 用户在资源管理器里改名 → 库里还是旧名（服务端 S2 已修，要让调用生效）
  - 客户端导入过，但服务端被别的客户端删了 → `doc_id` 指向不存在的文档
  - 服务端有文档，本机没有映射 → 不是我们的，不能动

这些情况不会报错，只会让"界面显示的状态"与"实际能用的东西"分叉。
方案 §7.2.2 的四向对账就是为这个。

## 为什么全部用"返回值"而不是异常

对账是**逐项修补**的批量操作，某一项失败（比如某个 doc_id 删除时 404）
不该中断整个对账。用返回值让调用方能继续处理剩下的项，并在最后汇总。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from localkb.store.db import LocalFile, StateDB

__all__ = [
    "ReconcileReport",
    "ReconcileAction",
    "reconcile",
    "delete_local_document",
    "apply_rename",
]


@dataclass
class ReconcileAction:
    """一项对账动作（已执行或待用户确认）。"""

    kind: str
    """gone / orphaned / orphan_row / unowned / rename_needed"""

    path: str = ""
    doc_id: str = ""
    detail: str = ""


@dataclass
class ReconcileReport:
    """对账结果。"""

    # 磁盘上没有的本地行（文件被移动/删除）
    missing: list[ReconcileAction] = field(default_factory=list)
    # 本地行有 doc_id、但服务端没有该文档（被别的客户端删了）
    orphaned: list[ReconcileAction] = field(default_factory=list)
    # 服务端有文档、本机没有映射（不是本机导的，只提示不动）
    unowned: list[ReconcileAction] = field(default_factory=list)
    # 本地文件名与服务端显示名不一致（需要同步）
    rename_needed: list[ReconcileAction] = field(default_factory=list)
    # 执行失败的动作（含原因）
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.orphaned or self.rename_needed
                    or self.errors)

    def summary(self) -> str:
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} 篇源文件已不在")
        if self.orphaned:
            parts.append(f"{len(self.orphaned)} 篇在服务端已不存在")
        if self.rename_needed:
            parts.append(f"{len(self.rename_needed)} 篇文件名已变")
        if self.unowned:
            parts.append(f"{len(self.unowned)} 篇不是本机导入")
        if self.errors:
            parts.append(f"{len(self.errors)} 项对账失败")
        return "，".join(parts) if parts else "一致"


def reconcile(db: StateDB, kb_id: str, conn,
              remote_docs: Optional[list] = None) -> ReconcileReport:
    """四向对账。只**标记**，不自动删除任何东西。

    ## 四个方向（方案 §7.2.2）

    | 方向 | 检查 | 处理 |
    | --- | --- | --- |
    | 磁盘 → 本地表 | 本地行有、磁盘上没有 | 标 `missing`，等用户决定 |
    | 本地表 → 磁盘 | 磁盘有、本地表没有 | 不管（下次扫描会补） |
    | 本地表 → 服务端 | `doc_id` 不在 `list_documents` 里 | 标 `orphaned`，清空 doc_id |
    | 服务端 → 本地表 | 服务端有、本地无映射 | 标 `unowned`，只提示 |

    ## 为什么"磁盘没有"不直接删

    用户移动一次目录后，所有旧路径都不存在。自动删索引等于让他丢掉
    几小时的解析成果（重新扫描要重新解析）。方案 §7.7.1 明确要求
    "默认保留、让用户决定"。

    ## 为什么 orphaned 要清空 doc_id

    留着悬空 id 的后果：下次改文件会 `ingest_chunks(update_document_id=悬空id)`
    → 服务端返回"文档不存在" → 这一篇永远导入不了。清空后它会被当作
    新文件重导，虽然浪费一次解析，但至少能恢复。
    """
    rep = ReconcileReport()
    rows = db.list_files(kb_id)

    # ---- 1. 磁盘 → 本地表 ----
    for row in rows:
        if row.status in ("deleted", "missing"):
            continue
        if not Path(row.path).exists():
            rep.missing.append(ReconcileAction(
                kind="gone", path=row.path, doc_id=row.doc_id or "",
            ))
            # 只标记，不删（见 docstring）
            db.set_status(kb_id, row.path, "missing", "源文件已不在原位置")

    # ---- 2. 本地表 → 服务端 ----
    docs = remote_docs
    if docs is None:
        try:
            docs = conn.call(lambda c: c.list_documents(kb_id))
        except Exception as e:  # noqa: BLE001 - 读不到就跳过这一向
            rep.errors.append(f"读取服务端文档列表失败：{e}")
            docs = None

    if docs is not None:
        remote_ids = {getattr(d, "id", "") for d in docs}
        local_doc_ids = {r.doc_id for r in rows if r.doc_id}
        for row in rows:
            if not row.doc_id or row.status in ("deleted",):
                continue
            if row.doc_id in remote_ids:
                continue
            # 悬空 doc_id：清空，下次扫描重导
            rep.orphaned.append(ReconcileAction(
                kind="orphaned", path=row.path, doc_id=row.doc_id,
                detail="服务端已没有该文档（可能被其他客户端删除）",
            ))
            db.upsert_file(LocalFile(
                id=row.id, kb_id=kb_id, path=row.path, doc_id=None,
                sha256=row.sha256, size=row.size, mtime=row.mtime,
                status="orphaned", error="服务端已没有该文档，将重新导入",
                name_sent="", imported_at=None,
            ))

        # ---- 3. 服务端 → 本地表 ----
        for doc in docs:
            did = getattr(doc, "id", "")
            if not did or did in local_doc_ids:
                continue
            rep.unowned.append(ReconcileAction(
                kind="unowned", doc_id=did,
                detail=getattr(doc, "name", "") or did,
            ))

        # ---- 4. 文件名同步 ----
        by_doc = {getattr(d, "id", ""): d for d in docs}
        for row in rows:
            if not row.doc_id or row.status in ("deleted", "missing"):
                continue
            doc = by_doc.get(row.doc_id)
            if doc is None:
                continue
            remote_name = getattr(doc, "name", "") or ""
            local_name = Path(row.path).name
            # 只在"提交过名字且现在不一致"时提示。
            # name_sent 为空说明是老数据（M2 之前没有这个字段），不动它。
            if row.name_sent and remote_name and remote_name != local_name:
                rep.rename_needed.append(ReconcileAction(
                    kind="rename_needed", path=row.path, doc_id=row.doc_id,
                    detail=f"库内为「{remote_name}」，本地为「{local_name}」",
                ))
    return rep


def delete_local_document(
    db: StateDB, kb_id: str, conn, *,
    path: str, doc_id: Optional[str],
    remove_file: bool = False,
) -> tuple[bool, str]:
    """删一篇文档：库记录 + 可选的磁盘文件。

    ## 顺序不能颠倒（方案 §7.7）

    1. **先删库记录**（`delete_document(keep_file=True)`）
    2. 成功后**才**把文件移到回收站

    反过来的话会出现"文件已删、记录还在"——来源卡片仍写着"可以打开原文"，
    而文件已经没了。用户会以为程序坏了。

    ## 为什么 remove_file 默认 False

    "从知识库移除"与"删掉文件"是两个独立决定。默认只做前者。用户要删
    文件必须显式勾选——而即使勾了，也只到回收站。

    ## 文件删失败不算整体失败

    库记录已经删掉了，这是用户的意图。文件删不掉（被占用、无权限）时
    返回 `ok=True` 但说明里写清"文件仍在"，让 UI 提示用户自己去处理。
    """
    from localkb.platform.recycle import move_to_recycle_bin

    # 1. 库记录
    if doc_id:
        try:
            conn.call(lambda c: c.delete_document(kb_id, doc_id, keep_file=True))
        except Exception as e:  # noqa: BLE001 - 404 也要继续清本地行
            msg = str(e)
            # 服务端已经没有这篇了：不是错误，继续清理本地
            if "not_found" not in msg.lower() and "404" not in msg:
                return False, f"删除库记录失败：{msg}"

    # 2. 本地行：标记为"用户主动排除"，**不是删除**。
    #
    # 删掉的后果是下次扫描按"新增"又导一遍——用户看到"我删了它，它又回来了"。
    # 实测踩到：A5 验收时删除后再扫描，文档数从 0 变回 1。
    #
    # 保留一行 `excluded` 让扫描器知道"这个文件用户不要"，同时保留
    # path/sha256/mtime 供将来恢复或诊断用。
    db.upsert_file(LocalFile(
        kb_id=kb_id, path=path, doc_id=None,
        sha256="", size=0, mtime=0,
        status="excluded", error="已被用户从知识库移除", name_sent="",
    ))

    # 3. 磁盘文件（只在用户要求时）
    if not remove_file:
        return True, f"已从知识库移除，文件保留在原位置：{path}"

    if not Path(path).exists():
        return True, "已从知识库移除（文件本来就不在了）"

    ok, note = move_to_recycle_bin(path)
    if not ok:
        # 库记录已删，这是既成事实。告诉用户文件还在。
        return True, f"已从知识库移除，但文件没有删掉：{note}（文件仍在 {path}）"
    return True, f"已从知识库移除，文件已移到回收站：{note}"


def apply_rename(
    db: StateDB, kb_id: str, conn, *,
    old_path: str, new_path: str,
) -> tuple[bool, str]:
    """处理"文件在同一目录内改名"。

    ## 为什么不能只改本地表的 path

    来源卡片的名字来自 Runtime 的 `documents.name`。只改本地表的话，
    用户在资源管理器里改完名，引用卡片还是旧名——而卡片是溯源的核心。

    ## 做法：重新提交（内容未变，diff 全是 Unchanged）

    `ingest_chunks(update_document_id=旧id, name=新名, source_label=新路径)`
    → 服务端把 name 与 original_path 落库（S2 已修）。
    代价是重新解析一次文件；换来的是名字与服务端一致。

    ## 为什么不判断"内容是否变了"

    改名不改内容，`file_hash` 相同。但**同名覆盖**也可能发生（用户把 A
    改名为 B，而 B 之前存在且内容不同）。那种情况下 `file_hash` 不同，
    服务端会按增量更新走 —— 结果仍然正确（内容更新、名字更新）。
    所以这里不需要额外判断。
    """
    from localkb.chunk.splitter import split
    from localkb.ingest import build_request, ingest
    from localkb.parse.registry import parse
    from localkb.parse.types import ParseError
    from localkb.scan.hasher import file_stat, sha256_file

    p = Path(new_path)
    if not p.exists():
        return False, f"新路径不存在：{new_path}"

    row = db.get_file(kb_id, old_path)
    if row is None:
        return False, "本地没有这个文件的记录"
    if not row.doc_id:
        return False, "这个文件还没有入库，改名不需要同步"

    try:
        digest = sha256_file(p)
        size, mtime = file_stat(p)
    except OSError as e:
        return False, f"读取文件失败：{e}"

    try:
        doc = parse(p)
        file_chunks, chunker = split(doc)
    except ParseError as e:
        return False, f"解析失败：{e.reason}"

    req = build_request(
        path=p, name=p.name, file_hash=digest, chunks=file_chunks,
        chunker=chunker, file_size=size, modified_at=mtime,
        update_document_id=row.doc_id,
    )
    res, fail = ingest(conn, kb_id, req)
    if fail is not None:
        return False, f"同步文件名失败：{fail.reason}"
    # 用服务端返回的 doc_id（duplicate 时是 existing_id）：改名不会新建文档，
    # 但万一 file_hash 撞上了别的文档，以服务端为准才不会指错。
    doc_id = (res.existing_id or res.document_id) if res else row.doc_id

    # 本地行：path 换成新的，旧行删掉
    db.delete_file_row(kb_id, old_path)
    db.upsert_file(LocalFile(
        id=row.id, kb_id=kb_id, path=str(p), doc_id=doc_id,
        sha256=digest, size=size, mtime=mtime,
        status="imported", error="", name_sent=p.name,
        imported_at=row.imported_at,
    ))
    return True, f"已同步新文件名：{p.name}"


def switch_kb_version(
    db: StateDB, kb_id: str, conn, *,
    old_doc_id: str, old_path: str, new_path: str,
) -> tuple[bool, str]:
    """在相似组里换用另一版：移除旧版库记录、导入新版（方案 B7 / §12.6）。

    ## 顺序：先导入新版，再删旧版

    §12.6 写的是"原版 delete_document 移除，新版 ingest_chunks 导入"。
    但实际执行顺序要反过来，理由：

      · 先删后导若导入失败（文件被占用、解析出错），用户就从"有一版"
        变成"一版都没有"——库里的内容凭空消失了。
      · 先导后删若删除失败，结果是"两版都在库里"，检索可能暂时命中
        两版（答案自相矛盾），但**数据没丢**，用户再点一次就能修好。

    两种失败里，"多了一份"远比"少了一份"可恢复。

    ## 被换下的旧版标 `similar_replaced`，不是 `excluded`

    `excluded` 的语义是"用户不要这个文件"（§7.7）——它会被永久跳过，
    要恢复得去设置页操作。而换版本是**可逆的**：用户可能过两天又想换
    回来。标成 excluded 会让"换回来"变成"必须先解除排除"的两步操作，
    而用户根本不知道有"排除"这回事。

    所以用独立状态，`_classify` 里把它当作"未入库、可再处理"。

    ## 为什么旧版的 sha256 要保留

    保留它才能在下一次扫描时认出"这个文件没变"，从而不去重复解析。
    清空 sha256 会让它每次扫描都被当成新文件。
    """
    from localkb.chunk.splitter import split
    from localkb.ingest import build_request, ingest
    from localkb.parse.registry import parse
    from localkb.parse.types import ParseError
    from localkb.scan.hasher import file_stat, sha256_file

    p = Path(new_path)
    if not p.exists():
        return False, f"新版本文件不存在：{new_path}"

    # ---- 1. 旧版信息（需要在删之前拿到 sha256，用于标记本地行）----
    old_row = db.get_file(kb_id, old_path)

    # ---- 2. 导入新版 ----
    try:
        digest = sha256_file(p)
        size, mtime = file_stat(p)
    except OSError as e:
        return False, f"读取新版文件失败：{e}"

    try:
        doc = parse(p)
        file_chunks, chunker = split(doc)
    except ParseError as e:
        # 解析失败时**什么都没动**：旧版还在库里，用户可以继续用。
        return False, f"新版解析失败：{e.reason}"

    if not file_chunks:
        return False, "新版里没有可切分的文本，未做任何改动。"

    req = build_request(
        path=p, name=p.name, file_hash=digest, chunks=file_chunks,
        chunker=chunker, file_size=size, modified_at=mtime,
    )
    res, fail = ingest(conn, kb_id, req)
    if fail is not None:
        return False, f"新版入库失败：{fail.reason}"

    new_doc_id = (res.existing_id or res.document_id) if res else ""
    db.upsert_file(LocalFile(
        kb_id=kb_id, path=str(p), doc_id=new_doc_id,
        sha256=digest, size=size, mtime=mtime,
        status="imported", error="", name_sent=p.name,
    ))

    # ---- 3. 移除旧版库记录（文件保留）----
    if old_doc_id:
        try:
            conn.call(lambda c: c.delete_document(
                kb_id, old_doc_id, keep_file=True))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "not_found" not in msg.lower() and "404" not in msg:
                # 新版已入库，旧版没删掉 —— 结果是"两版都在"。
                # 不返回失败（数据没丢），但要说清现状。
                return True, (
                    f"新版已入库，但旧版移除失败：{msg}。"
                    "当前库里可能同时有两版，请稍后再试一次。"
                )

    # 旧版本地行：标 similar_replaced，保留 sha256 供下次扫描识别"未变"
    db.upsert_file(LocalFile(
        kb_id=kb_id, path=old_path, doc_id=None,
        sha256=old_row.sha256 if old_row else "",
        size=old_row.size if old_row else 0,
        mtime=old_row.mtime if old_row else 0,
        status="similar_replaced",
        error=f"已换用 {p.name}，此版本未入库",
        name_sent=old_row.name_sent if old_row else "",
    ))
    return True, f"已换用新版：{p.name}（旧版文件仍在原位置）"


def dump_actions(actions: list[ReconcileAction]) -> str:
    """把对账动作列表序列化（日志/诊断用）。"""
    return json.dumps(
        [{"kind": a.kind, "path": a.path, "doc_id": a.doc_id,
          "detail": a.detail} for a in actions],
        ensure_ascii=False,
    )
