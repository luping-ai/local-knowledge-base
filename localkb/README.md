# `localkb` 包

LocalKB 的源代码。安装、测试、打包、诊断都见**仓库根的 `README.md`** ——
这里只讲这个包内部的分层，以及其他地方不容易看出的设计取舍。

## 分层

```
app.py              入口（单实例锁、state.db、--self-check）
  ↓
ui/                 界面层：只做装配与信号协调，不含业务逻辑
  ↓
scan/ ingest.py     编排层：遍历 → 解析 → 切块 → 入库 → 向量化
chat.py dedup.py
maintain.py
  ↓
parse/ chunk/       纯函数层：输入文件/文本，输出结构化块。不碰 Qt、不碰网络
  ↓
runtime_conn.py     与 Runtime 的唯一出口（探活 / 唤醒 / Client 重建）
store/db.py         state.db（本地事实）
platform/           操作系统差异（回收站、打开文件）
```

**依赖方向只能向下。** 反向依赖（比如 `parse/` 里 import `ui/`）会让
纯函数层失去"可以脱离 Qt 测试"的性质——而 381 项测试里有 300 项正是
靠这一点跑得飞快。

## 三个分层决定，以及为什么这么分

### 1. 为什么 `runtime_conn.py` 单独一层，而不是各处直接用 SDK 的 `Client`

因为**唤醒之后必须重建 Client**。Runtime 停止时 discover 回落到默认端口；
唤醒后拿到随机端口，旧 client 记住死端口，于是永远连不上。

把这件事收在一个地方，才不会有人图省事直接 `Client()`。回归测试在
`tests/test_runtime_conn.py`。

### 2. 为什么 `ScanJob` 不继承 `QThread`

`ScanJob` 是纯逻辑（可以用 pytest 同步跑），`ScanWorker` 只是把它搬到线程里。
分开的收益：45 个扫描测试不需要 `QApplication`，跑得快、也不受 Qt 事件循环
影响。

代价是多一层包装，但这个包装本身很薄（`localkb/ui/workers.py` 约 300 行，
含对话、扫描、通用调用三种）。

### 3. 为什么块文本与向量**不在** `state.db`

`state.db` 只存"磁盘上有什么、导过没有、用户在等什么确认"。
块文本与向量在 Runtime 的 `hub.db` 里。

重复存一份必然有一份是旧的，而"哪份是旧的"会在下一次提问时以
"引用了不存在的内容"暴露出来。这条边界是硬约束，不是优化。

## 想改某个功能时先看哪

| 想改 | 看这里 |
| --- | --- |
| 解析某种格式 / 加一种格式 | `parse/registry.py` 注册 + 新文件，参考 `parse/pptx.py` |
| 切块大小 / 边界规则 | `chunk/splitter.py`（`ChunkProfile`） |
| 扫描跳过哪些目录/文件 | `scan/walker.py`（`DEFAULT_EXCLUDED_DIRS` / `SkipReason`） |
| 入库失败怎么分类与提示 | `ingest.py`（`classify_error`） |
| 提问时的降级与溯源 | `chat.py` |
| 删除/改名的语义 | `maintain.py` |
| 重复与相似 | `scan/job.py`（完全重复）+ `dedup.py`（相似） |
| 界面 | `ui/`，页面之间不直接通信，都经 `main_window.py` 转 |
