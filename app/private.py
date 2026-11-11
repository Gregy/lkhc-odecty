"""Location of the installation's private data directory.

Everything specific to one installation that must not be committed (the original spreadsheet, the
importer mapping with tenant names and meter serials, evaluation ground truth, private notes) lives
in one git-ignored directory, by default ``private/`` in the project root. Override it with the
environment variable ``LKHC_PRIVATE_DIR`` or the ``--private-dir`` option of the tools that read it
(e.g. ``python -m app.importer --private-dir /private``).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "LKHC_PRIVATE_DIR"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIVATE_DIR = PROJECT_ROOT / "private"
MAPPING_FILE = "import-mapping.toml"


def private_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """``override`` (CLI option) > ``$LKHC_PRIVATE_DIR`` > ``<project>/private``."""
    if override:
        return Path(override)
    env = os.environ.get(ENV_VAR, "").strip()
    return Path(env) if env else DEFAULT_PRIVATE_DIR
