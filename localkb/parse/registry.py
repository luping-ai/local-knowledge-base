"""解析器分发：按扩展名选解析器，并把异常统一成 `ParseError`。

## 为什么要有这一层

三个理由，都是"没有它就会出问题"的：

1. **扩展名与内容不一致**：`.docx` 其实是 `.doc`（改了后缀）、`.pdf` 其实是
   一张图片。分发层要能识别并给出可读原因，而不是让底层抛
   `BadZipFile` 或 `KeyError`。

2. **异常类型不统一**：python-docx / openpyxl / PyMuPDF 各自抛不同的
   异常。扫描线程需要"任何一篇失败都不影响整体"（§10.5），所以必须
   在边界上把一切异常收敛成 `ParseError`。

3. **旧格式友好提示**：`.doc` / `.xls` / `.ppt` 是二进制老格式，python
   生态基本不支持。不提前拦截会得到"无法打开"这种无信息量的错误。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from localkb.parse.types import ParsedDoc, ParseError, UnsupportedError

__all__ = ["parse", "parser_for", "SUPPORTED_SUFFIXES"]

# 旧版二进制 Office 格式：明确不支持，但要说清怎么办
_LEGACY = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
    ".wps": ".docx",
    ".et": ".xlsx",
    ".dps": ".pptx",
}


def _parse_txt(p: Path) -> ParsedDoc:
    from localkb.parse.txt import parse_text

    return parse_text(p)


def _parse_md(p: Path) -> ParsedDoc:
    from localkb.parse.txt import parse_markdown

    return parse_markdown(p)


def _parse_docx(p: Path) -> ParsedDoc:
    from localkb.parse.docx import parse_docx

    return parse_docx(p)


def _parse_xlsx(p: Path) -> ParsedDoc:
    from localkb.parse.xlsx import parse_xlsx

    return parse_xlsx(p)


def _parse_pdf(p: Path) -> ParsedDoc:
    from localkb.parse.pdf import parse_pdf

    return parse_pdf(p)


def _parse_pptx(p: Path) -> ParsedDoc:
    from localkb.parse.pptx import parse_pptx

    return parse_pptx(p)


_REGISTRY: dict[str, Callable[[Path], ParsedDoc]] = {
    ".txt": _parse_txt,
    ".text": _parse_txt,
    ".log": _parse_txt,
    ".md": _parse_md,
    ".markdown": _parse_md,
    ".docx": _parse_docx,
    ".xlsx": _parse_xlsx,
    ".pdf": _parse_pdf,
    ".pptx": _parse_pptx,
}

SUPPORTED_SUFFIXES = frozenset(_REGISTRY)


def parser_for(path: Path) -> Callable[[Path], ParsedDoc] | None:
    """返回该扩展名对应的解析器，不支持时返回 None。"""
    return _REGISTRY.get(path.suffix.lower())


def parse(path: Path) -> ParsedDoc:
    """解析一个文件。

    ## 错误分类（决定用户看到什么、下一步做什么）

    | 情况 | 异常 | 用户该做什么 |
    | --- | --- | --- |
    | 老格式 `.doc` | UnsupportedError | 另存为 `.docx` |
    | 扫描件 PDF | UnsupportedError | 转文本 / 等 OCR |
    | 文件损坏 | ParseError | 检查文件 |
    | 有密码 | ParseError | 输入密码（将来支持） |
    | 扩展名伪装 | ParseError | 检查文件真实类型 |

    `UnsupportedError` 与 `ParseError` 分开，是因为**前者不该重试**
    （重试一百次还是不支持），而后者可能是暂态的（文件被占用）。
    """
    p = Path(path)
    if not p.exists():
        raise ParseError("文件不存在或已被移动。")

    suffix = p.suffix.lower()
    if suffix in _LEGACY:
        raise UnsupportedError(
            f"这是旧版 Office 格式（{suffix}），"
            f"请另存为 {_LEGACY[suffix]} 后重新扫描。",
        )

    fn = _REGISTRY.get(suffix)
    if fn is None:
        raise UnsupportedError(f"不支持的文件格式：{suffix or '（无扩展名）'}")

    try:
        doc = fn(p)
    except (ParseError, UnsupportedError):
        raise
    except MemoryError:
        raise ParseError(
            "文件太大，解析时内存不足。请拆分后重新扫描。",
        ) from None
    except OSError as e:
        # 权限 / 文件被占用 / 磁盘错误
        import errno

        if e.errno in (errno.EACCES, errno.EPERM):
            raise ParseError(
                "文件正被其他程序使用或没有读取权限。"
                "保存并关闭后会自动重试。",
                detail=str(e),
            ) from None
        raise ParseError("读取文件失败。", detail=str(e)) from None
    except Exception as e:
        # 这里刻意兜底：解析库的内部异常类型不可穷举（KeyError、
        # ValueError、AssertionError 都见过）。漏一个就会让整次扫描
        # 因为一个坏文件而中断。
        #
        # ## 但 SyntaxError / ImportError 要放行
        #
        # 它们是**我们自己代码或环境的问题**，不是"文件损坏"。把它们
        # 也包装成"格式异常或内容损坏"会在开发期造成极难排查的现象：
        # 明明写了语法错误，用户/开发者看到的却是"这个 PDF 损坏了"。
        # 实测踩到过：pdf.py 里一个引号嵌套错误被这条兜底吞掉，
        # 报错信息变成"无法解析这个文件（格式异常或内容损坏）"。
        if isinstance(e, (SyntaxError, ImportError, AttributeError,
                          NameError, TypeError)):
            raise
        raise ParseError(
            "无法解析这个文件（格式异常或内容损坏）。",
            detail=f"{type(e).__name__}: {e}",
        ) from None

    if not doc.blocks:
        raise ParseError("这个文件里没有可提取的文字。")
    return doc
