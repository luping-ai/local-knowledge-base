# LocalKB

个人本地知识库桌面端。把散落在硬盘上的文档（PDF / Word / Excel / PPT / Markdown）
变成一个可以提问、且**每条答案都能跳回原文那一页**的私有知识库。

全部在本机运行：不出网、不调外部 API、不需要账号。

---

## 它依赖另一个程序，但不包含它

LocalKB 是**客户端**。推理、向量化、知识库存储都在 **Local AI Hub**
（简称 Runtime）里，LocalKB 只做"扫描 → 解析 → 切块 → 提问 → 溯源"。

```
┌──────────────────┐         HTTP (127.0.0.1)         ┌──────────────────┐
│     LocalKB      │ ───────────────────────────────▶ │  Local AI Hub    │
│  （本仓库）       │                                  │  （另一个仓库）   │
│                  │                                  │                  │
│ 目录扫描         │                                  │ 向量化           │
│ 解析 PDF/Office  │                                  │ 向量与关键词索引  │
│ 切块 + 页码      │                                  │ RAG 拼接         │
│ 界面 + 溯源      │                                  │ 模型加载与推理    │
│ state.db         │                                  │ hub.db           │
└──────────────────┘                                  └──────────────────┘
```

**为什么不在安装包里自带一份 Runtime**：同一台机器只能有一份 Runtime
（单实例锁 + 固定数据目录）。装两份会互相争用端口、token 与数据目录，
而这类问题在用户现场几乎无法排查。所以 LocalKB 只连、不 spawn、不安装。

> ### 关于 Runtime 的获取方式
>
> Local AI Hub（Runtime）**不开源**，以免费安装包形式发布；本仓库（LocalKB）
> 开源，作为"如何用 Runtime + Python SDK 做应用"的参考实现。
>
> 安装包发布渠道**尚未开通**（发布前要先解决：代码签名、许可条款、模型许可
> 告知）。在此之前，本 README 中凡涉及 Runtime 的链接都不指向公开地址。
>
> 如果你是通过本仓库学习如何接入 SDK，不需要 Runtime 安装包 ——
> 装好 Runtime 后 `pip install luping-localai` 即可按本文档开发。

[hub]: # 占位（Runtime 仓库不公开，见上方「关于 Runtime 的获取方式」）

---

## 前置条件

1. **已安装 Local AI Hub 并启动**（当前为内测阶段，安装包需单独获取；
   见上方「关于 Runtime 的获取方式」）。
   未安装时 LocalKB 会正常启动，但会提示"没有检测到 Local AI Hub"
   并给出下载入口——不会假装能用。
2. Python 3.11+（开发机用 3.12 验证）。

---

## 快速开始

```powershell
# 1) 建虚拟环境
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
& .venv\Scripts\python.exe -m pip install -U pip

# 2) 装依赖（含 SDK，清单只有 pyproject.toml 一处）
& .venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3) 跑起来
& .venv\Scripts\python.exe -m localkb.app
```

第 2 步会从 PyPI 装 `luping-localai`（SDK）。**只在同时改 Runtime 源码时**
才需要换成本地 wheel，见下。

装包慢或卡住时换镜像：

```powershell
& .venv\Scripts\python.exe -m pip install -e ".[dev]" `
    -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30
```

实测：默认 PyPI 源装 `python-pptx` 卡了 4 分钟以上（下载中断、反复重试），
换清华镜像 30 秒完成。

### 依赖哪个版本的 SDK

SDK（`luping-localai`）与 Runtime 一起演进，**不锁精确版本**，只锁范围
（见 `pyproject.toml` 的 `dependencies` 与 `localkb/preflight.py` 的
`MAX_SDK_VERSION`）。

| 场景 | 做法 |
| --- | --- |
| **只跑桌面端 / 只想读代码** | 默认即可（第 2 步从 PyPI 装正式版） |
| **同时改 Runtime** | 从它构建出的本地 wheel 装：`pip install <path>\luping_localai-*.whl` |

第二种情况只对 Runtime 的维护者有意义 —— Runtime 仓库不公开。

**用旧 SDK 配新 Runtime 的典型症状**：溯源页码永远是"未知"。程序不报错，
只是功能静默降级——所以 `--self-check` 专门检查这一项。

---

## 测试

```powershell
& .venv\Scripts\python.exe -m pytest tests -q      # 全部（381 项）
& .venv\Scripts\python.exe -m ruff check localkb tests
```

| 类型 | 文件 | 需要 Runtime？ |
| --- | --- | --- |
| 纯逻辑 | `test_store_db` / `test_walker` / `test_scan_job` / `test_chunker` / `test_parsers` / `test_pdf_rules` / `test_parse_pptx` / `test_preflight` / `test_duplicates` / `test_dedup_similar` / `test_selfcheck_report` / `test_vectorize_gate` | 否 |
| 联调 | `test_runtime_conn` / `test_chat` / `test_ingest` / `test_maintain` / `test_ui_smoke` | **是**（服务未运行时自动跳过） |

联调测试**不 mock SDK**，因为要测的正是"与服务端的真实交互"（唤醒是否幂等、
discovery 陈旧时能否识别、换端口后能否恢复）。这些用 mock 一件都测不到。

**注意**：`test_chat.py` 里需要生成能力的用例依赖"模型已加载"。空闲卸载
（默认 90 秒 + 用户空闲 180 秒）会在你写代码时把模型卸掉——那些用例用
`loaded_conn` fixture 自己确保模型就绪（未加载就 load，失败就 skip）。

---

## 打包与安装

```powershell
# 完整（PyInstaller → 裁剪 → 冒烟 → 清单 → zip → Setup.exe，约 3 分钟）
powershell -ExecutionPolicy Bypass -File tools\package-localkb.ps1

# 只重打包（复用已有 dist-localkb）
powershell -ExecutionPolicy Bypass -File tools\package-localkb.ps1 -SkipBuild

# 排查"裁剪删错了某个文件"
powershell -ExecutionPolicy Bypass -File tools\package-localkb.ps1 -NoTrim

# 只出 zip，不编译安装向导
powershell -ExecutionPolicy Bypass -File tools\package-localkb.ps1 -NoInstaller
```

产物：

```
output\LocalKB-<ver>-Setup.exe      51.7 MB  ← 分发给用户
output\LocalKB-<ver>-win64.zip      73.8 MB  ← 绿色版（解压即用）
output\LocalKB-<ver>-manifest.json           ← 版本清单（含 exe sha256）
dist-localkb\LocalKB\              171.4 MB  ← 未压缩（调试用）
```

**打包会裁掉 68.6 MB**：`opengl32sw.dll`（19.7 MB，只用 QWidget 时不需要软件
OpenGL）与 PyMuPDF 的 16 个 `.onnx` 版面/表格模型（48.9 MB，我们只取文本层）。
裁剪后立刻跑冒烟测试——这两类文件删错会让某个功能"悄无声息地不能用"。

**安装向导**用 Inno Setup 6（`tools/localkb-installer.iss`），需要本机装
`ISCC.exe`；没有时可以加 `-NoInstaller` 只出 zip。

---

## 自检（现场诊断用）

```powershell
.\LocalKB.exe --self-check
```

检查 9 项：依赖导入、PDF/DOCX/XLSX/**PPTX** 解析、切块器、state.db、
Runtime 连接、SDK 与契约版本。结果**同时**写入
`%LOCALAPPDATA%\LocalKB\selfcheck.txt` 并弹窗显示。

（主 exe 是 GUI 子系统、没有 stdout，所以结果必须走文件 + 弹窗——
只 `print` 的话在正式包里什么都看不到。）

**自检不是"跑一遍 import"**：它真造文件并解析。实测踩到过 `import pptx` 会
加载 PIL，而打包时把 PIL 裁掉了 → 单测全绿、打包后只有 PPTX 崩。

---

## 目录结构

```
localkb/
├── app.py            入口：单实例、打开 state.db、建窗口、--self-check
├── runtime_conn.py   探活 / 唤醒 / Client 重建（← 最容易出错的一层）
├── preflight.py      SDK 版本 + 契约版本门禁
├── ingest.py         入库契约映射、错误分类
├── chat.py           提问编排、来源片段补查
├── dedup.py          相似检测（接 Runtime）与待确认管理
├── maintain.py       对账、删除、改名同步、换版本
├── parse/            PDF / DOCX / XLSX / PPTX / TXT / MD → 结构化块
├── chunk/            450/600/54 切块 + 标题路径 + 跨页不聚拢
├── scan/             walker（7 类 Windows 障碍）/ hasher / job
├── store/db.py       state.db（本地事实，不存块文本与向量）
├── platform/         回收站、打开文件
├── watch/            watchdog 监听（防抖、事件合并）
└── ui/               主窗口、知识库页、对话页、各对话框

tools/                打包、标定、端到端验证脚本
docs/                 设计文档、修改记录、实测记录、缺陷记录
tests/                381 项
```

---

## 文档在哪

| 文档 | 位置 |
| --- | --- |
| **设计方案**（2239 行，含全部决策与验收标准） | `docs/基于local-ai-runtime的个人本地知识库桌面端项目方案.md` |
| 里程碑落地记录 M0~M7 | `docs/修改记录/` |
| 实测数据（内存、检索耗时、规模标定） | `docs/实测记录/` |
| 发现的 Runtime 缺陷 | `docs/缺陷记录/` |
| **Runtime 侧待办** | 在 Runtime 仓库内（`docs/服务端改造增补需求-桌面端配套.md`） |
| **Runtime 改造方案** | 在 Runtime 仓库内（`docs/runtime改造方案-知识库解析下沉.md`） |

最后两份刻意留在 Runtime 仓库：它们的读者是要改 Runtime 的人，放在这边
只会让两份清单漂移。**Runtime 仓库目前不公开**，需要时请向维护者索取。

---

## 三条容易踩的坑（都已有回归测试）

### 1. 唤醒 Runtime 时轮询必须重建 Client

Runtime 停止时 `Client()` 的 discover 回落到默认端口（18765、token 空）；
唤醒后 Runtime 拿到的是**随机端口**。旧 client 记住死端口，于是无论等多久
都连不上。

症状极具误导性：安装器说"已启动"、`runtime.json` 已更新，但界面一直报
"启动超时"。实测修复前 27.7 秒失败，修复后 5.5 秒成功。

回归：`tests/test_runtime_conn.py::test_ensure_alive_rebuilds_while_polling`

### 2. `deleteLater()` 后 Python 包装器仍在

`QThread.finished` 里调 `deleteLater()` 会销毁 C++ 对象，但 Python 侧引用还在。
之后任何 `worker.isRunning()` 都会抛
`RuntimeError: Internal C++ object already deleted`。

实测症状：跑完一次扫描后**关窗口崩溃**在 `closeEvent`。

回归：`tests/test_ui_smoke.py::test_scan_runs_through_ui`（以 `win.close()` 结尾）

### 3. 路径要存两份

Windows 路径大小写不敏感、但**区分显示**。只存小写会让界面显示
`d:/工作/合同.pdf`，用户会怀疑这是不是自己的文件。

所以 `local_files` / `scan_roots` 都有 `path`（原样，展示用）与
`path_key`（归一化，唯一约束用）两列。

---

## 已知问题

| 问题 | 影响 | 说明 |
| --- | --- | --- |
| 相似检测对"填空模板"偏严 | 8 个填空版合同只成 2 组而非 1 组 | Runtime 的 `ThresholdShort=2` 是未校准的初值。偏严只会让用户多点几次确认，**不会误入库**。见 `docs/实测记录/20261116-相似阈值对模板文档过严.md` |
| 首次检索慢 | 10 万块的库首次检索 8.2 秒 | 索引载入的一次性成本（之后 1.7 秒），UI 需提示"正在载入索引" |
| **未做代码签名（已决定不买证书）** | 首次运行会弹 SmartScreen 警告 | 见下方「未签名安装包的说明」 |

### 未签名安装包的说明

本项目**不购买代码签名证书**。这是有意的决定：证书（OV 约 $200/年）对本阶段
的收益不足以覆盖成本 —— 而我们能做的是**把风险降到可核查**，而不是靠一张
证书让人"感觉安全"。

用户会看到什么：

```
Windows 已保护你的电脑
Microsoft Defender SmartScreen 阻止了无法识别的应用启动。
    [更多信息] → [仍要运行]
```

**这是正常的，不是中毒**。点「更多信息」→「仍要运行」即可。

#### 怎么自己验证安装包没被篡改

因为不签名，我们就必须提供**可独立核查**的凭据。每次发布都会同时公布：

| 凭据 | 在哪 |
| --- | --- |
| 安装包 SHA-256 | Release 说明里，与该版本的 `.exe` 并列 |

核对方法：

```powershell
# 1) 算出下载到的文件的哈希
Get-FileHash .\LocalAIHub-x.y.z-setup.exe -Algorithm SHA256

# 2) 与 Release 说明里的值逐字比对
```

哈希一致 = 文件与构建产物完全一致，没有被替换或注入。

**这比签名弱在哪**：签名能证明"是谁构建的"，哈希只能证明"与我公布的一致"。
如果发布渠道本身被攻破，两者都会失守 —— 但哈希至少让"下载站偷偷换了包"
这类中间人替换失效。

> **当前还未接入 CI**：现在的包是本地跑 `tools/package-localkb.ps1` 产出的，
> 哈希由该脚本算出并写进 `output/LocalKB-<ver>-manifest.json`。接入 CI 之后
> 才有第三方可见的构建日志 —— 在那之前，"可核查"的范围只到"你手上的文件
> 与 Release 说明一致"。

#### 如果你想自己从头构建

这是最彻底的一种信任：不信任任何下载，自己编。

```powershell
git clone https://github.com/luping-ai/local-knowledge-base.git
cd local-knowledge-base
# 按 README 的「快速开始」建环境
powershell -ExecutionPolicy Bypass -File tools\package-localkb.ps1
```

代价是要装 Python + Inno Setup 6，约 10 分钟。收益是你完全知道跑的是什么。

#### 如果你在企业环境

IT 部门可以用组策略或 `Add-MpPreference -ExclusionPath` 排除本程序，
或先用上面的哈希核对后再放行。我们不会提供"关闭 Defender"之类的建议 ——
那是把风险转嫁给用户。


---

## 许可

**待定**。本仓库尚未添加 `LICENSE` 文件。

### 一个绕不过去的约束：PyMuPDF 是 AGPL-3.0

PDF 解析用 **PyMuPDF**，它只有两种授权：**AGPL-3.0** 或 **商业授权**（付费）。

本仓库既然要开源，实际可选的就两条：

| 方案 | 代价 |
| --- | --- |
| **整体 AGPL-3.0** | 任何基于本仓库修改并**通过网络提供服务**的人，也必须以 AGPL 公开其源码。这是 AGPL 比 GPL 多出来的那一条 |
| **买 PyMuPDF 商业授权 + 本仓库用宽松许可（如 Apache-2.0）** | 花钱，但允许别人闭源使用本仓库的代码做二次开发 |

**这两个选择对"样板项目"的定位影响很大**：

- 选 AGPL：代码完全开放，但商业公司不敢基于它做闭源产品（法务一律劝退）
- 选 Apache-2.0 + 买授权：对公司最友好，别人可以照着改、拿走用在自家产品里

如果目标是"让别人参考我们怎么用 SDK"，AGPL 的传染性反而**降低**参考价值 ——
很多公司内部禁止引入 AGPL 代码。

> **注意**：以上是技术层面的后果，不构成法律意见。真要发布前建议找律师确认
> AGPL 的网络服务条款是否覆盖本项目形态。

其余依赖都是宽松许可（PySide6 是 LGPLv3，`--onedir` 动态链接通常满足；
python-docx / openpyxl / python-pptx / watchdog 都是 MIT 或 Apache-2.0）。
完整清单与逐项判断见方案 §13.5。
