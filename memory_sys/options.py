"""运行时选项：把散落的模块级开关收敛成显式、可注入、不可变的配置。

为什么需要它：`PREMISE_PROPER_ONLY` 曾是 `render.py` 的模块全局，而
`fusion.py` 用扁平导入（`from render import RenderMixin`）。于是
`import memory_sys.render` 与管道里的 `import render` 是**两个模块对象**，
改错一份等于没改——v3.11 的 A/B 审计就因为这一点得出过「0 题变化」的假结论。

现在所有开关都属于实例：`FusionMemory(options=...)`。两个实例可以携带不同
选项并存，单测能直接锁住「互不影响」。消融实验仍可用环境变量注入默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class RuntimeOptions:
    """一条管线实例的全部可调开关（默认值＝当前生产行为）。"""

    # 前提校验只报带专名信号（大写/数字/连字符）的词；False 恢复 v3.10 之前
    # 的宽口径（普通动词也算 suspect，会把 catch/caught 判成假前提）。
    premise_proper_only: bool = True
    # 以下三个是消融开关：关闭后分别去掉算术注记 / 句子级摘录 / 证据地板。
    enable_arith: bool = True
    enable_excerpts: bool = True
    enable_floor: bool = True

    @classmethod
    def from_env(cls) -> "RuntimeOptions":
        """环境变量覆盖默认值，供消融矩阵使用（ABL_* 与 PREMISE_PROPER_ONLY）。"""
        return cls(
            premise_proper_only=_flag("PREMISE_PROPER_ONLY"),
            enable_arith=_flag("ABL_ARITH"),
            enable_excerpts=_flag("ABL_EXCERPTS"),
            enable_floor=_flag("ABL_FLOOR"),
        )

    def with_(self, **changes) -> "RuntimeOptions":
        """返回替换若干字段后的新实例（本类型不可变，避免被就地改写）。"""
        return replace(self, **changes)
