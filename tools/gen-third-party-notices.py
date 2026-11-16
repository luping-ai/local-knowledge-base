"""生成 `THIRD-PARTY-NOTICES.txt`：把依赖的真实许可文本收集到一处。

## 为什么需要这个脚本，而不是手工维护一份清单

手写清单必然过期：加一个依赖、升一个版本，都没人会记得回来改。而许可证的
准确性是**法律问题**，不是文档质量问题 —— 一份漏了 AGPL 组件的清单，
会让下游用户以为可以闭源分发。

所以从**已安装的包元数据**里读，这是唯一真实来源。

## 它收集什么

对每个运行期依赖：

  1. `dist-info/METADATA` 里的 `License` / `License-Expression` 字段
  2. `dist-info/licenses/` 下的许可原文（PEP 639 之后逐渐普及）
  3. `dist-info/LICENSE*` 单文件（老式布局）

收集不到的（如 PySide6 的 LGPL 全文不随 wheel 发布）会明确标出
"需从官方获取"并给链接 —— 比装作收齐了要诚实。

## 用法

    python tools/gen-third-party-notices.py
    # 生成到仓库根的 THIRD-PARTY-NOTICES.txt

由 `tools/package-localkb.ps1` 在打包前自动调用。
"""

from __future__ import annotations

import importlib.metadata as md
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

__all__ = ["main"]

# 运行期会进安装包的依赖（与 pyproject 的 dependencies 对应，加上它们的传递依赖）
PACKAGES = [
    "PySide6",
    "shiboken6",
    "PyMuPDF",
    "pymupdf-layout",
    "pymupdf4llm",
    "python-docx",
    "openpyxl",
    "python-pptx",
    "watchdog",
    "luping-localai",
    "Pillow",
    "lxml",
    "XlsxWriter",
    "numpy",
    "onnxruntime",
]

# 需要人工补充获取途径的组件（许可全文不随 wheel 发布）
MANUAL_HINTS = {
    "PySide6": (
        "LGPL-3.0 全文不随 PySide6 的 wheel 发布。获取途径：\n"
        "  https://www.gnu.org/licenses/lgpl-3.0.txt\n"
        "Qt 的许可说明：https://www.qt.io/licensing/open-source-lgpl-obligations\n"
        "\n"
        "使用 LGPL 分支的三项义务（本项目已满足）：\n"
        "  1. 动态链接（PyInstaller --onedir：Qt 是独立 .dll）\n"
        "  2. 允许用户替换这些 DLL（_internal/PySide6/ 下可直接替换）\n"
        "  3. 声明使用了 Qt 并提供其许可副本（即本文件）"
    ),
    "PyMuPDF": (
        "本项目使用 AGPL-3.0 分支（未购买 Artifex 商业授权）。\n"
        "全文：https://www.gnu.org/licenses/agpl-3.0.txt\n"
        "\n"
        "**AGPL 的关键义务**：分发二进制需提供对应源码；通过网络提供服务时，\n"
        "也需向使用者提供源码。若需闭源分发，见 NOTICE 第四节的三条路。"
    ),
    "pymupdf-layout": "许可随 PyMuPDF 一并授权（AGPL-3.0 或 Artifex 商业）。",
    "pymupdf4llm": "许可随 PyMuPDF 一并授权（AGPL-3.0 或 Artifex 商业）。",
    "openpyxl": (
        "MIT 许可。wheel 未附带许可原文，全文见：\n"
        "  https://foss.heptapod.net/openpyxl/openpyxl/-/blob/branch/3.1/LICENCE.rst\n"
        "MIT 不要求随分发物附带全文，但仍需保留版权声明与许可声明（见本文件）。"
    ),
}


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    """取一个发行版随包附带的许可文件原文。"""
    out: list[tuple[str, str]] = []
    # PEP 639：dist-info/licenses/
    for f in (dist.files or []):
        name = str(f)
        low = name.lower()
        if "license" not in low and "copying" not in low:
            continue
        if not low.endswith((".txt", ".md", ".rst", "")):
            continue
        try:
            text = dist.locate_file(f).read_text(encoding="utf-8", errors="replace")
        except (OSError, AttributeError):
            continue
        if len(text.strip()) < 40:      # 太短的跳过（多是占位）
            continue
        out.append((name, text))
        if len(out) >= 3:               # 每个包最多 3 份，避免文件爆炸
            break
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out_path = root / "THIRD-PARTY-NOTICES.txt"

    lines: list[str] = [
        "LocalKB - 第三方组件许可",
        "",
        "本文件由 tools/gen-third-party-notices.py 从已安装的包元数据自动生成。",
        "重新生成：python tools/gen-third-party-notices.py",
        "",
        "本项目的**源码**采用 Apache-2.0（见 LICENSE）。但由我们构建并分发的",
        "安装包因捆绑了 AGPL-3.0 的 PyMuPDF，整体分发需遵守 AGPL-3.0 ——",
        "详见 NOTICE 第一节。",
        "",
        "=" * 78,
        "",
        "组件清单",
        "",
        "=" * 78,
        "",
    ]

    missing: list[str] = []
    texts: list[tuple[str, str, str]] = []

    for name in PACKAGES:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        meta = dist.metadata
        ver = meta.get("Version", "?")
        lic = meta.get("License-Expression") or meta.get("License") or ""
        lic = " ".join(str(lic).split())[:80]
        if not lic:
            cls = [c for c in (meta.get_all("Classifier") or []) if "License" in c]
            lic = cls[0].split("::")[-1].strip() if cls else "（未见声明）"
        home = meta.get("Home-page") or ""
        lines.append(f"  {name} {ver}")
        lines.append(f"    许可: {lic}")
        if home:
            lines.append(f"    主页: {home}")

        hint = MANUAL_HINTS.get(name)
        if hint:
            lines.append("")
            for hl in hint.splitlines():
                lines.append(f"    {hl}")

        got = _license_texts(dist)
        if got:
            for fname, text in got:
                texts.append((name, fname, text))
        elif not hint:
            missing.append(f"{name}（未随包附带许可原文）")
        lines.append("")

    if missing:
        lines.append("-" * 78)
        lines.append("以下组件未随 wheel 附带许可原文，请到其主页确认：")
        for m in missing:
            lines.append(f"  · {m}")
        lines.append("")

    if texts:
        lines.append("=" * 78)
        lines.append("")
        lines.append("许可原文（随包附带的部分）")
        lines.append("")
        lines.append("=" * 78)
        for name, fname, text in texts:
            lines.append("")
            lines.append("-" * 78)
            lines.append(f"{name} — {fname}")
            lines.append("-" * 78)
            lines.append("")
            # 许可证全文很长，但必须完整提供（截断等于没提供）
            lines.append(text.rstrip())
            lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"已生成 {out_path.name}（{size_kb:.1f} KB，{len(PACKAGES)} 个组件）")
    if missing:
        print(f"  注意：{len(missing)} 个组件未附带许可原文，已在文件里列出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
