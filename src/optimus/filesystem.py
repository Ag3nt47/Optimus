"""Small Windows-friendly atomic file operations for app-owned state."""
from __future__ import annotations
import time
from pathlib import Path

def replace_with_retry(temporary: Path, destination: Path, attempts: int = 6):
    try:
        for attempt in range(attempts):
            try:
                temporary.replace(destination)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
