#!/usr/bin/env python3
"""env.py — load KEY=VALUE pairs from the `.env` file next to this package.

Every entry point imports this module first, so `cp .env.example .env` (as the
README instructs) actually configures the pipeline. Real environment variables
always win over the file.
"""
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
_LOADED = False


def load_env(path=None):
    """Read `.env` into os.environ without overwriting existing values."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = path or os.path.join(_DIR, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


load_env()
