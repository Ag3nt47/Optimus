"""Small Windows-friendly atomic file operations for app-owned state."""
from __future__ import annotations
import time
from pathlib import Path


def canonical_file(path: Path) -> Path:
    """Resolve the containing directory, preserving the exact final file name.

    MSIX/Windows app-data aliases can resolve a new temporary file and an
    existing destination through different volumes. Use one physical parent
    representation for both operands; journals still require atomic replace.
    """
    path = Path(path)
    return path.parent.resolve() / path.name


def replace_with_retry(temporary: Path, destination: Path, attempts: int = 6):
    if type(attempts) is not int or attempts < 1:
        raise ValueError('Atomic replacement requires at least one attempt.')
    temporary = canonical_file(temporary)
    destination = canonical_file(destination)
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
