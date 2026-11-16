"""内容相似检测与"待确认"管理（方案 §12.4 / §12.5，D2/D3）。

## 这一层负责什么

Runtime 提供 `dedup_check` / `dedup_groups` / `dedup_diff` 三个只读端点。
本模块把它们包成"扫描流程能用的一个动作"，并处理三件容易做错的事：

  1. **`pending` 不能让结论不完整**（§12.4 的明确警告）
  2. **只对本次新入库的文档做**，不传空 doc_ids（§12.5）
  3. **待确认状态要持久化**（B8：关程序再打开仍在）

## 为什么相似检测必须调 `recompute=True`

SDK docstring 写得很清楚：快路径只对**已有指纹**的文档分组。刚导入的
文档可能还没有指纹，它们会出现在 `pending` 里——此时"没有重复"这个
结论是**不完整的**。

这个"不完整"极难被用户发现：他看到"没有相似文件"，于是相信了，
而实际上有 3 组相似文档正等着确认。所以宁可慢 10 秒（5000 篇的量级）
也要拿到完整结论。

## 为什么只对新入库的文档做

§12.5：增量扫描时只涉及改动的几个文件——而这正是用户最需要确认的
时刻（"我刚存了一版，是不是和旧版重复了"）。全库重算在 10 万块时
要几十秒，而其中 99% 的文档上次已经确认过了。

但 `doc_ids` 传空表示"该库全部文档"（服务端语义），所以**空列表必须
当错误处理**，不能顺手传下去。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from localkb.store.db import StateDB

__all__ = [
    "SimilarGroup",
    "SimilarResult",
    "check_similar",
    "format_diff",
    "load_pending_groups",
    "resolve_pending_group",
    "similar_default_choice",
    "SETTING_KEY",
]

# 设置项名（方案 §12.2.1）
SETTING_KEY = "similar_default_choice"

# 允许的取值。`none` 是默认，理由见方案 §12.2.1：
# 预选最新会让用户习惯性直接点确定，从而在"新版其实是误存的草稿"时
# 把旧版挤掉。哪个版本"应该进知识库"取决于内容差异，不是时间。
_VALID_CHOICES = ("none", "newest", "oldest")


@dataclass
class SimilarMember:
    """相似组里的一个成员。"""

    doc_id: str
    name: str
    # 汉明距离（越小越像）。Runtime 的 `DupMember.hamming`。
    hamming: int = 0
    # 本地补的信息（服务端不返回路径）
    path: str = ""
    mtime: int = 0
    # 是否当前选中的入库版本
    chosen: bool = False


@dataclass
class SimilarGroup:
    """一组内容相似的文档（同一份文档的不同版本）。"""

    group_id: str
    kind: str = "similar"
    members: list[SimilarMember] = field(default_factory=list)
    # 相似度范围（由 hamming 推算，用于文案"相似度 91%~96%"）
    similarity_min: int = 0
    similarity_max: int = 0

    def pick_default(self, strategy: str) -> str:
        """按策略挑一个默认选中项，返回 doc_id（空表示不预选）。

        ## 为什么 `newest` / `oldest` 按 mtime 排序而不是按 hamming

        hamming 只说明"有多像"，不说明"哪个是新的"。用户说"预选最新"，
        指的就是修改时间最新——按相似度排会给出完全不相关的结果。

        没有 mtime 的成员（本地找不到路径）排在最后：它们的"新旧"未知，
        不该被优先选中。
        """
        if strategy not in ("newest", "oldest") or not self.members:
            return ""
        with_time = [m for m in self.members if m.mtime]
        if not with_time:
            return ""
        with_time.sort(key=lambda m: m.mtime, reverse=(strategy == "newest"))
        return with_time[0].doc_id


@dataclass
class SimilarResult:
    """一次检测的结果。"""

    groups: list[SimilarGroup] = field(default_factory=list)
    # 还没算指纹的文档（结论不完整的原因）
    pending: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    # 是否因为 pending 而不完整
    incomplete: bool = False
    note: str = ""

    @property
    def has_groups(self) -> bool:
        return bool(self.groups)


def similar_default_choice(db: StateDB) -> str:
    """读设置项，非法值回退到 `none`。

    ## 为什么要校验

    这个值是用户可改的（设置页下拉框），也可能被手工改配置文件。传入
    未知值时若不校验，`pick_default` 会静默不预选——用户会以为设置没生效。
    回退到 `none` 并在设置页提示更诚实。
    """
    v = (db.get_setting(SETTING_KEY, "none") or "none").strip().lower()
    return v if v in _VALID_CHOICES else "none"


def check_similar(conn, db: StateDB, kb_id: str, doc_ids: list[str], *,
                  recompute: bool = True) -> SimilarResult:
    """对本次新入库的文档做相似检测，并把结果写入 `pending_dups`。

    ## 参数

    `doc_ids` 必须是**本次新入库的**文档。空列表直接返回空结果——
    这是刻意的（见模块 docstring：服务端把空列表当"全库"）。

    `recompute=True` 保证结论完整（含刚导入、还没有指纹的文档）。
    """
    from localai import DedupCheckRequest

    if not doc_ids:
        # ## 为什么这里必须拦住
        #
        # 服务端语义：`doc_ids` 为空 = "该库全部文档"。增量扫描时传空
        # 会让它去做全库重算（10 万块要几十秒），而 §12.5 明确要求
        # "只对本次新入库的文档做"。
        return SimilarResult(note="本次没有新入库的文档，跳过相似检测")

    try:
        req = DedupCheckRequest(doc_ids=list(doc_ids), recompute=recompute)
        res = conn.call(lambda c: c.dedup_check(kb_id, req))
    except Exception as e:  # noqa: BLE001
        # 相似检测失败不该让扫描失败：入库已经完成，关键词与语义检索都可用。
        # 这里只记说明，下次扫描会再试。
        return SimilarResult(note=f"相似检测未能完成：{type(e).__name__}: {e}")

    pending = list(getattr(res, "pending", []) or [])
    groups: list[SimilarGroup] = []
    for g in getattr(res, "groups", []) or []:
        sg = _convert_group(db, kb_id, g)
        if sg is not None:
            groups.append(sg)

    out = SimilarResult(
        groups=groups,
        pending=pending,
        elapsed_ms=int(getattr(res, "total_ms", 0) or 0),
        incomplete=bool(pending),
    )
    if pending:
        # 说不清"还有几篇没算"，用户会以为结论是完整的
        out.note = (f"还有 {len(pending)} 篇文档尚未生成指纹，"
                    "相似检测结论暂不完整。")
    if groups:
        _persist_groups(db, kb_id, groups)
    return out


def _convert_group(db: StateDB, kb_id: str, g) -> Optional[SimilarGroup]:
    """把 SDK 的 `DupGroup` 转成本地结构，并补上路径与 mtime。

    ## 为什么必须补路径

    `DupMember` 只有 `document_id` / `name` / `hamming`——服务端不知道
    文件在磁盘哪里（那是客户端 `local_files` 的信息）。而对话框要显示
    路径（用户靠路径区分"桌面那份"与"备份那份"），"在文件夹中显示"
    更需要它。
    """
    members: list[SimilarMember] = []
    for m in getattr(g, "members", []) or []:
        doc_id = getattr(m, "document_id", "") or ""
        if not doc_id:
            continue
        loc = db.get_file_by_doc_id(kb_id, doc_id)
        members.append(SimilarMember(
            doc_id=doc_id,
            name=getattr(m, "name", "") or "",
            hamming=int(getattr(m, "hamming", 0) or 0),
            path=loc.path if loc else "",
            mtime=loc.mtime if loc else 0,
        ))
    if len(members) < 2:
        return None
    ham = [m.hamming for m in members]
    # 汉明距离 → 相似度百分比。64 位指纹：相似度 = (64-h)/64
    return SimilarGroup(
        group_id=getattr(g, "id", "") or "",
        kind=getattr(g, "kind", "") or "similar",
        members=members,
        similarity_min=max(0, int((64 - max(ham)) / 64 * 100)),
        similarity_max=max(0, int((64 - min(ham)) / 64 * 100)),
    )


def _persist_groups(db: StateDB, kb_id: str, groups: list[SimilarGroup]) -> None:
    """把相似组写进 `pending_dups`（B8：关程序再打开仍在）。

    ## 为什么用 upsert 语义而不是"先删后插"

    用户可能已经对某组做过选择（`resolved=1`）。重新扫描时如果先删再插，
    那个选择就丢了，用户会看到"确认过的又回来了"。

    所以只对**未解决**的同组做更新；已解决的保留（除非分组内容变了）。
    """
    import time

    now = int(time.time())
    for g in groups:
        paths = json.dumps([
            {
                "doc_id": m.doc_id, "name": m.name, "path": m.path,
                "mtime": m.mtime, "hamming": m.hamming,
            }
            for m in g.members
        ], ensure_ascii=False)
        db.upsert_pending_group(
            kb_id=kb_id, group_id=g.group_id, kind=g.kind,
            paths=paths, now=now,
        )


def load_pending_groups(db: StateDB, kb_id: str) -> list[SimilarGroup]:
    """读出未确认的相似组（B8：启动时提示条用这个）。"""
    out: list[SimilarGroup] = []
    for row in db.list_pending_groups(kb_id):
        try:
            raw = json.loads(row["paths"] or "[]")
        except (ValueError, TypeError):
            continue
        members = [
            SimilarMember(
                doc_id=str(item.get("doc_id", "")),
                name=str(item.get("name", "")),
                path=str(item.get("path", "")),
                mtime=int(item.get("mtime", 0) or 0),
                hamming=int(item.get("hamming", 0) or 0),
            )
            for item in raw if isinstance(item, dict)
        ]
        if len(members) < 2:
            continue
        ham = [m.hamming for m in members]
        out.append(SimilarGroup(
            group_id=row["group_id"], kind=row["kind"], members=members,
            similarity_min=max(0, int((64 - max(ham)) / 64 * 100)),
            similarity_max=max(0, int((64 - min(ham)) / 64 * 100)),
        ))
    return out


def resolve_pending_group(db: StateDB, kb_id: str, group_id: str,
                          chosen_path: str) -> None:
    """记下用户的选择（`resolved=1`）。"""
    db.resolve_pending_group(kb_id, group_id, chosen_path)


def format_diff(conn, kb_id: str, doc_a: str, doc_b: str) -> str:
    """把 `dedup_diff` 的结果变成一段"人话"差异摘要。

    ## 为什么不用 unified diff

    用户判断"哪个版本该入库"时关心的是"多了几段、改了哪几处"，
    而不是行级的 `+`/`-`。段落级摘要足够，且更短。

    ## 为什么预览要截断

    一段可能几百字。对话框里显示三段全文就占满屏幕，反而看不清
    "有几处改动"这个更重要的信息。截到 60 字，需要细节的可以打开文件。
    """
    try:
        d = conn.call(lambda c: c.dedup_diff(kb_id, doc_a, doc_b))
    except Exception as e:  # noqa: BLE001
        return f"（差异信息获取失败：{type(e).__name__}）"

    added = list(getattr(d, "added", []) or [])
    removed = list(getattr(d, "removed", []) or [])
    changed = list(getattr(d, "changed", []) or [])
    unchanged = int(getattr(d, "unchanged", 0) or 0)

    if not (added or removed or changed):
        return f"内容相同（{unchanged} 段一致）。"

    lines: list[str] = []
    if added:
        lines.append(f"· 新增 {len(added)} 段" + _preview_list(added))
    if removed:
        lines.append(f"· 少 {len(removed)} 段" + _preview_list(removed))
    if changed:
        lines.append(f"· 修改 {len(changed)} 段" + _changed_preview(changed))
    lines.append(f"· 未变 {unchanged} 段")
    if getattr(d, "truncated", False):
        lines.append("（差异过多，只显示了前面一部分）")
    return "\n".join(lines)


def _preview_list(items, limit: int = 2, width: int = 60) -> str:
    """取前几段的预览，拼成"例如：…"。"""
    previews = [str(getattr(i, "preview", "") or "").strip() for i in items]
    previews = [p for p in previews if p]
    if not previews:
        return ""
    shown = "；".join(p[:width] for p in previews[:limit])
    more = "" if len(previews) <= limit else "…"
    return f"（例如：{shown}{more}）"


def _changed_preview(items, limit: int = 1, width: int = 40) -> str:
    """改动的"哪一段、改成什么"。"""
    out = []
    for it in items[:limit]:
        after = str(getattr(it, "after", "") or "").strip()[:width]
        ordinal = int(getattr(it, "ordinal", 0) or 0)
        if after:
            out.append(f"第 {ordinal} 段改为「{after}」")
    more = "" if len(items) <= limit else "…"
    return f"（{'；'.join(out)}{more}）" if out else ""
