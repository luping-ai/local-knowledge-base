# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：LocalKB 桌面端。

## 为什么用 --onedir 而不是 --onefile

onefile 每次启动都要把整个包解压到 `%TEMP%`：
  - PySide6 项目冷启动多 3~5 秒
  - 解压出来的临时文件会被杀毒反复扫描
  - 崩溃时 `%TEMP%` 里留下一堆残骸

onedir 启动 1 秒内，代价是目录里有很多文件（安装包会打成单文件，见 package-localkb.ps1）。

## 为什么排除这么多模块

PySide6 默认会把所有 Qt 模块（WebEngine、3D、Multimedia、Designer…）
全部拉进来，体积从 ~120 MB 涨到 400 MB+。本项目只用到 QtCore/Gui/Widgets。

`excludes` 里的每一项都要能说清"为什么不需要"——否则某天有人加了
一个功能（比如用 QWebEngineView 渲染 HTML）会得到"模块找不到"的
运行时错误，而不是打包失败。

## 为什么 localai 要用 collect_submodules

`luping-localai` 用动态 import 加载 `_discovery`（为了跨平台路径规则），
PyInstaller 的静态分析会漏掉它。`collect_submodules` 把整包显式列进
hiddenimports，比"漏了哪个再补"可靠。

## 数据文件

有三类必须随包：

  1. **LICENSE / NOTICE**（本项目的 Apache-2.0 与第三方组件清单）
  2. **THIRD-PARTY-NOTICES** —— 见下
  3. 图标（将来）

第 1 项不是"锦上添花"，是**合规义务**：Apache-2.0 第 4 条要求在分发物中
附带 LICENSE 与 NOTICE；LGPL（PySide6）要求附带其许可副本。实测发现
安装包里原本只有 install-manifest.json —— 不满足，已在此补上。

图标用 `datas=[('assets/icon.ico', 'assets')]`。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# 仓库根（本文件在 tools/ 下）
ROOT = Path(SPECPATH).parent

hidden = collect_submodules("localai")

# 合规文件：必须进包。
#
# ## 为什么 dest 写 "." 却出现在 _internal
#
# PyInstaller 的 onedir 模式把 datas 全部放进 `_internal/`（这是 6.x 的
# 行为：`_internal` 是"所有随包数据"的统一位置）。所以 `dest="."` 不等于
# "程序根目录"，而是 `_internal/`。
#
# 但这没关系 —— 真正给用户看的那份由 `package-localkb.ps1` 的 [2c] 步骤
# 复制到程序根目录，安装器再把它装到 `{app}` 下。
#
# ## 那为什么还留着 datas
#
# 为了"任何人用 spec 直接打包也合规"。如果只靠 PS 脚本复制，有人绕过脚本
# （`pyinstaller tools/localkb.spec`）就漏了合规文件。这里多留 18 KB 的
# 副本换来"任何构建路径都带许可"，值得。
_license_datas = [
    (str(ROOT / name), ".")
    for name in ("LICENSE", "NOTICE")
    if (ROOT / name).exists()
]

# 只保留实际用到的 Qt 模块。PySide6 的隐式导入很多，光靠 excludes
# 不够——这里用"显式排除大件"的方式，比"只 include 需要的"更稳
# （漏 include 会在运行时炸，漏 exclude 只是体积大）。
excludes = [
    # 本项目不用（Qt 的大件）
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtSql", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtSpatialAudio",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtHttpServer",
    "PySide6.QtNetworkAuth", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    # 开发期依赖，不该进发行包
    "pytest", "ruff", "setuptools", "pip", "wheel",
    "PyInstaller", "_pytest", "unittest", "pydoc", "doctest",
    # 本项目用不到的重型库
    #
    # ## PIL 不能排除（M6 踩到的坑）
    #
    # 这条原本写着 `"PIL"`，理由是"不做图像处理"。但 **python-pptx 依赖
    # Pillow**：`import pptx` 会经 `pptx.parts` 立即加载 `PIL.Image`，
    # 用于判断嵌入图片的格式。
    #
    # 后果是打包后**只有 PPTX 解析会崩**（ModuleNotFoundError），
    # 而开发环境装着 Pillow、单测全绿——典型的"单测通过但产品是坏的"。
    # 所以 PIL 必须留在包里，不能靠 excludes 省这十几 MB。
    #
    # 同理 `XlsxWriter`（python-pptx 的另一个依赖）也不能排除。
    "tkinter", "matplotlib", "numpy", "scipy", "pandas",
    "cv2", "IPython", "jupyter",
]

a = Analysis(
    [str(ROOT / "localkb" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_license_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalKB",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 会让杀毒误报率显著上升，且压缩后启动更慢
    console=False,      # GUI 子系统：不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalKB",
)

# 打包后裁剪交给 package-localkb.ps1 做。
#
# ## 为什么不在 spec 里做（试过，不生效）
#
# spec 是**被 exec 执行**的脚本，末尾代码在 COLLECT 完成后运行。但
# `DISTPATH` 在那里取到的值与预期不同（实测拼出的路径找不到文件），
# 于是裁剪静默跳过——而"跳过"与"删成功"在构建日志里长得一样。
#
# 放到 PowerShell 脚本里更可靠：路径由脚本自己算，删了什么会打印，
# 而且**可在构建后单独重跑**（调 spec 不划算：一次构建 2 分钟）。
