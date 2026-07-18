"""Minimal .env loader (P21/B8) — no third-party dependency, read-only.

Standard KEY=value format: one per line, '#' starts a full-line comment,
blank lines skipped, values may be single- or double-quoted (stripped).
Precedence: an already-set os.environ value is NEVER overridden — a var set
by the shell, Docker, or systemd always wins over the file. This module
never WRITES the file; authoring it (via the CLI interview or the settings
UI, P21/D4) is a separate, later concern.
"""
from __future__ import annotations

import os


def load_dotenv(path: str) -> int:
    """Load KEY=value pairs from `path` into os.environ, skipping any key
    already set. Returns the count of newly set variables. A missing or
    unreadable file is normal, not an error — silently loads nothing."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return 0
    n = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key or key in os.environ:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val
        n += 1
    return n
