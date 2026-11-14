"""The optional Mac App Store provider remains byte- and receipt-pinned."""
import hashlib
import json
import os

import pytest

from optimus import macos_app_store_provider as provider


def test_trusted_provider_requires_exact_owned_bytes_and_receipt(tmp_path, monkeypatch):
    payload = b'fixture mas provider'
    license_payload = b'fixture license'
    architecture = 'fixture'
    artifact = {
        'url': 'https://github.com/mas-cli/mas/releases/download/v7.0.0/fixture.pkg',
        'package_size': 1,
        'package_sha256': 'a' * 64,
        'binary_size': len(payload),
        'binary_sha256': hashlib.sha256(payload).hexdigest(),
    }
    monkeypatch.setitem(provider.ARTIFACTS, architecture, artifact)
    monkeypatch.setattr(provider, 'LICENSE_SHA256', hashlib.sha256(license_payload).hexdigest())
    root = tmp_path / 'providers' / 'mas' / provider.VERSION / architecture
    root.mkdir(parents=True, mode=0o700)
    binary = root / 'mas'
    binary.write_bytes(payload)
    binary.chmod(0o700)
    license_path = root / 'LICENSE'
    license_path.write_bytes(license_payload)
    license_path.chmod(0o600)
    receipt = {'schema': 1, 'provider': 'mas-cli/mas', 'version': provider.VERSION,
        'architecture': architecture, 'source_url': artifact['url'],
        'package_sha256': artifact['package_sha256'], 'binary_sha256': artifact['binary_sha256'],
        'upstream_package_signed': False}
    receipt_path = root / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    receipt_path.chmod(0o600)
    assert provider.trusted_executable(tmp_path, architecture=architecture) == binary
    command = provider.privileged_update_command(tmp_path, '462054704', architecture=architecture)
    assert command[:2] == ['/bin/sh', '-c']
    assert command[-3:] == [str(binary), artifact['binary_sha256'], '462054704']
    assert '/usr/bin/mktemp -d /private/tmp/optimus-mas.XXXXXX' in command[2]
    assert '/usr/bin/shasum -a 256' in command[2]
    binary.write_bytes(payload + b'changed')
    assert provider.trusted_executable(tmp_path, architecture=architecture) is None
    with pytest.raises(ValueError, match='no longer trusted'):
        provider.privileged_update_command(tmp_path, '462054704', architecture=architecture)


@pytest.mark.skipif(not hasattr(os, 'getuid'), reason='POSIX ownership and mode enforcement')
def test_writable_provider_directory_is_not_trusted(tmp_path, monkeypatch):
    payload = b'fixture mas provider'
    license_payload = b'fixture license'
    architecture = 'fixture'
    artifact = {'url': 'https://github.com/example/provider.pkg', 'package_size': 1,
        'package_sha256': 'a' * 64, 'binary_size': len(payload),
        'binary_sha256': hashlib.sha256(payload).hexdigest()}
    monkeypatch.setitem(provider.ARTIFACTS, architecture, artifact)
    monkeypatch.setattr(provider, 'LICENSE_SHA256', hashlib.sha256(license_payload).hexdigest())
    root = tmp_path / 'providers' / 'mas' / provider.VERSION / architecture
    root.mkdir(parents=True)
    (root / 'mas').write_bytes(payload)
    (root / 'mas').chmod(0o700)
    (root / 'LICENSE').write_bytes(license_payload)
    (root / 'LICENSE').chmod(0o600)
    receipt = {'schema': 1, 'provider': 'mas-cli/mas', 'version': provider.VERSION,
        'architecture': architecture, 'source_url': artifact['url'],
        'package_sha256': artifact['package_sha256'], 'binary_sha256': artifact['binary_sha256'],
        'upstream_package_signed': False}
    (root / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
    (root / 'receipt.json').chmod(0o600)
    root.chmod(0o770)
    assert provider.trusted_executable(tmp_path, architecture=architecture) is None


def test_provider_install_requires_explicit_acceptance_before_any_host_probe(tmp_path):
    with pytest.raises(ValueError, match='explicit acceptance'):
        provider.install(tmp_path)
    assert not list(tmp_path.iterdir())
