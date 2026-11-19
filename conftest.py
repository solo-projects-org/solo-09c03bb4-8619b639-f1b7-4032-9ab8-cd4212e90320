# Ensures the project root is on sys.path so `import app` works from any cwd,
# and keeps the module-level app's database out of the repo during tests.
import os
import tempfile

os.environ.setdefault(
    "FEED_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="feed-import-"), "import.db")
)
