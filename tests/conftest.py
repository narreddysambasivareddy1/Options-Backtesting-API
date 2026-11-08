import os
import tempfile

os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="options-backtest-")
