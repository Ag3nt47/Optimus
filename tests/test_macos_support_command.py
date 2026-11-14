"""Optional macOS support package downloader."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "scripts" / "Get-Optimus-macOS-Support.command"
TEXT = COMMAND.read_text(encoding="utf-8")


def _pin(name: str) -> str:
    match = re.search(rf"^{name}=(\S+)$", TEXT, re.M)
    assert match, name
    return match.group(1)


def test_pin_is_exact_and_canonical() -> None:
    assert _pin("REPOSITORY") == "Ag3nt47/Optimus-macOS-Support"
    assert re.fullmatch(r"[0-9a-f]{40}", _pin("COMMIT"))
    assert re.fullmatch(r"[0-9a-f]{64}", _pin("MANIFEST_SHA256"))
    assert re.fullmatch(r"[0-9]+", _pin("MANIFEST_SIZE"))


def test_download_is_https_only_and_asks_first() -> None:
    assert "--proto '=https'" in TEXT and "--max-redirs 0" in TEXT
    assert "sudo" not in TEXT
    assert TEXT.index("agreed=0") < TEXT.index("WORK=$(mktemp")
    assert 'if [ "$agreed" -ne 1 ]' in TEXT


def _fixture_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    (pack / "scripts").mkdir(parents=True)
    files = {"scripts/hello.sh": b"#!/bin/sh\necho hello\n", "README.md": b"readme\n"}
    for path, data in files.items():
        (pack / path).write_bytes(data)
    lines = ["# optimus-macos-support " + _pin("VERSION")]
    for path, data in sorted(files.items()):
        mode = "755" if path.endswith(".sh") else "644"
        lines.append(f"{hashlib.sha256(data).hexdigest()} {len(data)} {mode} {path}")
    (pack / "pack-manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pack


def _patched_command(tmp_path: Path, manifest: Path) -> Path:
    raw = manifest.read_bytes()
    text = re.sub(r"^MANIFEST_SHA256=\S+$", "MANIFEST_SHA256=" + hashlib.sha256(raw).hexdigest(),
                  TEXT, flags=re.M)
    text = re.sub(r"^MANIFEST_SIZE=\S+$", f"MANIFEST_SIZE={len(raw)}", text, flags=re.M)
    command = tmp_path / "Get-Optimus-macOS-Support.command"
    command.write_text(text, encoding="utf-8")
    return command


def _run(command: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SSH_CONNECTION": "test",
           "OPTIMUS_SUPPORT_NO_PAUSE": "1", "TMPDIR": str(home)}
    return subprocess.run(["/bin/sh", str(command), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, env=env, timeout=60, check=False)


macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS command")


@macos_only
def test_refuses_without_consent(tmp_path: Path) -> None:
    result = _run(COMMAND, tmp_path)
    assert result.returncode == 2 and "--yes" in result.stderr
    assert not (tmp_path / "Library").exists()


@macos_only
def test_installs_verified_pack_and_rejects_tampering(tmp_path: Path) -> None:
    pack = _fixture_pack(tmp_path)
    command = _patched_command(tmp_path, pack / "pack-manifest.txt")
    home = tmp_path / "home"
    home.mkdir()
    result = _run(command, home, "--yes", "--from-directory", str(pack))
    assert result.returncode == 0, result.stderr
    installed = home / "Library/Application Support/Optimus/macos-support/current/scripts/hello.sh"
    assert installed.read_bytes() == b"#!/bin/sh\necho hello\n" and os.access(installed, os.X_OK)
    assert "yes" in _run(command, home, "--status").stdout
    assert _run(command, home, "--uninstall").returncode == 0
    assert not installed.exists()

    (pack / "scripts/hello.sh").write_bytes(b"#!/bin/sh\necho changed\n")
    result = _run(command, home, "--yes", "--from-directory", str(pack))
    assert result.returncode == 1 and "Nothing was installed" in result.stderr
    assert not installed.exists()


@macos_only
def test_rejects_unsafe_manifest_paths(tmp_path: Path) -> None:
    pack = _fixture_pack(tmp_path)
    manifest = pack / "pack-manifest.txt"
    digest = hashlib.sha256(b"x").hexdigest()
    manifest.write_text(manifest.read_text() + f"{digest} 1 644 ../escape\n", encoding="utf-8")
    command = _patched_command(tmp_path, manifest)
    home = tmp_path / "home"
    home.mkdir()
    result = _run(command, home, "--yes", "--from-directory", str(pack))
    assert result.returncode == 1 and "unsafe entry" in result.stderr
    assert not (tmp_path / "escape").exists()
