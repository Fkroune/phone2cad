import os
import shlex
import sys
import tempfile
from pathlib import Path

# Configure before app import: module-level settings read the environment.
_tmp = Path(tempfile.mkdtemp(prefix="scanfit-test-"))
os.environ["SCANFIT_DATA"] = str(_tmp)
os.environ["SCANFIT_WEB_ORIGIN"] = "https://scanfit.pages.dev"
TOKEN = "test-token-" + "x" * 32
(_tmp / "token").write_text(TOKEN + "\n")
os.environ["SCANFIT_TOKEN_FILE"] = str(_tmp / "token")
os.environ["SCANFIT_WORKER_CMD"] = shlex.join([sys.executable, str(Path(__file__).parent / "fake_worker.py")])
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
