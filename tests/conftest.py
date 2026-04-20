import os
import sys
from pathlib import Path

# Ensure src/ is importable without installing the package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Neutralize any ambient config/key before tests run.
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("WORKLOG_API_KEY", None)
os.environ["WORKLOG_CONFIG"] = str(ROOT / "tests" / ".tmp-config.toml")
os.environ["WORKLOG_DATA"] = str(ROOT / "tests" / ".tmp-data")
