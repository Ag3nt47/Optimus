"""Pinned, user-scoped Mac App Store update provider for Optimus.

The optional provider is the official ``mas`` 7.0.0 release.  Its upstream
installer package and extracted executable are both hash-pinned because the
upstream package is not code signed.  Optimus never installs it silently: the
standalone setup command requires an explicit acceptance flag.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request


VERSION = "7.0.0"
ARTIFACTS = {
    "x86_64": {
        "url": "https://github.com/mas-cli/mas/releases/download/v7.0.0/mas-7.0.0-x86_64.pkg",
        "package_size": 3_526_147,
        "package_sha256": "9bca1de1fb6ea19c9e0b9d7b7c85249020c9ed65a9d434c2ef40896a2f3395f9",
        "binary_size": 11_518_752,
        "binary_sha256": "7bdd3967f62f5439fdc4ad3dd3a1d0972cb43ba09fd736bc58720121467c07ee",
    },
    "arm64": {
        "url": "https://github.com/mas-cli/mas/releases/download/v7.0.0/mas-7.0.0-arm64.pkg",
        "package_size": 3_260_087,
        "package_sha256": "bc218a854c85d9e1c95496a96b26287bb66056b95688b90f91d04fa62ed21b75",
        "binary_size": 10_923_456,
        "binary_sha256": "1a9ec80879b029b67fe3de485203722cfdbee8589fdd70d3bf3c3df59c9f32c9",
    },
}
LICENSE_SHA256 = "926b52ce77b2a26ac5cb7be146678a965d3a0d420187677368e6169cf3587f4d"
_RELEASE_HOSTS = {"github.com", "release-assets.githubusercontent.com"}

_PRIVILEGED_UPDATE_SCRIPT = r'''set -eu
provider_source=$1
expected_sha=$2
app_id=$3
provider_temp=$(/usr/bin/mktemp -d /private/tmp/optimus-mas.XXXXXX)
/bin/chmod 0700 "$provider_temp"
cleanup() {
  /bin/rm -rf "$provider_temp"
}
trap cleanup EXIT HUP INT TERM
/bin/cp "$provider_source" "$provider_temp/mas"
/bin/chmod 0500 "$provider_temp/mas"
actual_sha=$(/usr/bin/shasum -a 256 "$provider_temp/mas" | /usr/bin/awk '{print $1}')
if [ "$actual_sha" != "$expected_sha" ]; then
  echo "The copied Mac App Store provider failed its pinned SHA-256 check." >&2
  exit 65
fi
"$provider_temp/mas" update --inaccurate --check-min-os "$app_id"
'''


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _generation(data_dir: Path, architecture: str) -> Path:
    return Path(data_dir) / "providers" / "mas" / VERSION / architecture


def trusted_executable(data_dir: Path, *, architecture: str | None = None) -> Path | None:
    """Return the exact installed provider only while its receipt and bytes agree."""
    architecture = architecture or platform.machine()
    artifact = ARTIFACTS.get(architecture)
    if artifact is None:
        return None
    root = _generation(data_dir, architecture)
    binary = root / "mas"
    license_path = root / "LICENSE"
    receipt_path = root / "receipt.json"
    try:
        root_info = root.lstat()
        binary_info = binary.lstat()
        license_info = license_path.lstat()
        receipt_info = receipt_path.lstat()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    expected = {
        "schema": 1,
        "provider": "mas-cli/mas",
        "version": VERSION,
        "architecture": architecture,
        "source_url": artifact["url"],
        "package_sha256": artifact["package_sha256"],
        "binary_sha256": artifact["binary_sha256"],
        "upstream_package_signed": False,
    }
    identity = os.getuid() if hasattr(os, "getuid") else None
    posix_permissions_untrusted = identity is not None and (
        root_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or binary_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or license_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or receipt_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or root_info.st_uid != identity
        or binary_info.st_uid != identity
        or license_info.st_uid != identity
        or receipt_info.st_uid != identity
    )
    if (
        receipt != expected
        or not stat.S_ISDIR(root_info.st_mode)
        or not stat.S_ISREG(binary_info.st_mode)
        or not stat.S_ISREG(license_info.st_mode)
        or not stat.S_ISREG(receipt_info.st_mode)
        or binary_info.st_nlink != 1
        or license_info.st_nlink != 1
        or receipt_info.st_nlink != 1
        or binary_info.st_size != artifact["binary_size"]
        or posix_permissions_untrusted
        or _digest(binary) != artifact["binary_sha256"]
        or _digest(license_path) != LICENSE_SHA256
    ):
        return None
    return binary


def privileged_update_command(
    data_dir: Path,
    identity: str,
    *,
    architecture: str | None = None,
) -> list[str]:
    """Build an elevated command that verifies a root-owned copy before execution."""
    if not isinstance(identity, str) or not identity.isdigit() or len(identity) > 20:
        raise ValueError("Choose one exact Mac App Store ADAM ID.")
    architecture = architecture or platform.machine()
    artifact = ARTIFACTS.get(architecture)
    executable = trusted_executable(data_dir, architecture=architecture)
    if artifact is None or executable is None:
        raise ValueError("The pinned Mac App Store provider is no longer trusted.")
    # The authorized shell copies into a root-created 0700 directory, hashes
    # that root-owned copy, and executes only the verified copy. A same-user
    # process therefore cannot swap the executable after the privileged check.
    return ["/bin/sh", "-c", _PRIVILEGED_UPDATE_SCRIPT, "optimus-mas-update",
            str(executable), str(artifact["binary_sha256"]), identity]


def _download(destination: Path, artifact: dict[str, object]) -> None:
    request = urllib.request.Request(str(artifact["url"]), headers={"User-Agent": "Optimus-mas-provider/1"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as output:
        if urllib.parse.urlsplit(response.url).hostname not in _RELEASE_HOSTS:
            raise ValueError("The Mac App Store provider download left the reviewed GitHub release hosts.")
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > int(artifact["package_size"]):
                raise ValueError("The Mac App Store provider package exceeds its reviewed size.")
            digest.update(block)
            output.write(block)
    if size != artifact["package_size"] or digest.hexdigest() != artifact["package_sha256"]:
        raise ValueError("The Mac App Store provider package does not match its reviewed size/SHA-256.")


def install(
    data_dir: Path,
    *,
    accepted: bool = False,
    archive: Path | None = None,
    architecture: str | None = None,
) -> Path:
    """Install the pinned provider after explicit acceptance and return its executable."""
    if accepted is not True:
        raise ValueError("Installing the optional Mac App Store provider requires explicit acceptance.")
    if platform.system() != "Darwin":
        raise RuntimeError("The Mac App Store provider is only available on macOS.")
    architecture = architecture or platform.machine()
    artifact = ARTIFACTS.get(architecture)
    if artifact is None:
        raise RuntimeError("No reviewed Mac App Store provider exists for this architecture.")
    existing = trusted_executable(data_dir, architecture=architecture)
    if existing is not None:
        return existing
    final = _generation(data_dir, architecture)
    if final.exists() or final.is_symlink():
        raise ValueError("An unverified provider generation already exists; it was not replaced.")
    parent = final.parent
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.is_symlink() or not stat.S_ISDIR(parent.stat().st_mode):
        raise ValueError("The provider installation root is not an ordinary directory.")
    with tempfile.TemporaryDirectory(prefix="install.", dir=parent) as temporary:
        temporary_path = Path(temporary)
        package = temporary_path / "mas.pkg"
        if archive is None:
            _download(package, artifact)
        else:
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_nlink != 1:
                raise ValueError("The selected provider package is not an ordinary file.")
            shutil.copyfile(archive, package)
            if package.stat().st_size != artifact["package_size"] or _digest(package) != artifact["package_sha256"]:
                raise ValueError("The selected provider package does not match its reviewed size/SHA-256.")
        expanded = temporary_path / "expanded"
        subprocess.run(["/usr/sbin/pkgutil", "--expand-full", str(package), str(expanded)],
                       check=True, timeout=120, capture_output=True, text=True)
        payload = expanded / "mas.pkg" / "Payload" / "usr" / "local" / "opt" / "mas"
        source_binary = payload / "libexec" / "bin" / "mas"
        source_license = payload / "LICENSE"
        if (
            source_binary.is_symlink()
            or not source_binary.is_file()
            or source_binary.stat().st_nlink != 1
            or source_binary.stat().st_size != artifact["binary_size"]
            or _digest(source_binary) != artifact["binary_sha256"]
            or source_license.is_symlink()
            or not source_license.is_file()
            or _digest(source_license) != LICENSE_SHA256
        ):
            raise ValueError("The pinned package payload does not contain the reviewed provider bytes.")
        staging = temporary_path / architecture
        staging.mkdir(mode=0o700)
        shutil.copyfile(source_binary, staging / "mas")
        shutil.copyfile(source_license, staging / "LICENSE")
        (staging / "mas").chmod(0o700)
        (staging / "LICENSE").chmod(0o600)
        receipt = {
            "schema": 1,
            "provider": "mas-cli/mas",
            "version": VERSION,
            "architecture": architecture,
            "source_url": artifact["url"],
            "package_sha256": artifact["package_sha256"],
            "binary_sha256": artifact["binary_sha256"],
            "upstream_package_signed": False,
        }
        (staging / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        (staging / "receipt.json").chmod(0o600)
        check = subprocess.run([str(staging / "mas"), "version"], check=True, timeout=30,
                               capture_output=True, text=True)
        if check.stdout.strip() != VERSION:
            raise ValueError("The extracted provider did not report its reviewed version.")
        staging.rename(final)
    executable = trusted_executable(data_dir, architecture=architecture)
    if executable is None:
        raise ValueError("The installed provider failed its post-install byte/receipt verification.")
    return executable
