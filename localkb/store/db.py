"""`state.db`：桌面端本地事实的唯一存储。

## 与 Runtime 的 `hub.db` 严格分工

    桌面端 state.db  ← 磁盘上有什么、导过没有、用户在等什么确认
    Runtime hub.db   ← 已入库内容的唯一真相（块、向量、索引）

字段集合互斥：本库**不存块文本、不存向量**。重复存一份必然有一份是旧的，
而"哪份是旧的"在下一次用户提问时才会以"引用了不存在的内容"暴露出来。

## 迁移策略

用 `pragma user_version` 做版本号，每个版本一段 DDL。不用 ORM：这个库只有
六七张表，SQL 直接写反而更容易看清"哪张表属于谁"。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = ["StateDB", "LocalFile", "ScanRoot", "open_state_db"]

# 当前 schema 版本。加字段/加表时 +1，并在 _MIGRATIONS 里追加一段。
SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: r"""
    -- 扫描根目录
    --
    -- 唯一约束是 (kb_id, path_key) 而不是 path：同一目录可能要进两个库
    -- （"工作"与"2025年度"）。允许跨库共享是刻意的，但建库/加目录时
    -- 若与已有库重叠，UI 要提示"这些文件会在新库里再存一份"。
    --
    -- path 存原样（用户选的目录长什么样就显示什么样），path_key 存
    -- 归一化形式（唯一约束用）。理由同 local_files。
    create table if not exists scan_roots (
      id          integer primary key autoincrement,
      kb_id       text    not null,
      path        text    not null,
      path_key    text    not null,
      recursive   integer not null default 1,
      enabled     integer not null default 1,
      created_at  integer not null,
      unique(kb_id, path_key)
    );
    create index if not exists idx_roots_path on scan_roots(path_key);

    -- 排除规则（隐私承诺的实现点）
    create table if not exists excludes (
      id          integer primary key autoincrement,
      pattern     text    not null,        -- 绝对路径前缀或 glob
      kind        text    not null,        -- dir / glob
      is_default  integer not null default 0
    );

    -- 本地文件事实（唯一目的：判断"要不要重新导入"）
    --
    -- status 取值：
    --   pending / importing / imported / failed / skipped / deleted
    --   locked / too_long / cloud_only / too_large / orphaned / missing
    --   **excluded**
    --
    -- excluded 是"用户主动从库里移除了，但文件还在"。
    --
    -- ## 为什么必须有这个状态（实测踩到的缺陷）
    --
    -- 用户点"从知识库移除"后，`delete_document` 删掉了库记录、`local_files`
    -- 删掉了本地行。但**文件还在磁盘上**，所以下次扫描按"新增"又导了一遍。
    --
    -- 用户看到的现象是"我删了它，它又回来了"——而他会以为删除功能坏了。
    --
    -- 所以"移除"不能只删记录，必须留一条"这个文件我不要"的标记。
    -- 文件被修改时（sha256 变化）是否重新导入？**不导**——用户明确说过
    -- 不要它。要恢复只能显式操作（设置页的"已排除文件"列表里恢复）。
    --
    -- error 存可读原因（不是异常栈）——UI 直接展示给用户。
    -- name_sent 记上次提交给服务端的显示名，用于检测"本地改名了但
    -- 服务端还不知道"（Runtime 侧 name 落库已修，见服务端待办 S2）。
    --
    -- ## 为什么 path 与 path_key 两列
    --
    -- Windows 路径大小写不敏感，但它**区分显示**：用户在资源管理器里看到
    -- 的是 `D:\工作\合同.pdf`。只存小写会让 UI 显示 `d:/工作/合同.pdf`，
    -- 用户会怀疑"这是不是我的那个文件"。
    --
    -- 所以：path 存原样（展示用），path_key 存归一化形式（唯一约束与
    -- 比对用）。只用一个字段做不到两件事。
    create table if not exists local_files (
      id          integer primary key autoincrement,
      kb_id       text    not null,
      path        text    not null,        -- 原样，用于展示与打开
      path_key    text    not null,        -- 归一化（小写 + 正斜杠），用于比对
      doc_id      text,                    -- null 表示尚未成功导入
      sha256      text    not null default '',
      size        integer not null default 0,
      mtime       integer not null default 0,
      status      text    not null default 'pending',
      error       text    not null default '',
      name_sent   text    not null default '',
      imported_at integer,
      unique(kb_id, path_key)
    );
    create index if not exists idx_lf_doc  on local_files(doc_id);
    create index if not exists idx_lf_stat on local_files(kb_id, status);
    create index if not exists idx_lf_hash on local_files(kb_id, sha256);

    -- 待用户确认的相似组
    create table if not exists pending_dups (
      id          integer primary key autoincrement,
      kb_id       text    not null,
      group_id    text    not null,
      kind        text    not null,        -- exact / similar
      paths       text    not null,        -- JSON: [{path, sha256, mtime, size, doc_id}]
      resolved    integer not null default 0,
      chosen_path text    not null default '',
      created_at  integer not null
    );
    create index if not exists idx_pending on pending_dups(kb_id, resolved);

    -- 设置（键值）
    create table if not exists settings (
      key   text primary key,
      value text not null
    );

    -- 对话历史（不进 Runtime：会话是产品概念，不是运行时概念）
    --
    -- kb_mode 用三值而不是"kb_ids 为空即不用"：空数组在 /search 里是
    -- 错误、在 /chat 里是"不查库"，两边语义不同。显式记下用户当时的
    -- 意图，才能还原勾选状态。
    create table if not exists conversations (
      id          text    primary key,
      title       text    not null default '',
      kb_mode     text    not null default 'none',   -- none / selected / all
      kb_ids      text    not null default '[]',
      created_at  integer not null,
      updated_at  integer not null
    );

    create table if not exists messages (
      id              integer primary key autoincrement,
      conversation_id text    not null references conversations(id) on delete cascade,
      role            text    not null,
      content         text    not null,
      sources         text    not null default '[]',
      kb_mode         text    not null default 'none',
      kb_ids          text    not null default '[]',
      all_bases       integer not null default 0,
      base_count      integer not null default 0,
      model           text    not null default '',
      degraded        integer not null default 0,
      used_chunks     integer not null default 0,
      warnings        text    not null default '[]',
      created_at      integer not null
    );
    create index if not exists idx_msg_conv on messages(conversation_id, id);
    """,
}


@dataclass
class ScanRoot:
    id: int
    kb_id: str
    path: str
    recursive: bool
    enabled: bool
    path_key: str = ""


@dataclass
class LocalFile:
    """`local_files` 的一行。

    字段顺序与表一致，方便按位置构造。都用默认值是为了让测试只关心
    它真正断言的那几个字段。
    """

    id: int = 0
    kb_id: str = ""
    # path 是原样路径（展示、打开用）；path_key 是归一化形式（比对用）。
    # 从库里读出来时两个都有；构造时只给 path 也行，写入时会自动算 key。
    path: str = ""
    path_key: str = ""
    doc_id: Optional[str] = None
    sha256: str = ""
    size: int = 0
    mtime: int = 0
    status: str = "pending"
    error: str = ""
    name_sent: str = ""
    imported_at: Optional[int] = None


class StateDB:
    """`state.db` 的薄封装。

    ## 为什么用 WAL

    扫描线程持续写（每篇一次 upsert），UI 线程持续读（文档列表、进度）。
    默认的 rollback journal 会让读阻塞写、写阻塞读，表现为"扫描时界面卡住"。
    WAL 下读写互不阻塞。

    ## 为什么 `check_same_thread=False`

    连接会被 `ScanWorker` / `WatchWorker` 两个 QThread 共用。安全性靠两点：
    ① WAL + 短事务；② `busy_timeout` 让并发写排队而不是立刻抛
    `database is locked`。Qt 侧不要求同一连接跨线程，但这里刻意允许——
    否则每个线程一个连接会让事务跨线程失效（"写完没提交，另一个线程看不到"）。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("pragma journal_mode=wal")
        self._conn.execute("pragma synchronous=normal")
        # 5 秒：足够覆盖一次"扫描批次写库 + UI 一次查询"的竞争窗口，
        # 又不至于让用户觉得卡死（默认 5 秒其实一样，写明是为了留下意图）。
        self._conn.execute("pragma busy_timeout=5000")
        # 外键必须显式开启：SQLite 默认**不**强制执行 FOREIGN KEY 约束，
        # 所以 `on delete cascade` 在没有这一行时完全无效。
        #
        # 实测症状：删除会话后 `messages` 里的行仍然在（孤儿数据），
        # 且会随使用不断累积。这类"删除看起来成功但数据还在"的缺陷
        # 不会报错，只会让库文件慢慢变大。
        self._conn.execute("pragma foreign_keys=on")
        self._migrate()

    # ------------------------------------------------------------------ 迁移

    def _migrate(self) -> None:
        cur = self._conn.execute("pragma user_version")
        ver = cur.fetchone()[0]
        if ver >= SCHEMA_VERSION:
            return
        for v in range(ver + 1, SCHEMA_VERSION + 1):
            ddl = _MIGRATIONS.get(v)
            if ddl is None:
                raise RuntimeError(f"缺少 state.db 版本 {v} 的迁移脚本")
            self._conn.executescript(ddl)
            self._conn.execute(f"pragma user_version={v}")

    @property
    def version(self) -> int:
        return self._conn.execute("pragma user_version").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """显式事务。异常回滚。

        `isolation_level=None` 意味着 sqlite3 不会自动开事务，所以这里
        手动 begin/commit——这也是为什么可以放心用 `executescript`（它
        会隐式提交，在自动事务模式下会打断迁移）。
        """
        self._conn.execute("begin")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("rollback")
            raise
        else:
            self._conn.execute("commit")

    # ------------------------------------------------------------ scan_roots

    def add_root(self, kb_id: str, path: str, *, recursive: bool = True,
                 now: int = 0) -> int:
        """加一个扫描根。已存在则返回旧 id（不报错）。

        ## 为什么重复添加不报错

        用户可能点了两次"添加目录"，或因重启重放了一次操作。抛
        IntegrityError 会让调用方必须写 try/except，而它想表达的只是
        "确保这个目录在库里"。

        ## 为什么存原样路径

        `path` 是用户选的目录，UI 要照原样显示（`D:\\工作` 不是 `d:/工作`）。
        归一化只用于唯一约束——所以这里在比对时用 `_norm_path`，存的是原值。
        """
        import time

        now = now or int(time.time())
        key = _norm_path(path)
        with self.tx() as c:
            c.execute(
                "insert or ignore into scan_roots(kb_id, path, path_key,"
                " recursive, created_at) values (?,?,?,?,?)",
                (kb_id, path, key, 1 if recursive else 0, now),
            )
            row = c.execute(
                "select id from scan_roots where kb_id=? and path_key=?",
                (kb_id, key),
            ).fetchone()
        return int(row["id"])

    def list_roots(self, kb_id: str, *, enabled_only: bool = True) -> list[ScanRoot]:
        sql = ("select id, kb_id, path, path_key, recursive, enabled"
               " from scan_roots where kb_id=?")
        args: list[object] = [kb_id]
        if enabled_only:
            sql += " and enabled=1"
        sql += " order by id"
        return [
            ScanRoot(
                id=r["id"], kb_id=r["kb_id"], path=r["path"],
                path_key=r["path_key"],
                recursive=bool(r["recursive"]), enabled=bool(r["enabled"]),
            )
            for r in self._conn.execute(sql, args)
        ]

    def remove_root(self, root_id: int) -> None:
        with self.tx() as c:
            c.execute("delete from scan_roots where id=?", (root_id,))

    # ------------------------------------------------------------ local_files

    def get_file(self, kb_id: str, path: str) -> Optional[LocalFile]:
        row = self._conn.execute(
            "select * from local_files where kb_id=? and path_key=?",
            (kb_id, _norm_path(path)),
        ).fetchone()
        return _row_to_file(row) if row else None

    def upsert_file(self, f: LocalFile) -> None:
        """按 (kb_id, path_key) 插入或更新。

        `imported_at` 只在传入非 None 时更新——"扫描发现文件变了"不该
        抹掉上次成功导入的时间（那是"这个文件最后一次进库是什么时候"）。
        """
        key = f.path_key or _norm_path(f.path)
        with self.tx() as c:
            c.execute(
                """
                insert into local_files(kb_id, path, path_key, doc_id, sha256,
                                        size, mtime, status, error, name_sent,
                                        imported_at)
                values (?,?,?,?,?,?,?,?,?,?,?)
                on conflict(kb_id, path_key) do update set
                    path        = excluded.path,
                    doc_id      = excluded.doc_id,
                    sha256      = excluded.sha256,
                    size        = excluded.size,
                    mtime       = excluded.mtime,
                    status      = excluded.status,
                    error       = excluded.error,
                    name_sent   = excluded.name_sent,
                    imported_at = coalesce(excluded.imported_at, local_files.imported_at)
                """,
                (f.kb_id, f.path, key, f.doc_id, f.sha256, f.size, f.mtime,
                 f.status, f.error, f.name_sent, f.imported_at),
            )

    def set_status(self, kb_id: str, path: str, status: str, error: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "update local_files set status=?, error=? where kb_id=? and path_key=?",
                (status, error, kb_id, _norm_path(path)),
            )

    def list_files(self, kb_id: str, *, statuses: Optional[list[str]] = None
                   ) -> list[LocalFile]:
        sql = "select * from local_files where kb_id=?"
        args: list[object] = [kb_id]
        if statuses:
            sql += " and status in (" + ",".join("?" * len(statuses)) + ")"
            args.extend(statuses)
        sql += " order by path"
        return [_row_to_file(r) for r in self._conn.execute(sql, args)]

    def count_by_status(self, kb_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "select status, count(*) as n from local_files where kb_id=? group by status",
            (kb_id,),
        )
        return {r["status"]: r["n"] for r in rows}

    def delete_file_row(self, kb_id: str, path: str) -> None:
        with self.tx() as c:
            c.execute("delete from local_files where kb_id=? and path_key=?",
                      (kb_id, _norm_path(path)))

    def get_file_by_doc_id(self, kb_id: str, doc_id: str) -> Optional[LocalFile]:
        """按 doc_id 查本地行（来源卡片要 doc_id → path）。"""
        if not doc_id:
            return None
        row = self._conn.execute(
            "select * from local_files where kb_id=? and doc_id=?",
            (kb_id, doc_id),
        ).fetchone()
        return _row_to_file(row) if row else None

    def find_path_by_doc_id(self, doc_id: str) -> str:
        """跨库查 doc_id 对应的本地路径。不存在返回空串。

        ## 为什么可能不存在

        文档可能是别的客户端（管理后台、CLI、ragbench）导入的，本机没有
        它的路径记录。这时返回空串，让来源卡片显示"文件已不在"——
        这比假装知道路径要诚实。
        """
        if not doc_id:
            return ""
        row = self._conn.execute(
            "select path from local_files where doc_id=? limit 1", (doc_id,)
        ).fetchone()
        return str(row["path"]) if row else ""

    def find_duplicate_hashes(self, kb_id: str, min_count: int = 2) -> list[tuple[str, int]]:
        """完全重复分组（§12.3）。返回 [(sha256, 文件数)]。

        为什么这条查询在客户端：Runtime 在同库内按 file_hash 去重，5 份
        相同文件在 `documents` 表里只有 1 行，服务端侧分组查不出东西。
        磁盘路径的真相只在这里。

        ## status 集合要包含 duplicate

        `duplicate` 是"这份与另一份内容相同，所以没导它"——它正是分组
        要统计的成员。漏掉它会让"5 份相同文件"只统计出 1 份（那份
        imported），提示条永远不显示。
        """
        rows = self._conn.execute(
            """
            select sha256, count(*) as n from local_files
            where kb_id=? and sha256<>''
              and status in ('imported','skipped','pending','duplicate')
            group by sha256 having n >= ?
            order by n desc
            """,
            (kb_id, min_count),
        )
        return [(r["sha256"], r["n"]) for r in rows]

    def list_duplicate_files(self, kb_id: str) -> list[LocalFile]:
        """列出被判为完全重复的全部文件行（供提示条与详情视图）。

        按 sha256 排序，同一组的成员相邻——调用方可以直接 groupby。
        """
        rows = self._conn.execute(
            "select * from local_files where kb_id=? and status='duplicate'"
            " order by sha256, path",
            (kb_id,),
        )
        return [_row_to_file(r) for r in rows]

    def duplicate_masters(self, kb_id: str) -> dict[str, str]:
        """{sha256: 已入库那份的路径}，用于把"主副本"排到组首。

        ## 为什么单独一条查询

        主副本是 `status='imported'`，不在 `list_duplicate_files` 的结果里。
        详情对话框要标出"到底哪一份在库里"，所以必须知道它。

        只取 imported 的行——同一个 sha256 理论上只有一行 imported
        （Runtime 同库内按 hash 去重），但 SQLite 不保证，所以取第一条。
        """
        rows = self._conn.execute(
            "select sha256, path from local_files"
            " where kb_id=? and status='imported' and sha256<>''",
            (kb_id,),
        )
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r["sha256"], r["path"])
        return out

    # ---------------------------------------------------------- pending_dups

    def upsert_pending_group(self, *, kb_id: str, group_id: str, kind: str,
                             paths: str, now: int = 0) -> None:
        """写入/更新一个待确认的相似组。

        ## 为什么不删了重插（B8 的关键）

        `pending_dups` 里 `resolved=1` 的行代表"用户已经确认过"。若重新
        扫描时先删后插，那个确认就丢了——用户会看到"确认过的又回来了"，
        于是不再相信确认动作有用。

        所以：同组已存在且**已解决**时不动它（保留用户的选择）；
        未解决时更新内容（成员可能变了，比如又存了一版）。

        表上没有 (kb_id, group_id) 唯一约束，所以用 select + update/insert
        而不是 `on conflict`。
        """
        import time

        now = now or int(time.time())
        with self.tx() as c:
            row = c.execute(
                "select id, resolved from pending_dups"
                " where kb_id=? and group_id=?",
                (kb_id, group_id),
            ).fetchone()
            if row is None:
                c.execute(
                    "insert into pending_dups(kb_id, group_id, kind, paths,"
                    " created_at) values (?,?,?,?,?)",
                    (kb_id, group_id, kind, paths, now),
                )
                return
            if row["resolved"]:
                # 用户确认过的组：保留原内容与选择，不覆盖
                return
            c.execute(
                "update pending_dups set kind=?, paths=? where id=?",
                (kind, paths, row["id"]),
            )

    def list_pending_groups(self, kb_id: str) -> list[sqlite3.Row]:
        """未解决的相似组（B8：启动时读这个恢复提示）。"""
        return list(self._conn.execute(
            "select group_id, kind, paths, created_at from pending_dups"
            " where kb_id=? and resolved=0 order by created_at desc",
            (kb_id,),
        ))

    def resolve_pending_group(self, kb_id: str, group_id: str,
                              chosen_path: str) -> None:
        with self.tx() as c:
            c.execute(
                "update pending_dups set resolved=1, chosen_path=?"
                " where kb_id=? and group_id=?",
                (chosen_path, kb_id, group_id),
            )

    def count_pending_groups(self, kb_id: str) -> int:
        row = self._conn.execute(
            "select count(*) as n from pending_dups"
            " where kb_id=? and resolved=0", (kb_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "select value from settings where key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "insert into settings(key, value) values (?,?)"
                " on conflict(key) do update set value=excluded.value",
                (key, value),
            )

    # --------------------------------------------------------- conversations

    def create_conversation(self, conv_id: str, title: str = "",
                            kb_mode: str = "none",
                            kb_ids: str = "[]", now: int = 0) -> None:
        import time

        now = now or int(time.time())
        with self.tx() as c:
            c.execute(
                "insert into conversations(id, title, kb_mode, kb_ids,"
                " created_at, updated_at) values (?,?,?,?,?,?)",
                (conv_id, title, kb_mode, kb_ids, now, now),
            )

    def list_conversations(self, limit: int = 50) -> list[Conversation]:
        rows = self._conn.execute(
            "select id, title, kb_mode, kb_ids, created_at, updated_at"
            " from conversations order by updated_at desc limit ?",
            (limit,),
        )
        return [_row_to_conv(r) for r in rows]

    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        row = self._conn.execute(
            "select id, title, kb_mode, kb_ids, created_at, updated_at"
            " from conversations where id=?", (conv_id,),
        ).fetchone()
        return _row_to_conv(row) if row else None

    def update_conversation_scope(self, conv_id: str, kb_mode: str,
                                  kb_ids: str) -> None:
        """记住本次会话的库范围（下次打开能还原勾选）。"""
        import time

        with self.tx() as c:
            c.execute(
                "update conversations set kb_mode=?, kb_ids=?, updated_at=?"
                " where id=?",
                (kb_mode, kb_ids, int(time.time()), conv_id),
            )

    def add_message(self, conv_id: str, role: str, content: str, *,
                    sources: str = "[]", kb_mode: str = "none",
                    kb_ids: str = "[]", all_bases: bool = False,
                    base_count: int = 0, model: str = "", degraded: bool = False,
                    used_chunks: int = 0, warnings: str = "[]",
                    now: int = 0) -> int:
        """追加一条消息，返回自增 id。

        ## 为什么 sources 存 JSON 字符串而不是结构化列

        来源是"一组元数据 + 快照"，结构会随服务端字段演进（比如将来加了
        `text`）。存 JSON 让客户端不必跟着改表结构，代价是不能用 SQL
        按来源字段查询——而这个需求不存在。
        """
        import time

        now = now or int(time.time())
        with self.tx() as c:
            cur = c.execute(
                "insert into messages(conversation_id, role, content, sources,"
                " kb_mode, kb_ids, all_bases, base_count, model, degraded,"
                " used_chunks, warnings, created_at)"
                " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (conv_id, role, content, sources, kb_mode, kb_ids,
                 1 if all_bases else 0, base_count, model,
                 1 if degraded else 0, used_chunks, warnings, now),
            )
            msg_id = int(cur.lastrowid or 0)
            c.execute("update conversations set updated_at=? where id=?",
                      (now, conv_id))
        return msg_id

    def list_messages(self, conv_id: str, limit: int = 200) -> list[ChatMessageRow]:
        rows = self._conn.execute(
            "select * from messages where conversation_id=? order by id limit ?",
            (conv_id, limit),
        )
        return [_row_to_msg(r) for r in rows]

    def delete_conversation(self, conv_id: str) -> None:
        with self.tx() as c:
            c.execute("delete from conversations where id=?", (conv_id,))


@dataclass
class Conversation:
    id: str
    title: str = ""
    kb_mode: str = "none"
    kb_ids: str = "[]"
    created_at: int = 0
    updated_at: int = 0


@dataclass
class ChatMessageRow:
    id: int
    conversation_id: str
    role: str
    content: str
    sources: str = "[]"
    kb_mode: str = "none"
    kb_ids: str = "[]"
    all_bases: bool = False
    base_count: int = 0
    model: str = ""
    degraded: bool = False
    used_chunks: int = 0
    warnings: str = "[]"
    created_at: int = 0


def _norm_path(p: str) -> str:
    """路径归一化为比对键：小写 + 正斜杠 + 去尾斜杠。

    Windows 路径大小写不敏感，同一文件可能以 `D:\\A\\b.txt` 与
    `d:\\a\\B.txt` 两种形式出现。不归一化会让 `local_files` 里出现两行
    指向同一文件，进而导致"重复导入"与"完全重复"误报。

    **只用于比对与唯一约束**，不用于展示——UI 要显示用户实际看到的大小写
    （见 `local_files.path` 与 `path_key` 两列的说明）。
    """
    s = str(p).replace("\\", "/")
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s.lower()


def _row_to_conv(r: sqlite3.Row) -> Conversation:
    return Conversation(
        id=r["id"], title=r["title"], kb_mode=r["kb_mode"],
        kb_ids=r["kb_ids"], created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _row_to_msg(r: sqlite3.Row) -> ChatMessageRow:
    return ChatMessageRow(
        id=r["id"], conversation_id=r["conversation_id"], role=r["role"],
        content=r["content"], sources=r["sources"], kb_mode=r["kb_mode"],
        kb_ids=r["kb_ids"], all_bases=bool(r["all_bases"]),
        base_count=r["base_count"], model=r["model"],
        degraded=bool(r["degraded"]), used_chunks=r["used_chunks"],
        warnings=r["warnings"], created_at=r["created_at"],
    )


def _row_to_file(r: sqlite3.Row) -> LocalFile:
    return LocalFile(
        id=r["id"], kb_id=r["kb_id"], path=r["path"], path_key=r["path_key"],
        doc_id=r["doc_id"], sha256=r["sha256"], size=r["size"], mtime=r["mtime"],
        status=r["status"], error=r["error"], name_sent=r["name_sent"],
        imported_at=r["imported_at"],
    )


def open_state_db(data_dir: Path | str | None = None) -> StateDB:
    """打开（必要时创建）`state.db`。

    默认位置 `%LOCALAPPDATA%\\LocalKB\\state.db`，与 Runtime 的
    `%LOCALAPPDATA%\\LocalAIHub\\` 分开——两个产品各管各的目录，
    互不写对方的文件（方案 §13.2）。
    """
    if data_dir is None:
        import os

        base = os.environ.get("LOCALAPPDATA")
        data_dir = (Path(base) / "LocalKB") if base else (Path.home() / ".localkb")
    return StateDB(Path(data_dir) / "state.db")
