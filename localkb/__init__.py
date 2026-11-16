"""LocalKB —— 基于 local-ai-runtime 的个人本地知识库桌面端。

## 模块边界（详见 docs/基于local-ai-runtime的个人本地知识库桌面端项目方案.md）

    桌面端负责：目录扫描、文件监听、文档解析、切块、界面、来源卡片、系统集成
    Runtime 负责：向量化、向量存储、关键词索引、RRF、RAG 拼接、模型调度

两者之间只走 `luping-localai` SDK。

## 为什么这个包不含 runtime_supervisor

桌面端**不 spawn Runtime、不拼 serve 参数、不写 launch-spec.json**。Runtime 由
LocalAIHub 的安装器/launcher 管理，桌面端只做"探活 → 唤醒 → 重建 Client"。
理由见方案 §3.1：同机只能有一份 Runtime，自己 spawn 必然与已装实例冲突。
"""

__version__ = "0.1.0"
