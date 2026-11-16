"""`preflight` 测试：SDK 版本、契约版本、警告分级。

## 为什么这一层需要独立测试

它是"装完能不能用"的第一道关卡。判错的代价很高：

  - **误报致命**（明明能用却说版本不符）→ 用户以为装坏了，去重装
  - **漏报致命**（版本真的不对却放行）→ 用户看到一堆 404，无从判断原因

两条都是"用户无法自己解决"的故障，所以每个分支都要钉住。
"""

from __future__ import annotations

import pytest

from localkb.preflight import (
    MAX_SDK_VERSION,
    MIN_CONTRACT_VERSION,
    MIN_SDK_VERSION,
    InstallCheck,
    _cmp,
    check_install,
    format_check,
    parse_version,
)

# ------------------------------------------------------------ 版本解析

@pytest.mark.parametrize("raw, expected", [
    ("0.2.0", (0, 2, 0)),
    ("0.2.7", (0, 2, 7)),
    ("1.0", (1, 0)),
    ("0.2.0-dev", (0, 2, 0)),
    ("0.2.0+local", (0, 2, 0)),
    ("", ()),
    ("bad", ()),
])
def test_parse_version(raw: str, expected: tuple) -> None:
    assert parse_version(raw) == expected


def test_cmp_pads_short_versions() -> None:
    """`0.2` 与 `0.2.0` 应当相等（短的补 0）。"""
    assert _cmp((0, 2), (0, 2, 0)) == 0
    assert _cmp((0, 2, 1), (0, 2, 0)) == 1
    assert _cmp((0, 1, 9), (0, 2, 0)) == -1


# ------------------------------------------------------------ 假连接

class _FakeCap:
    def __init__(self, *, level="full", rag=True, reason="", schema=4):
        self.level = level
        self.rag_enabled = rag
        self.reason = reason
        self.schema_version = schema


class _FakeConn:
    """按顺序返回预设结果：第一次 capability，其余 active_model。"""

    def __init__(self, cap=None, *, fail: bool = False):
        self._cap = cap or _FakeCap()
        self._fail = fail
        self.calls = 0

    def call(self, fn):
        self.calls += 1
        if self._fail:
            raise ConnectionError("runtime down")
        return self._cap


# ------------------------------------------------------------ 契约版本

def test_contract_too_low_is_fatal() -> None:
    """契约版本低于下限：致命。

    ## 为什么必须致命

    M1/M2 的溯源（页码、标题路径）全依赖契约 2 的字段。放行的话
    用户能导入文档、能提问，但来源卡片永远显示"页码未知"——
    而他会以为是"这个 PDF 没有页码"，不会想到是版本问题。
    """
    res = check_install(_FakeConn(_FakeCap(schema=1)))
    assert res.ok is False
    assert any("契约版本" in m for m in res.fatal)


def test_contract_missing_field_is_fatal() -> None:
    """旧 Runtime 没有 schema_version 字段：致命，提示升级。"""
    res = check_install(_FakeConn(_FakeCap(schema=0)))
    assert res.ok is False
    assert any("过旧" in m or "契约版本" in m for m in res.fatal)


def test_contract_ok() -> None:
    res = check_install(_FakeConn(_FakeCap(schema=MIN_CONTRACT_VERSION)))
    assert res.ok is True
    assert res.fatal == []


def test_contract_newer_is_fine() -> None:
    """契约版本高于下限不是问题（服务端向后兼容）。"""
    res = check_install(_FakeConn(_FakeCap(schema=99)))
    assert res.ok is True


# ------------------------------------------------------------ 连接失败

def test_connection_failure_is_warning_not_fatal() -> None:
    """连不上服务端时不该判"版本不对"。

    ## 为什么

    连不上是 `runtime_conn` 的责任（唤醒/重建 client）。如果这里判致命，
    用户会看到"版本不匹配"这种完全跑偏的提示，而去重装程序——
    而实际只需要启动 LocalAIHub。
    """
    res = check_install(_FakeConn(fail=True))
    assert res.ok is True, "连不上不该说版本不对"
    assert res.warnings


# ------------------------------------------------------------ 能力警告

def test_rag_disabled_is_warning_only() -> None:
    """内存不足导致 RAG 不可用：警告，不致命。

    版本没问题、程序能跑，只是这台机器现在跑不动模型。用户关掉
    一些程序就能恢复——这是可行动的，不是"装坏了"。
    """
    res = check_install(_FakeConn(_FakeCap(rag=False, reason="可用内存不足")))
    assert res.ok is True
    assert any("内存" in w for w in res.warnings)


# ------------------------------------------------------------ SDK 版本

def test_sdk_version_in_range() -> None:
    """当前装的 SDK 必须在支持范围内（这条会在 SDK 升版时提醒我们适配）。"""
    import localai

    v = parse_version(getattr(localai, "__version__", ""))
    assert v, "SDK 必须有可解析的 __version__"
    assert _cmp(v, MIN_SDK_VERSION) >= 0, (
        f"当前 SDK {localai.__version__} 低于下限 "
        f"{'.'.join(map(str, MIN_SDK_VERSION))}"
    )
    assert _cmp(v, MAX_SDK_VERSION) < 0, (
        f"当前 SDK {localai.__version__} 已达上限 "
        f"{'.'.join(map(str, MAX_SDK_VERSION))}——"
        "桌面端需要适配新版本后上调 MAX_SDK_VERSION"
    )


# ------------------------------------------------------------ 格式化

def test_format_check_empty_when_clean() -> None:
    assert format_check(InstallCheck()) == ""


def test_format_check_lists_items() -> None:
    res = InstallCheck()
    res.fail("SDK 太旧")
    res.warn("没有模型")
    out = format_check(res)
    assert "需要处理" in out
    assert "SDK 太旧" in out
    assert "提示" in out
    assert "没有模型" in out
