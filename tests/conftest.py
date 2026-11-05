import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep test databases out of the project folder.
_tmp = tempfile.mkdtemp(prefix="bgv_test_")
os.environ.setdefault("BGV_DB_PATH", os.path.join(_tmp, "bgv_runs.sqlite"))
os.environ.setdefault("BGV_CACHE_PATH", os.path.join(_tmp, "bgv_cache.sqlite"))
