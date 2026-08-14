"""Launch Streamlit UI."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
subprocess.run(
    [sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "main.py")],
    check=False,
)
