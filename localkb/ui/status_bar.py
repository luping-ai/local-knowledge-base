"""状态栏：当前模型、资源状态、知识库可用性。

## 为什么这三项必须常驻

它们决定"用户现在能不能用"，而不是"某个功能有没有"：

  - 模型：不知道当前是哪个模型时，用户无法解释"为什么回答质量变差了"
    （可能降级到了更小的模型）
  - 资源：内存紧张是"扫描要暂停"与"提问会变慢"的直接原因
  - 知识库：`rag_enabled=false` 时提问不会查库，用户会以为是检索没命中

## 为什么显示"可用内存"而不是"占用/总量"

用户关心的不是程序占了多少，而是"还剩多少、够不够我继续用"。
`pressure` 是服务端算好的结论，比一个百分比更贴近"能不能干活"。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QLabel, QStatusBar, QWidget

__all__ = ["StatusBar"]

# 压力等级 → (中文, 颜色)。颜色用于背景，让"紧张"在余光里可见。
_PRESSURE_STYLE = {
    "NORMAL": ("正常", "#f0f9eb"),      # 极淡绿
    "LIMITED": ("偏紧", "#fdf6ec"),     # 淡橙
    "BUSY": ("紧张", "#fdf6ec"),
    "CRITICAL": ("严重", "#fef0f0"),    # 淡红
}


def _fmt_gib(n: int) -> str:
    return f"{n / (1024 ** 3):.1f} GiB"


class StatusBar(QStatusBar):
    """三格状态栏。

    ## 为什么用 QStatusBar 而不是自绘 widget

    QStatusBar 自带布局与"临时消息"机制（`showMessage` 会临时覆盖左侧
    内容 2 秒）。扫描进度、错误提示都能直接用它，不需要额外状态机。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = QLabel("模型：—")
        self._resource = QLabel("资源：—")
        self._kb = QLabel("知识库：—")
        for w in (self._model, self._resource, self._kb):
            w.setContentsMargins(8, 0, 8, 0)
            self.addPermanentWidget(w)

    # ------------------------------------------------------------------ 更新

    def set_model(self, model_id: str, *, degraded: bool = False) -> None:
        """显示**当前实际加载**的模型。

        ## 不要传 cap.rag_model 进来

        `cap.rag_model` 是"这台机器能跑哪个"（能力评估），不是"现在跑着哪个"。
        传错会让状态栏与"没有加载模型"的提示互相矛盾。

        能跑什么用 `set_capability_model()` 放进 tooltip。
        """
        if not model_id:
            self._model.setText("模型：未加载")
            self._model.setToolTip(
                "服务端还没有加载模型。请到管理后台加载一个（推荐 qwen2.5-1.5b）。"
            )
            return
        text = f"模型：{model_id}"
        if degraded:
            text += "（已降级）"
        self._model.setText(text)
        tip = "降级表示服务端按内存情况换了更小的模型。回答质量可能下降，但不会失败。" \
            if degraded else f"当前加载：{model_id}"
        self._model.setToolTip(tip)

    def set_capability_model(self, model_id: str) -> None:
        """把"本机能力上能跑哪个模型"追加到模型格的 tooltip。

        ## 为什么放在 tooltip 而不是主文本

        主文本已被"当前加载了什么"占用，而这是用户最常看的。能力评估是
        第二层信息——只在"为什么是未加载 / 为什么降级"时才想知道。
        """
        if not model_id:
            return
        cur = self._model.toolTip()
        extra = f"本机能力上可加载：{model_id}"
        self._model.setToolTip(f"{cur}\n{extra}" if cur else extra)

    def set_resource(self, st) -> None:
        """`st` 是 SDK 的 ResourceStatus；None 表示还没取到。"""
        if st is None:
            self._resource.setText("资源：—")
            self.setStyleSheet("")
            return

        label, color = _PRESSURE_STYLE.get(st.pressure, (st.pressure or "—", ""))
        self._resource.setText(
            f"资源：{label} · 可用 {_fmt_gib(st.available_bytes)}"
        )
        tip = [f"压力：{st.pressure}"]
        if st.reason:
            tip.append(st.reason)
        if st.consumers:
            tip.append("占用最多：")
            for c in st.consumers[:5]:
                tip.append(f"  {c.name}  {c.readable}")
        self._resource.setToolTip("\n".join(tip))

        # 背景色：只给状态栏上色，不整窗变红——那会让用户以为程序出错。
        if color:
            self.setStyleSheet(f"QStatusBar {{ background: {color}; }}")

    def set_kb(self, *, rag_enabled: bool, kb_endpoints_ok: bool,
               reason: str = "") -> None:
        """知识库可用性。

        ## 为什么要两个标志

        `rag_enabled` 是"这台机器能不能做 RAG"，`kb_endpoints_ok` 是
        "知识库端点是否存在"。低内存时 Runtime 根本不注册
        `/knowledge/*`（方案 §8.5），此时 rag_enabled 也可能为 false，
        但两者不完全同步——所以分开显示，避免把"端点 404"说成"内存不足"。
        """
        if kb_endpoints_ok and rag_enabled:
            self._kb.setText("知识库：可用")
            self._kb.setToolTip("")
        elif not kb_endpoints_ok:
            self._kb.setText("知识库：未启用")
            self._kb.setToolTip(
                "服务端没有启用知识库功能（通常是可用内存不足）。\n"
                "关闭部分程序后重启本地服务可恢复。"
            )
        else:
            self._kb.setText("知识库：仅检索")
            self._kb.setToolTip(
                reason or "知识库可用，但语义检索所需的向量模型未配置。"
            )

    def set_hint(self, text: str, *, timeout_ms: int = 4000) -> None:
        """临时提示（覆盖左侧区域）。用于"已取消""已导入 3 篇"这类一次性消息。"""
        self.showMessage(text, timeout_ms)
