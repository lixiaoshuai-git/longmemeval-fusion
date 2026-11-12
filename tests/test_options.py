"""运行时选项单测（v3.16）。

锁住两件事：
1. 开关随实例，不再随模块——两个实例可以并存不同配置且互不影响；
   这是 v3.11 审计事故（`import memory_sys.render` 与管道里的
   `import render` 是两个模块对象，改错一份得出「0 题变化」）的回归保护。
2. 环境变量仍能改默认值（消融矩阵沿用旧调用方式）。
"""
import importlib
import json

from datetime import datetime

from memory_sys.fusion import FusionMemory
from memory_sys.options import RuntimeOptions


def _mem(options=None):
    m = FusionMemory(client=object(), options=options)
    m.store.add_evidence("s1", datetime(2023, 5, 1), [
        {"role": "user",
         "content": "I caught 7 largemouth bass with Alex at Lake Michigan."}])
    return m


def test_defaults_are_current_production_behaviour():
    o = RuntimeOptions()
    assert o.premise_proper_only is True
    assert o.enable_arith and o.enable_excerpts and o.enable_floor


def test_options_are_per_instance_not_module_level():
    strict = _mem(RuntimeOptions(premise_proper_only=True))
    loose = _mem(RuntimeOptions(premise_proper_only=False))
    q = "How many largemouth bass did I catch with Alex at Lake Michigan?"
    # 严格口径：形态差异不算假前提
    assert strict._premise_check(q) is None
    # 宽口径（v3.10 之前）：catch/caught 被判成假前提
    assert loose._premise_check(q) is not None
    # 互不影响：再问一次严格实例，行为没有漂移
    assert strict.options.premise_proper_only is True
    assert strict._premise_check(q) is None


def test_ablation_flags_are_read_per_instance():
    off = FusionMemory(client=object(), options=RuntimeOptions(
        enable_arith=False, enable_excerpts=False, enable_floor=False))
    assert (off.enable_arith, off.enable_excerpts, off.enable_floor) == (
        False, False, False)
    on = FusionMemory(client=object())
    assert (on.enable_arith, on.enable_excerpts, on.enable_floor) == (
        True, True, True)


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("ABL_ARITH", "0")
    monkeypatch.setenv("PREMISE_PROPER_ONLY", "false")
    o = RuntimeOptions.from_env()
    assert o.enable_arith is False
    assert o.premise_proper_only is False
    assert o.enable_floor is True          # 未设置 → 默认开


def test_options_are_immutable():
    o = RuntimeOptions()
    try:
        o.enable_arith = False             # frozen dataclass
    except Exception as exc:               # noqa: BLE001
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:
        raise AssertionError("RuntimeOptions 应当不可变")


def test_module_global_switch_is_gone():
    """旧实现把开关放在 render 模块上；已删除，避免出现第二个真值来源。

    同时检查两份模块对象：扁平 `render`（管道实际加载的那份）与
    `memory_sys.render`（脚本里容易误 import 的那份）。
    """
    import render as flat_render
    pkg_render = importlib.import_module("memory_sys.render")
    for mod in (flat_render, pkg_render):
        assert not hasattr(mod, "PREMISE_PROPER_ONLY"), mod.__name__


def test_domain_terms_are_opt_in_only():
    """P6：领域词默认不进语言通用词表，必须显式注入。"""
    from memory_sys.lexicon import LANGUAGE_GENERIC

    assert "tank" not in LANGUAGE_GENERIC
    assert "gallon" not in LANGUAGE_GENERIC
    assert "undergrad" not in LANGUAGE_GENERIC

    plain = FusionMemory(client=object())
    assert "tank" not in plain._generic_toks()
    injected = FusionMemory(client=object(), options=RuntimeOptions(
        domain_terms=frozenset({"tank", "gallon"})))
    assert {"tank", "gallon"} <= injected._generic_toks()
    # 注入只影响该实例，不污染默认实例
    assert "tank" not in plain._generic_toks()


def test_save_cache_is_atomic_and_leaves_no_temp(tmp_path):
    """增量落盘：写到临时文件再原子替换，且不留 .tmp 残骸。"""
    from memory_sys.fusion import FusionMemory

    p = tmp_path / "cache.json"
    mem = FusionMemory(client=object(), cache={"k": ["v"]}, cache_path=str(p))
    mem._dirty = True
    mem.save_cache()
    assert json.loads(p.read_text(encoding="utf-8")) == {"k": ["v"]}
    assert not list(tmp_path.glob("*.tmp"))
    # 未变更时不应重写
    mtime = p.stat().st_mtime_ns
    mem.save_cache()
    assert p.stat().st_mtime_ns == mtime
