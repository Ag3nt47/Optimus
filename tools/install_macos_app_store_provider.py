#!/usr/bin/env python3
"""Install Optimus's optional pinned Mac App Store update provider."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from optimus.macos_app_store_provider import install  # noqa: E402
from optimus.filesystem import default_data_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accept-official-mas-provider", action="store_true",
                        help="accept the pinned official mas-cli provider and its MIT license")
    parser.add_argument("--archive", type=Path, help="use an already downloaded exact package")
    args = parser.parse_args()
    executable = install(default_data_root(), accepted=args.accept_official_mas_provider,
                         archive=args.archive)
    print(f"Mac App Store provider ready: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
