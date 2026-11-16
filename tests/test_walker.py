"""`walker` 的单元测试。

## 重点不是"能遍历"，而是"异常不中断且原因准确"

方案的 §7.5 列了 7 类 Windows 障碍。真实的 Windows 特有行为（junction、
云盘占位、260 字符）在 CI 上不好复现，所以这里的做法是：

  - 能用普通文件模拟的，真造文件（权限、临时文件、扩展名过滤）
  - 依赖平台属性的，用 monkeypatch 伪造 `st_file_attributes`
  - 只验证"判定逻辑"，不验证"Windows 真的会这样"

后者的验证在 M5 的干净虚拟机上做（方案 §11.3 的手工清单）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from localkb.scan.walker import (
    DEFAULT_EXCLUDED_DIRS,
    SKIP_MESSAGES,
    SUPPORTED_EXTS,
    SkipReason,
    Walker,
)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """造一棵有代表性的目录树。

    root/
      doc.pdf, note.md, readme.txt      ← 应该被扫到
      image.png, app.exe                ← 不支持的格式，静默忽略
      ~$draft.docx                      ← Word 临时文件，静默忽略
      half.pdf.crdownload               ← 下载中间态，静默忽略
      empty.txt                         ← 空文件，静默忽略
      sub/inner.docx, sub/deep/x.xlsx   ← 递归应该扫到

    刻意**不含** node_modules 之类的内置排除目录：这一组测试验证的是
    "扩展名过滤与临时文件识别"，内置排除项另有专项测试（需要显式打开
    use_default_excludes）。混在一起会让"排除逻辑错了"与"过滤逻辑错了"
    表现为同一个失败。
    """
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "note.md").write_text("# note", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "app.exe").write_bytes(b"MZ")
    (tmp_path / "~$draft.docx").write_bytes(b"tmp")
    (tmp_path / "half.pdf.crdownload").write_bytes(b"partial")
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")

    sub = tmp_path / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "inner.docx").write_bytes(b"PK")
    (sub / "deep" / "x.xlsx").write_bytes(b"PK")
    return tmp_path


def names(res) -> set[str]:
    return {p.name for p in res.files}


def walker(**kw) -> Walker:
    """测试用 Walker：默认关闭内置排除项。

    ## 为什么必须关

    pytest 的 `tmp_path` 在 `%LOCALAPPDATA%\\Temp` 下，而
    `/appdata/local/temp` 是内置排除项之一。不关的话所有遍历测试都
    扫不到任何文件——而"扫不到"与"逻辑写错"的表现一样，很容易误判。

    内置排除项本身另有测试（test_default_excludes_present /
    test_excludes_by_suffix_fragment），那里显式开着。
    """
    kw.setdefault("use_default_excludes", False)
    return Walker(**kw)


# ------------------------------------------------------------ 基本遍历

def test_walks_supported_files(tree: Path) -> None:
    res = walker().walk([tree])
    assert names(res) == {"doc.pdf", "note.md", "readme.txt", "inner.docx", "x.xlsx"}


def test_recurses_into_subdirs(tree: Path) -> None:
    res = walker().walk([tree])
    paths = {p.as_posix() for p in res.files}
    assert any("/sub/deep/x.xlsx" in p for p in paths)


def test_ignores_unsupported_exts(tree: Path) -> None:
    """`image.png` / `app.exe` 既不在 files 也不在 skipped。

    理由是噪音控制：用户选 D 盘会有上万个 .exe/.dll，逐个报"不支持"
    会淹没真正的失败原因。
    """
    res = walker().walk([tree])
    assert "image.png" not in names(res)
    assert "app.exe" not in names(res)
    skipped_names = {p.name for p, _ in res.skipped}
    assert "image.png" not in skipped_names
    assert "app.exe" not in skipped_names


def test_ignores_word_temp_files(tree: Path) -> None:
    """`~$` 前缀必须静默忽略——它是 Word 保存过程中的半成品。"""
    res = walker().walk([tree])
    assert "~$draft.docx" not in names(res)
    assert "~$draft.docx" not in {p.name for p, _ in res.skipped}


def test_ignores_download_partials(tree: Path) -> None:
    res = walker().walk([tree])
    assert "half.pdf.crdownload" not in names(res)


def test_ignores_empty_files(tree: Path) -> None:
    """0 字节文件没有内容可解析，静默忽略。"""
    res = walker().walk([tree])
    assert "empty.txt" not in names(res)


def test_excludes_node_modules(tmp_path: Path) -> None:
    """内置排除项生效（这里显式打开，其余测试默认关闭）。"""
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "keep.md").write_text("k", encoding="utf-8")

    res = walker(use_default_excludes=True).walk([tmp_path])
    assert names(res) == {"keep.md"}


def test_extension_match_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "UPPER.PDF").write_bytes(b"%PDF")
    res = walker().walk([tmp_path])
    assert "UPPER.PDF" in names(res)


# ------------------------------------------------------------ 排除规则

def test_custom_exclude_dir(tmp_path: Path) -> None:
    keep = tmp_path / "keep"
    secret = tmp_path / "secret"
    keep.mkdir()
    secret.mkdir()
    (keep / "a.md").write_text("a", encoding="utf-8")
    (secret / "b.md").write_text("b", encoding="utf-8")

    res = walker(excludes=[secret]).walk([tmp_path])
    assert names(res) == {"a.md"}


def test_default_excludes_present() -> None:
    """内置排除项必须覆盖隐私敏感目录（方案 §9 的 is_default）。"""
    joined = " ".join(DEFAULT_EXCLUDED_DIRS)
    for keyword in ("recycle.bin", "chrome/user data", "mozilla", "node_modules"):
        assert keyword in joined, f"内置排除项缺少 {keyword}"


def test_exclude_by_suffix_fragment(tmp_path: Path) -> None:
    """片段匹配：`/node_modules` 应命中任意层级的 node_modules。"""
    deep = tmp_path / "a" / "b" / "node_modules" / "x"
    deep.mkdir(parents=True)
    (deep / "c.md").write_text("c", encoding="utf-8")
    res = walker(use_default_excludes=True).walk([tmp_path])
    assert res.files == []


# ------------------------------------------------------------ 不可读

def patch_os_stat(monkeypatch, target: Path, *, exc: BaseException | None = None,
                  attributes: int | None = None):
    """只对 `target` 生效的 `os.stat` 替身；其他路径一律透传给真实实现。

    ## 为什么不用 monkeypatch.setattr(Path, "stat", fake)

    那样会**全局**替换 `Path.stat`，包括 pytest 自己检查源码路径时调用的
    那些（`_pytest._code.code.path` 会 `Path.exists()`）。测试里对 `Path`
    的等价比较还会反过来触发 `__str__`，实测两次踩到 RecursionError 与
    AttributeError，都在 pytest 的内部代码里，报错栈与真实缺陷无关。

    改为替换 `os.stat`：`Path.stat` 与 `os.walk` 都走它，且我们只在
    路径精确等于 target 时改变行为，其余全部透传，pytest 不受影响。

    ## 参数

    exc:        抛出的异常（模拟权限错误 / 文件消失）
    attributes: 伪造的 `st_file_attributes`（模拟云盘占位），None 表示不改
    """
    import os as _os

    real = _os.stat
    target_key = _os.path.normcase(str(target))

    class _FakeStat:
        """转发除 st_file_attributes 之外的所有字段。"""

        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return getattr(self._st, name)

        @property
        def st_file_attributes(self):
            return attributes

    def fake(path, *args, **kwargs):
        try:
            key = _os.path.normcase(_os.fspath(path))
        except TypeError:
            return real(path, *args, **kwargs)
        if key == target_key:
            if exc is not None:
                raise exc
            return _FakeStat(real(path, *args, **kwargs))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(_os, "stat", fake)


def test_unreadable_file_is_skipped_with_reason(tmp_path: Path, monkeypatch) -> None:
    """权限错误必须进 skipped（带原因），不能中断整个遍历。"""
    f = tmp_path / "locked.pdf"
    f.write_bytes(b"%PDF")
    patch_os_stat(monkeypatch, f, exc=PermissionError(13, "Permission denied"))

    res = walker().walk([tmp_path])
    assert res.files == []
    assert [(p.name, r) for p, r in res.skipped] == [("locked.pdf", SkipReason.UNREADABLE)]


def test_vanished_file_is_silently_ignored(tmp_path: Path, monkeypatch) -> None:
    """遍历期间被删的文件不报错（ENOENT 是正常竞态）。"""
    f = tmp_path / "gone.pdf"
    f.write_bytes(b"%PDF")
    patch_os_stat(monkeypatch, f, exc=FileNotFoundError(2, "No such file"))

    res = walker().walk([tmp_path])
    assert res.files == []
    assert res.skipped == []


def test_missing_root_is_recorded(tmp_path: Path) -> None:
    """根目录不存在是配置问题，要记一条原因让 UI 提示"重新选择"。"""
    res = walker().walk([tmp_path / "nope"])
    assert res.files == []
    assert len(res.skipped) == 1
    assert res.skipped[0][1] == SkipReason.UNREADABLE


# ------------------------------------------------------- 云盘占位 / 长路径

def test_cloud_placeholder_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """OneDrive 占位文件读它会触发下载，必须跳过并提示"先打开一次"。"""
    f = tmp_path / "cloud.pdf"
    f.write_bytes(b"%PDF")
    patch_os_stat(monkeypatch, f, attributes=0x00400000)  # RECALL_ON_DATA_ACCESS

    res = walker().walk([tmp_path])
    assert res.files == []
    assert ("cloud.pdf", SkipReason.CLOUD_ONLY) in [(p.name, r) for p, r in res.skipped]


def test_long_path_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """260 字符以上要给出准确原因，而不是让它 open() 失败。

    不真的创建超长路径（Windows 上需要 \\\\?\\ 前缀），而是伪造
    str(path) 的长度——walker 的判定只看 `len(str(p))`。

    这里替换的是 `Path.__str__`，与 pytest 无冲突（它不用 Path 的
    __str__ 做路径判定）。比较时用 `os.path.normcase` 而不是 `==`，
    避免 `Path.__eq__` 反过来调 `__str__` 造成递归。
    """
    f = tmp_path / "long.pdf"
    f.write_bytes(b"%PDF")

    import os as _os

    real_str = Path.__str__
    target_key = _os.path.normcase(str(f))

    def fake_str(self):
        s = real_str(self)
        if _os.path.normcase(s) == target_key:
            return s + "x" * 300
        return s

    monkeypatch.setattr(Path, "__str__", fake_str)
    res = walker().walk([tmp_path])
    assert ("long.pdf", SkipReason.TOO_LONG) in [(p.name, r) for p, r in res.skipped]


# ------------------------------------------------------------ 取消与上限

def test_cancel_stops_walk(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"{i}.md").write_text("x", encoding="utf-8")
    ev = threading.Event()
    ev.set()
    res = walker(cancel=ev).walk([tmp_path])
    assert res.files == []


def test_max_dirs_limit(tmp_path: Path) -> None:
    """目录数超限时停止并记原因，避免扫整个 D 盘跑两小时。

    用 `max_dirs=1`：根目录本身算第 1 个，进入子目录时就会超限。
    超限的标记打在"被丢弃的那个目录"上（不是根），所以断言只看
    有没有记录、以及结果是否不完整。
    """
    for i in range(5):
        d = tmp_path / f"d{i}"
        d.mkdir()
        (d / "a.md").write_text("a", encoding="utf-8")
    res = walker(max_dirs=1).walk([tmp_path])
    assert len(res.files) < 5, "目录数超限后不应继续收集文件"
    assert any(r == SkipReason.TOO_LARGE for _, r in res.skipped), \
        "超限必须记原因，否则 UI 无法解释'为什么少了文件'"


def test_progress_callback_called(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"{i}.md").write_text("x", encoding="utf-8")
    seen: list[int] = []
    walker(on_progress=seen.append).walk([tmp_path])
    # 至少末尾那次会回调（总数在遍历结束时上报一次）
    assert seen, "on_progress 一次都没被调用"
    assert seen[-1] == 10


# ------------------------------------------------------------ 多根目录

def test_multiple_roots(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "1.md").write_text("1", encoding="utf-8")
    (b / "2.md").write_text("2", encoding="utf-8")
    res = walker().walk([a, b])
    assert names(res) == {"1.md", "2.md"}


def test_duplicate_roots_not_deduped(tmp_path: Path) -> None:
    """同一条根传两次会扫两遍（去重是调用方的责任，不是 walker 的）。

    这里断言"不去重"是为了固定行为：若 walker 悄悄去重，调用方在
    "多个库共用同一目录"时会误以为每个库都扫到了。
    """
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    res = walker().walk([tmp_path, tmp_path])
    assert len(res.files) == 2


# ------------------------------------------------------------ 文案完整性

@pytest.mark.parametrize("reason", [
    SkipReason.LOCKED, SkipReason.TOO_LONG, SkipReason.CLOUD_ONLY,
    SkipReason.TOO_LARGE, SkipReason.UNREADABLE, SkipReason.NETWORK_SLOW,
    SkipReason.NO_PERMISSION_DIR, SkipReason.LEGACY_FORMAT,
])
def test_every_reason_has_message(reason: str) -> None:
    """每个跳过原因都必须有可读文案（方案 §5.6：不能只给"解析失败"）。"""
    assert SKIP_MESSAGES.get(reason), f"{reason} 缺少文案"


def test_supported_exts_are_lowercase() -> None:
    """匹配时统一小写，集合里不能混大写。"""
    assert all(e == e.lower() for e in SUPPORTED_EXTS)
    assert all(e.startswith(".") for e in SUPPORTED_EXTS)


# ------------------------------------------------------------ 旧版 Office 格式（M6）

@pytest.mark.parametrize("name", ["a.doc", "b.xls", "c.ppt", "d.wps"])
def test_legacy_office_format_reported_not_ignored(
        tmp_path: Path, name: str) -> None:
    """`.doc/.xls/.ppt` 必须计入 skipped 并给出转换指引，不能静默消失。

    ## 为什么这条重要

    其他不支持的扩展名（.mp4/.zip）静默忽略是对的——用户选 D 盘会看到
    几万个"不支持 mp4"，那是噪音。

    但旧版 Office 是**用户真正想导入的文档**，而且改个格式就能用。静默
    忽略的后果是扫完显示"新增 0 篇"，用户完全不知道为什么自己的 200 个
    .doc 一个都没进去——这类"什么都没发生"的故障最难自查。
    """
    (tmp_path / name).write_bytes(b"legacy")
    res = walker().walk([tmp_path])

    assert not res.files, f"{name} 不该被当作可解析文件"
    skipped = [(p.name, r) for p, r in res.skipped]
    assert (name, SkipReason.LEGACY_FORMAT) in skipped, (
        f"{name} 应报 LEGACY_FORMAT，实际 skipped={skipped}"
    )


def test_other_unsupported_exts_stay_silent(tmp_path: Path) -> None:
    """非 Office 的不支持格式仍然静默忽略（避免噪音）。"""
    for name in ("a.mp4", "b.zip", "c.exe"):
        (tmp_path / name).write_bytes(b"x")
    res = walker().walk([tmp_path])
    assert not res.files
    assert not res.skipped, f"这些不该进 skipped，实际 {res.skipped}"


def test_pptx_is_supported(tmp_path: Path) -> None:
    """`.pptx` 在支持列表里（M6 之前就预留了，这里钉住）。"""
    assert ".pptx" in SUPPORTED_EXTS
    (tmp_path / "deck.pptx").write_bytes(b"x")
    res = walker().walk([tmp_path])
    assert [p.name for p in res.files] == ["deck.pptx"]
