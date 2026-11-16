"""自检报告的写入格式。

## 为什么这个测试值得写

自检是"现场排障的唯一入口"——用户双击 `LocalKB.exe --self-check`，
把 `%LOCALAPPDATA%\\LocalKB\\selfcheck.txt` 发给我们。这份文件必须在
**用户的记事本里**能读。

而这个属性**用 Python 读回来验不出来**：无论写 UTF-8 还是 utf-8-sig，
`read_text(encoding="utf-8-sig")` 都能正常读。只有"按字节看有没有 BOM"
或"别的工具读"才能发现差别。

实测踩到过一次：`tools/verify-installer.ps1` 用 PowerShell 5.1 读同一份
文件，两条中文断言全 FAIL，而产品其实没问题。
"""

from __future__ import annotations

from pathlib import Path

from localkb.app import write_selfcheck_report


def test_report_has_utf8_bom(tmp_path: Path) -> None:
    path = write_selfcheck_report("LocalKB 自检\n  [OK  ] Runtime 连接  已连接", tmp_path)

    assert path is not None
    raw = path.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "缺 BOM：PowerShell 5.1 / 旧记事本会按 ANSI 解码成乱码"
    # BOM 之后是正常的 UTF-8
    assert "Runtime 连接" in raw[3:].decode("utf-8")


def test_report_path_is_under_given_dir(tmp_path: Path) -> None:
    path = write_selfcheck_report("x", tmp_path)
    assert path == tmp_path / "selfcheck.txt"


def test_report_overwrites_previous(tmp_path: Path) -> None:
    """重复自检不该出现新旧内容拼接。

    第一次是 FAIL、第二次是 OK 时，用户看到拼接的文本会以为还有问题。
    """
    write_selfcheck_report("old\n" * 50, tmp_path)
    write_selfcheck_report("new", tmp_path)

    text = (tmp_path / "selfcheck.txt").read_text(encoding="utf-8-sig")
    assert text == "new"
