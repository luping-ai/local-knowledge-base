"""目录遍历、扩展名过滤、排除规则。

## 这个模块唯一的目标：**不卡死、不漏原因**

方案的 §7.5 列了 7 类 Windows 现实障碍。它们的共同特点是"在开发者机器上
不出现，在用户机器上必现"，所以每一类都必须显式处理，并且给出**可读原因**
——"跳过了 97 个文件"而不给理由是用户最不能接受的表现（§5.6 同一原则）。

## 为什么不用 pathlib.Path.rglob

它会在以下情况卡住或抛错中断整个遍历：

  - 符号链接循环（junction 指向父目录）→ 无限递归
  - 网络盘（映射的 Z:\\）→ 单次 listdir 可能卡数分钟
  - 无权限目录 → PermissionError 直接冒泡，整个扫描失败

`os.walk` 的 `onerror` 回调 + `followlinks=False` 能逐个消化这些错误。
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "SkipReason",
    "WalkResult",
    "Walker",
    "DEFAULT_EXCLUDED_DIRS",
    "LEGACY_OFFICE_EXTS",
    "SUPPORTED_EXTS",
]

# 支持解析的扩展名。大小写都写出来（Windows 上 `.PDF` 与 `.pdf` 都存在），
# 但匹配时统一小写，所以这里只列小写，见 _ext_of。
SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".markdown",
})

# 内置排除目录（`excludes.is_default=1` 的初始内容）。
#
# 这些目录的共同点：文件数量巨大、内容与"个人文档"无关、且部分是隐私数据
# （浏览器历史、聊天记录）。不排除的话用户选 D 盘会扫几小时，且把
# Cookies 之类的东西读进知识库是隐私事故。
DEFAULT_EXCLUDED_DIRS: tuple[str, ...] = (
    # 系统
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata/microsoft", "c:/$recycle.bin", "c:/system volume information",
    # 用户目录里的技术垃圾
    "/appdata/local/temp", "/appdata/local/microsoft", "/appdata/local/packages",
    "/appdata/roaming/microsoft",
    # 隐私相关（方案 §9 excludes 的 is_default）
    "/appdata/local/google/chrome/user data", "/appdata/roaming/mozilla",
    "/appdata/local/microsoft/edge/user data",
    # 开发产物
    "/node_modules", "/.git", "/.venv", "/venv", "/__pycache__",
    "/.idea", "/.vscode", "/dist", "/build", "/target",
    # 云盘占位目录的常见容器（避免误扫未下载的内容）
    "/onedrive",
)


class SkipReason:
    """跳过原因。用常量而不是裸字符串，避免 UI 与统计各写一套拼写。"""

    LOCKED = "locked"
    """文件被占用（Word/Excel 正在编辑）。"""

    TOO_LONG = "too_long"
    """路径过长（Windows 260 字符限制）。"""

    CLOUD_ONLY = "cloud_only"
    """云盘占位文件，本机还没有内容。"""

    TOO_LARGE = "too_large"
    """文件体积或块数超限。"""

    UNREADABLE = "unreadable"
    """无读取权限。"""

    NETWORK_SLOW = "network_slow"
    """网络路径，枚举超时。"""

    NO_PERMISSION_DIR = "no_permission_dir"
    """目录本身不可列出。"""

    LEGACY_FORMAT = "legacy_format"
    """旧版二进制 Office 格式（.doc/.xls/.ppt），需另存为新格式。"""


# 给用户看的文案。每个都能指向下一步动作（方案 §5.6）。
SKIP_MESSAGES: dict[str, str] = {
    SkipReason.LOCKED: "文件正被其他程序使用（Word/Excel 可能还开着）。保存并关闭后会自动重试。",
    SkipReason.TOO_LONG: "路径过长，已跳过。可把文件移到较短的路径后重新扫描。",
    SkipReason.CLOUD_ONLY: (
        "这是云盘占位文件，本机还没有内容。"
        "请先在资源管理器中打开一次让它下载完成。"
    ),
    SkipReason.TOO_LARGE: "文件过大，已跳过。",
    SkipReason.UNREADABLE: "没有读取权限，已跳过。",
    SkipReason.NETWORK_SLOW: "网络路径响应过慢，已跳过。建议选择本地目录。",
    SkipReason.NO_PERMISSION_DIR: "没有权限列出这个目录，已跳过其内容。",
    SkipReason.LEGACY_FORMAT: (
        "这是旧版 Office 格式，需要先另存为新格式再扫描"
        "（.doc → .docx、.xls → .xlsx、.ppt → .pptx）。"
    ),
}

# ## 为什么旧格式要报"跳过"而不是静默忽略（M6 加的）
#
# 其他不支持的扩展名（.mp4/.zip/.exe）静默忽略是对的——用户选 D 盘会看到
# 几万个"不支持 mp4"，那是噪音。
#
# 但 `.doc` / `.xls` / `.ppt` 不一样：它们是**用户真正想导入的文档**，
# 而且改个格式就能用。静默忽略的后果是用户扫完看到"新增 0 篇"，
# 完全不知道为什么自己的 200 个 .doc 一个都没进去——这类"什么都不发生"
# 的故障最难自查。
#
# 所以这一小组扩展名单独处理：计入 skipped 并给出可读的转换指引。
LEGACY_OFFICE_EXTS: frozenset[str] = frozenset({
    ".doc", ".xls", ".ppt", ".wps", ".et", ".dps",
})

# 文件去向的三态判定。为什么不是 bool：见 Walker._classify 的说明。
_KEEP = "keep"
_SKIP = "skip"
_IGNORE = "ignore"

# 临时文件与下载中间态。Word 保存时先写 `~$xxx.docx` 再改名，
# 不排除会把半个文件导进去。
_SKIP_PREFIXES = ("~$", ".~", "~")
_SKIP_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download", ".!ut")

# FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS：OneDrive 等云盘的"占位"标志。
# 读它会触发下载，可能几秒到几十分钟。
_ATTR_RECALL_ON_DATA_ACCESS = 0x00400000
_ATTR_RECALL_ON_OPEN = 0x00040000
_ATTR_OFFLINE = 0x00001000


@dataclass
class WalkResult:
    """一次遍历的产物。"""

    files: list[Path] = field(default_factory=list)
    # skipped 是 (path, reason) 列表。**不能丢**：UI 要按原因分组展示。
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    dirs_scanned: int = 0
    # timed_out 为 True 表示有网络目录因为超时被跳过，结果是**不完整**的。
    timed_out: bool = False

    @property
    def total_seen(self) -> int:
        return len(self.files) + len(self.skipped)


def _ext_of(p: Path) -> str:
    return p.suffix.lower()


def _should_skip_name(name: str) -> bool:
    low = name.lower()
    if low.startswith(_SKIP_PREFIXES):
        return True
    return low.endswith(_SKIP_SUFFIXES)


def _is_cloud_placeholder(st: os.stat_result) -> bool:
    """是否为云盘占位文件（本机无内容）。

    ## 为什么用 st_file_attributes

    Windows 的 `os.stat_result` 带 `st_file_attributes`（Python 3.5+）。
    检测占位状态必须看这些位，而不是看文件大小（占位文件也有大小，
    那是"云端的完整大小"）。

    非 Windows 平台没有这个字段，直接返回 False。
    """
    attrs = getattr(st, "st_file_attributes", 0)
    if not attrs:
        return False
    return bool(attrs & (_ATTR_RECALL_ON_DATA_ACCESS
                         | _ATTR_RECALL_ON_OPEN
                         | _ATTR_OFFLINE))


def _is_reparse_point(st: os.stat_result) -> bool:
    """是否为符号链接 / junction（避免循环递归）。"""
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT) \
        if hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT") else False


class Walker:
    """目录遍历器。

    ## 用法

        w = Walker(excludes=[Path(r"D:\\私人")], allowed_exts=SUPPORTED_EXTS)
        res = w.walk([Path("D:/工作")])
        for f in res.files: ...
        for path, reason in res.skipped: ...

    ## 线程与取消

    `cancel` 是一个 `threading.Event`。遍历可能持续几分钟（5000 篇文档），
    用户点"取消"必须能立刻停。检查点放在每个目录进入时与每 200 个文件，
    而不是每个文件——每次循环都查 Event 会让遍历慢 20% 以上。
    """

    # 每处理这么多文件检查一次取消与超时
    _CHECK_EVERY = 200

    def __init__(
        self,
        *,
        excludes: Optional[list[Path]] = None,
        use_default_excludes: bool = True,
        allowed_exts: Optional[frozenset[str]] = None,
        max_dirs: int = 50_000,
        network_timeout_sec: float = 20.0,
        cancel: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._excludes = [Path(p) for p in (excludes or [])]
        # use_default_excludes=False 的用途：
        #   1. 测试（pytest 的 tmp_path 本身在 AppData\Local\Temp 下，
        #      而那是内置排除项——不开这个开关的话所有遍历测试都扫不到东西）
        #   2. 用户明确要求"我就是要扫这个目录"（高级设置里的选项）
        #
        # 为什么做成开关而不是让调用方传一份完整的 excludes：内置项有
        # 二十多条且会随版本增加，让调用方复制一遍必然有一天不同步。
        self._use_default_excludes = use_default_excludes
        self._allowed = allowed_exts or SUPPORTED_EXTS
        self._max_dirs = max_dirs
        self._network_timeout = network_timeout_sec
        self._cancel = cancel
        self._on_progress = on_progress

    # ------------------------------------------------------------------ 入口

    def walk(self, roots: list[Path]) -> WalkResult:
        out = WalkResult()
        seen_real: set[str] = set()
        deadline = time.monotonic() + self._network_timeout
        checked = 0

        for root_path in roots:
            root = Path(root_path)
            if not root.exists():
                # 根目录本身不存在：这是配置问题，不是跳过。记一条原因即可
                # （调用方会拿它提示"目录不存在，请重新选择"）。
                out.skipped.append((root, SkipReason.UNREADABLE))
                continue

            is_network = _is_network_path(root)
            for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False, onerror=self._on_walk_error
            ):
                if self._cancel is not None and self._cancel.is_set():
                    return out

                out.dirs_scanned += 1
                if out.dirs_scanned > self._max_dirs:
                    out.skipped.append((Path(dirpath), SkipReason.TOO_LARGE))
                    return out

                # 网络盘：每进一个目录检查总耗时，超时就整体标记为不完整。
                # 单次 listdir 卡住时这里也回不来（os.walk 内部阻塞），
                # 所以真正的保护是"给用户的提示"，不是硬超时。
                if is_network and time.monotonic() > deadline:
                    out.timed_out = True
                    out.skipped.append((Path(dirpath), SkipReason.NETWORK_SLOW))
                    return out

                # 剪枝：排除目录 + junction 循环
                dirnames[:] = [
                    d for d in dirnames
                    if not self._is_excluded(Path(dirpath) / d)
                    and not self._is_loop(Path(dirpath) / d, seen_real)
                ]

                for name in filenames:
                    checked += 1
                    if checked % self._CHECK_EVERY == 0:
                        if self._cancel is not None and self._cancel.is_set():
                            return out
                        if self._on_progress is not None:
                            self._on_progress(out.total_seen)

                    p = Path(dirpath) / name
                    action, reason = self._classify(p)
                    if action == _IGNORE:
                        # 静默忽略：不支持的格式、临时文件、空文件。
                        # 不进 skipped，否则用户选大目录会看到上万条"不支持"。
                        continue
                    if action == _SKIP:
                        out.skipped.append((p, reason))
                        continue
                    out.files.append(p)

        if self._on_progress is not None:
            self._on_progress(out.total_seen)
        return out

    # -------------------------------------------------------------- 内部判断

    def _is_excluded(self, d: Path) -> bool:
        """目录是否在排除列表里。

        ## 为什么用"路径后缀匹配"而不是精确路径比较

        内置排除项写的是 `c:/windows`、`/node_modules` 这种片段，而实际
        路径是 `C:\\Windows\\System32`、`D:\\proj\\node_modules\\x`。
        比较片段更实用，代价是可能误伤（比如 `D:\\myappdata\\local\\temp`）。
        误伤的后果是"少扫一个目录"，比"把用户隐私读进库"轻得多。
        """
        norm = str(d).replace("\\", "/").lower().rstrip("/")
        for ex in self._excludes:
            exn = str(ex).replace("\\", "/").lower().rstrip("/")
            if not exn:
                continue
            if norm == exn or norm.startswith(exn + "/"):
                return True
        # 内置片段
        if self._use_default_excludes:
            for frag in DEFAULT_EXCLUDED_DIRS:
                if norm.endswith(frag) or f"/{frag.strip('/')}/" in f"{norm}/":
                    return True
        return False

    def _is_loop(self, d: Path, seen_real: set[str]) -> bool:
        """junction / symlink 指向已访问过的真实目录时剪枝。

        ## 为什么需要

        Windows 的 junction（mklink /J）与 Linux 的 symlink 不同，`os.walk`
        的 followlinks=False 对 junction **不生效**（它不是 symlink，是
        reparse point），会真的递归进去。用户目录里出现自引用 junction 时
        会无限递归直到路径超长。
        """
        try:
            st = os.stat(d, follow_symlinks=True)
        except OSError:
            return False
        if not _is_reparse_point(st):
            return False
        try:
            real = os.path.realpath(d)
        except OSError:
            return True
        key = real.lower()
        if key in seen_real:
            return True
        seen_real.add(key)
        return False

    def _classify(self, p: Path) -> tuple[str, str]:
        """判定一个文件的去向：保留 / 跳过（带原因）/ 静默忽略。

        ## 为什么必须三态

        早期实现让"不支持的格式"与"可以解析"都返回 None，结果 `.exe`
        与 `.png` 全被收进 files 列表，再往下走解析器时才逐个失败。
        反过来，如果让它们都进 skipped，用户选 C 盘会看到十万条"不支持
        的格式"——两种都会让真正的失败原因被淹没。

        所以这里显式区分：
          _KEEP    进 files，后续解析
          _SKIP    进 skipped，UI 展示原因（用户需要知道为什么某文件没进库）
          _IGNORE  什么都不做（用户的注意力是稀缺资源）
        """
        if _should_skip_name(p.name):
            # Word 临时文件 / 下载中间态：用户不需要知道这些。
            return _IGNORE, ""

        ext = _ext_of(p)
        if ext not in self._allowed:
            # ## 旧版 Office 格式要报出来（M6）
            #
            # 其他不支持的扩展名静默忽略是对的（.mp4/.zip 是噪音），
            # 但 .doc/.xls/.ppt 是用户真正想导入的文档，而且改格式就能用。
            # 静默忽略会让他们看到"新增 0 篇"却不知道原因——这类
            # "什么都没发生"的故障最难自查。
            if ext in LEGACY_OFFICE_EXTS:
                return _SKIP, SkipReason.LEGACY_FORMAT
            return _IGNORE, ""

        try:
            # 260 字符是 Windows 的传统限制。超过时 open() 会失败，
            # 但 stat() 有时仍成功——所以先按长度判断，给出准确原因。
            if len(str(p)) >= 260:
                return _SKIP, SkipReason.TOO_LONG
            st = p.stat()
        except OSError as e:
            import errno

            if e.errno in (errno.EACCES, errno.EPERM):
                return _SKIP, SkipReason.UNREADABLE
            if e.errno == errno.ENOENT:
                return _IGNORE, ""  # 遍历期间被删了，正常竞态
            return _SKIP, SkipReason.UNREADABLE

        if _is_cloud_placeholder(st):
            return _SKIP, SkipReason.CLOUD_ONLY
        if not st.st_size:
            return _IGNORE, ""  # 空文件没有内容可切块
        return _KEEP, ""

    @staticmethod
    def _on_walk_error(err: OSError) -> None:
        """`os.walk` 的错误回调。

        ## 为什么只吞掉、不记录到结果里

        `os.walk` 的 onerror 只给 OSError，拿不到"哪个目录"的结构化信息
        （虽然 err.filename 通常有）。把这里的错误并入 skipped 会与
        文件级原因混在一起，UI 无法区分"这个文件读不了"与"这个目录进不去"。

        目录级失败已在 `dirnames` 剪枝与根目录检查里覆盖；这里保持静默，
        避免同一件事记两遍。
        """
        return None


def _is_network_path(p: Path) -> bool:
    """是否为网络路径（UNC `\\\\server\\share` 或映射盘）。

    映射盘（Z:）无法从路径本身判断是不是网络盘，只能查 Win32 的
    DriveType。这里做保守判断：只把 UNC 当网络路径，映射盘按本地处理
    —— 因为查 DriveType 需要额外 API 调用，而误判"本地"的后果只是
    "没有超时保护"，误判"网络"会让本地盘也被限制。
    """
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")


def iter_supported(files: list[Path]) -> Iterator[Path]:
    """过滤出可解析扩展名的文件（遍历结果通常已过滤，这个留作兜底）。"""
    for f in files:
        if _ext_of(f) in SUPPORTED_EXTS:
            yield f
