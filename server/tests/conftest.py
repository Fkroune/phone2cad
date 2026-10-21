import os
import shlex
import sys
import tempfile
from pathlib import Path

# Configure before app import: module-level settings read the environment.
_tmp = Path(tempfile.mkdtemp(prefix="phone2cad-test-"))
os.environ["PHONE2CAD_DATA"] = str(_tmp)
os.environ["PHONE2CAD_WEB_ORIGIN"] = "https://phone2cad.pages.dev"
TOKEN = "test-token-" + "x" * 32
(_tmp / "token").write_text(TOKEN + "\n")
os.environ["PHONE2CAD_TOKEN_FILE"] = str(_tmp / "token")
os.environ["PHONE2CAD_WORKER_CMD"] = shlex.join([sys.executable, str(Path(__file__).parent / "fake_worker.py")])
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
