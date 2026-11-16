# 基于 local-ai-runtime 的个人本地知识库桌面端 —— 项目方案

> 代号：**LocalKB**（正式名称待定）
> 定位：local-ai-runtime + Python SDK 的**第一个正式产品**
> 状态：方案（设计意图，不代表实现现状）
> 版本：v1.3（v1.1 按代码复核修正；v1.2 定案 SDK/resources、删除语义、AI 提炼、chat/search 分工；v1.3 把服务端需求抽成独立文档）
> **前置依赖（两份，都要就绪）**：
> 1. `docs/runtime改造方案-知识库解析下沉.md` —— 解析下沉主干（M1 才能开工）
> 2. `docs/服务端改造增补需求-桌面端配套.md` —— 五项补齐（S1/S2 硬阻塞）
>
> 依据：`local-ai-runtime.md`、`docs/知识库与检索-补充方案.md`、`docs/实测记录/20261112-大模型RAG能力实测.md`、ADR 0001 / 0008 / 0009 / 0011 / 0016

**本文档只写桌面端的设计意图。服务端要改什么，一律写在 `docs/服务端改造增补需求-桌面端配套.md`，不在这里展开。**

**两条不可动摇的产品边界（v1.1 明确）**：

1. **不重复装 Runtime**：安装时若检测到本机已装 `LocalAIHub`，**复用它**，不再装第二份 Runtime、不写第二份 discovery/lock。
2. **模型与知识库服务端配置归服务端**：模型下载、模型加载、Embedding 模型配置、Runtime 参数，全部由 LocalAIHub 的管理后台负责。桌面端**不下载、不加载、不改配置**，只读状态并在缺失时引导用户去管理后台。

这两条不是"更省事"，而是为了避免同一台机器上出现两份 Runtime、两套数据目录、两个 token 来源——那类问题在用户现场无法远程排查（§3.1）。

---

## 0. 一页摘要

用户装一个安装包，选一个文件夹，程序扫描里面所有文档、建成本地知识库；然后在聊天窗口里提问，答案附带"来自哪个文件、第几页、哪一段"，点一下直接跳到原文。

**技术形态（四条）：**

1. **Runtime 是向量与模型的唯一执行者**——向量化、向量存储、关键词索引、RRF 融合、RAG 拼接、模型调度全在它里面。**它不解析文档、不切块，只接收已切好的文本块。**
2. **桌面端负责"从文件到文本块"**——目录扫描、文件监听、文档解析、切块（含页码/标题路径）、界面、来源卡片、系统集成。
3. **两者之间只走 `luping-localai` SDK**（已发布 PyPI，零运行时依赖，方法清单见 §6.2）。
4. **模型与服务端参数归 LocalAIHub 管理后台**——桌面端只读状态、只做引导（见文首两条产品边界）。

**开发顺序是硬约束**：先改 Runtime（见配套文档），再做桌面端。理由见 §1.3——桌面端的核心价值（页码溯源）依赖 Runtime 先提供字段，反过来做会返工。

**四个必须记住的数字**：

| 项 | 值 | 含义 |
| --- | --- | --- |
| 10 万 | 单库 chunk 上限 | 超出必须拆库，不是"尽力支持" |
| 2000 | 单次入库块数上限 | Runtime 硬约束，决定单文档可入库的最大体量（§5.7） |
| 450/600/54 | 切块参数（字符） | 来自实测 0.63 token/字，不是估算 |
| 0 次 | 允许的静默降级 | 每次降级都必须让用户看见（§8.5） |

**内存预算不在本文档写死**：8GB 机器的可用上限必须由极限测试标定后回填（§10），在那之前不要引用任何具体 GiB 数字。

---

## 1. 目标与前置条件

### 1.1 目标

| 编号 | 目标 | 可验收形式 |
| --- | --- | --- |
| G1 | 一键扫描本机文档目录，建立本地知识库 | 选目录 → 扫描 → 进度可见 → 完成后可提问 |
| G2 | 聊天式问答，答案**只依据**知识库内容 | 资料中没有时明确回答"未找到依据"，不编造 |
| G3 | **答案可溯源**：文件名 + 页码 + 段落，可点击跳转 | 点击来源卡片能打开原文件并定位到页 |
| G4 | 增量更新：新增/修改/删除文件后索引自动跟上 | 改一个文件后 30 秒内索引状态变化 |
| G5 | 8GB 内存电脑可用，全程不联网 | 峰值内存不超过 §10 极限测试标定值；抓包无外发 |
| G6 | 一个安装包，非开发者可装完即用 | 双击 → 下一步 → 完成 → 可用（已装 LocalAIHub 时复用，不重复装） |
| G7 | 发现重复与相似文件，提醒用户确认入库版本 | 见 §12；**只建议，不自动删，不移动文件** |
| G8 | 与 LocalAIHub 共存不冲突 | 同一台机器上只有一份 Runtime、一份数据目录；桌面端不写它的配置 |

### 1.2 非目标（v0.1 明确不做）

- 多人协作、账号体系、权限、云端同步。
- 移动端、Web 版。
- **模型下载、模型加载、Embedding 模型配置、Runtime 启动参数**（归 LocalAIHub 管理后台，见 §3.1.1）
- 扫描件 OCR（见 §16 P1）。
- 图片/扫描件的近似判重（pHash）。
- 多跳推理、Agent 工具链、联网检索。
- Reranker 精排（需额外模型 = 额外内存，低配上收益不明显）。
- **用大模型"提炼/改写"文档内容后再入库**（会破坏页码溯源，§5.8）。文档结构问题用确定性规则解决。
- macOS / Linux 发行（v0.1 只做 Windows，架构上不阻塞）。
- 任何形式的**批量**"一键清理/删除文件"（ADR 0015）。单篇文档的显式删除是允许的，见 §7.7。
- **自带一份 Runtime 或第二个数据目录**（见 §3.1，同一台机器只允许一个实例）

### 1.3 为什么必须先改 Runtime

桌面端的核心价值是 G3（溯源）。而溯源所需的字段（`page_start` / `heading_path`）必须由 Runtime 存储并透出。如果反过来先做桌面端：

1. 来源卡片只能显示文件名 → 界面做完还得推翻。
2. 客户端解析出的页码无处存放，只能自己另存一份 → 立刻变成 §4.2 要避免的"双份真相"。
3. 切块器只能先照抄 Go 的实现，而 Go 有两个已知缺陷（§5.5）→ 抄完再改 = 双重返工。

**阶段划分**：

```
阶段 A：Runtime 改造（配套文档）     ← 必须先完成
   交付：documents/chunks 端点、四列、chunker 版本、溯源字段透出、SDK 新方法
   退出条件：能用 curl 提交块并检索到 page_start

阶段 B：桌面端（本文档）             ← 阶段 A 完成后开工
   交付：安装包
```

阶段 A 期间可以做**不依赖新契约**的桌面端工作：目录扫描、文件监听、`state.db`、连接已运行的 Runtime（discovery 探活）。这些见 §14 的 M0。

### 1.4 与外部方案的关键分歧

| 外部方案建议 | 本方案 | 理由 |
| --- | --- | --- |
| Qdrant 单机版 | **不用**，沿用 Runtime 的 SQLite BLOB + 内存暴力扫描 | 10 万 chunk 实测 7ms；Qdrant 同规模约 1~2ms，用户不可感知，代价是多分发一个 Rust 服务、0.8GB 内存预算（ADR 0010） |
| Python 侧解析 + Go 侧再切一遍 | **解析与切块都只在 Python 做**，Runtime 不再切 | 两套切块器会让页码/偏移对不上。只有一个实现，就不存在不一致 |
| 8GB 用 3B Q4 | **服务端基线用 Qwen2.5-1.5B Q4**（模型选择归服务端） | 3B 实测 ctx=512 就要 3.1 GiB；1.5B 是 1.6 GiB，且 RAG 用例 13/13 + 拒答 3/3（`internal/degrade/capability.go` 已硬编码为基线） |
| 桌面端自带一份 Runtime 并管理模型 | **复用已装 LocalAIHub 的 Runtime，模型由后台管理** | 否则同机会有两份实例锁、两套 discovery、两个 token 来源，且版本锁定无法保证（§3.1） |
| 桌面端自己判断内存是否够 | **只读服务端的能力与资源状态** | 内存的判断需要 RSS 外推与实测基线（`internal/degrade`），客户端复刻必然分叉（§10.4） |

---

## 2. 产品形态

### 2.1 主界面

```
┌──────────────────────────────────────────────────────────────┐
│  LocalKB                                        ⚙  ·  —  ×  │
├───────────────┬──────────────────────────────────────────────┤
│               │                                              │
│  知识库        │   保修期多久？                                │
│  ┌─────────┐  │   ───────────────────────────────────────    │
│  │ 我的文档 │  │   根据资料，设备保修期为两年。 [1]            │
│  │ 1,204 篇 │  │                                              │
│  └─────────┘  │   依据：                                      │
│               │   ┌────────────────────────────────────┐    │
│  + 新建知识库  │   │ [1] 售后服务手册.pdf  第 3 页        │    │
│               │   │     第三章 保修条款 > 3.2 保修期限   │    │
│  ─────────    │   │ "……整机保修期为两年，自购买之日起……" │    │
│  扫描状态      │   │                        打开原文 ↗  │    │
│  ● 索引中 63%  │   └────────────────────────────────────┘    │
│               │                                              │
│               │   ┌────────────────────────────────────┐    │
│               │   │ 输入问题…                        ➤ │    │
├───────────────┴──────────────────────────────────────────────┤
│ 模型 qwen2.5-1.5b  ·  可用内存 4.6 GB  ·  知识库已启用        │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 三个页面

| 页面 | 内容 | 必须展示的状态 |
| --- | --- | --- |
| **知识库** | 知识库列表、文档列表、扫描进度、失败原因、待确认提示条、**missing 汇总**、**删除入口** | 每篇文档的 `ACTIVE/PENDING/MISSING/FAILED` 与 `PENDING/PARTIAL/READY` |
| **对话** | 消息流、来源卡片、输入框、**知识库开关（不使用 / 选中 / 全部）** | 排队位置、模型加载中、降级提示、知识库未命中 |
| **检索** | 查询框、结果列表（`keyword_rank` / `semantic_rank`）、**只读的库范围说明** | 必须选库（§8.1.1） |
| **设置** | 扫描目录与排除目录、重复提示策略、**missing 处理策略**、诊断 | 能力等级（full/chat_only/rule_only）与原因 |

**设置页不做模型管理**。模型下载/加载、Embedding 模型配置都在 LocalAIHub 管理后台（§3.1.1）。桌面端只显示"当前模型 + 是否可用"，并提供"打开管理后台"的跳转。

**重复文件确认不是一个页面**，是知识库页上的一条提示 + 一个对话框（§12.7）。

**检索可以是独立页面，也可以是对话页里的一个"只查不答"开关**。两种都行，但**不能与对话共用那个知识库开关**——对话允许"不用知识库"，检索不允许。

### 2.3 状态栏（常驻）

必须显示三项，因为它们决定用户能不能用：

- 当前模型（含是否降级）
- 服务端资源状态（`pressure` 与可用内存，来自 `/api/v1/resources`）
- 知识库是否可用（`rag_enabled` + 知识库端点是否注册）

**数据来源是服务端，不是桌面端自己算**：桌面端只渲染，不判断（§10.4）。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│  LocalKB 桌面端（Python 3.11 + PySide6，冻结为 exe）              │
│                                                                  │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌────────────┐  │
│  │ 扫描器      │ │ 监听器      │ │ 聊天 UI       │ │ 来源卡片    │  │
│  │ (os.walk)  │ │ (watchdog) │ │ (QThread)    │ │ + 文件跳转  │  │
│  └─────┬──────┘ └─────┬──────┘ └──────┬───────┘ └─────┬──────┘  │
│        │              │               │               │          │
│  ┌─────▼──────────────▼───────────────┤               │          │
│  │  解析器（PyMuPDF / python-docx /    │               │          │
│  │  openpyxl / python-pptx）           │               │          │
│  │  → 切块（450/600/54 + 标题路径      │               │          │
│  │    + 页码 + 块类型）                │               │          │
│  └─────┬───────────────────────────────┘               │          │
│        │                                               │          │
│  ┌─────▼──────────────┐                                │          │
│  │  state.db          │ ← 本地事实：path↔doc_id↔sha256 │          │
│  │                    │    目录/排除规则/待确认项      │          │
│  └─────────┬──────────┘                                │          │
└────────────┼───────────────────────────────────────────┼──────────┘
             │  已切好的文本块 + 元数据                    │
             │  luping-localai SDK（自动发现，无需配置端口）│
┌────────────▼───────────────────────────────────────────▼──────────┐
│  Local AI Hub Runtime（Go，零 CGO）                               │
│  ← 不解析文档、不切块                                             │
│  接收文本块 → Embedding → 向量(SQLite) + bigram/FTS5 索引         │
│  检索 bigram+FTS5 ⊕ 语义 → RRF → RAG 拼接（防注入+预算）          │
│  模型准入 / 能力评估 / 调度 / SSE / 指标 / 诊断                   │
└──────────────────────────────┬───────────────────────────────────┘
                               │ 127.0.0.1 + random token
┌──────────────────────────────▼───────────────────────────────────┐
│  llama-server 子进程（Job Object 内存/CPU 硬限额）                │
│    · 生成模型：qwen2.5-1.5b-q4（8G）/ 3b-q4（16G）                │
│    · Embedding：multilingual-e5-small-q8                          │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 进程模型：复用唯一实例，不装第二份 Runtime

```
LocalKB.exe  (Python, 单进程 + Qt 事件循环 + 若干 QThread)
   │ 只做客户端：连"已经存在的那个 Runtime"
   │   ① client.live() 探活 → 成功即用（SDK 自己按 LAIH_URL → discovery → 默认端口查找）
   │   ② 探活失败 → 调 localai-cli-installer.exe start（按 launch-spec.json 拉起），再探测
   │   ③ 安装器也不存在 → 引导先安装 LocalAIHub
   ▼
localai-cli-launcher.exe（GUI 子系统，由安装器写进 HKCU Run）
   └─ localai-cli.exe serve ...   ← 参数只有一份，来自 launch-spec.json
          └─ llama-server.exe      ← Runtime 用 Job Object 管
```

**桌面端不 spawn Runtime、不拼 `serve` 参数、不写 `launch-spec.json`。** 三条理由：

1. **同一台机器只能有一份 Runtime**。`discovery.EnsureSingleInstance()` 在 `serve` 的最前面，第二个实例直接 `ErrAlreadyRunning`。桌面端若自己 spawn，成功时连的是别人的实例、失败时用户看到"服务起不来"。
2. **模型与参数归服务端**（文首第二条边界）。`-embedding-model`、`-models`、`-registry`、`-idle-unload-sec` 全在 `launch-spec.json` 里，由安装器 `runtimeArgv` 唯一构造。桌面端再造一份必然漂移——`docs/修改记录/` 里"装完能用、重启后不能用"就是这个缺陷的实例。
3. **版本一致性**。桌面端不锁 Runtime 版本，只校验 API 契约下限（§13.4）。硬锁版本会让"用户升级了 LocalAIHub"变成"桌面端用不了"。

#### 3.1.1 安装与启动的完整流程

**安装时（LocalKB 安装器）**：

| 检测项 | 判定方式 | 已存在时的行为 |
| --- | --- | --- |
| LocalAIHub 程序目录 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalAIHub` 的 `InstallLocation`（安装器 `registerUninstall` 写入）；读不到时回退检测 `%LOCALAPPDATA%\Programs\LocalAIHub\localai-cli.exe` | **跳过 Runtime 组件安装**，不复制 exe、不写 Run 键、不动 `launch-spec.json` |
| Runtime 正在运行 | `localai-cli-installer.exe status` 或 `client.live()` | 不重启它（重启会打断用户正在进行的工作） |
| API 契约版本 | `/health/ready` 或 `/api/v1/capability` 响应 | 低于本文档要求的最小契约版本时，只提示"请升级 LocalAIHub"，不阻止安装 |
| 用户数据目录 | `%LOCALAPPDATA%\LocalAIHub` | 一律不动 |

未检测到 LocalAIHub 时**不是"自己装一份精简 Runtime"**，而是运行随包附带的 LocalAIHub 安装程序（同一个 `LocalAIHub-x.y.z-setup.exe`，静默参数），装完再继续。这样两条路径最终产生的目录结构、注册表项、`launch-spec.json` 完全一致——只有一份实现。

**每次启动时（LocalKB.exe）**：

```
1. client.live() 通？ ──是──→ 3
   │否
2. 调 localai-cli-installer.exe start（幂等：已在跑则只打印后台地址），再轮询 live()
   轮询 client.live()，最多 20s
   ├─ 通了 → 3
   └─ 仍不通 → 显示"本地服务未启动" + 两个动作：
              [打开管理后台] [查看日志目录]
3. 读 /api/v1/capability + /api/v1/resources，进入主界面
```

**为什么用 `live()` 探活而不是"文件存在即认为可连"**：Runtime 异常退出会留下 `runtime.json`（`writer.Remove()` 只在正常退出时执行），SDK 的 `_read_discovery_file` 也不探活。只判文件存在会拿到一个死端口，用户看到的是"连接被拒绝"而不是"服务没启动"。

**陈旧 discovery 不删除**：它不是桌面端的文件，删除属于越权修改服务端状态；而且并发场景下另一个进程可能正在写它。探活失败时**忽略它**（SDK 之后会重读，Runtime 重启会覆盖）。

**唤醒失败要给出可执行动作**，不能只说"服务未启动"：日志目录是 `%LOCALAPPDATA%\LocalAIHub\runtime.log`，管理后台是 `http://<base_url>/admin/`（token 在 discovery 文件里，或让用户在后台输入）。

#### 3.1.2 进程监管的边界

| 场景 | 桌面端做什么 | 不做什么 |
| --- | --- | --- |
| Runtime 正常在跑 | 只发请求 | 不重启、不改配置 |
| Runtime 未运行 | 调 `installer start` | 不自己拼 `serve` 参数 |
| Runtime 崩溃（`live()` 连续失败） | 显示"正在恢复"，退避重试探活；连续 3 次失败后调一次 `installer start` | 不做无限重启循环（会掩盖真实故障），不杀进程 |
| Runtime 版本低于契约要求 | 提示升级 + 禁用依赖新字段的功能 | 不静默降级（§8.5） |
| 用户关掉 LocalKB | 什么都不做 | **不 stop Runtime**（它服务其他客户端，且有空闲卸载） |

**`Client` 必须可重建**：SDK 的 `Client.__init__` 只 `discover()` 一次，把 base_url/token 缓存住。Runtime 重启会换端口、token 轮换会换 token，旧 client 之后全是 `ErrRuntimeUnavailable`/401。`sdk_bridge.py` 的规则：

- 连接类错误（`ErrRuntimeUnavailable` / `ErrUnauthorized`）→ **丢弃旧 client，重建一次**并重试该请求；
- 重建后仍失败才向用户报错（避免把"服务正在重启"渲染成一片红灯）；
- 重建有 2 秒防抖，避免每个请求都重读 discovery 文件；
- 重建后主动刷新一次 `capability()` / `resources()`，因为新实例的能力评估可能与旧的不同（模型被换过）。

### 3.2 职责边界

| 能力 | 归谁 |
| --- | --- |
| 目录遍历、扩展名过滤、排除规则 | **桌面端** |
| 文件监听、sha256 变更检测 | **桌面端** |
| 文档解析（PDF/DOCX/XLSX/PPT） | **桌面端** |
| 切块（含页码、标题路径、块类型） | **桌面端** |
| 文件级重复清单（sha256 分组） | **桌面端**（§12.3） |
| 相似检测的指纹与分组 | Runtime（Phase 2） |
| 向量化、向量存储、向量检索 | Runtime |
| 关键词索引（bigram + FTS5） | Runtime |
| RRF 融合、RAG 拼接、防注入、预算裁剪 | Runtime |
| 模型加载/准入/调度、SSE | Runtime |
| **模型下载、模型加载、Embedding 模型配置** | **LocalAIHub 管理后台** |
| **内存/资源是否够用的判断** | **Runtime**（`/api/v1/resources`、`/api/v1/capability`） |
| **Runtime 启动参数、进程生命周期** | **LocalAIHub 安装器 / launcher** |
| 文件路径映射 | **桌面端**（不通过 API 暴露，§6.3） |
| 打开原文件、跳页 | **桌面端** |

**为什么分词、融合、拼接必须留在 Runtime**：见配套文档 §3.1。一句话——这三件事必须与检索侧完全一致，散给多个客户端做必然分叉。

**为什么内存判断不能放桌面端**：判断依据是模型清单里的实测 RSS 外推（`internal/degrade/capability.go`、`model.Admit`），不是"剩多少字节"。客户端复刻这套公式，一旦清单更新就会两边结论不一致，而用户只会看到"桌面端说能用、服务端说不能用"。

### 3.3 两个数据库，字段互斥

| 表 | 归谁 | 内容 |
| --- | --- | --- |
| `documents` / `document_chunks` / `chunk_fts` / `kb_index_config` | Runtime | 已入库内容的唯一真相 |
| `local_files` / `scan_roots` / `excludes` / `settings` / `pending_dups` / `local_fingerprints` / `messages` | **桌面端** | 本地事实：路径、已见 hash、扫描规则、待确认项、对话历史 |

桌面端**不存向量、不存块文本**（`local_fingerprints` 只存指纹，见 §12.9）。两侧字段集合互斥，不会互相矛盾。

**`messages` 表是 v1.1 新增**：对话历史是产品概念，Runtime 明确不做会话持久化（§16 P5）。没有它，用户关掉程序就丢失全部问答记录，而溯源卡片需要历史消息才能复现。

---

## 4. 桌面端模块划分

```
localkb/
├── app.py                  # 入口：单实例、连 Runtime、建主窗口
├── runtime_conn.py         # discovery 探活 + 唤醒服务 + Client 重建（§3.1）
├── sdk_bridge.py           # 对 luping-localai 的薄封装（重试、能力缓存）
├── scan/
│   ├── walker.py           # 目录遍历、扩展名过滤、排除规则、跳过策略（§7.5）
│   ├── hasher.py           # sha256（分块读，8MB buffer）
│   └── job.py              # 顺序导入队列 + 进度 + 取消
├── parse/
│   ├── pdf.py              # PyMuPDF：block + 字号 + 页码
│   ├── docx.py             # python-docx：Heading 树
│   ├── xlsx.py             # openpyxl：表头 + 行组
│   ├── txt.py              # 纯文本 / Markdown
│   └── pptx.py             # python-pptx：按页 + 标题占位符（M6）
├── chunk/
│   ├── splitter.py         # 450/600/54 + 段落→句末→弱边界→硬切
│   └── heading.py          # 标题路径拼装
├── watch/
│   └── watcher.py          # watchdog + 2s 防抖 + 启动对账
├── dedup/
│   └── exact.py            # sha256 分组（§12.3）
├── store/
│   ├── db.py               # state.db 连接与迁移
│   ├── files.py            # local_files / scan_roots CRUD
│   └── messages.py         # 对话历史（§9）
├── ui/
│   ├── main_window.py
│   ├── kb_page.py          # 知识库 + 文档列表 + 扫描进度 + 待确认/缺失提示条
│   ├── chat_page.py        # 消息流 + 输入框 + 知识库开关（§8.1.1）
│   ├── search_page.py      # 只查不答（§8.4），必选库
│   ├── source_card.py      # 来源卡片 + 打开原文 / 查看片段（§7.7.2）
│   ├── dedup_dialog.py     # 确认入库版本（§12.7）
│   ├── delete_dialog.py    # 移出库 / 同时移到回收站（§7.7）
│   ├── settings_page.py    # 目录/排除/重复策略/缺失策略/诊断（无模型管理）
│   └── status_bar.py
└── platform/
    ├── open_file.py        # 打开文件 + 跳页 + 定位片段（§8.3）
    └── recycle.py          # 移到回收站（SHFileOperationW + FOF_ALLOWUNDO，§7.7）
```

**没有 `runtime_supervisor.py`**。v1.0 里有，v1.1 删除：桌面端不 spawn、不监管、不停止 Runtime（§3.1）。`runtime_conn.py` 只做三件事——探活、唤醒（调安装器的 `start`）、重建 `Client`。

### 4.1 线程模型

Qt 的硬约束：UI 只能在主线程更新。

| 线程 | 职责 |
| --- | --- |
| 主线程 | Qt 事件循环、所有 widget 更新 |
| `ScanWorker`（QThread） | 遍历 + hash + 解析 + 切块 + 入库，通过 signal 报进度 |
| `WatchWorker`（QThread） | watchdog 队列消费 + 防抖 |
| `ChatWorker`（QThread） | 一次提问的完整生命周期（SDK 是同步阻塞的），逐 delta 上抛 |
| `SearchWorker`（QThread） | 只查不答的一次检索（§8.4） |

`ChatWorker` 必须支持 `cancel`：SDK 的流式迭代器支持 `close()`，提前关闭会让服务端停止生成（否则引擎继续烧 CPU 到结束）。

**解析必须放在 worker 线程**：PyMuPDF 解析一份 100 页 PDF 约 1~3 秒，放在主线程会让界面卡死。

---

## 5. 解析与切块（客户端核心工作）

### 5.1 必须与实测结论对齐的参数

| 项 | 要求 | 依据 |
| --- | --- | --- |
| 目标长度 | 450 字符（中文技术文本） | 实测 0.63 token/字，e5-small 上限 512 token 对应约 800 字 |
| 硬上限 | 600 字符 | 长句本身可能超目标值 |
| 重叠 | 12%（54 字符），**仅硬切兜底时使用** | 结构边界不需要重叠 |
| 最小块 | 50 字符，过短的并入相邻块 | 文档末尾常剩一两句 |
| 边界优先级 | 段落 → 中文句末标点 → 弱边界（逗号/顿号/空格）→ 硬切 | 与 `local-ai-runtime.md` 22.5 一致 |
| 中文分词 | 滑窗 bigram + ASCII 词整体 | 与 `internal/knowledge/bigram` 一致，否则关键词通道命中率会变 |
| 标题路径 | `[H1 > H2 > H3]` 前缀拼在 `text` 之前 | 解决"标题与内容分离" |
| Excel | 每块前置表头行 | 否则第二块之后没有列名 |
| 页码 | 从解析器直接取，不靠反查 | 这是下沉解析的主要收益 |

### 5.2 标题路径：最值得做的一件事

现在（Go 侧）的问题很具体：DOCX 里的"2.1 付款方式"和它下面的正文被切成两块后，第二块完全不知道自己属于哪一节。用户问"付款方式是什么"，向量里没有"付款方式"四个字，只能靠语义通道的运气。

做了标题路径之后：

```
[第二章 付款 > 2.1 付款方式]
甲方应在收到发票后 30 日内支付……
```

这段文本参与向量化，问"付款方式"就能命中。**成本几乎为零，收益很明显。**

实现方式按格式不同：

| 格式 | 标题来源 | 可靠性 |
| --- | --- | --- |
| DOCX | `python-docx` 的 `paragraph.style.name`（`Heading 1/2/3`）或 `outlineLvl` | **高**，样式是显式的 |
| PDF | 字号 > 正文中位数 × 1.15 且单独成行 | **中**，需标注集验证，误判率高 |
| XLSX | sheet 名 + 表头行 | 高 |
| PPTX | 页面标题占位符 | 高 |

**PDF 的标题识别建议后置**（v0.1.1）。它的误判会污染向量——把正文误判为标题，会让整节内容都带上错误的路径前缀。

### 5.3 Excel 必须每块重复表头

这是当前正在发生的真实缺陷。Go 侧把整个 sheet 拼成一片文本，表头是第一行。450 字切下来，第一块有表头，后面的块只有一堆没有列名的数字。用户问"A 公司上个月销售额多少"，后面的块里根本没有"销售额"这个词。

实现方式：`openpyxl` 按行读，切块时把表头行（第一行或识别出的表头）拼在每个块前面。块大小按"表头 + N 行"估算，`N` 视列数而定（列多则行少）。

### 5.4 PPTX 与 OCR

#### PPTX（✅ M6 已落地，`localkb/parse/pptx.py`）

`python-pptx` 按页取文本框内容，**一页一块**（页内文字合并），`page = slide_number`。

| 决定 | 理由 |
| --- | --- |
| 一页一块，不跨页合并 | 页码是溯源核心。切块器的 `cross_page` 规则保证不同页的短句各自成块——PPT 每页通常只有 50~300 字，不拦就会被合并，来源卡片指错页 |
| 标题用 `placeholder_format.type` 判定 | 这是 OOXML 枚举（`TITLE`/`CENTER_TITLE`/`SUBTITLE`），与界面语言无关。靠 `shape.name` 匹配会漏掉中文版的"标题 1" |
| 表格必须单独读 `shape.table` | `shape.text_frame` 对表格（GraphicFrame）**不存在**。只读 text_frame 会让 PPT 里的表格内容整片丢失，而且不报错 |
| 形状按 z-order 顺序 | python-pptx 没有"阅读顺序"概念；`slide.shapes` 的顺序与视觉顺序大体一致，不排序会让标题落到正文后面 |

**打包时必须保留 Pillow**：`import pptx` 会经 `pptx.parts` 加载 `PIL.Image`
（用于判断嵌入图片格式）。`tools/localkb.spec` 的 excludes 曾包含 `PIL`，
后果是打包后只有 PPTX 解析崩，而开发环境装 Pillow、单测全绿。
`--self-check` 已加 PPTX 项专门守这个。

#### OCR（v0.1 不做，但要把原因说清）

**决策**：不做（§16 P1）。理由写进用户可见文案，因为"不给原因"会让用户
反复重试或以为软件坏了：

1. OCR 引擎（PaddleOCR 一类）要多占 **200MB~1GB 内存**，低配机上会挤掉对话模型
2. 识别出错时会出现"检索到了但内容是乱码"——**这比检索不到更伤信任**，
   且用户无法区分"资料里没有"与"识别错了"

**扫描件 PDF 的处理**（`localkb/parse/pdf.py` 的 `SCANNED_PDF_REASON`）：

```
这是扫描件（整份 PDF 没有文字层，内容都是图片），当前版本不支持 OCR 识别。
为什么不做：OCR 引擎要多占 200MB~1GB 内存，低配机上会挤掉对话模型；
而且识别出错时会出现「检索到了，但内容是乱码」——那比检索不到更难排查。
可以这样做：① 用 Adobe Acrobat / WPS 的「识别文字」功能转成可搜索 PDF；
② 用手机扫描类 App 导出为文本；③ 如果原件还有 Word/PPT 版本，直接导入那个。
```

同一结构也用于两个相邻场景，措辞保持一致（用户可能同时遇到多种）：

| 场景 | 异常类型 | 文案要点 |
| --- | --- | --- |
| 扫描件 PDF（无文字层） | `UnsupportedError` | 是什么 → 为什么不做 → 怎么办 |
| PDF 只有零星字符 | `UnsupportedError` | 同上（多半是扫描件 + 页码/水印） |
| 纯图片 PPT | `UnsupportedError` | 同上 |
| 旧格式 `.doc/.xls/.ppt` | `SkipReason.LEGACY_FORMAT` | 给出对应的新格式 |

**为什么旧格式要报"跳过"而不是静默忽略**：其他不支持的扩展名（`.mp4`/`.zip`）
静默忽略是对的（用户选 D 盘会看到几万个"不支持 mp4"）。但旧版 Office 是
用户真正想导入的文档，且改个格式就能用。静默忽略会让他们看到"新增 0 篇"
却不知道原因——这类"什么都没发生"的故障最难自查。

### 5.5 不要复刻 Go 实现的两个缺陷

Go 的 `internal/knowledge/chunk` 有两处已知缺陷（Runtime 改造方案附一记录）。**客户端实现请以修正后的语义为准**，不要为了"和 Go 一致"而复刻：

1. **`isSentenceEnd` 只接收单个 rune**，注释写的"英文句号需后跟空白"没有实现。后果：`3.14`、`v1.2.3`、`e.g.` 会被拦腰切断。
   → 客户端应检查 `.` 后是否紧跟空白或结尾。
2. **重叠在所有切点都应用**（包括段落边界），且切点靠近块首时会产生"近乎完全重复的块"。
   → 客户端应只在硬切兜底时应用重叠，且保证新块长度 > 上一块的 50%。

`chunker.version` 本来就是用来区分这类差异的，所以"和 Go 不一致"是**预期行为**，不需要对齐。

### 5.6 解析失败必须带可操作的原因

不要出现"解析失败"这种无信息量的文案。原因要能直接展示给用户并指向下一步动作：

| 情况 | 文案 |
| --- | --- |
| 扫描件 PDF | "这是扫描件（无可提取文本），当前版本不支持 OCR。可先用其他工具转为文本。" |
| 旧版 `.doc` / `.xls` | "这是旧版 Office 格式，请另存为 .docx / .xlsx 后重新扫描。" |
| 加密 PDF 且未提供密码 | "此 PDF 需要密码。可在导入时输入。" |
| 非 UTF-8 编码 | "文件不是 UTF-8 编码（可能是 GBK），请转换后重试。" |
| 文件损坏 | "文件已损坏，无法读取。" |
| 文件被其他程序占用 | "文件正被其他程序使用（Word/Excel 可能还开着）。保存并关闭后会自动重试。" |
| 路径过长 / 无权限 | "路径过长或没有读取权限，已跳过。可把文件移到较短的路径或检查权限。" |
| 云盘占位文件（OneDrive 等） | "这是云盘占位文件，本机还没有内容。请先在资源管理器中打开一次让它下载完成。" |
| 块数超出上限 | "这个文件切出 N 个片段，超过单文档上限 2000。请拆分后再扫描。"（§5.7） |

### 5.7 单文档块数上限：2000（硬约束）

Runtime 的入库端点限制**单次请求 2000 块 / 20 MB**（`document.MaxChunksPerRequest`）。更重要的是：`ReplaceChunksIncremental` 按 ordinal 做差集删除——**第二批提交会把第一批里没出现的 ordinal 全部删掉**。因此"分批提交同一文档"在当前实现下是坏的，不能靠它绕开上限。

按 §5.1 的 450 字/块估算，2000 块 ≈ 90 万字（中文）≈ 2.7 MB 文本。这与"50 MB 文件"完全不是一回事：

| 口径 | 上限 | 依据 |
| --- | --- | --- |
| 单文档块数 | **2000** | Runtime 硬约束，不可绕过 |
| 单文件字节 | **不单独设限** | 字节数无法推断块数（DOCX 压缩过、PDF 图文混排），按字节拒绝会误伤合法文件 |
| 超出后的处理 | 解析后若块数 > 2000 → **不提交**，记 `local_files.status=too_large` 与可读原因；不静默截断 | 静默截断会让用户以为整个文件都进了库 |

**为什么不用字节数预筛**：一个 3 MB 的 PPT 可能只有 30 块，一个 800 KB 的 DOCX 可能有 2500 块。字节数是错的判据，块数才是。代价是超大文件要解析完才发现超限——比错误拒绝好。

**为什么不做"自动只导前 2000 块"**：那样用户问到的内容取决于切块顺序，而不是文档内容——答案会随机正确或缺失，且没有任何提示能解释。宁可明确拒绝。

`v1.0 的 §10.6` 写的"单文件 50 MB"作废。

### 5.8 能不能先让模型"提炼分段"再入库？（结论：默认不做）

这个想法很自然——PDF 抽出来的章节乱、多栏串行、页眉页脚掺在正文里，让模型整理一遍再入库，检索质量会更好。但**在当前硬件与产品约束下，它会让产品的核心价值失效**。

#### 5.8.1 三个硬阻碍

| 阻碍 | 具体数字/事实 |
| --- | --- |
| **速度** | qwen2.5-1.5B 实测 **15.8 tok/s 生成、48 tok/s 预填**（`docs/实测记录/20261112-吞吐基准与结构化输出.md`）。一篇 3000 字的中文文档，改写输出约 2000 token ≈ **2 分钟**；100 篇 ≈ **3.3 小时** |
| **独占引擎** | Runtime 的引擎并发上限是 1。整理期间用户提问会排在后面（`queued`），或者整理被交互请求抢占而反复中断 |
| **溯源失效** | 模型改写会**静默改变文字**：数字、型号、否定词都可能被"顺手改对"，也可能被改错。而 `page_start` 指向的是**原文页码**，改写后的文字在那页上根本找不到。G3（点一下跳回原文）直接不成立 |

第三条是决定性的。本产品卖的是"答案能回到原文"，一旦入库内容不是原文，这个承诺就是假的。而且这类错误**无法被用户发现**——他看到的引用片段读起来很通顺，只是原文里没有这句话。

#### 5.8.2 该做的是"规则修复"，不是"模型改写"

文档结构的问题大部分可以用确定性规则解决，不需要模型：

| 问题 | 规则做法 | 是否需要模型 |
| --- | --- | --- |
| 页眉页脚重复出现在每页 | 统计每页首/末行，出现率 > 60% 的整行丢弃 | 否 |
| PDF 硬换行把句子切断 | 行尾不是句末标点且下一行首字非缩进 → 合并 | 否 |
| 多栏被读成串行 | 按 block 的 x 坐标聚类分栏，再按栏内 y 排序 | 否（PyMuPDF 有 block 坐标） |
| 目录页、封面页污染索引 | 前 N 页若是"目录/封面"特征（大量点线、页码列表）→ 标记 `chunk_type=heading` 或跳过 | 否 |
| 章节标题识别不出（无样式） | 字号 + 行高 + 独占行启发式；**误判时宁可不带标题路径** | 否 |
| 表格跨页断裂 | 表头行识别 + 续页补表头 | 否 |

这些都在 §5.2 / §5.3 的范围里，且**可验证**（给定输入，输出唯一）。§11.3 的回归测试就是为它们准备的。

#### 5.8.3 如果将来确实要做，做成"可选的增强层"，且不改原文

留给 v0.2 之后的设计约束（现在不做，但先把边界定死，避免以后做歪）：

| 约束 | 理由 |
| --- | --- |
| **原文块必须保留** | 改写产物只能**新增**块，不能替换原块。来源卡片永远指向原文 |
| **生成的块必须可识别** | `chunk_type` 或文本前缀标明"模型生成的摘要"，检索命中它时卡片显示"这是模型整理的内容，原文在第 N 页" |
| **只在用户显式开启时跑** | 设置项默认关闭；开启时提示"将为 N 篇文档各花约 X 分钟，期间提问会变慢" |
| **后台优先级 + 可中断** | `Priority.BACKGROUND`，每篇落库一次，中断后从断点继续 |
| **产出可丢弃** | 关掉开关就能一键删除全部生成块，回到纯原文索引 |

**一个具体可行的窄用途**：为每节生成 1~2 句"本节讲什么"的摘要块，插在该节第一块之前。它提升的是"换个说法也能搜到"的能力（语义通道），而**不触碰任何原文**。即便如此，也要先有 §11.4 的 recall 对照数据来证明它确实有收益——否则只是多花 3 小时换来一堆稀释向量的噪声。

#### 5.8.4 结论

| 项 | 决策 |
| --- | --- |
| v0.1 用模型提炼/分段再入库 | **不做** |
| 文档结构问题 | 用 §5.2/§5.3/§5.8.2 的规则解决 |
| 解析失败（扫描件等） | 明确报错并给下一步（§5.6），不做"猜内容" |
| 未来做"AI 整理" | 只能新增块、必须可识别、默认关闭、可一键清除（§5.8.3） |

---

## 6. 通过 Python SDK 的调用契约

### 6.1 用法（无需配置端口）

```python
from localai import Client

client = Client()   # 自动发现：LAIH_URL → discovery 文件 → 默认 18765
cap = client.capability()
```

`_discovery.py` 已实现三平台路径与环境变量覆盖，与 Go SDK 顺序一致。桌面端**不要自己读 `runtime.json` 或判断"文件在不在"**——重复实现路径规则是经典 bug 源（尤其 Windows 的 `LOCALAPPDATA` 缺失回退）。探活统一用 `client.live()`（§3.1.1）。

**`Client` 不是长期有效的**：它把 base_url/token 缓存在构造时。Runtime 重启会换端口、token 轮换会换 token，旧实例之后全部失败。重建规则见 §3.1.2。

### 6.2 用到的端点

**以下签名以 SDK 实际实现为准**（`sdk/local-ai-runtime-sdk/python`）。它用"位置参数 + 请求对象"的风格，不是关键字参数——照抄旧文档的 `name=...` 写法会直接 `TypeError`。

| 用途 | SDK 方法（真实签名） | 端点 |
| --- | --- | --- |
| 探活 | `live() -> bool` | `GET /health/live` |
| 健康详情 | `health() -> HealthReport` | `GET /health/ready` |
| 能力检查 | `capability() -> Capability` | `GET /api/v1/capability` |
| 资源状态 | ⚠ **SDK 未封装，见 §6.7** | `GET /api/v1/resources` |
| 建库 / 列库 / 删库 | `create_kb(name, ...)` / `list_kb()` / `delete_kb(kb_id)` | `/api/v1/knowledge/bases` |
| 统计 | `kb_stat(kb_id) -> KBStat` | `GET .../{id}/stats` |
| **入库（已切好的块）** | **`ingest_chunks(kb_id, IngestChunksRequest(...))`** | **`POST .../{id}/documents/chunks`** |
| 删文档 | `delete_document(kb_id, doc_id)` | `DELETE .../{id}/documents/{docId}` |
| 列文档 | `list_documents(kb_id) -> list[DocumentSummary]` | `GET .../{id}/documents` |
| 向量化 | `vectorize(kb_id, is_async=True)` | `POST .../{id}/vectorize` |
| 检索 | `search(SearchRequest(...))` | `POST /api/v1/search` |
| 对话流式 | `stream_events(ChatRequest(...))` | `POST /api/v1/chat` |
| 模型列表 | `models()` / `active_model()` | `GET /api/v1/models` |
| 相似检测 | `dedup_check(kb_id, DedupCheckRequest(...))` | `POST .../{id}/dedup/check` |
| 相似组 | `dedup_groups(kb_id)` | `GET .../{id}/dedup/groups` |
| 组内差异 | `dedup_diff(kb_id, doc_a, doc_b)` | `GET .../{id}/dedup/diff` |

**不要用** `import_document` / `upload_document`：服务端路由已隐藏（返回 404），SDK 保留它们只为让错误信息可读。桌面端一律走 `ingest_chunks`。

**不要用** `load_model()` / `unload_model()`：模型加载归管理后台（§3.1.1）。桌面端只读 `active_model()` 用于状态栏展示。

### 6.3 文档路径不通过 API 暴露

Runtime 的 `docSummary` 不返回 `original_path`。桌面端的 `local_files.doc_id → path` 映射本来就必须存在（监听目录要它做增量），直接复用。好处：任何 API 调用方都拿不到用户文件系统布局，诊断包与日志也不用担心路径泄露。

**来源卡片显示的名字以服务端为准**：`source_name` 来自 Runtime 的 `documents.name`。改名时桌面端必须把新名字一起提交（§7.2），否则卡片会长期显示旧名。

### 6.4 错误处理

SDK 已把错误映射成 `ErrorKind`，并按类别决定重试。桌面端**不要自己写分类逻辑**：

```python
from localai import (APIError, ErrQueueFull, ErrResourceLimited,
                     ErrModelUnavailable, ErrChunkerMismatch)

try:
    res = client.ingest_chunks(kb_id, IngestChunksRequest(
        name=f.name,
        file_hash=h,
        chunks=chunks,
        chunker=ChunkerInfo(name="localkb-py", version="1.0.0", ...),
        file_type=f.suffix.lstrip("."),
        file_size=f.stat().st_size,
        modified_at=int(f.stat().st_mtime),
        source_label=str(f),
        update_document_id=(row.doc_id if row else None),
    ))
except APIError as e:
    if e.kind is ErrQueueFull:
        ...            # SDK 已重试 2 次仍失败 → 暂停扫描并提示"系统繁忙"
    elif e.kind is ErrResourceLimited:
        ...            # 服务端资源不足 → 暂停扫描，等恢复（不要自己算内存）
    elif e.kind is ErrChunkerMismatch:
        ...            # 切块器版本与库内不一致 → 提示需重建，不要重试
    elif e.kind is ErrModelUnavailable:
        ...            # Embedding 模型未配置 → 引导去管理后台（§8.5）
    else:
        ...            # 记录 e.message 到 local_files.error
```

**`ErrChunkerMismatch`（409）绝不能重试**，重试必然再失败。补救路径见 §7.6——注意**不是"逐篇删除重导"**，那样永远还是 409。

**已知的一处 SDK 行为**（`docs/修改记录/20261113-0061`）：`vectorize()` 在某嵌入模型未配置时返回 `model_unavailable`，SDK 会按"模型可能正在加载"重试 2 次，白等约 4 秒。桌面端应在扫描前先看 `capability()` 与 `kb_stat().vectorize.model`，避免走进这条路径。

### 6.5 SSE 事件（必须全部处理）

| 事件 | 处理 |
| --- | --- |
| `queued` | 显示排队位置与 `estimated_wait_ms`；**这是模型加载期的唯一信号** |
| `meta` | 记录 `model` / `degraded` / `knowledge.sources`（来源在这里） |
| `warning` | 累积到"提示"区，不要丢弃 |
| `delta` | 追加文本 |
| `usage` | 记录 TTFT、token 数（`prompt_tokens` 可能为 `None` = 未知，不是 0） |
| `done` | 结束，渲染来源卡片 |
| `error` | 断流；SDK 会抛 `APIError`，按 §6.4 处理 |

**`sources` 来自 `meta` 事件，不是 `done`。** 若等到 `done` 才拿来源，流式中途失败时用户看不到任何依据——而那时他更需要知道"系统本来打算引用什么"。

### 6.6 模型相关：桌面端一律不碰

模型下载、加载、卸载、Embedding 模型配置**全部归 LocalAIHub 管理后台**（文首第二条边界）。桌面端只做两件事：

1. **读**：`capability()` 拿到 `rag_enabled` / `rag_model` / `reason` / `warnings`；`models()` 拿到 `present` / `loaded` / `size_bytes`。
2. **引导**：缺模型或模型不可用时，显示可读原因 + 一个"打开管理后台"按钮（`http://<discovery base_url>/admin/`，token 从 discovery 文件读，或提示用户在后台输入）。

**不做的事**（逐条都是 v1.0 里写过、现在删除的）：

| v1.0 的做法 | 为什么删掉 |
| --- | --- |
| 桌面端调 `localai-cli models download` | 模型归服务端管理；且依赖另一个 exe 的 stdout 格式是脆弱耦合 |
| 桌面端自带模型下载进度条 / 镜像选择 | 后台已有（`/models/download` + 断点续传 + sha256 + 镜像） |
| 桌面端调 `load_model()` / `unload_model()` | 后台已有，两边都会改同一状态，必然打架 |
| 桌面端"从本地导入模型" | `localai-cli models import` 是运维手段，不是产品功能 |

**提示文案要指向动作**，不要只说"模型不可用"：

| 服务端状态 | 桌面端显示 |
| --- | --- |
| `rag_model` 为空且 `rag_enabled=false` | "当前没有可用于知识库问答的模型。请到管理后台下载并加载一个（推荐 qwen2.5-1.5b）。" [打开管理后台] |
| `rag_enabled=true` 但 `models()` 里无 `present` | "模型文件还没下载。请到管理后台的模型页下载。" [打开管理后台] |
| `rag_enabled=true`、模型 `present=true`、`loaded=false` | 正常提问即可（首个请求会自动加载），UI 显示"正在准备模型" |
| Embedding 未配置（`kb_stat().vectorize.model` 为空 / `vectorize` 报未配置） | "未配置向量模型，语义检索不可用，当前只能做关键词检索。请到管理后台配置后重启服务。" |

### 6.7 `resources()`：实时资源状态

§2.3 的状态栏需要**实时**内存（`pressure` / `available_bytes` / `total_bytes`），§10.4 的"资源紧张时暂停扫描"也需要它。数据来源是 `GET /api/v1/resources`，SDK 已封装为 `client.resources()`（Python 与 Go 一致，2026-11-16 落地，见修改记录 0072）。

**`capability()` 不能替代它**：`capability` 是**启动时算一次**的快照（`cmd/localai-cli/serve.go` 里 `capReport` 被闭包捕获），回答"这台机器能做什么"；`resources` 是**实时**值，回答"现在有多紧张"。§10.4 需要的是后者。

用法：

```python
st = client.resources()
if st.pressure == "CRITICAL":
    pause_scan()          # 别撞 OOM
elif st.strained:         # LIMITED / BUSY / CRITICAL
    sleep(2)              # 让一步再继续
```

`strained` 的判据（`LIMITED` 及以上）与服务端 `Pressure.ShouldWarn()` 的起点一致——不另设阈值，否则两边会不同步。

**注意两件事**：

- **不做**：走 `client._transport` 之类私有接口（升版会静默坏掉）。
- **不放进每篇文档的循环里**：一次调用会枚举进程列表，几百次会让扫描明显变慢。按批（50 篇）或按时间（每几秒）查一次。
- `resources()` 不是"要不要发请求"的开关：Runtime 仍会在准入时拦下放不下的请求并返回 `ErrResourceLimited`。它是**提前让路**，减少无谓往返与 OOM 风险。

**旧版 Runtime 没有该方法**（SDK 侧是 404）：此时退化为"撞到 `ErrResourceLimited` 才暂停"（§17.1）。

---

## 7. 扫描与增量

### 7.1 首次扫描

```python
def scan(roots, kb_id, progress, cancel):
    cap = client.capability()                       # 1. 能力检查（启动快照）
    if not cap.rag_enabled:
        progress.fail(cap.reason,
                      action="打开管理后台")          # 不扫描，给出动作
        return

    files = walker.walk(roots, excludes)             # 2. 枚举（§7.5 跳过策略）
    progress.total(len(files))

    changed = []
    for f in files:                                  # 3. diff
        if cancel.is_set(): return
        try:
            h = hasher.sha256(f)                     #    分块读，不整体载入
        except OSError as e:
            db.local_files.mark_skipped(kb_id, f, skip_reason(e))
            continue
        row = db.local_files.get(kb_id, f)
        if row and row.sha256 == h:
            continue                                 #    未变，跳过
        changed.append((f, h, row))

    for i, (f, h, row) in enumerate(changed):        # 4. 解析 + 切块 + 入库
        if cancel.is_set(): return

        # 4a. 解析（客户端职责）
        try:
            parsed = parser.parse(f)                 # ParsedError 带可读原因（§5.6）
        except ParsedError as e:
            db.local_files.mark_skipped(kb_id, f, e.reason)
            continue

        # 4b. 切块（含页码/标题路径/块类型）
        chunks, chunker = chunker.split(parsed, CHUNK_PROFILE)
        if len(chunks) > MAX_CHUNKS_PER_DOC:         # §5.7 硬上限
            db.local_files.mark_too_large(kb_id, f, len(chunks))
            continue

        # 4c. 入库（Runtime 只收块，不碰文件）
        res = client.ingest_chunks(kb_id, IngestChunksRequest(
            name=f.name,
            file_hash=h,
            chunks=chunks,
            chunker=chunker,
            file_type=f.suffix.lstrip("."),
            file_size=f.stat().st_size,
            modified_at=int(f.stat().st_mtime),
            source_label=str(f),                     # 只存展示，Runtime 不读
            update_document_id=(row.doc_id if row else None),
        ))

        doc_id = res.existing_id if res.duplicate else res.document_id
        db.local_files.upsert(kb_id, f, h, doc_id, "imported")
        progress.step(i + 1)
        time.sleep(0.05)                             # 让交互请求插队

    client.vectorize(kb_id, is_async=True)           # 5. 一次性触发
    while client.kb_stat(kb_id).vectorize.running:   # 6. 轮询
        if cancel.is_set(): break
        progress.vectorizing(client.kb_stat(kb_id).vectorize)
        time.sleep(1.0)
```

**为什么向量化按整个知识库触发一次**：`vectorize` 的语义是"为该 KB 内所有缺向量的切片补向量"。逐篇调用每次都要遍历全库统计，反而更慢。

**为什么用 `is_async=True` 而不是同步**：同步 `vectorize` 在几千块时要跑几分钟，HTTP 请求会超时。`kb_stat().vectorize.running` 是可用信号（`VectorizeProgress` 已含 `total_chunks` / `embedded_chunks` / `pending_chunks` / `stale_chunks` / `running`）。

**单 worker 顺序提交，不并发**：Runtime 的引擎并发上限是 1，并发提交只会堆队列并撞 `queue_full`。

**入库前不查 `resources()`**：能力是否够用由服务端在自己那边判断，客户端只在收到 `ErrResourceLimited` 时暂停（§10.4）。客户端自己"预判内存不够就不提交"会造成"服务端明明能跑、桌面端却拒绝"的分歧。

### 7.2 增量更新

| 事件 | 处理 |
| --- | --- |
| 新增文件 | `ingest_chunks`（`update_document_id=None`）→ 记录 `doc_id` |
| 修改文件 | sha256 变化 → `ingest_chunks(update_document_id=旧 doc_id)` → **必须传这个字段**，否则会新建一份重复文档 |
| 删除文件 | `delete_document(kb_id, doc_id)` + 删 `local_files` 行 |
| **重命名** | 见下方"重命名必须同时改服务端名字" |
| 启动对账 | 双向：磁盘 ↔ `local_files`、`local_files` ↔ `list_documents()`（见 §7.2.2） |

#### 7.2.1 重命名必须同时改服务端名字

只更新 `local_files.path` 是**错的**：来源卡片的名字来自 Runtime 的 `documents.name`。用户把"合同_v1.docx"改成"合同_终稿.docx"后，引用卡片仍然显示旧名——而卡片是溯源的核心。

**`name` 已会落库**（S2 于 2026-11-16 落地，见修改记录 0072）。此前 Runtime 接收该字段但**从未写库**，改名后卡片仍显示旧名；那个缺陷已修，且同时修掉了"文件从 MISSING 恢复后重新扫描报文档不存在"的连带问题。

**正确做法**：

```
检测到 path 变化（同 sha256、同 doc_id）
  → ingest_chunks(update_document_id=doc_id,
                  name=新文件名,
                  source_label=新路径,
                  chunks=与上次完全相同的块)      # 内容未变，diff 全是 Unchanged
  → 更新 local_files.path / name_sent
```

代价说明：需要重新解析一次该文件（sha256 未变，内容必然相同），换来名字与服务端一致。若文件很大解析昂贵，退而记录一条"显示名可能过期"的待更新项，但**不能默认不处理**（v1.0 就是默认不处理）。

**兜底**（服务端 S2 未就绪期间）：桌面端以 `local_files.path` 的当前文件名覆盖显示（卡片文案用本地名，`page_start` 等仍用服务端返回值）。这样用户至少看不到旧名，代价是"和 API 返回的 `source` 不一致"——比显示错名字好。

#### 7.2.2 启动对账（双向）

v1.0 只查"磁盘文件是否还在"。但有两条会破坏这个假设：

1. **管理后台可以删整个知识库**（`DELETE /knowledge/bases/{id}`，`documents` 级联删除）。用户从网页删库后，桌面端的 `local_files` 里全是悬空 `doc_id`，下次改文件会 `update_document_id` 指向已不存在的文档而报错。
2. **服务端也可能有本机没导过的文档**。管理后台当前只能删库、不能删单篇文档（`web/src/api/client.js` 里没有单文档删除调用，`KnowledgePage.vue` 只提供删库），但 Runtime 的 API 是开放的——未来的第二个客户端、CLI、`ragbench` 都可能往同一个库里写。桌面端不能假设"库里的文档都是自己导的"。

启动时做四步：

| 方向 | 检查 | 处理 |
| --- | --- | --- |
| 磁盘 → 本地表 | `local_files` 中 status≠deleted 但文件不存在 | `delete_document` + 标 `deleted` |
| 本地表 → 磁盘 | 文件存在但 `local_files` 无记录 | 标 `pending`，下次扫描补导 |
| **本地表 → 服务端** | `doc_id` 不在 `list_documents(kb_id)` 里 | 标 `orphaned`，清空 `doc_id`，下次扫描重导 |
| **服务端 → 本地表** | 服务端有文档但本地无映射 | 只提示"库内有 N 篇文档不是本机导入的"，不删除、不改动 |

最后一条为什么只提示：Runtime 是共享的。删掉别人导入的内容是越权，而且是不可恢复的（桌面端不知道那些文件的路径）。

**库被删的情况**：`list_kb()` 里没有这个 `kb_id` 时，知识库页显示"该知识库已在服务端被删除"，提供"重建并重新扫描"（`create_kb` + 全量扫）与"从本机移除"两个动作。

**watchdog 的坑**（必须处理，否则会导入到一半的文件）：

- Word/Excel 保存时先写临时文件（`~$xxx.docx`）再改名 → **排除 `~$` 前缀与 `.tmp/.crdownload/.part` 后缀**。
- 编辑器可能触发 `created` + `modified` + `modified` 三次 → 防抖 2 秒，按 path 合并。
- 大文件复制过程中持续触发 `modified` → 防抖窗口中若有 `modified` 就重置计时。

#### 7.2.3 已删除的文件不会被自动清理

`delete_document` 的默认行为是 `removeFile=true`，但 `Importer.Delete` 只在 `StorageMode == Managed` 时删托管副本；`ingest_chunks` 写入的是 `StorageExternal`（`ingest.go` 的 `ingestNew`），**永远不会删除用户原文件**。这条与 ADR 0015 一致。

**桌面端的调用约定**：`delete_document(kb_id, doc_id, keep_file=True)` —— `keep_file` 对 `StorageExternal` 是 no-op，但显式传它有两个作用：① 意图在代码里可读（"我没打算删文件"）；② 万一将来某条路径产生了 `Managed` 副本，默认行为不会误删。真正删文件只在 §7.7 的对话框里由用户勾选，且只到回收站。

### 7.3 扫描状态机

```
             ┌──────── 用户选目录 ────────┐
             ▼                            │
      ┌─────────────┐                     │
      │  capability  │  rag_enabled=false  │
      │   检查       │─────────────────────┘→ 提示原因，不扫描
      └──────┬──────┘
             │ true
             ▼
      ┌─────────────┐  枚举 + 过滤扩展名 + 排除目录
      │  WALKING    │──────────────────────────┐
      └──────┬──────┘                          │
             ▼                                 │
      ┌─────────────┐  sha256 与 local_files 比对
      │  DIFFING    │──────────────────────────┤
      └──────┬──────┘                          │
             │ 新增/变更                        │ 未变：跳过
             ▼                                 │
      ┌─────────────┐  parse → chunk → ingest   │
      │  IMPORTING  │  （顺序，50ms 间隔）      │
      └──────┬──────┘                          │
             ▼                                 │
      ┌─────────────┐  整体触发一次 vectorize   │
      │  VECTORIZE  │                          │
      └──────┬──────┘                          │
             ▼                                 │
      ┌─────────────┐  轮询直到 running=false   │
      │   READY     │◄─────────────────────────┘
      └─────────────┘
```

### 7.4 空闲卸载与"马上提问"的冲突

Runtime 默认 `-idle-unload-sec 90`：模型空闲 90 秒且用户空闲 180 秒后卸载。**这会导致用户切回来提问时遇到冷启动**（1.5B 首 token 需加载 1~2 秒）。

**参数不由桌面端设置**（§3.1）——它写在 `launch-spec.json` 里由安装器维护。桌面端能做的只有三件：

1. **UI 必须显示"模型加载中"**：`stream_events` 的第一个事件是 `queued`，`meta` 之前可能已等了几秒。在 `queued` 时显示"正在准备模型"，**不要显示"正在思考"**（后者会让用户以为模型在推理，实际在加载）。
2. **预热**：知识库构建完成后（READY），后台发一个 `max_tokens=1` 的请求预热。代价约 1 秒 CPU，换来首问 0 等待。预热请求用 `Priority.BACKGROUND`，避免挤占用户的交互请求。
3. **内存紧张时主动让路**：从 `resources()` 看到 `pressure=high` 且 `idle.enabled=true` 时，提示用户"模型将在空闲后自动卸载以释放内存"。**不主动调 `unload_model()`**（那会打断别人的请求）。

**需要改默认阈值时改的是服务端配置**（管理后台的 `idle_unload_seconds` / `user_idle_seconds`，`admin.Config` 已支持动态修改），不是桌面端的启动参数。

### 7.5 Windows 上的扫描现实：跳过策略

`os.walk` 在真实机器上会遇到这些情况，每一条都会让"扫描 100 篇"变成"扫描 3 篇 + 97 条异常"或让整个扫描卡死。**必须在 `walker` 层处理，且给出可读原因**（与 §5.6 同一原则）：

| 情况 | 现象 | 处理 |
| --- | --- | --- |
| 文件被占用（Word/Excel 正在编辑） | `PermissionError` on open | 标记 `locked`，**不重试**（占用是暂态，下次扫描会自然重试），提示"文件正被占用" |
| 路径 > 260 字符 | `OSError` / 找不到 | 标记 `too_long`，提示路径过长。不做 `\\?\` 长路径前缀（会引入新的兼容问题） |
| OneDrive / 云盘占位文件 | 打开时触发下载，可能几秒到几十分钟 | 检查 `st_file_attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` → 跳过并标记 `cloud_only`，提示"请先在资源管理器打开一次" |
| UNC / 映射网络盘 | `os.walk` 可能卡数分钟 | 枚举阶段对网络路径加超时（线程 + 超时），超时标记 `network_slow` 并建议选本地目录 |
| 符号链接 / junction 循环 | 无限递归 | `os.walk(followlinks=False)`，且记录已访问的真实路径防环 |
| 无读取权限的目录 | `PermissionError` on listdir | `onerror` 回调里记录并继续，不中断整体 |
| 系统目录（`C:\Windows`、`Program Files`、`$Recycle.Bin`） | 大量无意义文件 | 内置排除项（`excludes.is_default=1`） |

**扫描前估算**：文件数 > 2 万或总字节 > 2 GB 时先提示"范围过大，建议缩小目录"，用户确认后才继续（避免选整个 D 盘白等两小时）。

**`local_files.status` 的取值因此扩展**：`pending / importing / imported / failed / skipped / deleted / locked / too_long / cloud_only / too_large / orphaned / missing`。UI 上 `skipped` 类统称"已跳过"，但要能展开看具体原因——**"跳过了 97 个文件"而不给理由是用户最不能接受的表现**。`missing` 不是跳过，见 §7.7.1（文件不在了但库内内容可能保留）。

### 7.6 切块器版本冲突的补救：只能重建知识库

**v1.0 写的"逐篇删除重导"是错的**，必须改掉。原因：Runtime 会把切块器版本记在**知识库级**的索引配置里，而该配置在库内已有记录时**只比对、不覆盖**。所以删单篇文档不会清掉它，重导只会再撞 409——用户会陷入死循环，且每次都要重新解析。

| 情形 | 正确动作 |
| --- | --- |
| 桌面端切块器升级（`1.0.0` → `1.1.0`）后首次扫描 | 立刻收到 409。提示"索引由旧版切块器建立，需要重建"，给一个明确按钮；**不自动重建**（重建要重新解析全部文件，几分钟到几小时） |
| 用户点"重建" | `delete_kb(kb_id)` → `create_kb(同名)` → 清空 `local_files` 的 `doc_id`（保留 path/sha256）→ 全量重扫 |
| 用户点"稍后" | 该库进入只读状态：可以检索已有内容，但**不导入新文件**（避免新旧混存），并在知识库页常驻提示 |
| 用户从 Go 侧（`chunker_name=go`）迁到桌面端 | 同上。提示文案要写明"原索引由旧版解析器建立" |

**重建前必须让用户确认代价**：显示"将重新解析 N 篇文档，预计约 X 分钟"。X 从 `local_files.imported_at` 的分布与上次扫描耗时估，不要空口说"几分钟"。

**为什么不在服务端自动重建**：Runtime 手里没有原文件（ADR 0011 的直接后果），重建必须由客户端驱动。这也是"不做重建端点"的理由——`DELETE` + 重新 `POST` 已经是唯一路径（服务端侧已列为明确不做，见增补需求 §5 N2）。

### 7.7 删除：文件与库记录是两个独立决定

删除这件事有**两个独立维度**，v1.0 把它们混成了一句"删除文件 → delete_document + 删 local_files 行"。必须拆开：

| 维度 | 选项 | 谁决定 |
| --- | --- | --- |
| **磁盘上的文件** | 保留 / 移到回收站 | **用户**（桌面端只在明确授权下动文件） |
| **知识库里的记录** | 移除 / 保留（继续可检索） | **用户** |

| 用户意图 | 磁盘 | 知识库 | 实现 |
| --- | --- | --- | --- |
| **只移出知识库**（文件留着） | 保留 | 移除 | `delete_document(kb_id, doc_id, keep_file=True)` + 删 `local_files` 行 |
| **文件和库都清** | 移到回收站 | 移除 | 先 `delete_document(..., keep_file=True)`，成功后 `SHFileOperationW(FO_DELETE + FOF_ALLOWUNDO)` |
| **文件删了，但库里的知识留着** | 移到回收站 | **保留** | 只删文件 + 标 `local_files.status=deleted`；文档在 Runtime 侧标 `MISSING`，内容仍可检索（§7.7.1） |
| 只暂时移出扫描范围 | 保留 | 保留 | 从 `scan_roots` 移除，不做任何删除 |

删除对话框因此有两个复选框，默认值见下：

```
┌─ 从知识库移除「售后服务手册.pdf」──────────────────────┐
│                                                      │
│  ☑ 从知识库移除（移除后无法通过提问检索到它）           │
│  ☐ 同时把文件移到回收站                               │
│     D:\工作\售后手册.pdf （2.4 MB）                   │
│                                                      │
│  ℹ 不勾第二项时，文件保持原样。                        │
│    之后想恢复，重新扫描该目录即可。                     │
│                                                      │
│                          [取消]        [确定]        │
└──────────────────────────────────────────────────────┘
```

**"删除磁盘文件"的四条硬约束**：

1. **只在用户勾选时发生**，且那一行必须显示完整路径与大小。不提供"默认勾选"。
2. **只走回收站，不做永久删除**。用 `FOF_ALLOWUNDO`，用户能从回收站还原。理由：误删不可逆，而这个产品没有"撤销"能力。
3. **`delete_document` 先成功，再动文件**。反过来的话会出现"文件已删、记录还在"，而那张卡片还写着"可以打开原文"。
4. **与"一键清理"无关**：这是用户对**单个（或选中的几个）文档**的显式动作。批量清理、按规则自动删、按重复组删一律不做（§12.1）。

**为什么不让 Runtime 删文件**：ADR 0015 明确 Runtime 不执行任何文件操作。`delete_document` 的 `keep_file` 对 `StorageExternal` 本来就是 no-op（`Importer.Delete` 只删 `StorageManaged` 的副本），所以"删文件"只能且必须由桌面端在用户授权下做。

#### 7.7.1 允许知识库里存在"没有对应实体文件"的文档

**这是允许的，而且是刻意的**：`documents` 表里已有 `DocMissing`（`MISSING`）状态，`UpdateDocumentStatus` 的注释也明确"传 ACTIVE 会把 MISSING 洗成正常，让用户以为知识库是好的"。所以 Runtime 本来就支持"记录在、文件不在"。

桌面端的规则：

| 场景 | 桌面端行为 |
| --- | --- |
| 扫描时发现 `local_files` 里有、磁盘上没了 | **不自动删**。标记 `local_files.status=missing`，并在知识库页汇总："有 N 篇文档的源文件已不在原位置" |
| 用户点"处理" | 三个动作：`[保留库内内容]` / `[从库中移除]` / `[重新指定文件位置]`（选新路径，重算 hash，若相同则 `ingest_chunks(update_document_id=...)` 只更新 `source_label`） |
| 用户不处理 | 保持 `missing`。文档**仍可被检索命中**，来源卡片显示"源文件已不在"，按钮变成"查看片段"而不是"打开原文" |
| 同一批有很多 missing（如整个目录被移动） | 先提示"检测到 37 个文件的源路径失效，可能是目录被移动"，提供"这是移动到了新位置"入口（选新根目录 → 按文件名+hash 匹配 → 批量重绑） |

**为什么默认不自动删**：文件被"移动"和"删除"在磁盘上看是同一件事（路径不存在）。自动删记录会让用户移动一次目录就丢掉整个库的索引，而重建要重新解析几小时。**默认保留、让用户决定**是唯一安全的做法。

**设置项**：`missing_file_policy`（`keep` 默认 / `ask` / `remove`）。默认 `keep` + 知识库页提示条；选 `ask` 时每次启动弹一次汇总对话框；`remove` 需二次确认，且在文案里写清"将移除 N 篇文档的索引，重新扫描需重新解析"。

#### 7.7.2 来源卡片必须能处理"文件不在"

引用卡片有三种终态，不能只做前两种：

| 情况 | 卡片按钮 | 文案 |
| --- | --- | --- |
| 文件在且能跳页 | "跳到第 N 页" | — |
| 文件在但不能跳页 | "打开文件" | — |
| **文件不在** | "查看片段" | "源文件已不在 `D:\工作\报表.xlsx`（可能被移动或删除）。库内内容仍可检索，点此查看引用的片段。" |

"查看片段"用内置面板显示 `source.text`（`hitResult.text` 有全文；`KnowledgeSource` 只有元数据，所以历史消息要用 §9 里 `sources` 存的 `snippet`）。

#### 7.7.3 `excluded`：移除必须留痕

"从知识库移除"**不能只删 `local_files` 行**。文件还在磁盘上，下次扫描会按"新增"又导一遍——用户看到的是"我删了它，它又回来了"，只会以为删除功能坏了。

所以移除后保留一行，`status = "excluded"`：

| 行为 | 判据 |
| --- | --- |
| 扫描时跳过 | `_classify` 里判 `excluded` → `UNCHANGED` |
| 内容变了也不导 | 用户明确说过不要它（可能只是编辑了这个他不想入库的文件） |
| 落库时不覆盖 | `_persist` 跳过 `previous.status == "excluded"`，否则下轮又变回 pending |
| 恢复 | 只能在设置页的"已排除文件"列表里显式操作 |

这一条在方案 v1.0~v1.3 里都缺失，是 M4 验收 A5 时实测暴露的。

#### 7.7.4 与后台的关系

后台已支持**删除单篇文档**（S5 于 2026-11-16 落地：`web/src/api/client.js` 的 `deleteDocument`，`keepFile` 默认 `true`，文档表每行有"移除"按钮 + 二次确认）。此前只能整库删——用户在网页上看到一篇明显不该在库里的文档，只能连库一起删掉。

后台与桌面端删的是同一条语义：**只从知识库移除**，`StorageExternal` 下原文件恒不受影响（ADR 0015）。

---

## 8. 提问与溯源

### 8.1 请求

```python
from localai import ChatRequest, Message, Priority, ALL_BASES

req = ChatRequest(
    messages=history + [Message("user", question)],
    kb_ids=kb_ids,                  # 见下方"库的范围"
    knowledge_mode="hybrid",        # 关键词 + 语义，RRF 融合
    knowledge_top_k=20,             # 先多召回
    max_knowledge_chunks=6,         # 再由预算筛选
    context_tokens=None,            # 交给服务端按模型上下文 40% 自动算
    history_tokens=600,
    priority=Priority.INTERACTIVE,
    max_tokens=512,
    temperature=0.2,
)
```

`messages` 只带**最近若干轮**（由 `history_tokens` 控制裁剪），完整历史在桌面端 `messages` 表里。传全量历史会让 `history_tokens` 的裁剪失效——服务端只按预算截断，而桌面端传的是"已被截断过的历史"时，用户会看到上下文突然丢失且无人解释。

#### 8.1.1 库的范围：chat 可勾选，search 必须查库

两个入口的语义**不同，不能统一**：

| 入口 | 是否必须查库 | 传什么 |
| --- | --- | --- |
| **对话（chat）** | 用户勾选决定。**允许不查**（普通对话） | 不勾选 → `kb_ids=None`；勾选具体库 → `kb_ids=["kb_1",...]`；勾"全部" → `kb_ids=[ALL_BASES]` |
| **检索（search）** | **必须查**。没有"不查库的检索"这回事 | 必须给具体 ID 或 `[ALL_BASES]`；传 `None` / `[]` 服务端返回 400 |

Runtime 侧的实现本来就是这个分工：`/chat` 里空 `kb_ids` 是合法（普通对话），`/search` 里空是错误（`expandKBIDs` 直接 400，`docs/runtime改造方案-知识库解析下沉.md` §4.11）。桌面端不要为了"统一"把 chat 改成必填，那会让普通对话做不了。

**界面**：对话页输入框上方一个知识库选择器（"不使用知识库 / 我的文档 / 全部库 / 多选"），默认值取上次选择；检索页不显示这个开关，只显示"在哪些库中检索"的只读说明。

| 用户意图 | chat 传什么 | 服务端行为 |
| --- | --- | --- |
| 只用勾选的这几个库 | `kb_ids=["kb_1","kb_2"]` | 只查这些 |
| 查全部库 | `kb_ids=[ALL_BASES]`（即 `["all"]`） | 展开为全部库，响应 `all_bases=true` |
| 不查知识库（普通对话） | `kb_ids=None` | 不做检索。**仅 chat 允许** |

**必须避免的三个错误**：

1. **不能"一个库都没勾"就传 `all`**。用户取消所有勾选的意思是"不用知识库"，不是"查全部"。传 `all` 会让答案意外带上全库内容，用户无法解释来源。
2. **不能在 `rag_enabled=false` 时传 `all`**。服务端会忽略，但用户会以为检索过了（这条与 §8.5 的降级表一致）。
3. **不能让 search 在没选库时自动降级为 `all`**。那会把"我以为只查了这一个库"变成"其实查了全部"，是最危险的静默降级。

**UI 上必须如实展示**：传 `all` 时，回答上方的来源提示要写"已检索全部 N 个知识库"，而不是省略。`all` 是用户显式选择的，展示它才是完整的告知。

**默认值**：对话页默认沿用上次选择（首次为"不使用知识库"）；不把"全部库"设为默认。


### 8.2 流式处理

`StreamEvent` 的字段是**判别名 + 各事件专属字段**（`_stream.py` 里 `type` 判别，`knowledge` 是原始 dict，不是类型化对象）：

```python
from localai import StreamEventType  # 或直接比较字符串

sources = []
with client.stream_events(req) as events:      # ClosingIterator，提前 break 会断流
    for ev in events:
        if ev.type == StreamEventType.QUEUED:
            ui.show_preparing(ahead=ev.ahead,
                              wait_ms=ev.estimated_wait_ms)   # "正在准备模型"
        elif ev.type == StreamEventType.META:
            ui.show_model(ev.model, ev.degraded)
            # knowledge 是 dict：kb_ids / all_bases / base_count /
            # used_chunks / context_tokens / degraded / warnings / sources
            sources = (ev.knowledge or {}).get("sources") or []
            ui.show_kb_scope(ev.knowledge)
        elif ev.type == StreamEventType.WARNING:
            ui.add_warnings(ev.warnings)
        elif ev.type == StreamEventType.DELTA:
            ui.append(ev.text)
        elif ev.type == StreamEventType.USAGE:
            ui.show_usage(ev.usage)          # prompt_tokens 可能是 None（未知，不是 0）
        elif ev.type == StreamEventType.DONE:
            ui.finish(ev.finish_reason)      # "stop" / "length"
            ui.render_sources(sources)
```

**取消**：`ChatWorker.cancel()` 里调迭代器的 `close()`。SDK 的 `ClosingIterator.close()` 会关闭底层连接，服务端随之停止生成；不 close 会让引擎继续烧 CPU 到结束（用户以为已经取消了）。

**`finish_reason == "length"` 必须提示**"回答被截断"，否则用户会以为模型只答了一半就自己停了。

**历史落库时机**：`done` 时写一条 `assistant` 消息（含 `sources` 快照，§9）；中断（用户取消或 `error`）时写一条带 `warnings=["本次回答未完成"]` 的记录——不写会丢掉用户已经看到的半截内容，而那是他上下文的一部分。

### 8.3 来源卡片与文件跳转

```python
path = files.get_path(doc_id)      # 查本地薄表，不经 API
page = src.page_start              # Runtime 透出的字段（0 = 未知）
open_file.jump(path, page, snippet=src_text)
```

| 文件类型 | 跳转方式 | 备注 |
| --- | --- | --- |
| PDF | 优先 `AcroRd32.exe /A "page=N"`；无 Acrobat 则 `os.startfile(path)` + 页码复制到剪贴板 | 页码参数只有 Acrobat/福昕支持，**不能保证所有机器有** |
| DOCX / XLSX / PPTX | `os.startfile(path)`，无页码；把片段文本复制到剪贴板，便于用户 Ctrl+F | Word/Excel 不接受命令行页码/行号 |
| TXT / MD | 内置预览器滚到命中位置 | **不能靠 `start_rune`**——见下 |

**`start_rune` 不可用**：检索结果只透出 `page_start` / `page_end` / `heading_path` / `chunk_type`，**没有 `start_rune` / `rune_count`**。所以 TXT/MD 的"精确滚到某一行"当前做不到。

**决策：在文本里搜片段，不为此改契约**。理由：`start_rune` 是相对"解析后的纯文本"的偏移，而用户打开的是**原文件**（带 Markdown 标记、YAML front matter、CRLF），两者本来就不一致——即便透出也定位不准。搜片段反而更贴近用户看到的字符串。（服务端侧已明确不做该字段，见增补需求 §5 N1。）

```
snippet = src_text[:40]                     # 去掉空白与常见 markdown 前缀
pos = file_text.find(snippet)
if pos >= 0: cursor.setPosition(pos)
else:        cursor.setPosition(0); 提示"未能在文件中定位，已打开文件开头"
```

**必须诚实**：定位失败时按钮文案为"打开文件"，成功时才说"跳到命中处"。界面承诺了却做不到，比不承诺更糟。

### 8.4 只查不答（第二个入口）

"检索"按钮不走向量+生成，直接：

```python
client.search(SearchRequest(query=q, kb_ids=kb_ids, mode="hybrid", top_k=10))
```

用途：

- 用户想看"库里到底有什么"，不想等生成。
- 排查"为什么这个问题答不上来"——看 `keyword_rank` / `semantic_rank` 能直接判断是关键词漏了还是语义没命中。

几乎零成本（一个已有端点），但对调试和用户信任的价值很高。

### 8.5 降级必须可见

**先明确一件事实**：低内存时 Runtime **根本不初始化知识库子系统**（`cmd/localai-cli/serve.go`：`if !capReport.RAGEnabled() { } else { initKnowledge(...) }`），`deps.Knowledge` 为 nil，于是 `/api/v1/knowledge/*` 与 `/api/v1/search` **全部 404**。

因此"仅关键词检索仍可用"在低内存机器上**不成立**——连 `create_kb` 都没有。v1.0 的这一行是错的，按 v1.1 重写：

| 服务端状态 | 桌面端表现 | 禁止行为 |
| --- | --- | --- |
| `rag_enabled=false`（`rule_only`） | 知识库页与对话页都进入只读：顶部常驻黄条"本机内存不足，知识库与问答都不可用（需要 X，当前 Y）。请关闭部分程序后重启服务。" 提供"打开管理后台" | 静默忽略 `kb_ids`、假装检索过、假装在索引 |
| `rag_enabled=false`（`chat_only`） | 对话可用；知识库页显示"当前内存只够普通对话，知识库不可用"。**不要写"仅关键词检索可用"**——那需要知识库子系统存在 | 让用户以为能建库/能导文件 |
| 知识库端点探测失败（`create_kb` 返回 404） | 视为"知识库子系统未启用"，与 `rag_enabled=false` 同一套 UI | 显示"内部错误"这类无法行动的信息 |
| 向量化不可用（`kb_stat().vectorize.model` 为空 / `vectorize` 报未配置） | 检索退化为关键词，输入框上方黄条："未配置向量模型，当前只能按关键词检索。语义检索需在管理后台配置 Embedding 模型并重启服务。" | 假装语义检索生效 |
| `meta.degraded=true` | 回答上方小字："本次使用 qwen2.5-1.5b（原请求 qwen2.5-3b）" | 不提示 |
| `knowledge.warnings` 非空 | 回答下方折叠区展示（如"上下文预算不足，已裁掉 3 段资料"） | 丢弃 |
| `knowledge.used_chunks == 0` | 回答前显示"知识库中未找到相关资料，以下为通用回答" | 让普通回答看起来像有依据 |
| `queue_full` / `resource_limited` | "正在排队，前面还有 N 个任务"，按 SDK 的 `Retry-After` 自动重试 | 直接报错或无限重试 |

**怎么判断"知识库子系统是否存在"**：`capability()` 只给 `rag_enabled`，不给端点是否注册。可靠做法是启动时探一次 `list_kb()`：404 → 子系统不存在；200 → 存在（哪怕结果是空列表）。**不要用 `rag_enabled` 推断端点存在性**——两者在实现上是两件事。

**关于提示文案的准确性**：用户看到"内存不足"时会去关程序，所以必须同时给出 `baseline_need_bytes` 与 `available_bytes` 的真实数字（`capability()` 都有），不要只说"内存不足"。数字来自服务端，桌面端不复算。

**这是产品底线**：知识库问答的可信度来自"用户知道这次答案有没有依据"。任何一次静默降级都会让用户对整个功能失去信任，而这个信任无法通过后续正确回答恢复。

---

## 9. 数据模型（`state.db`）

```sql
-- 扫描根目录
--
-- ## 为什么唯一约束是 (kb_id, path) 而不是 path
--
-- 同一个目录可能要进两个库（比如"工作"与"2025年度"），而 path 全局唯一
-- 就禁止了这件事。改成复合唯一后，同一个路径可以在多个库里各有一条。
--
-- ## 为什么还需要 scan_overlaps
--
-- 允许目录跨库会让"同一个文件被两个库各导一份"变成默认行为。这是刻意的
-- （§12 B2），但必须**让用户看见**：建库或加目录时若与已有库重叠，提示
-- "这些文件已经在「工作」库里，会在新库里再存一份"，由用户决定。
create table scan_roots (
  id          integer primary key autoincrement,
  kb_id       text    not null,
  path        text    not null,        -- 绝对路径，已归一化（小写 + 去尾斜杠）
  recursive   integer not null default 1,
  enabled     integer not null default 1,
  created_at  integer not null,
  unique(kb_id, path)
);
create index idx_roots_path on scan_roots(path);

-- 排除规则（隐私承诺的实现点）
create table excludes (
  id          integer primary key autoincrement,
  pattern     text    not null,        -- 绝对路径前缀或 glob
  kind        text    not null,        -- dir / glob
  is_default  integer not null default 0  -- 内置的密码库/浏览器数据规则
);

-- 本地文件事实（唯一目的：判断"要不要重新导入"）
create table local_files (
  id          integer primary key autoincrement,
  kb_id       text    not null,
  path        text    not null,        -- 归一化后的绝对路径
  doc_id      text,                    -- null 表示尚未成功导入
  sha256      text    not null,
  size        integer not null,
  mtime       integer not null,
  -- status 取值见 §7.5 / §7.7.1：
  --   pending / importing / imported / failed / skipped / deleted
  --   locked / too_long / cloud_only / too_large / orphaned / missing
  --   **excluded**（用户主动从库中移除，文件仍在磁盘）
  status      text    not null,
  error       text    not null default '',   -- 失败/跳过的可读原因（§5.6）
  name_sent   text    not null default '',   -- 上次提交给服务端的显示名（§7.2.1）
  imported_at integer,
  unique(kb_id, path)
);
create index idx_lf_doc  on local_files(doc_id);
create index idx_lf_stat on local_files(kb_id, status);
create index idx_lf_hash on local_files(kb_id, sha256);  -- §12.3 完全重复分组

-- 待用户确认的相似组（§12）
create table pending_dups (
  id          integer primary key autoincrement,
  kb_id       text    not null,
  group_id    text    not null,      -- Runtime 侧 dup_groups.id（exact 组为空）
  kind        text    not null,      -- exact / similar
  paths       text    not null,      -- JSON: [{path, sha256, mtime, size, doc_id}]
  resolved    integer not null default 0,
  chosen_path text    not null default '',
  created_at  integer not null
);
create index idx_pending on pending_dups(kb_id, resolved);

-- 指纹本地副本（§12.9 说明为什么需要）
create table local_fingerprints (
  kb_id   text not null,
  path    text not null,
  simhash integer not null,
  runes   integer not null,
  primary key (kb_id, path)
);

-- 对话历史（§16 P5 决策：存桌面端，不进 Runtime）
--
-- ## 为什么 sources 存 JSON 快照而不是只存 doc_id
--
-- 引用卡片要显示"文件名 + 页码 + 片段"。若只存 doc_id，用户删掉或重建
-- 知识库之后，历史消息里的引用就变成了空卡片——而"我当时是依据什么
-- 回答的"恰恰是历史消息唯一的用处。存快照让历史可读，代价是几十字节。
create table conversations (
  id          text    primary key,       -- uuid
  title       text    not null default '',
  -- kb_mode 是会话级的知识库开关，取值 none / selected / all。
  --
  -- ## 为什么用三值而不是"kb_ids 为空即不用"
  --
  -- 空数组在 /search 里是错误、在 /chat 里是"不查库"，两边语义不同。
  -- 用一个显式的 mode 记下用户当时的意图，才能在重开会话时还原
  -- 勾选状态，也不会让"我曾经选了库但后来全取消"与"我从没用过库"
  -- 变成同一件事。
  kb_mode     text    not null default 'none',
  kb_ids      text    not null default '[]',  -- JSON 数组，kb_mode=selected 时有效
  created_at  integer not null,
  updated_at  integer not null
);

create table messages (
  id              integer primary key autoincrement,
  conversation_id text    not null references conversations(id) on delete cascade,
  role            text    not null,          -- user / assistant
  content         text    not null,
  -- sources 是 assistant 消息的引用快照，JSON：
  --   [{index, doc_id, name, page_start, page_end, heading_path, snippet, path,
  --     file_available}]
  -- path 只在本地表里存，用于"打开原文"；不进任何外发内容。
  -- file_available 记录写这条消息时文件是否还在——历史消息不随磁盘变化
  -- 而改变结论（§7.7.2 的"查看片段"依赖它）。
  sources         text    not null default '[]',
  -- kb_mode / kb_ids / all_bases / base_count 记录本次实际用了哪些库。
  -- 用户问"这条答案查了哪些库"时不能靠猜。
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
create index idx_msg_conv on messages(conversation_id, id);

-- 设置
create table settings (key text primary key, value text not null);
```

**`local_files` 不存 `text`、不存 `chunk_count`**——后者从 `list_documents()` 读。重复存一份必然有一份是旧的。

**`messages` 存的是"用户看到过的东西"，不是 Runtime 的原始响应**：`sources` 里带 `path` 是为了"打开原文"，带 `snippet` 是为了服务端内容变化后仍能显示当时引用了什么。这两点都不会离开本机。

---

## 10. 内存与性能：先测，再预算

**这一节不写具体 GiB 数字**。理由很直接：桌面端的解析库（PyMuPDF 峰值）、Qt 渲染长列表、Embedding 引擎的 Job 上限（当前 512 MB）都还没有实测数据，凭估算写出来的表看着精确、实际会误导排期与验收（v1.0 就写了 4.70 GiB 与"< 5.5 GiB"，两者都没有依据）。

**做法**：M5 打包完成后做一轮极限测试，结果落 `docs/实测记录/`，再回填本节。在那之前，验收只看"服务端报什么、桌面端是否如实展示"（§10.4），不看具体数字。

> **已回填（2026-11-16）**：实测数据见
> `docs/实测记录/20261116-桌面端与Runtime内存标定.md`，方法见
> `tools/bench_memory.py`。下面是结论摘要；**估算被推翻的两处**用 ⚠️ 标出。

### 10.0 实测摘要（16 GiB 开发机）

| 对象 | 实测 | 说明 |
| --- | --- | --- |
| 桌面端空转 | **31 MB** | 只 import 依赖 |
| 桌面端解析 500 页 PDF | **72 MB** | 相对空转 +41 MB，耗时 4.4 s |
| 桌面端峰值 | **105 MB** | 出现在"向量化等待"（累计 chunk 列表）；真实桌面端逐篇提交，峰值约 70~80 MB |
| Runtime 无模型基线 | **344 MB** | 含 63 MB 的 Embedding 引擎 |
| 入库 5.9 万块 | +33 MB | 块文本 + FTS5 索引 |
| 向量化 5.9 万块 | +109 MB | 向量存 SQLite BLOB，不常驻内存；约 **0.001 MB/块** |
| 加载 qwen2.5-3b | **+3318 MB** | 占 Runtime 总量 90%，是固定成本 |
| 一次提问（模型已加载） | 27 s / 回答 23 字 | 桌面端增量 0 MB |
| PDF 解析速度 | **113 页/秒**（8.8 ms/页） | 500 页 4.42 秒 |
| 切块速度 | 约 3 万块/秒 | 1000k 字 → 6579 块，0.2 秒 |
| **向量化速度** | **9.9 块/秒** | ⚠️ 这是真正的瓶颈，见 §10.6 |
| 检索（296 块库） | 140~170 ms | 含 FTS5 + 向量 + RRF + HTTP |
| 检索（16016 块库，热） | **469 ms** | 通道拆解见 `docs/实测记录/20261116-检索耗时通道拆解.md` |
| 检索（16016 块库，冷启） | 688 ms | 只比热多 219 ms → `LoadIndex` 很快 |
| **检索（100100 块库，热）** | **1704 ms** | 中位数，范围 1625~2063 ms（n=13） |
| **检索（100100 块库，冷启）** | **8156 ms** | 一次性索引载入，之后常驻；需 UI 提示 |
| **入库 10 万块** | 84.1 s | Runtime +2 MB |
| **向量化 10 万块** | **10084 s（2.8 h）** | 9.9 块/秒，Runtime **+125 MB** |

⚠️ **推翻的估算 ①**：本文件早先按"PyMuPDF 解析时峰值再加 150~250 MB"做预算。
实测 500 页 PDF 只增 **5 MB**——PyMuPDF 按需分页，不把整份文档解压进内存。
结论：**桌面端按 100 MB 预算足够**（留 3.5 倍余量），不必为"解析大文件与推理
撞车"设计更严格的串行。

⚠️ **推翻的估算 ②**：`§10.5` 原写"检索耗时 < 50 ms"。296 块库实测 140~170 ms，
5.9 万块首次 2.4 s。**HTTP + RRF 的固定开销就有 100 ms 量级**，50 ms 不现实。
目标调整为：热检索 **< 300 ms**（待补测），首次检索单独提示"正在载入索引"。

### 10.1 极限测试要测什么（M5 必做）

| 场景 | 采集 | 判定 |
| --- | --- | --- |
| 空闲：桌面端 + Runtime + 模型已加载，无交互 | 进程 RSS 总和、系统可用内存 | 基线 |
| 单库 10 万 chunk + 模型已加载 + 一次提问 | 同上 + 首 token 延迟 | 峰值是否触碰 `capability()` 的判定边界 |
| 解析 500 页 PDF（约 20~40 MB）同时提问 | 桌面端 RSS 峰值 + Runtime RSS 峰值 | 实测此项不紧张（解析只 +5 MB），但并发仍需测 |
| 扫描 5000 篇（含 Excel/PPT） | 内存曲线、CPU、耗时 | 会不会随时间单调上升（泄漏） |
| 长会话（200 轮）后再提问 | 桌面端 RSS | 消息流控件是否会累积 |
| 16GB 机器重复第一项 | 同上 | 是否有资格用 3B |

**结果写入 `docs/实测记录/YYYYMMDD-桌面端内存标定.md`**，字段包括：机型、RAM、OS 版本、模型、chunk 数、每场景的 RSS 峰值与稳定值、是否触发过 `resource_limited`。

### 10.1.1 测量的方法学要求（第一版测错了，值得记）

测 Runtime 内存**必须先固定它的状态**：模型加载/卸载会让 RSS 变化 **3 GB**，
足以淹没任何精细测量。

第一版标定因为没做这件事，得出了三个不可能的数字：

| 症状 | 原因 |
| --- | --- |
| "每块向量内存 = **-0.052 MB**" | 向量化跑了 8000 秒，期间模型被空闲卸载（`-idle-unload-sec 90`），RSS 从 3407 掉到 302 |
| "向量化 7.4 块/秒" | 同上，被卸载打断（实际 11.6） |
| "提问回答 0 字" | 模型已被卸载，请求失败 |

**正确顺序**：入库 → **显式 `unload_model()`** → 等 3 秒 → 测基线 →
向量化 → 测增量。此时增量为纯向量 + 索引开销。

### 10.2 在测试完成前，代码里怎么写

| 要做 | 不要做 |
| --- | --- |
| 状态栏显示 `resources()` 给的 `available_bytes` / `total_bytes` / `pressure` | 自己用 `psutil` 之类算"还够不够" |
| 收到 `ErrResourceLimited` 就暂停扫描并提示 | 解析前预判"内存可能不够"然后拒绝执行 |
| 解析串行、单文件、单线程 | 并发解析多文件 |
| 按 `capability().baseline_need_bytes` 解释"差多少" | 在代码里硬编码某个 GiB 阈值 |

### 10.3 规模上限

| 指标 | 上限 | 超出后的行为 |
| --- | --- | --- |
| 单库 chunk 数 | **10 万** | 到 8 万时黄条提示"接近上限"；到 10 万停止导入并说明原因 |
| 单文档块数 | **2000**（§5.7） | 超出不提交，标 `too_large`，提示拆分 |
| 单库文档数 | 由 chunk 上限推导，不单独设限 | 展示"还能再导入约 N 篇"（按当前平均块数估） |
| 单 PDF 页数 | 500 页（可配） | 超出提示拆分，不静默截断 |

**10 万是硬约束**（`local-ai-runtime.md` 22.7.2 实测：30 万 chunk 需 42ms/439MiB）。到上限的正确做法是**明确告知并建议拆库**，不是继续塞。

### 10.4 解析与推理的峰值错开

| 手段 | 说明 |
| --- | --- |
| 单文件解析 | 不并发解析多个文件（本来也是顺序导入） |
| 有对话在生成时让路 | `resources()` 显示 `pressure=high` 或有流式请求进行中时，解析队列暂停 2 秒再试；连续 3 次仍紧张则提示"内存紧张，已暂停扫描" |
| 页数上限 | 单 PDF 默认最多解析 500 页（可配），超出提示拆分 |
| 资源状态来源 | **只读 `resources()` 与 `ErrResourceLimited`，不在客户端计算**（§3.2） |

### 10.5 性能目标

**已按实测调整**（2026-11-16）。原表里的数字多为估算，两处被推翻
（检索 50 ms、PDF 单页 15~30 ms）。

| 项 | 目标 | 实测 | 说明 |
| --- | --- | --- | --- |
| 冷启动到可提问 | < 8 s | 未测 | 含唤醒 Runtime + 模型加载 |
| 首 token（热） | < 1.5 s | 未测 | 1.5B / ctx≈1500 |
| 检索耗时（**< 5000 块**） | < 200 ms | 296 块 140 ms；2002 块 keyword 110 ms | ✅ 实测成立 |
| 检索耗时（**5000~2 万块**） | **< 600 ms** | 16016 块 **469 ms** | ✅ 实测成立 |
| 检索耗时（**2 万~10 万块**） | < 2 s | **100100 块 1704 ms** | ✅ 落在区间内，但已明显超出门槛 |
| 检索耗时（首次，索引冷启动） | 单独提示 | 16016 块 +219 ms；**10 万块 8156 ms** | 10 万块的 8 秒**必须**有"正在载入索引"提示 |
| 100 篇 PDF 解析+切块+入库 | **< 1 min**（解析）/ 另有向量化 | 实测 **113 页/秒**（8.8 ms/页）→ 2000 页约 **18 秒** | ⚠️ 原估"单页 15~30 ms"偏悲观 3 倍。**但入库后的向量化另算**：100 篇约 2000 块 → 约 3 分钟 |
| 来源跳转 | < 1 s | 未测 | 含 PDF 阅读器启动 |
| 单篇失败不影响整体 | 必达 | ✅ M2 已实现 | 失败记 `local_files.error`，扫描继续 |

#### 检索目标为什么必须分档（实测依据）

`docs/实测记录/20261116-检索耗时通道拆解.md`（16016 块）与
`20261116-规模标定10万块.md`（100100 块）给出完整曲线：

| 库规模 | 热检索 | 冷启（首次） | 说明 |
| --- | --- | --- | --- |
| 296 块 | 140 ms | — | |
| 16016 块 | 469 ms | 688 ms | 冷启只多 219 ms |
| **100100 块** | **1704 ms** | **8156 ms** | 冷启多 6.5 秒（读 125 MB 向量 + 建索引） |

16016 块的通道拆解（`keyword` 203 ms / 合计 469 ms）说明 **FTS5 占 43%**。
按此比例外推，10 万块的 keyword 约 700~800 ms——**这是最值得优化的部分**。

三个必须记住的判断：

1. **"检索 < 50 ms"永远不可能达成**。仅 HTTP + FTS5 的固定成本就有 86 ms。
2. **不要优化向量索引**。16016 块时冷启只比热多 219 ms——如果扫描是瓶颈，
   冷启不会这么接近热态。`internal/knowledge/vector/vector.go` 的
   连续 `[]float32` + 8 路并行在 10 万块以内够用。
3. **首次检索的 8 秒是独立问题**。它不是"检索慢"，是索引载入。UI 必须
   把它和普通检索区分开说，否则用户会得出"这软件很卡"的结论，而实际
   后续都是 1.7 秒。

### 10.6 真正的瓶颈：向量化 10 块/秒

这是实测里**唯一影响产品形态**的结论。CPU 上 384 维 embedding 就是这个
量级，不是 bug。而且速度几乎不随规模衰减（16k 块 9.6 块/秒、10 万块
9.9 块/秒），所以可以放心用固定速率做估算：

| 库规模 | 一次性建库耗时（实测） |
| --- | --- |
| 1 万块（约 500 篇） | 约 17 分钟 |
| 5 万块（约 2500 篇） | 约 83 分钟 |
| 10 万块（上限） | 约 **2.8 小时**（实测 10084 秒） |

> 早期用 500 块基准测出的 11.6 块/秒偏乐观（样本小 + 引擎预热）。
> 10 万块的全程平均 **9.9 块/秒**才是可用于产品文案的数字。

**所以首次建库必须分阶段可用**（§15 已定原则，这里给量化依据）：

1. **解析阶段就可检索**：每篇解析完立即入库，**关键词检索立刻可用**
2. **向量化在后台**：显示"语义检索准备中（3000/60000）"，不阻塞提问
3. **明确告知剩余时间**：用实测 **10 块/秒** 估算，不要只转圈
4. **允许中断**：`vectorize` 分批落库，已完成的保留

提示语文案要求：不能写"正在建立索引"，要写
**"正在生成向量 3000/50000（约 65 分钟）"**。

一个 3000 篇目录的真实体验（按实测 113 页/秒 + 10 块/秒推算）：

```
解析 3000 篇（6 万页）   约 9 分钟
入库                    约 1.4 分钟
向量化 6 万块            约 100 分钟
────────────────────────────
总计                    约 110 分钟
```

**瓶颈完全是向量化**：解析 6 万页只要 9 分钟，向量化要 100 分钟。

而**第 10 分钟起就能用关键词检索了**。这是产品必须传达的信息——
否则用户会以为"软件卡住了"然后关掉。

---

## 11. 验收标准

### 11.1 功能验收（P0，缺一不可发布）

| 编号 | 用例 | 通过条件 |
| --- | --- | --- |
| A1 | 选一个含 100 篇 PDF/DOCX/XLSX/MD 的目录，扫描 | 全部完成或被跳过；**每个未导入的都有可读原因**（§5.6/§7.5） |
| A2 | 提 20 个已知答案的问题（分布在 20 个不同文档的不同页） | 答案正确 ≥ 17/20；引用文档正确 20/20；**页码正确 20/20** |
| A3 | 提 5 个资料中不存在的问题 | 5/5 回答"知识库中未找到依据"，不编造 |
| A4 | 修改 1 个文件，等 30 秒 | 该文档 `updated_at` 变化，chunk 数变化，向量重建，检索结果更新 |
| A5 | 删除 1 个文件 | 索引移除，再次提问不引用它 |
| A6 | 点击来源卡片 | 打开正确文件；PDF 跳到正确页（或按钮文案诚实为"打开文件"） |
| A7 | 断网运行全流程 | 全部通过（模型已就位的前提下，桌面端不发起任何外网请求） |
| A8 | 8GB 机器跑 A1~A6 | 全通过、无 OOM；内存曲线符合 `docs/实测记录/` 的标定值（§10.1） |
| A9 | 扫描中强制关闭程序，重启 | 已导入的保留（不重复导入）；未导入的重新开始 |
| A10 | 扫描中提问 | 提问能正常回答（不被解析阻塞） |
| A11 | 同一份 DOCX | 每个块的 `heading_path` 非空，与文档标题层级一致 |
| A12 | 一份 20 行 × 8 列的 Excel | 第 2 块之后仍含表头，问"某列某行"能命中 |
| A13 | 库内是旧切块器版本，用新版本导入 | 报 409 并提示"需重建"；点重建后 `delete_kb` + 全量重扫成功，无新旧混合 |
| A14 | 用 Go 侧数据（`chunker_name=go`）与客户端数据混用 | 同 A13（**不是**逐篇删除重导，那样永远 409，见 §7.6） |
| **A15** | **已装 LocalAIHub 的机器上安装 LocalKB** | 不重复装 Runtime、不写第二条 Run 键、不改 `launch-spec.json`；启动后连的是同一个实例（`/api/v1/capability` 的地址与 PID 一致）<br>✅ **已通过**（2026-11-16，20 项自动断言，`tools/verify-installer.ps1`） |
| **A16** | **未装 LocalAIHub 的干净机器** | 安装流程内完成 Runtime 安装；装完只有一个 Runtime、一份数据目录 |
| **A17** | **Runtime 被手动停止后打开 LocalKB** | 桌面端自动唤醒（`installer start`），20 秒内进入可用状态；唤醒失败提示"服务未启动"并给日志入口 |
| **A18** | **Runtime 运行中重启（换端口/token）** | 桌面端在一次重建后恢复，不要求用户重启程序；期间显示"正在恢复"而不是一堆报错 |
| **A19** | **在管理后台删掉正在用的知识库** | 桌面端启动对账后提示"该库已在服务端删除"，提供"重建并重扫"/"从本机移除"；不静默丢数据 |
| **A20** | **在资源管理器里重命名一个已入库文件** | 来源卡片显示新文件名；`local_files.path` 与 `name_sent` 同步更新。（依赖服务端 S2；未修复时按 §17.1 用本地文件名兜底） |
| **A21** | **模型未下载 / 未加载 / Embedding 未配置** | 三种情况各显示对应文案与"打开管理后台"按钮（§6.6），不出现"内部错误" |
| **A22** | **服务端 `rag_enabled=false`** | 知识库与对话页显示"内存不足"黄条并带真实数字；不显示"仅关键词检索可用"（§8.5） |
| **A23** | **单文档切出 > 2000 块** | 不提交，标 `too_large` 并说明"请拆分"；库里不出现半个文档 |
| **A24** | **文件被 Word 占用 / 云盘占位 / 超长路径** | 各自跳过并给出对应原因（§5.6/§7.5），扫描继续 |
| **A25** | **对话页知识库开关：不勾选 / 勾选具体库 / 勾"全部"** | 三种分别传 `None` / `["kb_1"]` / `[ALL_BASES]`；不勾选时回答不带任何来源卡片；勾"全部"时显示"已检索全部 N 个知识库" |
| **A26** | **检索页未选库** | 提示"请先选择要检索的知识库"，**不发请求**；不自动降级为全部库（§8.1.1） |
| **A27** | **从库里移除一篇文档，文件保留** | `delete_document(keep_file=True)`；磁盘文件仍在；`local_files` 行删除；再提问不引用它 |
| **A28** | **从库里移除并勾选"移到回收站"** | 先删记录成功，文件进回收站（可从回收站还原）；卡片不再存在 |
| **A29** | **删除文件后保留库内内容** | 文档标 `MISSING`，仍可被检索命中；来源卡片显示"源文件已不在"+ "查看片段"按钮 |
| **A30** | **移动整个目录后重启** | 汇总提示"检测到 N 个文件的源路径失效，可能是目录被移动"，提供"这是移动到了新位置"批量重绑；**不自动删索引** |

### 11.2 质量门禁

| 项 | 标准 |
| --- | --- |
| 静默降级 | **0 次**。所有 `warnings` / `degraded` / `used_chunks=0` 都必须可见（§8.5） |
| 页码错误 | **0 次**（A2 的页码项一票否决） |
| 重复文档 | 同一内容导入两次后 `documents` 只有 1 条 |
| 日志正文 | 日志与诊断包**不含文档正文**（只记路径与状态） |
| 崩溃恢复 | 引擎崩溃后 Runtime 自动重启，桌面端显示"正在恢复"而不是卡死；Runtime 进程本身崩溃时按 §3.1.2 恢复 |
| 解析失败文案 | 每条都能指向下一步动作（§5.6） |
| **桌面端不改服务端状态** | 除知识库/文档/向量化/相似检测外，不调用任何写接口；不写 `launch-spec.json`、不读写 `runtime.json` 之外的服务端文件 |
| **单实例不冲突** | 装有 LocalAIHub 的机器上，安装与运行时都只有一份 Runtime、一份 `%LOCALAPPDATA%\LocalAIHub` |
| **删文件必须用户勾选** | 任何路径下都不出现"未询问就删除用户文件"；删除走回收站，可还原（§7.7） |
| **原文不被改写** | 入库的块文本与解析结果逐字一致，不含模型生成的任何内容（§5.8） |

### 11.3 回归测试

- **解析器**：每格式至少 3 个样本（正常 / 超长 / 异常）。PDF 需含中文与多栏。
- **切块器**：必须覆盖
  - 段落边界优先
  - 英文句号后不跟空白时**不**切（§5.5 缺陷 1）
  - 重叠只在兜底阶段出现（§5.5 缺陷 2）
  - 最小块合并
  - 覆盖原文 90% 以上
  - 标题路径拼在正文之前
  - Excel 表头重复
- **页码**：10 页 PDF，抽查 20 个块的 `page_start` 与人工标注一致。
- **扫描器**：`walker`（含 §7.5 的 7 类跳过）/ `hasher` / `excludes` / 防抖用 pytest 覆盖（不依赖 Qt）。
- **UI**：手工清单（含 A15~A30 的联调用例）。
- **端到端**：`LAIH_E2E=1 pytest sdk/local-ai-runtime-sdk/python/tests/test_live.py` 保持通过。**注意**：该文件当前用 `import_document`（服务端已隐藏的接口），阶段 A 必须把它迁到 `ingest_chunks`，否则这个门禁是空转。

### 11.4 与 Go 实现的对照（发布前必做）

> **前置条件（阶段 A 交付）**：`tools/ragbench` 现在走 `POST /documents`，那条路由已隐藏（404）。必须先把它改成"CLI 内置解析 + `chunk.Split` 产块 → `ingest_chunks`"，并把 `chunker_name=go`，否则基线组跑不起来，本节判据无法执行。

用 `tools/ragbench` 的固定语料，对同一批文档跑两遍：

| 组 | 语料 | 切块器 |
| --- | --- | --- |
| 基线 | 50 篇中文文档 | Go（`chunker_name=go`） |
| 新版 | 同一批 | 客户端（`chunker_name=localkb-py`） |

比较指标：`recall@5`、`correct_doc_rate`、`noanswer_rate`。

**判据**：新版的 `recall@5` 不得低于基线的 90%。若低于，先查切块参数是否与 §5.1 一致，再查标题路径是否引入了噪声（标题误判会稀释向量）。

---

## 12. 重复与相似文件提醒

> 目标（G7）：扫描时发现"同一份内容的多个副本"或"同一文档的多个版本"，**列出来提醒用户**，由用户指定一份作为入库版本。其余文件原样留在磁盘上，只是不入库。
>
> 一个动作同时达成两个目的：
> 1. **对用户**——知道这台电脑上哪些文件重复、哪些是同一文档的不同版本，一共占了多少空间。
> 2. **对知识库**——同一内容不重复入库，检索时不会有三份一模一样的片段霸占 top_k，答案也不会因命中不同版本而自相矛盾。

### 12.1 三个"不做"

外部方案把它做成了"重复文件清理工具"：归档、移动、版本链分类、处置动作状态机、把"可释放空间"作为行动号召。这套能力本身没错，但把产品边界放大了三倍，而**与本产品要解决的问题无关**。

本产品在重复检测上只需要一件事——**入库前确认版本**。因此：

| 不做 | 为什么 |
| --- | --- |
| **不为重复组提供任何"删除/归档/移动"动作** | 重复检测的产物只是"选哪份入库"。用户想清理磁盘，自己按提示的路径去资源管理器处理。这与 §7.7 的"单个文档的显式删除"不是同一件事——后者是用户对特定文件的明确意图，前者是系统替用户批量做决定 |
| **不做处置动作状态机**（keep / archive / mark / ignore） | 只有两个状态：`已入库`、`未入库`。第二种随时可改。 |
| **不把"可释放空间"做成行动号召** | 只作信息展示（"这 5 个文件共占 21 MB"），不提供"一键释放"。 |

省下来的复杂度是实打实的：没有跨卷移动、没有权限与被占用处理、没有撤销栈、没有"移动到一半崩溃"的部分成功状态。**回收站语义只在 §7.7 的单文档删除里出现一次**，且是用户在对话框里勾的。

### 12.2 两个层级就够

| 层 | 判据 | 要用户做的判断 | 默认行为 |
| --- | --- | --- | --- |
| **完全重复** | 文件 sha256 相同 | **不需要判断**——内容一模一样 | 自动选最早修改的那份，只提示"另有 N 个副本未入库" |
| **内容相似** | 文本指纹相近但不是同一文件 | **需要判断**——选哪一版 | 见下方"默认选中策略" |

#### 12.2.1 相似组的默认选中策略（可配置）

**默认由用户来选**（不做任何预选），因为"哪个版本对"是内容判断，程序猜不准。

但设置页提供一个开关，让愿意省事的用户打开自动预选：

| 设置项 | 取值 | 行为 |
| --- | --- | --- |
| `similar_default_choice` | `none`（默认） | 对话框里**全部不选中**，用户必须自己点一个才能确定 |
| | `newest` | 预选修改时间最新的那份（标记"最新"，仍可改） |
| | `oldest` | 预选最早的那份（适用于"以归档版为准"的用户） |

**不管选了哪个，都不会自动提交**：预选只是把单选按钮放在某个位置，`[确定]` 仍要用户点。这与"不做自动决定"不矛盾——自动决定指的是"不问用户就入库"。

`none` 作为默认的理由：同一份文档的两个版本，哪个"应该进知识库"取决于内容差异，而不是时间。预选最新会让用户习惯性直接点确定，从而在"新版其实是误存的草稿"时把旧版挤掉。

**为什么不需要"版本链"作为独立分类**：外部方案分它，因为动作不同（前者建议删，后者不建议删）。这里两者动作**完全一样**（都是"选一个入库"），分类无意义。

### 12.3 完全重复：为什么必须在客户端判

`importer.Import` 在同知识库内按内容哈希去重，命中就直接返回已有文档、**不新建记录**（Runtime 侧源码）。且 `FindDocumentByHash` 只在**单个知识库内**查。

后果有两条，都决定架构：

1. **同一个 KB 内的 5 份相同文件，`documents` 表里只有 1 行。** 在 Runtime 侧按 `file_hash` 分组只会查出一堆单元素组——这个功能等于不存在。
2. **跨知识库查不到。**

**结论：完全重复的"文件清单"真相在客户端 `local_files` 表**，它记录每个磁盘路径 + 每个文件的 sha256（扫描器的天然产物）。实现是一条 SQL：

```sql
select sha256, count(*) as n, group_concat(path, '|') as paths
from local_files
where kb_id in (...) and status in ('imported','skipped','pending')
group by sha256 having n > 1
order by n desc;
```

零新增接口、零 Runtime 改动、几万条毫秒级返回。

**这条也说明一件事**：如果将来把扫描器挪进 Runtime，必须先让它记录全部路径，否则这个功能退化（ADR 0013）。

### 12.4 内容相似：Runtime 负责检测

指纹算法、阈值、并查集与直径校验、Excel 结构化指纹、数据模型、三个只读端点，**全部在 Runtime 侧**（配套文档 Phase 2）。

客户端只需要：

1. 扫描结束后调一次 `dedup_check(kb_id, DedupCheckRequest(doc_ids=本次新入库的文档))`。
2. 有分组就写 `pending_dups` 并显示提示条。

**两处必须注意**（SDK docstring 明确警告）：

1. **要检查返回的 `pending`**：快路径只对已有指纹的文档分组。刚导入的文档可能还没有指纹，它们会出现在 `pending` 里——此时"没有重复"这个结论是**不完整的**。做法是传 `recompute=True`（5000 篇约 10 秒，background 优先级），或轮询到 `pending` 为空。
2. **不要传空 `doc_ids`**：空表示"该库全部文档"，增量扫描时会变成全库重算，与 §12.5"只对本次新入库的文档做"矛盾。

**为什么检测必须在 Runtime**：它要读已入库的块文本算指纹。客户端算一遍等于重复解析，且两边的分词必须一致（否则相似度不可比）。

### 12.5 什么时候提示（时机比算法重要）

外部方案是"用户在清理页点开始扫描"。本方案**嵌在扫描流程里**，因为用户不会主动去点一个他不知道自己需要的按钮：

```
扫描目录
  │
  ├─ 逐文件算 sha256，查 local_files
  │     ├─ 已在库里、hash 未变      → 跳过
  │     ├─ hash 在别处已存在        → 【完全重复】不入库，记一条提示
  │     └─ 新文件或内容变了          → 进导入队列
  │
  ├─ 全部导入完成
  │
  └─ 【内容相似】检测（仅本次新入库的文档）
        └─ 有相似组 → 结束时报一次：
           "本次导入的 240 个文件里，有 6 组共 19 个文件内容相似，
             可能是同一文档的不同版本。要现在确认吗？[去确认] [稍后]"
```

**四个关键设计**：

- **只对本次新入库的文档做**，不对全库重算。增量扫描时只涉及改动的几个文件——而这正是用户最需要确认的时刻（"我刚存了一版，是不是和旧版重复了"）。
- **完全重复不打断流程**，只在结果里聚合展示。内容一模一样，没有决策要做。
- **内容相似在扫描结束后统一提示一次**，不逐文件弹窗。扫 240 个文件被弹 6 次窗，用户会关掉所有弹窗。
- **提示可延后**。点"稍后"，下次打开知识库页仍有待办（`pending_dups` 记着）。

### 12.6 与知识库的关系

| 情况 | 处理 |
| --- | --- |
| 完全重复 | 不入库。界面只告知"另有 3 个副本未入库" |
| 内容相似 | 用户选一版入库，其余不入库。没入库的版本不在库里，检索不会命中，因此不会出现"答案自相矛盾" |
| 用户改变主意 | 随时可换：原版 `delete_document(kb_id, doc_id)` 移除，新版 `ingest_chunks` 导入，索引与向量跟着重建 |

**不做的一件事**：不自动选"最新版"并提交。看起来聪明，实际会让用户问"我明明有这个文件，为什么搜不到"——而那个文件是他自己三个月前写的。**入库必须是用户点的那一下**（预选只是省一次点击，见 §12.2.1）。

### 12.7 界面

**不新增页面**，挂在知识库页文档列表上方：

```
┌─ 知识库：我的文档 ────────────────────────────────────────┐
│  2,847 篇 · 索引中 12 篇                                   │
├───────────────────────────────────────────────────────────┤
│  ⚠ 有 3 组文件内容相似，需要确认入库版本        [去确认]   │
├───────────────────────────────────────────────────────────┤
│  文档列表…                                                │
└───────────────────────────────────────────────────────────┘
```

点"去确认"打开对话框：

```
┌─ 确认入库版本 ───────────────────────────────────────────┐
│  这 3 个文件内容相似（相似度 91%~96%），请选一个入库：      │
│                                                          │
│  ○ D:\工作\项目报告_v2.docx      2025-02-20  14 段       │
│  ○ D:\工作\项目报告_final.docx   2025-03-11  15 段       │
│  ○ D:\备份\项目报告.docx         2025-01-08  12 段       │
│                                                          │
│  差异（选中任一文件后显示）：                              │
│  · v2 比 final 少 2 段                                    │
│  · 12 段被修改，最大改动在"第三部分 预算"                  │
│                                                          │
│  ☑ 其余 2 个文件不加入知识库（文件本身不动）                │
│                                                          │
│              [稍后再说]            [确定]                 │
└──────────────────────────────────────────────────────────┘
```

必须有的：

- **默认不预选**（`similar_default_choice=none`）；用户把这个设置改成 `newest` / `oldest` 时才预选，且预选后仍要用户点确定（§12.2.1）。
- **未选中时 `[确定]` 不可点**，并显示一行灰字"请选择要入库的版本"——否则用户会点确定然后什么都没发生。
- **明确写"文件本身不动"**。这句是消除顾虑的关键——用户确认的是"哪个进知识库"，不是"删掉哪个文件"。
- 差异摘要辅助判断，但**不强制阅读**。

完全重复的组不需要对话框，只给一行：

```
ℹ 另有 12 个文件与库内文档内容完全相同，已跳过（共 48 MB）
```

**禁止**：任何"清理""释放空间""删除副本"的措辞或按钮。

### 12.8 计算开销

完全重复是客户端一条 SQL（< 100 ms）。内容相似的检测在 Runtime（配套文档 §5.4）：5000 篇约 10 s，background 优先级，不占 GPU、不调 embedding、不加载模型。

### 12.9 为什么客户端也要存一份指纹

Runtime 的 `doc_fingerprints` 只覆盖**已入库**文档，而"完全重复"要在入库前就判出来——那份重复文件根本不会导入，它不进 Runtime 的表。

`local_files` 已有 sha256，`local_fingerprints` 补 simhash，两个判定都在客户端完成，**不需要为"不入库的文件"在服务端建表**。

（Phase 1 期间可以只做完全重复，`local_fingerprints` 表先不建。）

### 12.10 验收

| 编号 | 用例 | 通过条件 |
| --- | --- | --- |
| B1 | 同一文件复制到 5 个不同目录（同一 KB） | 1 组，5 成员；只入库 1 份 |
| B2 | 同一文件放进两个不同 KB | 两个库各自入库 1 份（跨库不共享，符合隔离要求）；加目录时提示了重叠 |
| B3 | 一份 3000 字文档改 5 处错别字 | 判为 similar 组，差异摘要列出改动；`pending` 为空（否则结论不完整，§12.4） |
| B4 | 一份文档末尾加 3 段 | 判为 similar 组，摘要显示"+3 段" |
| B5 | 两份不同的 1 月/2 月 Excel | **不成组** |
| B6 | 合同模板的 8 个填空版 | 成组；确认后 1 份入库、7 份不入库 |
| B7 | 用户选 A 入库，之后想换成 B | A 从库中移除，B 导入，检索结果跟着变 |
| B8 | 用户点"稍后再说"，关程序再打开 | 知识库页仍有"待确认"提示，不丢失 |
| **B9** | **默认（`none`）打开确认对话框** | 无预选；未选时 `[确定]` 不可点并给出提示 |
| **B10** | **设置 `newest` 后打开同一对话框** | 预选最新那份（标"最新"），仍可改；不点确定不入库 |
| **B11** | **在确认对话框里看到的重复组，点"打开所在位置"** | 只在资源管理器里选中文件，**不提供任何删除/移动按钮**（§12.1） |

**B5 是核心难度**：这条不过说明 Excel 结构化指纹没做对，用户会看到"所有报表都是重复的"，然后关掉这个功能再也不打开。

**B8 是产品底线**：待确认状态必须持久化。丢了比不提示更糟——用户以为确认过了。

### 12.10.1 实测结果（2026-11-16）

验证脚本 `tools/localkb_m7_verify.py`（19 项）+ `tests/test_duplicates.py`（9 项）
+ `tests/test_dedup_similar.py`（16 项）。

| 编号 | 状态 | 实测 |
| --- | --- | --- |
| B1 | ✅ | 5 份 → 1 份入库、4 份 duplicate，库内与服务端都只有 1 篇 |
| B2 | ✅ | 按 `kb_id` 隔离（`local_files` 天然按库分） |
| B3/B4 | ⚠️ | 取决于阈值（见 B6） |
| **B5** | ✅ | **两份不同报表不成组**（核心通过） |
| **B6** | ❌ | **8 个填空版只成 2 组共 5 个**——`ThresholdShort=2` 偏严 |
| B7 | ✅ | `switch_kb_version()`（先导后删，见 §12.6 的实现说明） |
| B8 | ✅ | 落库 + 新建连接读回（模拟重启） |
| B9 | ✅ | 无预选、`[确定]` 灰掉、"请选择要入库的版本" |
| B10 | ✅ | 预选最新（2025-03-03），差异区立刻显示 |
| B11 | ✅ | 对话框内只有"在文件夹中显示"，无任何删除/移动按钮 |

**B6 的根因是阈值未校准，不是代码缺陷**：
`internal/knowledge/dedup/simhash.go` 的注释自己写着"这些是初值，
必须用标注集校准后回填"。详见
`docs/实测记录/20261116-相似阈值对模板文档过严.md`。

**这条可以作为已知问题发布**：阈值偏严只会让用户多点几次"确认"，
不会产生错误结果；反方向（过松）会导致"确认后另一份内容搜不到"，
那是不可逆的信息损失。所以"宁漏勿错"的取舍是对的，只是当前偏保守
到 B6 不成立。

### 12.11 里程碑

| 阶段 | 内容 | 验收 | 预估 |
| --- | --- | --- | --- |
| **D1** | 完全重复：`local_files` 按 sha256 分组 + 跳过 + 告知条 | B1/B2 | 0.5 天 |
| **D2** | 内容相似：接 Runtime 的 `dedup_check`（含 `pending` 处理）+ 确认对话框 + 默认选中策略设置项 | B3/B5/B6/B9/B10 | 1.5 天 |
| **D3** | 换版本流程 + 待确认持久化 | B4/B7/B8 | 1 天 |

**D1 随 M3（扫描入库）一起做**：`local_files` 那时正在写，加一条 SQL 和一个提示条即可。

D2/D3 依赖 Runtime 的 Phase 2，排在 M6 之后。

**实际完成情况（2026-11-16）**：D1~D3 一起在 M7 完成。
D1 没有"随 M3 一起做"——那时只写了 `find_duplicate_hashes` 这条 SQL，
扫描流程并未接线（`_classify` 不判重），所以 B1 在 M3 阶段是不成立的。
这属于排期判断失误：当时以为"加一条 SQL 和一个提示条即可"，
实际还需要新的 `FileAction`、状态持久化、提示条与详情视图。

---

## 13. 打包与分发

### 13.1 一个安装包，内含两部分

```
LocalKB-Setup-<ver>.exe        （体积以 M5 构建产物为准，不预估）
├── LocalKB.exe                # PyInstaller --onedir 冻结的桌面端
├── _internal/                 # Python 运行时 + PySide6 + 解析库
├── vendor/
│   └── LocalAIHub-setup.exe   # 仅在"本机没有 LocalAIHub"时运行（§3.1.1）
├── uninstall.exe
└── install-manifest.json      # 版本、文件哈希、API 契约下限
```

**不打包 Runtime 二进制**。v1.0 的 `runtime/localai-cli.exe` + `engines/` + `models/registry/` 全部删除：那会让本机出现第二份 Runtime，与 §3.1 直接冲突。

**没有 Runtime 时怎么办**：安装向导检测不到 LocalAIHub 时，运行 `vendor/LocalAIHub-setup.exe`（静默参数），装完再继续。两条路径最终落到同一个 `%LOCALAPPDATA%\Programs\LocalAIHub`，只有一份实现、一份 `launch-spec.json`。

**为什么不用 PyInstaller `--onefile`**：onefile 每次启动要解压到 `%TEMP%`，PySide6 项目冷启动多 3~5 秒，且被杀毒反复扫描。`--onedir` 启动 1 秒内。

**模型不进安装包**（也不由桌面端下载）：模型是服务端资产，由管理后台下载到 `%LOCALAPPDATA%\LocalAIHub\models\`（§6.6）。

### 13.2 安装位置与用户数据分离

| 内容 | 位置 | 谁写 |
| --- | --- | --- |
| 桌面端程序 | `%LOCALAPPDATA%\Programs\LocalKB\` | LocalKB 安装器 |
| 桌面端状态（`state.db`、设置、对话历史） | `%LOCALAPPDATA%\LocalKB\` | LocalKB |
| Runtime 程序 | `%LOCALAPPDATA%\Programs\LocalAIHub\` | LocalAIHub 安装器（**已存在则不动**） |
| Runtime 数据（`hub.db`、日志、discovery） | `%LOCALAPPDATA%\LocalAIHub\` | Runtime |
| 模型 | `%LOCALAPPDATA%\LocalAIHub\models\` | 管理后台 |

**两个产品各管各的目录，互不写对方的文件**。这是 §3.1 那条边界在文件系统上的体现。

**卸载时保留用户数据**，明确询问"是否同时删除知识库与对话历史"。**默认不删 Runtime 的数据与模型**——那是 LocalAIHub 的资产，LocalKB 无权处置；若用户同时想清掉，提供"打开管理后台"的入口或提示用 LocalAIHub 的卸载程序。

### 13.3 首次启动引导

```
1. 探测 Runtime（client.live()）
   ├─ 通 → 继续
   └─ 不通 → 调 localai-cli-installer.exe start，等 20s；
             仍失败 → 显示日志路径（%LOCALAPPDATA%\LocalAIHub\runtime.log）与"打开管理后台"
2. 读 /api/v1/capability：显示能力等级与原因
3. 检查模型可用性（只读，不下载）
   ├─ 无可用 RAG 模型 → "请到管理后台下载并加载模型" [打开管理后台]
   ├─ 模型未下载     → 同上
   └─ 模型已就绪     → 继续
4. 检查 Embedding（只读）
   └─ 未配置 → 提示"当前只能关键词检索，语义检索需在管理后台配置"
5. 让用户选第一个扫描目录 + 显示默认排除项（密码库/浏览器/聊天记录）
```

**没有模型下载进度条、没有镜像选择、没有"从本地导入模型"**——这些都是管理后台的职责（§6.6）。

### 13.4 API 契约版本（不锁 Runtime 版本）

桌面端**不锁 Runtime 的构建版本**，只要求 API 契约满足下限：

```json
// install-manifest.json
{
  "product": "LocalKB",
  "version": "0.1.0",
  "sdk": { "requires": ">=0.3.0,<0.4.0" },
  "runtime_contract": { "min": 2 }
}
```

> SDK 下限从 `0.2.0` 提到 `0.3.0`：`resources()` 与 `schema_version` 是
> 0.3.0 新增的公开接口，§2.3 的状态栏与 §10.4 的资源让路都直接依赖它们。
> 写 `>=0.2.0` 会让装了 0.2.x 的桌面端在运行期得到 `AttributeError`——
> 那比"启动时提示版本不匹配"晚得多，也更难归因。

| 检查项 | 时机 | 不满足时 |
| --- | --- | --- |
| SDK 版本 | 启动 | 提示"桌面端与 SDK 版本不匹配，请重新安装" |
| 契约版本（≥ 2，即含溯源四列与 `documents/chunks`） | 首次连接 | 禁用依赖新字段的功能（页码、标题路径、`all` 查询）；`< 2` 时无法导入，只读浏览 |
| 探测方式 | 启动一次 | 读 `capability().schema_version`（或 `health().schema_version`，同一值）。`0` 表示旧版 Runtime 没有该字段，按 §17.1 降级 |

**为什么不锁 Runtime 版本**：用户升级 LocalAIHub 是正常运维。硬锁会让"升级服务端"变成"桌面端用不了"，而正确做法是**用能力探测决定功能开关**。`docs/修改记录/` 里那次"运行过期二进制导致 404"的教训，在锁版本方案里会以另一种形式复现——用户看到的是"装完了但不能用"，且无法通过升级服务端解决。

**`ingest_chunks` 是硬门槛**：契约版本 < 2 时无法导入任何文档，此时桌面端应明确进入"只读浏览已有知识库"模式，而不是让每次扫描都 404。

### 13.5 代码签名与许可

`tools/package-localkb.ps1` 有 `-SignSubject` 参数（按证书主题匹配，不写死指纹）。桌面端 exe 与安装器一起签。

**签名顺序是有讲究的**（第一版弄错过）：裁剪 → 签 exe → 冒烟 → 清单 → 编译 Setup.exe → 签 Setup.exe。
如果先编 Setup.exe 再签 exe，包里装的是未签名的 exe；而清单里的 `entrySha256`
记的是签名前的哈希，与实际文件对不上。两处都错且都不报错。

**Setup.exe 必须单独签**：用户双击的是它。未签名的 Setup.exe 会被 SmartScreen
直接拦下，里面那个已签名的 LocalKB.exe 根本没机会被看到。

未签名时 Windows SmartScreen 首次运行会拦，用户需点"更多信息 → 仍要运行"。v0.1 内测可接受，公开发布前必须解决。

#### 安装向导（Inno Setup 6）

产物 `output/LocalKB-<ver>-Setup.exe`（46.9 MB，比 zip 小 19 MB，lzma2/max 优于 deflate）。

| 设置 | 值 | 原因 |
| --- | --- | --- |
| `PrivilegesRequired` | `lowest`（每用户安装） | 与 ADR 0002 的每用户实例一致；办公机用户通常没有提权 |
| `DefaultDirName` | `{localappdata}\Programs\LocalKB` | 不需要管理员，卸载项进 HKCU |
| `AppId` | 固定 GUID | 一旦发布不能改，否则新版本被当成另一个软件（并存 + 两个卸载项） |
| 语言 | `tools/ChineseSimplified.isl` | **Inno 6 官方不自带简体中文**，写 `compiler:Languages\...` 会编译失败 |
| 文件编码 | UTF-8 **带 BOM** | Inno 只在有 BOM 时按 UTF-8 解析，否则按 ANSI → 中文界面乱码，而**编译不报错** |

**安装包不含 Runtime**（§3.1）。向导只做三件事：装本体、读注册表检测
LocalAIHub 并在说明页告知、注册卸载项与开始菜单（含"自检"入口）。

**卸载语义**：交互式卸载询问"是否同时删除本地数据"（默认不删）；
**静默卸载一律不删**。

这一条踩过两层坑，都值得记：

1. `/SUPPRESSMSGBOXES` 抑制消息框时，**需要选择的场景默认响应是 Yes**
   ——`MB_DEFBUTTON2` 只影响用户看得见按钮时的高亮位置，对抑制后的返回值
   无效。所以静默卸载会无条件删光 `state.db`。
2. 改用 `WizardSilent()` 判断时，它在卸载阶段**会抛异常**
   （`Cannot call "WizardSilent" function during Uninstall`）。数据"保留"了，
   但那是因为异常提前中断了流程——巧合而非正确。

正确函数是 `UninstallSilent()`（已用一次性探测脚本实测：`/VERYSILENT` 下返回
True 且无异常）。

**许可清单**（M1 前必须逐项确认，不只 PDF 库）：

| 依赖 | 许可 | 影响 |
| --- | --- | --- |
| PyMuPDF | AGPL-3.0 或商业 | 闭源分发需购买授权，或换 `pdfplumber`（§16 P2） |
| PySide6 / Qt | LGPLv3 或商业 | `--onedir` 动态链接通常满足 LGPL；改 Qt 源码或静态链接则需开源/买授权 |
| python-docx | MIT | 无 |
| openpyxl | MIT | 无 |
| python-pptx | MIT | 无 |
| watchdog | Apache-2.0 | 无 |
| luping-localai | 见 SDK 仓库 LICENSE | 无 |

---

## 14. 里程碑

**前置条件**：阶段 A（Runtime 改造）完成。M1 之后的所有工作都依赖新契约。

| 阶段 | 内容 | 验收 | 预估 |
| --- | --- | --- | --- |
| ~~**M0 骨架**~~ ✅ | 连接已有 Runtime（探活 + 唤醒 + `Client` 重建）、显示能力与资源；目录扫描（含 §7.5 跳过策略）+ `state.db` 基础表 | 扫描能列出文件数并给出跳过原因；**A17/A18 实测通过**；A15/A16 待 M5 | ~~4 天~~ **已完成**（`docs/修改记录/20261116-0073`） |
| ~~**M1 解析与切块**~~ ✅ | `parse/`（PDF/DOCX/XLSX/TXT/MD）+ `chunk/`（450/600/54 + 标题路径 + 页码）+ §5.8.2 的 6 类结构修复规则 | 切块单测通过；**A11/A12 实测通过** | ~~5 天~~ **已完成**（`docs/修改记录/20261116-0074`） |
| ~~**M2 扫描入库**~~ ✅ | `parse → chunk → ingest_chunks` 串联、进度、取消、向量化轮询、2000 块上限 | **A1 实测通过**（3 篇 296 块全流程）；A23 预检有测试 | ~~3 天~~ **已完成**（`docs/修改记录/20261116-0075`） |
| ~~**M3 提问溯源**~~ ✅ | 聊天页（含**知识库开关**）、检索页、来源卡片、文件跳转、降级提示、对话历史落库 | **A2 3/3、A3 3/3 实测通过**；A6/A21/A22/A25/A26 通过 | ~~4 天~~ **已完成**（`docs/修改记录/20261116-0076`） |
| ~~**M4 增量与删除**~~ ✅ | watchdog、防抖、**双向启动对账**、**重命名同步名字**、chunker 版本冲突重建、**删除对话框（含回收站）**、**missing 处理** | **A4/A5/A19/A20 实测通过**（7/7）；A13/A14 临时库验证通过 | ~~5 天~~ **已完成**（`docs/修改记录/20261116-0077`） |
| **M5 打包与标定**（✅ 完成） | PyInstaller、打包脚本、**Inno Setup 安装向导**、首次引导、契约版本检查；**内存极限测试并回填 §10** | 打包链路 ✅、安装/卸载 ✅（20 项自动验收，含 A15）、自检 ✅、版本门禁 ✅、内存标定 ✅；**A7/A8/A16 待目标机型** | 已完成约 4 天（`docs/修改记录/20261116-0079`、`0080`） |
| **M6 扩展格式** ✅ | PPTX 按页（标题占位符 + 表格）；OCR 决策落地（文案三层结构）；旧格式跳过提示 | 10 项 PPTX 测试；自检含 PPTX；见 `docs/修改记录/20261116-0081` | 已完成 |
| **M7 重复提醒** ✅ | §12 的 D1~D3（含默认选中策略设置项） | B1/B5/B7~B11 通过（19 项端到端）；**B6 不通过**（服务端阈值未校准，见 `docs/实测记录/20261116-相似阈值对模板文档过严.md`） | 已完成 |

**关键路径是 M1 → M2 → M3**。M1 是最大的不确定性（解析器质量 + 切块语义对齐 + 结构修复规则），必须最先做完并跑通 §11.4 的对照。

M0~M4 已完成（`docs/修改记录/20261116-0073` ~ `0077`）：连接、扫描、解析、切块、入库、向量化、提问、溯源、增量与删除全部打通，309 个测试通过。"选目录 → 扫描 → 提问 → 点来源跳回原文 → 改/删文件自动跟上"的完整产品链路可用。

**M5 完成**（`docs/修改记录/20261116-0079`、`0080`）：PyInstaller 打包链路（zip 65.9 MB / 解压 156.9 MB）、**Inno Setup 安装向导**（Setup.exe 46.9 MB）、`--self-check` 自检、SDK 与契约版本门禁、内存标定回填 §10、**20 项安装验收自动断言**（含 A15 同机复用 Runtime、静默卸载不删用户数据）。**剩余：A7/A8/A16 需要 8GB 目标机型与干净虚拟机。**

内存标定的关键结论见 §10.0 与 §10.6：**桌面端按 100 MB 预算**（原估 358 MB 偏高 3.5 倍）、**PyMuPDF 解析几乎不占内存**（500 页 +5 MB，原估偏高 50 倍）、**向量化 9.9 块/秒是唯一瓶颈**（10 万块实测 2.8 小时，因此首次建库必须分阶段可用）、**10 万块热检索 1704 ms / 冷启 8156 ms**（首次检索必须提示"正在载入索引"）。

**相比 v1.0 的工时增量**（+3 天，总计约 31 天）：

| 阶段 | 增量 | 原因 |
| --- | --- | --- |
| M1 | +1 | §5.8.2 的 6 类结构修复规则（页眉页脚、硬换行、多栏、目录页、标题启发式、跨页表格）——这些原本没有明确归属，实际必须做，否则 PDF 质量撑不住 §11.2 的页码门禁 |
| M3 | ±0 | 知识库开关与检索页只读说明不增加工时，但把"chat/search 语义不同"这条落到了界面上 |
| M4 | +1 | 删除对话框（两种语义 + 回收站 + 顺序保证）+ missing 处理（汇总提示、批量重绑） |
| — | +1 | §5.8 的结论要写成 ADR 并留证，避免以后重新讨论"要不要让模型改写原文" |

**M0 与 M4 相比 v1.0 各多出一天**：M0 增加"Runtime 归属与恢复"（A15~A18），M4 增加"双向对账 + 重命名同步 + 删除语义"（A19/A20/A27~A30）。这两块在 v1.0 里被低估了——它们不是边角功能，而是"用户能不能长期用下去"的分界线。

**M0 实际用了约 3 天**（含环境搭建），比预估的 4 天少：扫描器与存储层是纯 Python + pytest，反馈很快；真正的耗时在连接层的两个缺陷上（见修改记录 0073 §2 的 2.1 / 2.2），而这两个属于"一次修完就再也不回来"的类型。

**M1 实际用了约 2 天**（预估 5 天），差距主要来自"六类结构修复规则"的实现比预想简单——它们都是纯函数（输入 `_Line` 列表、输出重排结果），可以脱离真实 PDF 用构造数据测试。省下的时间被 9 个缺陷的排查吃掉了（见修改记录 0074 §3），其中 4 个是静默失效型（越界返回、动态属性、状态写错、异常被兜底吞掉），只有靠"断言到具体值"的测试才能发现。

**M2 实际用了约 1 天**（预估 3 天）。契约映射（本地 `Chunk` → SDK `ChunkInput`）比预想直接，`ingest_chunks` 的语义与 §7.1 的伪代码基本一致。真正花时间的是**发现语义检索根本不可用**（见修改记录 0075 §3）——这不是代码问题，是 `launch-spec.json` 缺 `-embedding-model`，而排查它需要先怀疑代码、再怀疑 SDK、最后才查到部署参数。

**M3 实际用了约 1.5 天**（预估 4 天）。聊天界面的骨架（气泡 + 滚动 + 输入）很直接，`QScrollArea + 竖排布局` 比 `QTextBrowser` 灵活得多（后者放不了可点按钮）。花时间的是三件"不试就发现不了"的事：`capability()` 说能对话但模型没加载（§3.1）、下拉框与实际选择不一致（§3.2）、SQLite 外键默认关闭导致删会话留孤儿（§3.3）。**这三条都是"不报错、只是不对"的类型**，靠截图与端到端跑才暴露出来。

**M4 实际用了约 1.5 天**（预估 5 天）。监听与对账的核心逻辑不难，但**"移除后又导回来"这个缺陷**花了最多时间——因为方案 §7.7 没有定义 `excluded` 状态，我按"删记录"实现，直到跑 A5 验收才发现删除是假的。这条值得单独记：**"删除成功"与"删除生效"是两件事**，前者看返回值，后者要重跑一次扫描。另外测试基础设施踩了一次空闲卸载的坑（见修改记录 0077 §5），它同时也是 S10 的现场证据。

---

## 15. 风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| **切块质量不如 Go 实现** | 检索质量退化且不易察觉 | M1 的切块单测覆盖 §5.1 每一条；发布前跑 §11.4 的对照，recall 偏差 > 10% 不发布 |
| **PDF 解析库的许可**（PyMuPDF 是 AGPL / 商业双许可） | **可能被迫开源整个桌面端，或买商业授权** | **M1 开始前必须定**，见 §16 P2；完整许可清单见 §13.5 |
| **页码在真实 PDF 上不准**（多栏、页眉页脚重复） | 溯源失效，产品核心价值丢失 | M1 用 ≥10 份真实中文 PDF 做标注集；偏差 > 5% 就退化为"段内定位"并诚实地只显示文件名 |
| 扫描件 PDF 占比高 | 大量"解析失败"，用户觉得不好用 | 失败原因必须具体（§5.6）；设置页提供"仅扫描文本型 PDF"；OCR 见 §16 P1 |
| **解析峰值内存与推理撞车** | 低配机 OOM | §10.4；**判断依据只来自服务端**（`resources()` / `ErrResourceLimited`），客户端不自己算 |
| 首扫 5000 篇耗时过长 | 用户中途放弃 | 分阶段可用：关键词检索在导入后**立即可用**，语义检索随后补齐；进度条显示"已可用 N 篇" |
| watchdog 在 Windows 上漏事件 | 增量失效 | 启动对账兜底（§7.2.2）；不依赖实时事件做正确性保证 |
| **Windows 扫描的现实障碍**（占用/长路径/云盘占位/网络盘） | 用户看到"跳过了 97 个文件"却不知道为什么 | §7.5 的跳过策略 + 每类可读原因；扫描前对超大目录提示 |
| **同机两份 Runtime 互相踩** | 端口/token/数据目录冲突，且现场无法远程排查 | §3.1：安装时检测已装则跳过、运行时只连不 spawn；A15~A18 专门验收 |
| **桌面端与 Runtime 版本漂移** | 新字段缺失导致功能静默失效 | 契约版本下限 + 能力探测决定功能开关（§13.4）；不锁构建版本 |
| **切块器版本冲突的补救做错** | 用户陷入"删了重导还是 409"的死循环 | §7.6：只能 `delete_kb` + 重建，并在 UI 上明说代价 |
| **管理后台删库导致的悬空 doc_id** | 增量更新报错、列表错乱 | M4 的双向启动对账（§7.2.2）；A19 验收 |
| PyInstaller 冻结后缺 DLL | 安装包在别的机器起不来 | 在**干净虚拟机**（无 Python、无 VC++ 运行库）验证；CI 加一次全新 Win11 安装测试 |
| 杀毒误报（未签名 exe） | 用户装不上 | 代码签名；文档说明；提交 Defender 误报 |
| 用户目录含 20 万文件（如整个 D 盘） | 扫描数小时 + 撞 10 万 chunk 上限 | 扫描前估算：文件数 > 2 万或总大小 > 2 GB 时提示"范围过大，建议缩小" |
| **服务端没有可用模型 / 未配置 Embedding** | 用户以为桌面端坏了 | §6.6 的四档文案 + "打开管理后台"；A21 验收 |
| **空闲卸载导致每次提问都冷启动** | 体验断崖 | §7.4；阈值改在服务端配置，桌面端只做"正在准备模型"与预热 |
| **相似检测误报**（模板文档） | 用户失去信任 | Runtime 侧阈值按长度分档、Excel 走结构化指纹；客户端提供"这不是重复"抑制；默认不预选（§12.2.1） |
| **旧版文件被误删** | 数据永久丢失 | 不做**批量**删除；单篇删除需用户在对话框显式勾选，且只移到回收站（`FOF_ALLOWUNDO`），可还原（§7.7）；Runtime 不执行文件操作 |
| **模型改写导致溯源失效** | 引用片段在原文里找不到，且用户无法察觉 | v0.1 不让模型改写任何入库内容（§5.8）；结构问题用确定性规则解决 |

---

## 16. 待定问题

| 编号 | 问题 | 需要谁决定 | 建议 |
| --- | --- | --- | --- |
| **P1** | 扫描件 PDF / 图片做不做 OCR | 产品 | **已落地（M6）**：v0.1 不做，拒绝文案按三层写清"是什么 → 为什么不做 → 怎么办"（§5.4）。做的话 PaddleOCR 占 200MB~1GB，低配机必须与 LLM 错开峰值，且 OCR 乱码时"检索到但内容是垃圾"比检索不到更伤信任。等真实用户反馈再定 |
| **P2** | **PDF 解析库选哪个**（许可问题） | **必须 M1 前定** | `PyMuPDF` 能力最强（block + 字号 + 坐标），但是 **AGPL-3.0 或商业授权**。若桌面端不分发源码，必须买商业许可。替代：`pdfplumber`（MIT，能拿 block 与字号，慢 3~5 倍）、`pypdf`（BSD，只有纯文本，**拿不到字号 → 做不了标题识别**）。建议：内部/开源发行用 PyMuPDF + AGPL 合规；商业闭源发行前买授权，或切 `pdfplumber` 并把标题识别降级为可选 |
| **P3** | 知识库切换是否支持"临时只用某几个目录" | 产品 | v0.1 用"多知识库"解决（建库时分好）；不做按目录动态过滤 |
| **P4** | macOS / Linux 何时做 | 产品 | v0.2 之后。Runtime 与 SDK 都已跨平台，卡点在 PySide6 打包与文件跳转的实现差异 |
| **P5** | 对话历史存哪 | 技术 | **已定案**：桌面端 `state.db` 的 `conversations` / `messages` 表（§9）。Runtime 明确不做会话持久化 |
| **P6** | 客户端解析器是否做成可插拔 | 技术 | v0.1 不做。先固化一套，等 OCR 需求明确再抽协议。Runtime 的入库契约已让"换解析器"不需要改服务端 |
| **P7** | 是否支持 PDF 里的表格提取（转结构化） | 产品 | v0.1 不做。文本流式提取已够检索用；结构化需要 `pdfplumber` 的 table API 或 `camelot`，误判率高，收益不明 |
| **P8** | 目录能否跨库共享 | 产品 | **已定案：可以**。`scan_roots` 用 `(kb_id, path)` 复合唯一（§9）。建库/加目录时若与已有库重叠，提示"这些文件会在新库里再存一份"（§12 B2 的预期行为） |
| **P9** | 相似组的默认选中 | 产品 | **已定案：默认不预选**，设置页可开 `newest` / `oldest`；无论哪种都要用户点确定（§12.2.1） |
| **P10** | `resources()` 是否进 SDK | 技术 | **已定案：补进 SDK**（两语言一致）。§6.7 给了 `ResourceStatus` 结构；桌面端在它到位前只显示 `capability()` 的静态结论，不写私有接口 |
| **P11** | Runtime 归属方案 | 架构 | **已定案**：复用已装 LocalAIHub，不装第二份（§3.1）。`localai-cli-installer.exe start` 经代码复核**已是幂等**（`startMain` 先用单实例锁判断，已在跑则只打印后台地址并返回 0，且只在确认无其他实例后才清理 discovery）。剩余待办：未装时随包分发 LocalAIHub 安装程序 |
| **P12** | `ragbench` / `test_live` 迁移到 `ingest_chunks` | 技术 | **已定案：放到后面测**——不在阶段 A 做，等文件管理功能完成、做端到端联调时一起迁。在此之前 §11.3/§11.4 的门禁标记为"待联调"，不作为发布判据 |
| **P13** | 是否让模型"提炼/分段"文档后再入库 | 产品 | **已定案：v0.1 不做**（§5.8）。三条硬阻碍：15.8 tok/s 下 100 篇要 3.3 小时、引擎并发为 1 会阻塞提问、改写后 `page_start` 指向的原文里找不到那段文字。结构问题改用 §5.8.2 的确定性规则；未来若做，只能"新增可识别的块、不替换原文、默认关闭"（§5.8.3） |
| **P14** | 删除的语义 | 产品 | **已定案**：文件与库记录是两个独立决定（§7.7）。默认只移出库、文件保留；勾选删文件则只移到回收站。库内允许存在没有实体文件的文档（`MISSING`） |

---

## 17. 服务端依赖（只引用，不在此维护）

本文档的实现依赖一批服务端改动（Runtime / SDK / 管理后台）。**这些需求不在本文档维护**，见：

> **`docs/服务端改造增补需求-桌面端配套.md`**

那一份是服务端唯一的待办清单，包含：每项的为什么、改哪个文件、验收用例、明确不做的理由、以及"桌面端哪一节在等它"。

**桌面端侧只需要知道两件事**：

1. **S1~S5 已全部落地**（2026-11-16，见服务端增补需求与修改记录 0072）：SDK 有 `resources()`、`name` 会落库、409 文案已改、契约版本可读、后台能删单篇。桌面端可以全功能开工。
2. **S6 / S7 留待联调阶段**（工具与 e2e 测试迁到 `ingest_chunks`），不作为发布判据。

§17.1 的降级方式**保留**：它同时是"连到旧版 Runtime 时怎么办"的答案——用户机器上可能装着升级前的 LocalAIHub，那种情况下桌面端不能崩，要按降级路径工作。

### 17.1 服务端未就绪时的降级（也适用于"连到旧版 Runtime"）

S1~S5 已落地，但用户机器上可能装着升级前的 LocalAIHub。下表既是"当年没做时怎么办"的记录，也是**今天连到旧版时的实际降级路径**。

| 服务端项 | 桌面端降级 |
| --- | --- |
| S1 `resources()` | 调用返回 404 → 状态栏只显示 `capability()` 的静态结论（能力等级与原因），不显示实时内存；"资源紧张让路"退化为"撞到 `ErrResourceLimited` 才暂停" |
| S2 `name` 落库 | 来源卡片文案改用本地 `local_files.path` 的文件名。与服务端返回的 `source` 不一致，但至少不显示旧名（§7.2.1 兜底） |
| S3 文案 | 不直接用 `e.message`，桌面端自己写"需要重建知识库（会重新解析全部文档）" |
| S4 契约版本 | `schema_version == 0`（字段缺失）→ 判定未知，按 §13.4 调一次 `list_documents()` 探测：404 → 只读浏览，200 → 全功能 |
| S5 后台删单篇 | 无影响，桌面端自己就能删（§7.7） |

**永远不要用 `rag_enabled` 推断端点存在性**——两者在实现上是两件事（§8.5）。这条在 S4 落地后依然成立：`rag_enabled` 说的是模型能力，`schema_version` 说的才是端点契约。

---

## 18. 与原外部方案的差异总览

| 项 | 外部方案 | 本方案 | 依据 |
| --- | --- | --- | --- |
| 向量库 | Qdrant 单机版 | Runtime 内 SQLite BLOB + 内存暴力扫描 | 10 万 chunk 实测 7ms vs 30 万 42ms；Qdrant 省下的 5ms 用户不可感知 |
| 文档解析 | Python | **Python（采纳）** | Runtime 侧覆盖率上限约 70%（无 OCR、无 PPT、标题层级丢失） |
| 切块 | Python | **Python（采纳）** | 与解析同处，页码/标题路径才能拿到 |
| 元数据 | Python 存 `files` + `chunks_meta` | Runtime 存已入库内容 + 客户端薄表存本地事实 | 字段集合互斥，不存两套文本/分块 |
| 接口 | 自定义 `/v1/ingest/batch`、`/v1/query` | Runtime 的 `/documents/chunks` + 现有端点 + 零依赖 SDK | 不重复造 |
| Runtime 归属 | 桌面端自带一份 Runtime 并管理其生命周期 | **复用已装 LocalAIHub 的唯一实例**，只连不 spawn | 单实例锁 + 一份 `launch-spec.json`，否则同机两套数据目录与 token（§3.1） |
| 模型管理 | 桌面端下载/加载模型 | **全部归 LocalAIHub 管理后台**，桌面端只读状态 + 引导 | 模型是服务端资产；两边同改一处状态必然打架（§6.6） |
| 8GB 模型 | 3B Q4（2.5GB 估算） | 1.5B Q4（1.6GB 实测） | 3B 实测 3.1 GiB |
| 16GB 7B | 可选 7B Q4 | **不做** | 实测 7.1~7.3 GiB，加载后余量不足 |
| 混合检索 | v0.2 才做 | **已有**（bigram FTS5 + 语义 + RRF） | 已实测并落地 |
| 来源定位 | payload 存 page/char_offset | 客户端入库时直接报 `page_start` / `heading_path` | 比"Runtime 反查页偏移"简单一个数量级 |
| 存储模式 | 未提及 | **概念取消**：Runtime 不再复制或引用文件 | 不碰文件就没有 Managed/External |
| 重复文件 | 清理工具（归档/移动/状态机） | **入库版本确认**（不碰文件） | 用户要的是"知识库别乱"，不是"帮我清硬盘" |
| 删除语义 | 未提及（隐含"删文档即删文件"） | 文件与库记录**两个独立决定**；删文件只到回收站 | 用户常想"从库里拿掉但文件留着"，也常有"文件被移走了但知识还想留着"（§7.7） |
| 入库内容 | 可选"AI 整理后再入库" | **原文入库，不做模型改写** | 15.8 tok/s 下 100 篇要 3.3 小时；改写后页码溯源失效且用户无法察觉（§5.8） |
| 依赖总量 | Qdrant + PyMuPDF + python-docx + openpyxl + python-pptx + PaddleOCR + watchdog | PySide6 + PyMuPDF + python-docx + openpyxl + watchdog + `luping-localai` | 少一个常驻服务；解析库是必须的 |

---

## 附：本文档的维护约定

遵循 `docs/adr/0008`：

- 本文档写**桌面端的设计意图**，不写实现进度。进度看 `docs/修改记录/`。
- **属于 Runtime / SDK / 管理后台的改动，一律写在 `docs/服务端改造增补需求-桌面端配套.md`**。本文档只写"服务端就绪后，客户端要做什么"，以及"服务端未就绪时怎么降级"（§17.1）。
- 解析下沉主干的契约（入库端点、溯源字段、chunker 版本）见 `docs/runtime改造方案-知识库解析下沉.md`，本文档只引用。
- §12.4 的指纹算法细节在 Runtime 文档里，本文档只引用。
- **§10 不写具体内存数字**。数字来自 `docs/实测记录/` 的极限测试，测试做了才回填；在此之前不要引用任何 GiB 估值。
- §16 P2（PDF 解析库许可）必须在 M1 开工前确定，它会改变工作量与功能范围。
- 任何"让本文档某个结论失效"的改动，必须建修改记录。

---

## 附：变更清单

### v1.3（服务端需求抽离）

**本次只做文档结构调整，不改设计**：

| 变化 | 说明 |
| --- | --- |
| 新建 `docs/服务端改造增补需求-桌面端配套.md` | 把原本散在本文档 §17、§6.7、§7.2.1、§7.6、§13.4、§7.7.3 里的"服务端要改什么"集中成一份独立需求，含 S1~S7 / N1~N5 / T1~T3 与完整验收用例 |
| §17 重写为"只引用，不在此维护" | 从"10 条待办表"压缩为：指向增补文档 + 两张小表（硬阻塞项、未就绪时的降级） |
| §6.7 / §7.2.1 / §7.6 / §8.3 / §13.4 / §7.7.3 | 删除服务端实现细节（改哪个文件、改哪行代码），改为"现象 + 已提服务端待办编号" |
| 头部 | 前置依赖从一份变两份；补一句"服务端要改什么不在这里展开" |
| v1.2 的六项决策 | 第 1~3 项与第 6 项的位置列改为指向增补文档编号 |

**为什么值得单独拆一份**：桌面端方案已经 1900 行，其中约 1/6 在讲 Runtime 怎么改。这违反 ADR 0008 的分层约定（"属于 Runtime 与 SDK 的改动不写在这里"），也让"服务端改好了没有"这件事没有唯一清单可勾。拆出去之后，服务端可以独立推进、独立验收，桌面端等它就绪再继续。

> **后续（2026-11-16）**：S1~S5 与 T1/T3 已全部落地，见增补需求文档的勾选表与修改记录 0072。S6/S7 仍待联调。上方 v1.2 / v1.1 的历史表格保留原样——它们记录的是"当时为什么这么定"，不回填结果。

### v1.2（六个决策定案）

| # | 决策 | 位置 |
| --- | --- | --- |
| 1 | **`resources()` 补进 SDK**（两语言一致），桌面端不写私有接口 | §6.7 / P10 / 服务端待办 S1 |
| 2 | **`ragbench` / `test_live` 的迁移放到后面**，与文件管理完成后的端到端联调一起做；在此之前不作为发布判据 | P12 / §11.3 / §11.4 / 服务端待办 S6、S7 |
| 3 | **Runtime 补 `name` 落库**，否则重命名永远不生效 | §7.2.1 / 服务端待办 S2 |
| 4 | **删除是二维语义**：文件与库记录各自独立决定；默认只移出库；勾选删文件则只到回收站；库内允许 `MISSING` 文档 | §7.7 / P14 / A27~A30 |
| 5 | **chat 可勾选知识库、search 必须查库**，两入口语义不统一是刻意的 | §8.1.1 / A25/A26 |
| 6 | **不做"模型提炼分段后再入库"**：三条硬阻碍（速度、独占引擎、溯源失效）；结构问题用确定性规则解决 | §5.8 / P13 / 服务端待办 T1 |

同时落地的界面与模块变化：对话页加知识库开关、新增检索页、知识库页加 missing 汇总、新增 `delete_dialog.py` / `search_page.py` / `platform/recycle.py`、`SearchWorker` 线程。

工时影响：M1 +1 天（结构修复规则）、M3 ±0（新增两个页面元素）、M4 +1 天（删除语义 + missing）、另 +1 天写 ADR，总计约 31 天。

### v1.1（按代码实现复核后修正）

v1.1 是**按代码实现复核后修正**的版本。变更分三类：修正错误结论、补上缺失边界、删掉不该由桌面端做的事。

### 一、修正错误结论（这些是 v1.0 会做错的地方）

| # | 章节 | v1.0 的问题 | v1.1 的写法 |
| --- | --- | --- | --- |
| 1 | §3.1（原） | 桌面端自己 spawn Runtime 并复用 `%LOCALAPPDATA%\LocalAIHub`，与已装 LocalAIHub 争单实例锁 | 复用唯一实例，只连不 spawn；安装时检测已装则跳过（§3.1.1） |
| 2 | §4/§6.6（原） | 桌面端调 `localai-cli models download` 下载模型、调 `load_model()` 加载 | 模型全归管理后台，桌面端只读状态 + 引导（§6.6） |
| 3 | §7.2（原） | 重命名"只更新 local_files 的 path"，导致来源卡片永远显示旧名 | 必须同时 `ingest_chunks(name=新名)`；并指出 Runtime 侧 `name` 其实没落库，需先修（§7.2.1 / 服务端待办 S2） |
| 4 | §6.4/§12.6（原） | 切块器版本冲突时"逐篇删除重导"——Runtime 的知识库级索引配置在库内已有记录时只比对不覆盖，重导永远 409 | 只能 `delete_kb` + 重建，并明说代价（§7.6） |
| 5 | §8.5（原） | 低内存时"仅关键词检索仍可用"——实际知识库端点根本没注册（404） | 按"子系统是否存在"重写，明确不提供关键词退路（§8.5） |
| 6 | §10.4（原） | 解析前查 `capability()` 判断内存——那是启动快照，不是实时值 | 实时值只来自 `resources()`；客户端不自己算（§6.7/§10.4） |
| 7 | §10.6（原） | "单文件 50 MB"——Runtime 单次 2000 块上限，且分批提交会删掉前一批块 | 改为单文档 2000 块硬约束（§5.7） |
| 8 | §11.3/§11.4（原） | 门禁依赖 `ragbench` / `test_live.py`，而两者都走已被隐藏的 `POST /documents` | 标为"待联调"，与文件管理完成后的端到端测试一起迁移（P12），在此之前不作为发布判据 |
| 9 | §6.4/§7.1/§12.4（原） | SDK 调用写成关键字参数，实际是"位置参数 + 请求对象" | 全部按真实签名改写 |
| 10 | §13.3（原） | "检查 Embedding 模型，132 MB 同上"——registry 里没有 e5 清单，`models download` 会直接拒绝 | 删除桌面端下载路径；模型资产归服务端 |
| 11 | §13.1（原） | 安装包"约 140 MB"与构成明细（110 + 21 + 61 = 192 MB）矛盾 | 不预估体积，以 M5 产物为准 |
| 12 | §8.3（原） | "TXT/MD 滚到 `start_rune`"——该字段根本没透出 | 改为"搜片段文本"定位，并说明为什么不透出 `start_rune`（§8.3） |
| 13 | §8.1（原） | chat 与 search 的 `kb_ids` 语义不同，v1.0 只写了 chat 侧 | 明确 chat 可勾选/可不查、search 必须查（§8.1.1） |

### 二、补上缺失的边界（v1.0 没写，用户会撞到）

| # | 内容 | 位置 |
| --- | --- | --- |
| 14 | Runtime 不可用时的唤醒与恢复、`Client` 必须可重建、陈旧 discovery 的处理 | §3.1.1 / §3.1.2 |
| 15 | `local_files` ↔ `list_documents()` 双向对账（管理后台可删库） | §7.2.2 |
| 16 | Windows 扫描的 7 类现实障碍与跳过原因 | §7.5 |
| 17 | 单文档 2000 块上限与"不静默截断" | §5.7 |
| 18 | 对话历史表 `conversations` / `messages`（含 sources 快照、kb_mode） | §9 |
| 19 | 目录跨库共享（`unique(kb_id, path)`）与重叠提示 | §9 / P8 |
| 20 | 相似组默认选中策略（默认不预选，可配） | §12.2.1 / P9 |
| 21 | 契约版本下限（不锁构建版本）+ 许可清单（含 PySide6） | §13.4 / §13.5 |
| 22 | **删除的两维语义**：文件与库记录独立决定；删文件只到回收站；库内允许 `MISSING` 文档 | §7.7 / P14 |
| 23 | **"AI 提炼后再入库"的结论**：三条硬阻碍 + 结构问题改用确定性规则 | §5.8 / P13 |
| 24 | **知识库开关**：chat 可勾选、search 必填，两者不统一 | §8.1.1 |
| 25 | **`resources()` 进 SDK** 的决策与 `ResourceStatus` 结构 | §6.7 / P10 |
| 26 | 新增验收 A15~A30（同机共存、恢复、对账、重命名、模型缺失、降级、上限、跳过、开关、删除、MISSING） | §11.1 |
| 27 | 新增验收 B9~B11（默认不预选、可配预选、重复组不提供删除动作） | §12.10 |

### 三、删掉的（不再由桌面端负责）

| 内容 | 原因 |
| --- | --- |
| `runtime_supervisor.py`（spawn/wait/stop） | 桌面端不管理 Runtime 进程（§4） |
| 模型下载、镜像选择、进度条、`models import` | 归管理后台（§6.6） |
| `load_model()` / `unload_model()` 调用 | 同上；两边同时改同一状态必然打架 |
| `-idle-unload-sec` / `-user-idle-sec` 启动参数设置 | 归 `launch-spec.json` 与管理后台配置（§7.4） |
| §10 的具体 GiB 预算表（4.70 / 6.80 GiB、"< 5.5 GiB"） | 无实测依据，改为极限测试后回填（§10.1） |
| "104 个单测通过"这类具体数字 | 会过期，且不属于设计意图 |
| ~~"不删、不移、不归档用户文件"~~（改写为限定范围） | 原表述绝对化，会让"用户明确想删掉一个误导入的文件"变成做不到的事。改为：重复检测不提供任何删除动作（§12.1）；单篇删除需用户显式勾选且只到回收站（§7.7） |
| "`start_rune` 精确滚动" | 字段未透出，且与原文偏移不一致，改为搜片段（§8.3） |
