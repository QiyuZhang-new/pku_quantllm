"""兼容旧启动命令，统一进入原量化交易系统。"""
from pathlib import Path
import sys
import runpy

LEGACY = Path(__file__).resolve().parent / "lesson04"
if str(LEGACY) not in sys.path:
    sys.path.insert(0, str(LEGACY))
if __name__ == "__main__":
    runpy.run_path(str(LEGACY / "app.py"), run_name="quantllm_compat")["main"](default_section="因子演化")
