"""Verified HTTPS trust for native and frozen Optimus release checks.

Keep Python's native trust stores and client security defaults, then explicitly
add the packaged Mozilla root bundle. Frozen macOS apps must not depend on a
Python installation or a build machine's OpenSSL certificate path. Importing
this module performs no file access, certificate loading or network requests.
"""
from __future__ import annotations

import hashlib
import ssl


_MAX_BUNDLE_BYTES = 2 * 1024 ** 2


def _strict(context):
    if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise ValueError('Verified HTTPS requires certificate and hostname validation.')
    return context


def _ca_count(context):
    value = context.cert_store_stats().get('x509_ca')
    if type(value) is not int or value <= 0:
        raise ValueError('The HTTPS trust store contains no usable CA certificates.')
    return value


def _bundle():
    import certifi

    # cadata loads these exact bounded bytes, rather than reopening a path after
    # inspection. PyInstaller may legitimately link macOS bundle resource paths.
    with open(certifi.where(), 'rb') as stream:
        raw = stream.read(_MAX_BUNDLE_BYTES + 1)
    if not raw or len(raw) > _MAX_BUNDLE_BYTES:
        raise ValueError('The packaged HTTPS root bundle is empty or exceeds its bound.')
    try:
        pem = raw.decode('ascii')
    except UnicodeDecodeError as error:
        raise ValueError('The packaged HTTPS root bundle is not ASCII PEM.') from error
    return raw, pem


def _build():
    raw, pem = _bundle()
    # Prove the bundled material contains actual CA roots independently of any
    # roots already present on the host. Empty/unusable bundles fail closed.
    bundled = _strict(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    bundled.load_verify_locations(cadata=pem)
    bundled_count = _ca_count(bundled)

    context = _strict(ssl.create_default_context())
    context.load_verify_locations(cadata=pem)
    _strict(context)
    report = {
        'status': 'ready',
        'trust_source': 'system_plus_bundled_mozilla',
        'bundle_sha256': hashlib.sha256(raw).hexdigest(),
        'bundle_size_bytes': len(raw),
        'bundled_ca_count': bundled_count,
        'trusted_ca_count': _ca_count(context),
        'hostname_checking': True,
        'certificate_verification': 'CERT_REQUIRED',
        'network_requests': 0,
        'maintenance_applied': False,
    }
    return context, report


def create_update_tls_context():
    """Return a strict client context with native and packaged CA roots."""
    return _build()[0]


def trust_probe():
    """Describe the actual packaged CA trust without making a network request."""
    return _build()[1]
