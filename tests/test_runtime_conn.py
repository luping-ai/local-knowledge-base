"""连接层测试：探活、唤醒、Client 重建（方案 A15~A18）。

## 这些测试必须真连服务，不能 mock

`RuntimeConnector` 的价值全在"与服务端的真实交互"上：安装器是否幂等、
discovery 文件是否陈旧、重建后 token 是否更新。mock 掉 `Client` 之后
这几件事一件都测不到，而它们正是线上会出问题的地方。

## 服务不可用时跳过

"本机没装 LocalAIHub"不是代码缺陷。跳过并明确说明原因，比让人看到
一条红色失败去猜要好。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from localkb.runtime_conn import RuntimeConnector, WakeResult, _installer_path


def _discovery_path() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "LocalAIHub" / "runtime.json"


@pytest.fixture()
def live_conn() -> RuntimeConnector:
    conn = RuntimeConnector()
    if not conn.alive():
        pytest.skip("本机 LocalAIHub 未运行，跳过连接层测试")
    return conn


# ------------------------------------------------------------ 基础探活

def test_alive_when_runtime_running(live_conn: RuntimeConnector) -> None:
    assert live_conn.alive() is True


def test_ensure_alive_when_already_running(live_conn: RuntimeConnector) -> None:
    """Runtime 在跑时，ensure_alive 不该做任何事（不重启、不改配置）。"""
    before = live_conn.rebuild_count
    r = live_conn.ensure_alive()
    assert r.ok is True
    assert live_conn.rebuild_count == before, "已在运行时不该重建 client"


def test_call_returns_value(live_conn: RuntimeConnector) -> None:
    cap = live_conn.call(lambda c: c.capability())
    assert cap.level in ("full", "chat_only", "rule_only")
    assert isinstance(cap.schema_version, int)


def test_resources_available(live_conn: RuntimeConnector) -> None:
    """S1 的验收点：resources() 字段完整可用。"""
    st = live_conn.call(lambda c: c.resources())
    assert st.pressure in ("NORMAL", "LIMITED", "BUSY", "CRITICAL")
    assert st.available_bytes > 0
    assert st.total_bytes >= st.available_bytes
    # capability 内嵌，一次请求拿全
    assert st.capability is not None
    assert st.capability.schema_version >= 2


def test_strained_matches_pressure(live_conn: RuntimeConnector) -> None:
    """`strained` 必须与 pressure 语义一致（LIMITED 及以上）。"""
    st = live_conn.call(lambda c: c.resources())
    expected = st.pressure in ("LIMITED", "BUSY", "CRITICAL")
    assert st.strained is expected


# ------------------------------------------------------------ 安装器检测

def test_installer_is_found() -> None:
    """装了 LocalAIHub 的机器上必须能找到安装器。

    A17 依赖它：Desktop 端不自己 spawn Runtime，只调它的 start。
    """
    p = _installer_path()
    if p is None:
        pytest.skip("本机未安装 LocalAIHub")
    assert p.is_file()
    assert p.name == "localai-cli-installer.exe"


def test_installer_start_is_idempotent() -> None:
    """`start` 在已运行时必须返回 0 且不产生第二个实例（A15 的关键前提）。

    ## 为什么这条必须测

    桌面端唤醒 Runtime 的唯一手段就是调它。若它不幂等，用户每次打开
    LocalKB 都会触发一次 Runtime 重启——而重启会打断别人正在进行的对话。
    """
    p = _installer_path()
    if p is None:
        pytest.skip("本机未安装 LocalAIHub")

    conn = RuntimeConnector()
    if not conn.alive():
        pytest.skip("Runtime 未运行，这条测的是「已在跑」的情形")

    before = json.loads(_discovery_path().read_text(encoding="utf-8"))
    proc = subprocess.run(
        [str(p), "start"], capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")

    after = json.loads(_discovery_path().read_text(encoding="utf-8"))
    # 同一实例：pid 与 started_at 都不该变
    assert after["pid"] == before["pid"], "start 不该重启已在运行的实例"
    assert after["started_at"] == before["started_at"]


# ------------------------------------------------------------ 陈旧 discovery

def test_stale_discovery_detected(tmp_path: Path) -> None:
    """discovery 文件指向一个死端口时，alive() 必须返回 False。

    ## 为什么需要这条

    Runtime 异常退出会留下 runtime.json（正常才 Remove）。SDK 的
    `_read_discovery_file` 不探活，所以"文件存在"完全不代表"服务可用"。
    桌面端必须自己探活，否则用户看到的是"连接被拒绝"这种莫名错误。
    """
    bad = tmp_path / "runtime.json"
    bad.write_text(json.dumps({
        "url": "http://127.0.0.1:1",   # 不可能有人监听
        "token": "x", "pid": 1, "version": "0", "api": "v1",
        "started_at": 1, "state": "ready",
    }), encoding="utf-8")


    old_url = os.environ.get("LAIH_URL")
    old_token = os.environ.get("LAIH_TOKEN")
    try:
        # 用显式 URL 覆盖 discovery，模拟"文件里有死地址"
        os.environ["LAIH_URL"] = "http://127.0.0.1:1"
        os.environ["LAIH_TOKEN"] = "x"
        conn = RuntimeConnector()
        assert conn.alive() is False
    finally:
        if old_url is None:
            os.environ.pop("LAIH_URL", None)
        else:
            os.environ["LAIH_URL"] = old_url
        if old_token is None:
            os.environ.pop("LAIH_TOKEN", None)
        else:
            os.environ["LAIH_TOKEN"] = old_token


# ------------------------------------------------------------ 唤醒失败路径

def test_wake_fails_gracefully_without_installer(monkeypatch) -> None:
    """找不到安装器时，要给出可行动的提示，而不是抛异常。"""
    monkeypatch.setattr("localkb.runtime_conn._installer_path", lambda: None)
    conn = RuntimeConnector()
    r = conn.wake()
    assert isinstance(r, WakeResult)
    assert r.ok is False
    assert "安装" in r.message
    assert r.detail


def test_ensure_alive_rebuilds_while_polling(monkeypatch) -> None:
    """唤醒后的轮询必须每轮重建 client（回归测试）。

    ## 这条测试保护的是什么缺陷

    第一版用 `self.alive()` 轮询，结果是**必然超时**：

      Runtime 停止时 Client() 的 discover 回落到默认端口 18765（token 空）
      唤醒后 Runtime 拿到随机端口（如 62481）
      旧 client 记住死端口 → live() 永远 False → 20 秒后报"启动超时"

    现象极具误导性：安装器说"已启动"、discovery 文件已更新，
    但桌面端一直说超时。真实耗时 27.7 秒后失败，修复后 5.5 秒成功。

    ## 怎么在不重启 Runtime 的前提下测

    用假 conn 类：`alive()` 在重建次数 < 2 时返回 False，之后返回 True。
    若实现忘了重建，`alive()` 永远返回 False，测试就会超时失败。
    """
    import localkb.runtime_conn as rc

    # 让 wake 立刻"成功"（不真的去调安装器）
    monkeypatch.setattr(rc.RuntimeConnector, "wake",
                        lambda self: WakeResult(ok=True))
    # 把轮询间隔与超时压小，测试不必等 20 秒
    monkeypatch.setattr(rc, "_WAKE_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(rc, "_WAKE_TIMEOUT_SEC", 1.0)

    conn = rc.RuntimeConnector()
    state = {"rebuilds": 0}

    # 记录重建次数，模拟"只有重建后才会连上新端口"
    real_rebuild = conn._rebuild

    def counting_rebuild(force: bool = False) -> None:
        state["rebuilds"] += 1
        real_rebuild(force=force)

    monkeypatch.setattr(conn, "_rebuild", counting_rebuild)
    # alive() 只在"重建过"之后才为真 —— 与真实场景同构
    monkeypatch.setattr(conn, "alive", lambda: state["rebuilds"] >= 1)
    monkeypatch.setattr(conn, "_last_rebuild", 0.0)

    r = conn.ensure_alive()
    assert r.ok is True, "轮询期间没有重建 client，导致永远连不上新端口"
    assert state["rebuilds"] >= 1


def test_timeout_detail_reports_discovery_state(monkeypatch, tmp_path) -> None:
    """超时的诊断信息必须说明 discovery 的状态。

    三种"超时"的原因完全不同（文件不存在 / 未 ready / ready 但连不上），
    不区分的话用户只能看到一句"超时"。
    """
    import localkb.runtime_conn as rc

    fake = tmp_path / "runtime.json"
    monkeypatch.setattr(rc, "_discovery_path", lambda: fake)

    conn = rc.RuntimeConnector()

    # 情形 1：文件不存在
    detail = conn._timeout_detail()
    assert "不存在" in detail

    # 情形 2：文件存在但 state != ready
    import json

    fake.write_text(json.dumps({
        "url": "http://127.0.0.1:1234", "state": "starting", "pid": 42,
    }), encoding="utf-8")
    detail = conn._timeout_detail()
    assert "starting" in detail
    assert "1234" in detail


# ------------------------------------------------------------ 重建

def test_rebuild_on_connection_error(monkeypatch, live_conn: RuntimeConnector) -> None:
    """连接类错误时自动重建一次并重试。

    ## 怎么模拟

    不改真实 client，而是让第一次调用的 fn 抛 ErrRuntimeUnavailable。
    这正好覆盖"Runtime 重启换端口"的情形——旧 client 的地址失效。
    """
    from localai import APIError, ErrRuntimeUnavailable

    calls = {"n": 0}
    def fn(c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise APIError(ErrRuntimeUnavailable, message="conn reset")
        return c.capability()

    before = live_conn.rebuild_count
    cap = live_conn.call(fn)
    assert cap.level in ("full", "chat_only", "rule_only")
    assert calls["n"] == 2, "应该重试一次"
    assert live_conn.rebuild_count == before + 1, "应该重建一次"


def test_non_connection_error_not_retried(live_conn: RuntimeConnector) -> None:
    """业务错误不该触发重建——那只是浪费一次重连。"""
    from localai import APIError, ErrInvalidRequest

    calls = {"n": 0}

    def fn(c):
        calls["n"] += 1
        raise APIError(ErrInvalidRequest, message="bad param")

    before = live_conn.rebuild_count
    with pytest.raises(APIError):
        live_conn.call(fn)
    assert calls["n"] == 1, "业务错误不该重试"
    assert live_conn.rebuild_count == before, "业务错误不该重建"


def test_rebuild_debounced(live_conn: RuntimeConnector) -> None:
    """连续两次重建请求在防抖窗口内只生效一次。

    否则"Runtime 重启时每个请求都重建"会变成每 100ms 重读一次
    discovery 文件。
    """
    before = live_conn.rebuild_count
    live_conn._rebuild(force=False)
    live_conn._rebuild(force=False)
    assert live_conn.rebuild_count == before + 1


def test_log_hint_points_to_runtime_log() -> None:
    hint = RuntimeConnector.log_hint()
    assert "LocalAIHub" in hint
    assert hint.endswith("runtime.log")
