"""与 Runtime 的连接：探活、唤醒、Client 重建。

## 为什么需要这一层（而不是直接用 Client）

SDK 的 `Client.__init__` 只在构造时 `discover()` 一次，把 base_url 与 token
缓存住。Runtime 重启会换端口（动态分配），token 轮换会换 token，之后旧
client 的所有请求都是 `ErrRuntimeUnavailable` / `ErrUnauthorized`。

桌面端是常驻程序，用户可能关掉再打开 LocalAIHub、可能从管理后台轮换 token，
所以必须能重建。方案 §3.1.2 给了四条规则，本模块就是它们的实现：

  1. 连接类错误（ErrRuntimeUnavailable / ErrUnauthorized）→ 重建一次并重试该请求
  2. 重建后仍失败才向用户报错
  3. 重建有 2 秒防抖
  4. 重建后刷新 capability / resources（新实例的能力可能与旧的不同的）

## 为什么唤醒要调安装器而不是自己 spawn

同一台机器只能有一份 Runtime（`discovery.EnsureSingleInstance`）。自己 spawn
在已有实例时必然 ErrAlreadyRunning，且会与安装器的 launch-spec.json 打架。
`localai-cli-installer.exe start` 是幂等的（见 main.go 的 startMain），所以
"调它"在任何状态下都安全。
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TypeVar

from localai import Client, ErrRuntimeUnavailable, ErrUnauthorized

__all__ = ["RuntimeConnector", "WakeResult"]

T = TypeVar("T")

# 重建防抖：两次重建之间至少间隔这么久。
#
# 为什么需要：Runtime 重启期间每个请求都会失败，若每个都触发重建，
# 会变成"每 100ms 重读一次 discovery 文件"的无意义 IO。2 秒足够覆盖
# 一次正常的重启窗口（实测优雅关闭 + 启动约 1~3 秒）。
_REBUILD_COOLDOWN_SEC = 2.0

# 唤醒后等多久开始探活、总共等多久。
#
# 20 秒来自安装器的 waitForDiscovery 默认值（main.go），保持一致：
# 用户在命令行看到的超时与桌面端的行为不该是两个数。
_WAKE_TIMEOUT_SEC = 20.0
_WAKE_POLL_INTERVAL_SEC = 0.25

_CONN_ERRORS = (ErrRuntimeUnavailable, ErrUnauthorized)


@dataclass
class WakeResult:
    """唤醒结果。"""

    ok: bool
    # message 是给用户看的一句话。为空表示成功。
    message: str = ""
    # detail 是排障用的细节（日志路径、安装器输出），不要直接塞进 toast。
    detail: str = ""


def _discovery_path() -> Optional[Path]:
    """返回 discovery 文件路径。

    ## 为什么要自己拼路径（SDK 已经有 DiscoveryPath）

    这里只用于**诊断信息**（告诉用户"文件在不在、内容是什么"），
    而 SDK 的 `DiscoveryPath()` 返回的是 `pathlib.Path`，可以复用——
    但只在运行时导入，避免模块级依赖 SDK 的内部结构。
    """
    try:
        from localai._discovery import DiscoveryPath

        return Path(DiscoveryPath())
    except Exception:  # noqa: BLE001 - 诊断信息取不到不该影响主流程
        return None


def _installer_path() -> Optional[Path]:
    """返回 localai-cli-installer.exe 的路径。找不到返回 None。

    ## 查找顺序

    1. 注册表 `HKCU\\...\\Uninstall\\LocalAIHub` 的 `InstallLocation`
    2. `%LOCALAPPDATA%\\Programs\\LocalAIHub\\`

    为什么先查注册表：用户可能装到了非默认目录（安装器支持 -dir）。
    只查默认目录会在那种机器上误判为"没装"，然后给用户一条错误的
    "请先安装 LocalAIHub"提示。
    """
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LocalAIHub",
        )
        try:
            loc, _ = winreg.QueryValueEx(key, "InstallLocation")
        finally:
            winreg.CloseKey(key)
        if loc:
            p = Path(loc) / "localai-cli-installer.exe"
            if p.is_file():
                return p
    except (ImportError, OSError):
        pass

    import os

    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / "Programs" / "LocalAIHub" / "localai-cli-installer.exe"
        if p.is_file():
            return p
    return None


class RuntimeConnector:
    """持有 Client，负责探活、唤醒与重建。

    ## 用法

        conn = RuntimeConnector()
        conn.ensure_alive()          # 启动时调一次：探活 → 必要时唤醒
        cap = conn.call(lambda c: c.capability())

    `call` 是唯一推荐的调用入口：它在连接类错误时自动重建并重试一次，
    调用方不需要写 try/except 重连逻辑（写一遍就会漏一处）。
    """

    def __init__(self) -> None:
        self._client = Client()
        self._last_rebuild = 0.0
        # 重建次数用于诊断（"为什么刚才那一下卡了"）
        self.rebuild_count = 0

    @property
    def client(self) -> Client:
        """当前 Client。仅用于不便包在 `call` 里的场景（如取 base_url）。"""
        return self._client

    # ------------------------------------------------------------------ 探活

    def alive(self) -> bool:
        """Runtime 是否可连。

        ## 为什么不看 discovery 文件是否存在

        Runtime 异常退出会留下 `runtime.json`（`writer.Remove()` 只在正常
        退出时执行），而 SDK 的 `_read_discovery_file` 也不探活。只判文件
        存在会拿到一个死端口，用户看到的是"连接被拒绝"而不是"服务没启动"。
        """
        return self._client.live()

    def ensure_alive(self) -> WakeResult:
        """探活，不通就唤醒，再探活。启动时调一次。

        返回 ok=False 时，`message` 已经是可直接展示给用户的话，
        `detail` 是给"查看日志"按钮用的路径。

        ## 轮询过程中必须重建 Client（踩过的坑）

        第一版用 `self.alive()` 轮询，结果**必然超时**：Runtime 停止时
        `Client()` 的 discover 回落到默认端口（18765，且 token 为空）；
        唤醒后 Runtime 拿到的是一个随机端口（如 62481）。旧 client 记住
        的是那个死端口，于是无论等多久 `live()` 都是 False——而 Runtime
        其实早就起来了。

        现象很迷惑：安装器说"已启动"，discovery 文件也更新了，但桌面端
        一直报"启动超时"。所以每轮轮询前都要重读 discovery。
        """
        if self.alive():
            return WakeResult(ok=True)

        wake = self.wake()
        if not wake.ok:
            return wake

        deadline = time.monotonic() + _WAKE_TIMEOUT_SEC
        while time.monotonic() < deadline:
            # 每轮都重建：discover() 只是一次文件读，成本可忽略，而
            # 不重建就会一直连旧端口（见上面注释）。
            self._rebuild(force=True)
            if self.alive():
                return WakeResult(ok=True)
            time.sleep(_WAKE_POLL_INTERVAL_SEC)

        return WakeResult(
            ok=False,
            message="本地服务启动超时（20 秒内没有就绪）。",
            detail=self._timeout_detail(wake.detail),
        )

    def _timeout_detail(self, extra: str = "") -> str:
        """超时时的诊断信息。

        ## 为什么要拼出 discovery 的状态

        "启动超时"有三种完全不同的原因，而它们对用户的下一步动作完全不同：

          1. discovery 文件不存在 → Runtime 根本没起来（去看日志）
          2. discovery 存在但 state 不是 ready → 引擎还在加载（再等等）
          3. discovery ready 但连不上 → 端口被防火墙挡了 / 文件是别的实例的

        不区分的话，用户只能看到一句"超时"，然后去问人。
        """
        import json

        parts = [f"日志：{self.log_hint()}"]
        try:
            p = _discovery_path()
            if p is None or not p.is_file():
                parts.append("discovery 文件不存在（Runtime 未写出地址）")
            else:
                info = json.loads(p.read_text(encoding="utf-8"))
                parts.append(
                    f"discovery: {info.get('url', '?')} "
                    f"state={info.get('state', '?')} pid={info.get('pid', '?')}"
                )
        except (OSError, ValueError) as e:
            parts.append(f"discovery 读取失败：{e}")
        if extra:
            parts.append(extra)
        return "\n".join(parts)

    def wake(self) -> WakeResult:
        """调安装器 start。幂等：已在运行时它只打印地址并返回 0。"""
        installer = _installer_path()
        if installer is None:
            return WakeResult(
                ok=False,
                message="没有检测到 Local AI Hub。请先安装它，再打开本程序。",
                detail="未找到 localai-cli-installer.exe（注册表与默认目录都没有）",
            )

        try:
            # 安装器是控制台程序，但 start 子命令很快返回；用 CREATE_NO_WINDOW
            # 避免闪一个黑框。超时给 30 秒：它内部还要等 discovery（最多 20 秒）。
            proc = subprocess.run(
                [str(installer), "start"],
                capture_output=True,
                timeout=30,
                check=False,   # 返回码由下面显式判断（失败要读 stderr 给用户）
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return WakeResult(
                ok=False,
                message="启动本地服务超时。",
                detail=f"{installer} start 超过 30 秒未返回",
            )
        except OSError as e:
            return WakeResult(
                ok=False,
                message="无法启动本地服务。",
                detail=f"执行 {installer} 失败：{e}",
            )

        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
            return WakeResult(
                ok=False,
                message="本地服务启动失败。",
                detail=out or f"安装器返回码 {proc.returncode}",
            )
        return WakeResult(ok=True)

    @staticmethod
    def log_hint() -> str:
        """Runtime 日志路径提示（给"查看日志"用）。"""
        import os

        base = os.environ.get("LOCALAPPDATA")
        if base:
            return str(Path(base) / "LocalAIHub" / "runtime.log")
        return "runtime.log"

    # ------------------------------------------------------------------ 调用

    def call(self, fn: Callable[[Client], T]) -> T:
        """执行一次 SDK 调用；连接类错误时重建 Client 并重试一次。

        ## 为什么只重试一次

        重建后仍失败说明 Runtime 真的不在（或 token 真的被撤销）。继续重试
        只会让用户看到一个转圈几十秒的界面，而正确的表现是立刻告诉他
        "服务不可用"。

        ## 为什么重试的是"整个 fn"而不是"重发 HTTP 请求"

        我们只能控制 SDK 的调用粒度。对幂等读操作（capability/resources/
        list_kb）重试整个 fn 是安全的；对写操作（ingest_chunks）重试需要
        服务端幂等性——`ingest_chunks` 用 `file_hash` 去重，所以也是安全的
        （第二次会返回 duplicate=true）。
        """
        try:
            return fn(self._client)
        except Exception as e:
            if getattr(e, "kind", None) not in _CONN_ERRORS:
                raise
            self._rebuild()
            return fn(self._client)

    def _rebuild(self, force: bool = False) -> None:
        """丢弃旧 Client，新建一个（会重新 discover）。"""
        now = time.monotonic()
        if not force and (now - self._last_rebuild) < _REBUILD_COOLDOWN_SEC:
            # 防抖窗口内不重建：并发请求会同时走到这里，只需一个真正重建。
            return
        self._last_rebuild = now
        self.rebuild_count += 1
        self._client = Client()
