"""移到回收站。**不做永久删除。**

## 为什么只到回收站

方案的 §7.7 定了三条硬约束，第二条是"只走回收站，不做永久删除"。

理由很直接：误删**不可逆**，而这个产品没有任何"撤销"能力——没有版本
历史、没有备份、没有操作日志回放。用户点错一次"确定"，三年的资料就没了。

回收站是 Windows 自带的安全网：用户随时能还原，且不需要我们实现任何
撤销栈、回收站语义、跨卷移动。

## 为什么用 SHFileOperationW 而不是 os.remove

1. `os.remove` 是**永久删除**，不进回收站
2. `SHFileOperationW` + `FOF_ALLOWUNDO` 是资源管理器"删除"的同一个 API，
   行为与用户手动删完全一致（含"文件太大不进回收站"这类系统策略）

## 为什么先删库记录、再动文件

反过来的话会出现"文件已删、记录还在"——那张来源卡片还写着"可以打开原文"，
而文件已经没了。用户会以为程序坏了。

调用方（UI）负责这个顺序；本模块只负责"把文件移到回收站"。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

__all__ = ["move_to_recycle_bin", "can_recycle"]


# SHFileOperationW 的结构体。ctypes 没有内置定义，必须手写。
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004            # 不显示进度对话框
_FOF_NOCONFIRMATION = 0x0010    # 不弹"确认删除"（我们自己已经确认过）
_FOF_ALLOWUNDO = 0x0040         # **关键**：进回收站而不是永久删除
_FOF_NOERRORUI = 0x0400         # 出错时不弹系统对话框（我们给可读提示）
_FOF_NOCONFIRMMKDIR = 0x0200


def can_recycle() -> bool:
    """当前平台是否支持"移到回收站"。

    非 Windows 返回 False，调用方据此禁用"同时删除文件"选项——
    而不是让用户勾了之后发现失败。
    """
    return sys.platform == "win32"


def move_to_recycle_bin(path: str | Path) -> tuple[bool, str]:
    """把文件移到回收站。返回 `(成功, 说明)`。

    ## 为什么返回元组而不是抛异常

    调用方是"删除对话框"的确定按钮。删除失败时最合适的反馈是"告诉用户
    文件没删掉、库记录已删"，而不是让异常冒泡（那会让 UI 卡在半完成状态）。

    ## `pFrom` 必须是双 NUL 结尾

    `SHFileOperationW` 接受**路径列表**，用 `\\0` 分隔、以 `\\0\\0` 结束。
    单路径时也必须有两个 NUL。少一个的后果是函数读到越界内存——可能
    静默失败，也可能删错东西。

    ctypes 的 `c_wchar_p` 会自动加一个 NUL，所以这里手动补一个。
    """
    if not can_recycle():
        return False, "当前平台不支持移到回收站。"

    p = Path(path)
    if not p.exists():
        # 文件本来就不在了：对"删除"这个意图来说算成功。
        # 返回 True 让调用方不必区分"删掉了"与"本来就没了"。
        return True, "文件已不在（可能已被移动或删除）。"

    if p.is_dir():
        # 目录不进回收站：我们只删文件。删目录会让"误删整个文件夹"
        # 变成一次点击就能做到的事，风险远大于收益。
        return False, "不支持删除目录，只能删除单个文件。"

    try:
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = _FO_DELETE
        # 双 NUL 结尾（见上面的说明）
        op.pFrom = str(p.resolve()) + "\x00"
        op.pTo = None
        op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION
                     | _FOF_SILENT | _FOF_NOERRORUI | _FOF_NOCONFIRMMKDIR)
        op.fAnyOperationsAborted = 0
        op.hNameMappings = None
        op.lpszProgressTitle = None

        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if ret != 0:
            # 返回码是 Win32 错误码。0x71 是 DE_ACCESSDENIEDSRC（被占用/无权限）
            return False, f"移到回收站失败（系统返回 {ret:#x}）。"
        if op.fAnyOperationsAborted:
            return False, "操作被中止。"
        return True, "已移到回收站（可从回收站还原）。"
    except Exception as e:  # noqa: BLE001 - 系统调用边界
        return False, f"移到回收站时出错：{e}"
