"""示例不能腐烂：docs/api.md 里引用的 examples/quickstart.py 必须能跑通。"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_quickstart_runs_offline():
    out = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "quickstart.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "新增 2 条事件" in out.stdout
    assert "答案：" in out.stdout
