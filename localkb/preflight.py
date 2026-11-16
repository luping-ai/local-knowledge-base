"""启动自检：契约版本、SDK 版本、安装完整性。

## 为什么这一层独立于 `runtime_conn`

`runtime_conn` 关心"能不能连上"，这一层关心"连上的那个东西**够不够新**"。

两者的失败处理完全不同：
  - 连不上 → 重试、唤醒（`runtime_conn` 的事）
  - 连上了但太旧 → **重试没有意义**，必须告诉用户"去升级 LocalAIHub"

## 为什么用契约版本而不是 Runtime 版本号

Runtime 版本号是发布节奏（0.2.7、0.3.0…），与"有没有 `page_start` 字段"
无关。锁版本号会让"用户升级服务端"变成"桌面端不能用"（方案 §13.4）。

契约版本直接对应能力：
  1 → Phase 1 之前（无 documents/chunks）
  2 → 含溯源四列 + documents/chunks + chunker_version（M1/M2 的依赖）
  3 → 含相似检测三表
  4 → 含表格结构化指纹

## 为什么 SDK 版本要在本地查而不是问服务端

SDK 是**客户端依赖**，服务端不知道装的是哪个版本。`localai.__version__`
是唯一可靠来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "MIN_CONTRACT_VERSION",
    "MIN_SDK_VERSION",
    "InstallCheck",
    "check_install",
    "parse_version",
]


# 桌面端要求的最低契约版本。
#
# 2 = 含 `page_start` / `heading_path` / `chunk_type` 四列 + `documents/chunks`
# 端点 + `chunker_version` 一致性检查。低于这个值，M1/M2 的功能全部不可用。
MIN_CONTRACT_VERSION = 2

# 要求的最低 SDK 版本。
#
# ## 为什么从 0.2.0 提到 0.3.0（迁移到独立仓库时改的）
#
# 0.2.0 引入 `ingest_chunks`（M2 起就够用）。但 M7 用了
# `dedup_check` / `dedup_groups` / `dedup_diff` 三个方法，它们**是 0.3.0
# 才有的**。
#
# 原来写 0.2.0 时还没做 M7，所以没错。但迁移时核对发现：`pyproject.toml`
# 的 `dependencies` 已经是 `luping-localai>=0.3.0`，而这里写 0.2.0 ——
# 两者矛盾。矛盾的实际后果：pip 装的是 0.3.0（满足 pyproject），
# preflight 会放过一个 0.2.x 的环境（不满足运行时需求），
# 然后在调用 `dedup_check` 时 `AttributeError`。
#
# 门禁的意义就是"在启动时说清缺什么"，而不是等某个功能静默失效。
MIN_SDK_VERSION = (0, 3, 0)
# 上限（不含）。
#
# ## 为什么是 0.4 而不是 0.3（实测踩到）
#
# 第一版写 `0.3.0`，而本项目实际开发用的 SDK 就是 **0.3.0**——
# 于是自检一启动就报"桌面端与 SDK 0.3.0 不兼容"，而它明明跑得好好的。
#
# 教训：上限要留出"当前已适配的版本"。语义是"我验证到 0.3.x 为止，
# 0.4 可能有破坏性改动"。
MAX_SDK_VERSION = (0, 4, 0)


@dataclass
class InstallCheck:
    """安装完整性检查结果。"""

    ok: bool = True
    # 致命问题：必须解决才能用（SDK 版本不符、契约过低）
    fatal: list[str] = field(default_factory=list)
    # 警告：能用但功能受限（语义检索未启用等）
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.fatal.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def parse_version(s: str) -> tuple[int, ...]:
    """把 `"0.2.0"` 解析成 `(0, 2, 0)`。解析失败返回空元组。

    ## 为什么不用 packaging.version

    那是第三方依赖，而本项目的 Python 侧**刻意保持依赖精简**（打包体积
    与启动速度都受影响）。版本号只用来比大小，三位数字足够。
    """
    out: list[int] = []
    for part in (s or "").split("."):
        # 去掉 `-dev` / `+local` 之类的后缀
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """比较版本元组。短的补 0。"""
    n = max(len(a), len(b))
    a2 = a + (0,) * (n - len(a))
    b2 = b + (0,) * (n - len(b))
    return (a2 > b2) - (a2 < b2)


def check_install(conn) -> InstallCheck:
    """检查"装的东西版本对不对"。

    ## 检查顺序（有依赖关系）

    1. SDK 版本 —— 本地就能查，最便宜
    2. 契约版本 —— 需要连服务端
    3. 模型/Embedding —— 只给警告，不阻止使用

    先查本地再查远程：如果 SDK 都不对，后面的检查结果没有意义
    （SDK 缺方法时报的是 `AttributeError`，看起来像服务端的问题）。
    """
    res = InstallCheck()

    # ---- 1. SDK 版本 ----
    try:
        import localai

        raw = getattr(localai, "__version__", "")
        v = parse_version(raw)
        if not v:
            res.warn(f"无法解析 SDK 版本（{raw!r}），跳过版本检查")
        elif _cmp(v, MIN_SDK_VERSION) < 0:
            res.fail(
                f"桌面端需要 SDK ≥ {'.'.join(map(str, MIN_SDK_VERSION))}，"
                f"当前 {raw}。请重新安装 LocalKB。"
            )
        elif _cmp(v, MAX_SDK_VERSION) >= 0:
            res.fail(
                f"桌面端与 SDK {raw} 不兼容（需要 "
                f"< {'.'.join(map(str, MAX_SDK_VERSION))}）。"
                "请重新安装 LocalKB。"
            )
    except ImportError as e:
        res.fail(f"缺少 SDK 依赖：{e}。请重新安装 LocalKB。")

    # ---- 2. 契约版本 ----
    if res.fatal:
        # SDK 都不对，没必要再连服务端
        return res

    try:
        cap = conn.call(lambda c: c.capability())
    except Exception as e:  # noqa: BLE001
        # 连不上不是"版本不对"，交给 runtime_conn 处理
        res.warn(f"无法读取服务端能力（{e}），跳过契约版本检查")
        return res

    sv = int(getattr(cap, "schema_version", 0) or 0)
    if sv == 0:
        # 旧版 Runtime 没有这个字段。这是"服务端太旧"的确定信号。
        res.fail(
            "本地服务的版本过旧（没有契约版本字段）。"
            "请到管理后台或安装目录升级 Local AI Hub 后重启。"
        )
    elif sv < MIN_CONTRACT_VERSION:
        res.fail(
            f"本地服务的知识库契约版本为 {sv}，桌面端需要 ≥ "
            f"{MIN_CONTRACT_VERSION}（含页码与标题路径字段）。"
            "请升级 Local AI Hub 后重启。"
        )

    # ---- 3. 只给警告的项 ----
    if not getattr(cap, "rag_enabled", False):
        res.warn(
            "当前内存不足以启用知识库问答"
            f"（{getattr(cap, 'reason', '')}）。"
            "关闭部分程序后重启本地服务可恢复。"
        )

    return res


def format_check(res: InstallCheck) -> str:
    """把检查结果格式化成给用户看的文字。"""
    if res.ok and not res.warnings:
        return ""
    parts: list[str] = []
    if res.fatal:
        parts.append("需要处理：")
        parts.extend(f"  · {m}" for m in res.fatal)
    if res.warnings:
        parts.append("提示：")
        parts.extend(f"  · {m}" for m in res.warnings)
    return "\n".join(parts)
