"""Portable TLS fixtures; all connections stay on an ephemeral loopback port."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import os
from pathlib import Path
import socket
import ssl
import threading
from unittest.mock import Mock

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from optimus import self_update, tls_context


def certificate(name, *, issuer=None, ca=True):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject)
        .issuer_name(issuer[1].subject if issuer else subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    return key, builder.sign(issuer[0] if issuer else key, hashes.SHA256())


def pem(value):
    return value[1].public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = certificate('Optimus fixture root')
    path = tmp_path / 'ca.pem'
    path.write_bytes(pem(root))
    monkeypatch.setattr(certifi, 'where', lambda: str(path))
    return root, path


def test_module_import_does_not_load_trust_read_files_or_connect(monkeypatch):
    monkeypatch.setattr('builtins.open', Mock(side_effect=AssertionError('No import-time file access.')))
    monkeypatch.setattr(ssl, 'create_default_context', Mock(side_effect=AssertionError('No import-time context.')))
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No network.')))
    importlib.reload(tls_context)


def test_actual_bundle_probe_is_strict_bounded_and_does_not_connect(monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', Mock(side_effect=AssertionError('No probe network.')))
    before = dict(os.environ)
    raw = Path(certifi.where()).read_bytes()
    report = tls_context.trust_probe()
    assert report == {
        'status': 'ready', 'trust_source': 'system_plus_bundled_mozilla',
        'bundle_sha256': hashlib.sha256(raw).hexdigest(), 'bundle_size_bytes': len(raw),
        'bundled_ca_count': report['bundled_ca_count'], 'trusted_ca_count': report['trusted_ca_count'],
        'hostname_checking': True, 'certificate_verification': 'CERT_REQUIRED',
        'network_requests': 0, 'maintenance_applied': False,
    }
    assert report['trusted_ca_count'] >= report['bundled_ca_count'] > 0
    assert dict(os.environ) == before


def test_missing_native_roots_are_filled_from_the_actual_bundle(bundle, monkeypatch):
    created = []
    def rootless():
        value = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        assert value.cert_store_stats()['x509_ca'] == 0
        created.append(value)
        return value
    monkeypatch.setattr(ssl, 'create_default_context', rootless)
    monkeypatch.setenv('SSL_CERT_FILE', '/nonexistent-builder-only-ca-file')
    before = dict(os.environ)
    context = tls_context.create_update_tls_context()
    assert context is created[0] and context.cert_store_stats()['x509_ca'] == 1
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert dict(os.environ) == before


def test_native_roots_and_verification_flags_are_retained(bundle, monkeypatch):
    native = certificate('Native fixture root')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=pem(native).decode('ascii'))
    flags = context.verify_flags
    monkeypatch.setattr(ssl, 'create_default_context', lambda: context)
    assert tls_context.create_update_tls_context() is context
    expected = {value[1].public_bytes(serialization.Encoding.DER) for value in (native, bundle[0])}
    assert set(context.get_ca_certs(binary_form=True)) == expected
    assert context.verify_flags == flags
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize('content', [b'', b'not a certificate', b'\xff', b' ' * (2 * 1024 ** 2 + 1)],
    ids=['empty', 'malformed-pem', 'non-ascii', 'oversized'])
def test_missing_or_malformed_bundled_trust_fails_closed(bundle, content):
    bundle[1].write_bytes(content)
    with pytest.raises((ValueError, ssl.SSLError)):
        tls_context.create_update_tls_context()


def test_absent_packaged_bundle_is_an_error_even_when_native_roots_exist(bundle):
    bundle[1].unlink()
    with pytest.raises(FileNotFoundError):
        tls_context.trust_probe()


def test_leaf_certificate_is_not_misreported_as_a_usable_ca_bundle(bundle):
    bundle[1].write_bytes(pem(certificate('localhost', ca=False)))
    with pytest.raises(ValueError, match='no usable CA'):
        tls_context.trust_probe()


@pytest.mark.parametrize('mode', [ssl.CERT_REQUIRED, ssl.CERT_NONE])
def test_weakened_default_context_is_refused_without_repairing_it(bundle, monkeypatch, mode):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = mode
    monkeypatch.setattr(ssl, 'create_default_context', lambda: context)
    with pytest.raises(ValueError, match='hostname'):
        tls_context.create_update_tls_context()
    assert context.check_hostname is False and context.verify_mode == mode


@pytest.mark.parametrize('case', ['trusted', 'untrusted_certificate', 'wrong_hostname'])
def test_actual_tls_handshake_accepts_only_trusted_matching_certificates(bundle, monkeypatch, tmp_path, case):
    # Simulate a fresh frozen app with no OpenSSL/native CA path available.
    monkeypatch.setattr(ssl, 'create_default_context', lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    issuer = certificate('Untrusted fixture root') if case == 'untrusted_certificate' else bundle[0]
    server_identity = certificate('localhost', issuer=issuer, ca=False)
    cert_path, key_path = tmp_path / 'server.pem', tmp_path / 'server.key'
    cert_path.write_bytes(pem(server_identity))
    key_path.write_bytes(server_identity[0].private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    observed = []
    def serve():
        try:
            connection, _ = listener.accept()
            connection.settimeout(5)
            try:
                with server_context.wrap_socket(connection, server_side=True) as secure:
                    secure.sendall(b'fixture')
                    observed.append('verified_connection')
            except OSError:
                connection.close()
                observed.append('verification_rejected')
        finally:
            listener.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    context = tls_context.create_update_tls_context()
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
            hostname = 'wrong.example' if case == 'wrong_hostname' else 'localhost'
            if case == 'trusted':
                with context.wrap_socket(connection, server_hostname=hostname) as secure:
                    assert secure.recv(7) == b'fixture'
            else:
                with pytest.raises(ssl.SSLCertVerificationError):
                    context.wrap_socket(connection, server_hostname=hostname)
    finally:
        thread.join(6)
        assert not thread.is_alive()
    assert observed == ['verified_connection' if case == 'trusted' else 'verification_rejected']


def test_github_transport_uses_the_bundled_context_for_its_https_handler(monkeypatch):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    factory = Mock(return_value=context)
    monkeypatch.setattr(self_update, 'create_update_tls_context', factory)
    opener = Mock()
    build = Mock(return_value=opener)
    monkeypatch.setattr(self_update.urllib.request, 'build_opener', build)
    self_update.GitHubTransport()._open(self_update.API + '/releases/latest', 'application/vnd.github+json')
    factory.assert_called_once_with()
    handler = build.call_args.args[1]
    assert isinstance(handler, self_update.urllib.request.HTTPSHandler) and handler._context is context
    opener.open.assert_called_once()
