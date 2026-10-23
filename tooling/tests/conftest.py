from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
for source_dir in (
    REPO_ROOT / "projects" / "LocalAI" / "desktop",
    REPO_ROOT / "projects" / "LocalAI-SE" / "desktop",
):
    sys.path.insert(0, str(source_dir))
