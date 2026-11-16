"""启动前提检查：判断"能不能提问"，并给出可执行的下一步。

## 这个模块解决什么问题

用户装完 LocalKB 双击，可能遇到三种"不是程序坏了，但用不了"的状态：

  1. **没装 Local AI Hub** —— 程序无法自己做任何事（不含 Runtime）
  2. **装了但没模型** —— 能看界面，问不了问题
  3. **装了、有模型、但内存不够加载** —— 能看界面，问了会失败

这三种都必须给出**具体动作**，而不是"初始化失败"。

## 为什么只有"检查 + 文案"，没有对话框了

第一版这里有一个 `StartupDialog`，由 `MainWindow._check_startup_prereq()`
用 `while True: dlg.exec()` 驱动。实测发现三个问题（详见
`main_window._check_startup_prereq` 的 docstring）：

  · 它在窗口还没画完时弹模态框 → 用户看到"启动就卡死"
  · 它在 UI 线程做 3 次同步 HTTP → Runtime 慢的时候窗口冻结
  · 它让 GUI 测试永远不返回（`exec()` 没人点按钮就不回来）

所以改成**非阻塞**：同一份文案渲染成对话页的常驻黄条，配一个
"重新检查"按钮。对话框整个删掉了——留着它就是留一条没有调用点的死代码，
而下一个人会以为"启动时还会弹框"。

## 为什么做成"问题提示"而不是欢迎向导

方案 §13.3 原文写的是"首次启动引导：检查 Runtime → 检查模型 → 选目录…"。
做成向导有两个问题：

  - 用户第二次打开还要再看一遍（或者要维护"已完成引导"的标记，
    而那个标记会在换机器/清数据后失效）
  - 每个检查项的状态是**随时会变**的（模型可能刚被卸载）

所以做成"只在真的有问题时出现"的一次性提示，每项独立、互相不依赖。
状态正常时什么都不显示——这比"欢迎向导"更尊重老用户。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StartupCheck", "describe_startup_problem", "evaluate_startup"]


@dataclass
class StartupCheck:
    """启动时的前提检查结果。"""

    # Runtime 是否可用
    runtime_ok: bool = False
    # 有模型已加载
    model_loaded: bool = False
    # 能力等级
    level: str = ""
    # 给用户看的原因（来自 capability.reason）
    reason: str = ""
    # 管理后台地址（为空表示拿不到）
    admin_url: str = ""

    @property
    def has_problem(self) -> bool:
        return not (self.runtime_ok and self.model_loaded)


def evaluate_startup(conn) -> StartupCheck:
    """评估启动前提。

    ## 为什么不用 capability 判断"有没有模型"

    `capability` 回答"这台机器能不能跑"，不是"现在有没有加载"。
    实测踩到过：`chat_enabled=true` 但 `active_model()` 为空——因为
    Runtime 重启后不会自动加载模型（服务端待办 S10）。

    所以"模型是否就绪"必须查 `active_model()`。

    ## 三次调用的代价（调用方要知道）

    这里有 3 次可能走网络的调用：`alive()`、`capability()`、
    `active_model()`。正常情况合计 10~50 ms，但 Runtime 刚被要求加载
    1 GB 模型时会到 1~3 秒。

    所以**不要在 UI 线程上反复调用它**（比如轮询）。启动时调一次可以，
    "重新检查"走 `CallWorker`。
    """
    chk = StartupCheck()

    try:
        if not conn.alive():
            return chk
        chk.runtime_ok = True
    except Exception:  # noqa: BLE001
        return chk

    # 管理后台地址：从 discovery 拿 base_url
    try:
        d = conn.client.discovery
        url = getattr(d, "url", "") if d is not None else ""
        if url:
            chk.admin_url = url.rstrip("/") + "/admin/"
    except Exception:  # noqa: BLE001 - 拿不到就不给按钮
        pass

    try:
        cap = conn.call(lambda c: c.capability())
        chk.level = getattr(cap, "level", "")
        chk.reason = getattr(cap, "reason", "")
    except Exception:  # noqa: BLE001 - 读不到就当作未知，不影响"有没有模型"的判断
        chk.level = ""
        chk.reason = ""

    try:
        chk.model_loaded = bool(conn.call(lambda c: c.active_model()))
    except Exception:  # noqa: BLE001 - 读不到就当作未加载（保守）
        chk.model_loaded = False

    return chk


def describe_startup_problem(chk: StartupCheck) -> str:
    """把启动检查结果变成一段"原因 + 动作"的提示文本。

    ## 为什么单独抽成函数

    同一份文案要在两个时机用：启动时的黄条、以及"重新检查"之后更新的黄条。
    两处各写一份必然漂移——改了一处忘了另一处，用户看到的提示就对不上
    实际状态。这类漂移在"文案是唯一产品表现"的地方代价很高。

    返回空字符串表示没有问题（调用方据此隐藏黄条）。
    """
    if not chk.has_problem:
        return ""

    if not chk.runtime_ok:
        return (
            "没有检测到 Local AI Hub。\n"
            "LocalKB 是客户端：它复用已安装的 Local AI Hub 提供向量化与模型推理，"
            "自己不带运行环境。请先安装 Local AI Hub，然后点下面的「重新检查」。\n"
            "（同一台机器只能有一份 Runtime——单实例锁会让两份互相争用端口与数据目录，"
            "所以本程序不自己装一份。）"
        )

    lines = [
        "本地服务已连接，但没有加载对话模型，暂时无法提问。",
        "请到 Local AI Hub 管理后台的模型页加载一个（推荐 qwen2.5-1.5b，约 1.1 GB），"
        "加载完点下面的「重新检查」。",
        "知识库的扫描与导入不受影响，可以先继续整理目录。",
    ]
    # ## 为什么把 reason 标成"能力评估"而不是"原因"
    #
    # `capability().reason` 在**有能力**时也是肯定语气，例如实测拿到的是
    # "可用内存 4.4 GiB，可加载 qwen2.5-3b-instruct-q4 做知识库问答"。
    #
    # 第一版把它放在"未能提问"的提示下面、标成"服务返回的原因"，于是同一
    # 个框里出现了"没有加载模型"和"可加载 3B 做问答"两句互相打架的话——
    # 用户会以为程序在胡说，而实际只是这句话的用途被标错了。
    #
    # 它说的是"这台机器能跑什么"，所以照实标成能力评估；括号里补一句
    # 消除"那为什么不能用"的疑问。
    if chk.reason:
        lines.append(f"（本机能力评估：{chk.reason}）")
        lines.append("上面这条是内存与模型能力的判断依据，不是报错；"
                     "当前只是没有加载模型。")
    return "\n".join(lines)
