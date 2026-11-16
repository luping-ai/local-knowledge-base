# LocalKB M6：PPTX 按页解析与 OCR 决策落地

- **日期**：2026-11-16
- **类型**：扩展格式支持 + 产品决策落地
- **状态**：**完成**
- **关联**：`docs/基于local-ai-runtime的个人本地知识库桌面端项目方案.md` §5.4 / §16 P1、`docs/修改记录/20261116-0080`（M5）

---

## 1. 交付了什么

| 模块 | 文件 | 内容 |
| --- | --- | --- |
| PPTX 解析器 | `localkb/parse/pptx.py` | 按页一块、标题占位符、表格内容、异常分类 |
| 注册 | `localkb/parse/registry.py` | 加 `.pptx` 分发 |
| 旧格式提示 | `localkb/scan/walker.py` | `.doc/.xls/.ppt/.wps/.et/.dps` 报"跳过"并给出转换指引 |
| OCR 文案 | `localkb/parse/pdf.py` | `SCANNED_PDF_REASON` 三层结构（是什么/为什么不做/怎么办） |
| 自检 | `localkb/app.py` | `--self-check` 加 PPTX 项 |
| 打包 | `tools/localkb.spec` | **修掉 excludes 里的 `PIL`**（见 §3） |
| 依赖 | `pyproject.toml` | 补全解析库（含 `python-pptx`） |
| 测试 | `tests/test_parse_pptx.py` | 10 项 |
| 测试 | `tests/test_walker.py` | +4 项（旧格式 3 + pptx 支持 1） |

**测试：348 个通过**（334 → 348：+10 PPTX、+4 walker）。

---

## 2. PPTX 的四个设计决定

### 2.1 一页一块，不跨页合并

页码是溯源核心。切块器的 `cross_page` 规则（不同页的短句各自成块）正好
保证 PPT 不会被合并——PPT 每页通常只有 50~300 字，若不拦就会被拼成一块，
来源卡片只能显示"第 1~5 页"。

这个规则本来是 PDF 的需求，在 PPTX 上同样有效，**没有为此新增代码**。

### 2.2 标题用 `placeholder_format.type` 而不是形状名

```python
pf.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.SUBTITLE)
```

`shape.name` 是"Title 1"这类**可以随便改**的标签，中文版存的是"标题 1"。
`placeholder_format.type` 是 OOXML 枚举，与界面语言无关。

方案 §5.2 把 PPTX 的标题可靠性标为"高"，正是因为可以读结构而不是猜字号。

### 2.3 表格必须单独读 `shape.table`

这是本轮最容易漏的一处：`shape.text_frame` 对表格（GraphicFrame）
**不存在**。只读 text_frame 会让 PPT 里的表格内容**整片丢失且不报错**。

我专门验证了这个测试的有效性：临时把表格分支改成 `if False:`，
`test_table_content_is_extracted` 立刻失败，错误信息精确指出
「表格单元格「公司」丢失，实际：'数据页'」。

### 2.4 形状按 z-order（`slide.shapes` 原序）

python-pptx 没有"阅读顺序"概念。`slide.shapes` 的顺序是绘制顺序
（从底到顶），与视觉顺序大体一致。不排序会让标题落到正文后面，
标题路径就拼错了。

---

## 3. 一个真实的打包缺陷：excludes 里的 `PIL`

`tools/localkb.spec` 原本写着：

```python
excludes = [..., "PIL", "cv2", "IPython", ...]   # "本项目用不到的重型库"
```

看起来合理——项目确实不做图像处理。但 **python-pptx 依赖 Pillow**：

```
$ python -c "import pptx, sys; print('PIL' in sys.modules)"   →  True
$ python -c "import importlib.metadata as md; print(md.requires('python-pptx'))"
Pillow >=3.3.2
XlsxWriter >=0.5.7
lxml >=3.1.0
typing-extensions >=4.9.0
```

`import pptx` 会经 `pptx.parts` 立即加载 `PIL.Image`（用于判断嵌入图片格式）。

**后果**：打包后**只有 PPTX 解析会崩**（`ModuleNotFoundError: PIL`），
而开发环境装着 Pillow、单测全绿、其他格式都正常。这是典型的
"单测通过但产品是坏的"。

修法两条：

1. 从 excludes 移除 `PIL`（并注明原因，避免下次又"优化"掉）
2. **`--self-check` 加 PPTX 项**——自检要真造一个 pptx 并解析，
   不能只 `import pptx`（那样在缺 lxml 时就炸，在缺 PIL 时反而可能过）

第 2 条是更重要的部分：它把"打包后才发现的缺陷"变成"自检能发现的缺陷"。

---

## 4. OCR 决策的实际落地

方案 §16 P1 的结论是"v0.1 不做 OCR"。但"不做"如果只写在一句
"当前版本不支持 OCR"里，用户会反复重试或以为软件坏了。

所以文案按三层组织（`localkb/parse/pdf.py` 的 `SCANNED_PDF_REASON`）：

| 层 | 内容 |
| --- | --- |
| 是什么 | 整份 PDF 没有文字层，内容都是图片 |
| **为什么不做** | OCR 引擎多占 200MB~1GB 内存，低配机会挤掉对话模型；识别出错时"检索到了但内容是乱码"比检索不到更难排查 |
| 怎么办 | ① Acrobat/WPS 识别文字 ② 手机扫描 App 导出文本 ③ 导入原件对应的 Word/PPT |

第 2 层是刻意写给用户看的。实测经验：**用户看不到原因时会反复重试**，
而重试一百次结果都一样。

同一结构用在三个相邻场景，措辞保持一致（用户可能同时遇到多种）：

| 场景 | 异常 | 位置 |
| --- | --- | --- |
| 扫描件 PDF（无文字层） | `UnsupportedError` | `pdf.py: SCANNED_PDF_REASON` |
| PDF 只有零星字符 | `UnsupportedError` | `pdf.py`（兜底分支） |
| 纯图片 PPT | `UnsupportedError` | `pptx.py` |
| 旧格式 `.doc/.xls/.ppt` | `SkipReason.LEGACY_FORMAT` | `walker.py` |

### 4.1 为什么旧格式要报"跳过"而不是静默忽略

其他不支持的扩展名（`.mp4`/`.zip`/`.exe`）静默忽略是对的：用户选 D 盘会
看到几万个"不支持 mp4"，那是噪音。

但 `.doc` / `.xls` / `.ppt` 不一样：它们是**用户真正想导入的文档**，
而且改个格式就能用。静默忽略的后果是用户扫完看到"新增 0 篇"，
完全不知道为什么自己的 200 个 `.doc` 一个都没进去。

**这类"什么都没发生"的故障最难自查**——没有报错、没有日志、界面上
一切正常，就是没内容。

---

## 5. 验证

### 5.1 PPTX 测试覆盖（10 项）

| 用例 | 保护什么 |
| --- | --- |
| 每页一块 + 页码 1/2/3 | 页码精度 |
| 标题 → HEADING + 正文带 heading | 标题路径 |
| parser 名与 total_chars | 元数据 |
| **表格内容被提取** | 最易静默丢内容（`text_frame` 对表格不存在） |
| 表格按行保留换行 | 表格语义 |
| 纯图片 PPT → `UnsupportedError` | 不是"损坏" |
| 损坏文件 → `ParseError` | 与上一条区分 |
| `.ppt` → 提示另存为 `.pptx` | 旧格式 |
| 空白页跳过 + 警告 | 不静默丢页号 |
| **3 页短内容不被合并** | 与切块器的配合（页码精度） |

### 5.2 测试有效性验证

对"表格内容"这条做了反向验证：临时改成 `if False:`（等价于不处理表格），
测试立刻失败：

```
AssertionError: 表格单元格「公司」丢失，实际：'数据页'
```

恢复后全绿。**一个抓不住 bug 的测试等于没写。**

### 5.3 自检输出（**冻结版**实测）

```
[OK  ] PPTX 解析  2 块（含标题，页码 1）
```

**这一条是本轮最关键的验证。** 打包后 `LocalKB.exe --self-check` 显示
PPTX 项 OK，说明 §3 的 PIL 修复在冻结环境下确实生效。如果没修，这里会是：

```
[FAIL] PPTX 解析  ModuleNotFoundError: No module named 'PIL'
```

注意自检输出里的 `冻结 True` —— 这证明测的是打包产物，不是开发环境。

`--self-check` 现在 9 项，全部通过。

### 5.3.1 打包与安装验收

| 项 | 结果 |
| --- | --- |
| 解压体积 | 156.9 → **171.4 MB**（+14.5 MB） |
| 安装包 | 46.9 → **51.6 MB**（+4.7 MB） |
| 安装验收 | **20/20 通过** |

增加的 14.5 MB 是 Pillow + XlsxWriter（python-pptx 的依赖）。
**这是必要成本**：省掉它们会让 PPTX 在打包后不可用，而单测发现不了。

### 5.4 端到端验证（`tools/localkb_m6_verify.py`，15 项）

单测证明"解析器能读 pptx"，但产品链路是
`parse → split → ingest_chunks → 向量化 → search`。任何一环对 `page`
语义处理不一致，都会让"来源卡片跳错页"——而这只在真实入库后检索才能发现
（`SearchHit` 的页码来自数据库，不是解析器的内存对象）。

实测结果：

```
[1/4] 解析    8 块，页码 [1,2,3,4]，4 个标题，表格在
[2/4] 切块    4 块，页码 [1,2,3,4]，每块含标题路径
[3/4] 入库    4 块，库内一致
[4/4] 检索    top-1 = 第 3 页（财务目标），内容含"营收 1200 万"
              完整文本：[财务目标] 财务目标 营收 1200 万 毛利率 62% 季度 营收 Q1 ...
```

注意最后一行：**标题路径 `[财务目标]` 被正确拼到块前面** ——这正是
§5.8.2 说的"标题粘在正文前面"，否则问"营收目标"时正文块里没有
"财务目标"这个词。

### 5.5 两次"断言太宽松"的自我修正

写这些验证时，我自己犯了两次同类错误——都是**断言恒真，等于没验**：

| 位置 | 错误写法 | 为什么无效 | 修正 |
| --- | --- | --- | --- |
| 端到端检索 | `3 in pages_hit` | 4 块的库用 top_k=5 会返回全部 4 页，恒真 | 断言 **top-1** 的页码与内容 |
| 安装验收 | `level=full` | `level` 取决于当前可用内存（标定跑完就降级为 `rule_only`） | 只断言"报告了等级"，并打印实际值 |

两次的共性：**把"我希望的状态"当成"必然成立的状态"**。前者是样本量
误判（忘了 top_k 会返回全部），后者是环境依赖（忘了内存会变）。

修正后：
- 端到端 → `top-1 是第 3 页（分数 0.033）`，这才验到排序质量
- 安装验收 → `[PASS] 自检：报告了能力等级 -> level=rule_only`，
  并附一句"通常是内存不足导致的降级，不是缺陷"

---

## 6. 与方案文档的偏差

| 方案原写 | 实际 | 原因 |
| --- | --- | --- |
| PPTX "v0.1.1 再做" | **M6 就做了** | 提前做的好处是能在打包链路稳定前引入（连带发现 PIL 被裁的问题） |
| 扫描件文案只有一句 | **三层结构**（是什么/为什么/怎么办） | 只写"不支持 OCR"会让用户反复重试 |
| — | **新增旧格式跳过提示** | 方案没提，但静默忽略 `.doc` 是真实痛点 |

---

## 7. 顺带完成：10 万块规模标定

M6 开始前，上一轮的 10 万块标定已跑完（**10084 秒 / 2.8 小时**）。
结果写进 `docs/实测记录/20261116-规模标定10万块.md`，并回填方案 §10.5/§10.6：

| 指标 | 值 |
| --- | --- |
| 向量化 10 万块 | 10084 s，**9.9 块/秒** |
| Runtime 内存（向量） | **+125 MB** |
| **热检索** | **1704 ms** |
| **冷启（首查）** | **8156 ms** |

方案里两处数字被修正：

- 向量化速度 11.6 → **9.9 块/秒**（11.6 来自 500 块的小样本，偏乐观）
- 建库耗时 2.4 小时 → **2.8 小时**

新增一条产品约束：**首次检索的 8 秒必须单独提示"正在载入索引"**。
它不是"检索慢"，而是索引载入；不解释的话用户会得出"这软件很卡"的
结论，而实际后续都是 1.7 秒。

---

## 8. 顺带修掉的开发环境问题

装 `python-pptx` 时踩到两个会让**新同事照 README 做就直接失败**的问题。
两者都不是产品缺陷，但都属"文档说能跑、实际跑不起来"。

### 8.1 README 的 pip install 清单漏了 6 个依赖

原文逐条列：

```powershell
pip install "PySide6>=6.6"
pip install ..\sdk\...\luping_localai-0.3.0-py3-none-any.whl
pip install pytest ruff
```

漏了 **PyMuPDF / python-docx / openpyxl / python-pptx / watchdog /
pytest-timeout**。照着做会在 import 阶段到处失败，而错误信息指向的是
"缺某个模块"，不是"README 过期了"——排查方向完全错。

改成用 `pyproject.toml` 作为唯一依赖来源：

```powershell
pip install -e ".[dev]"
```

### 8.2 `pip install -e .` 在仓库根会直接失败

改完先试了一遍，报错：

```
error: Multiple top-level packages discovered in a flat-layout:
       ['localkb', 'tests', 'tools', 'docs', 'cmd', ...]
ERROR: Failed to build 'file:///D:/codex/local-ai-runtime'
```

仓库根有多个顶级目录，setuptools 的自动发现无法判断哪个是包。
必须显式声明：

```toml
[tool.setuptools.packages.find]
include = ["localkb*"]
namespaces = false
```

**这里我自己也犯了一次错**：同时写了 `packages = ["localkb"]` 和
`packages.find`，TOML 报 `Cannot declare ('tool','setuptools','packages',
'find') twice`。两者只能取一个。

教训：**改完文档必须真的执行一遍那条命令**。如果只是"看起来对"就写进
README，等于把问题传给下一个人。

### 8.3 `.gitignore` 漏了 LocalKB 的产物目录

`.venv/`、`dist-localkb/`（171 MB）、`build-localkb/`、`localkb.egg-info/`
都在 git status 里显示为未跟踪。补进 `.gitignore` 后干净了。

### 8.4 国内网络装包卡住

默认 PyPI 源装 `python-pptx` 卡了 4 分钟以上（下载中断 + 反复重试），
换清华镜像 30 秒完成。已在 README 补上镜像用法。

---

## 9. 下一步

| 项 | 状态 |
| --- | --- |
| M6 PPTX + OCR 决策 | ✅ 完成 |
| 开发环境（README / pyproject / gitignore） | ✅ 修正并实测 |
| 安装包重建 | ✅ 51.6 MB，冻结版自检 9/9 |
| **M7 重复提醒**（D1~D3） | 未开始 |
| 10 万块通道拆解 | 需重跑（本次库未 `--keep`，已清理） |
| A16 / A8 / 8 GB 机型 | 需目标机器 |
