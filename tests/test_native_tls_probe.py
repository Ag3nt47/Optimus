"""Offline TLS CLI fixtures never initialize app services or run maintenance."""
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app, tls_context


def ready_probe():
    return {'status': 'ready', 'trust_source': 'system_plus_bundled_mozilla',
            'bundle_sha256': 'a' * 64, 'bundle_size_bytes': 1234,
            'bundled_ca_count': 10, 'trusted_ca_count': 12,
            'hostname_checking': True, 'certificate_verification': 'CERT_REQUIRED',
            'network_requests': 0, 'maintenance_applied': False}


@pytest.fixture
def forbidden_services(monkeypatch):
    import optimus.backend
    import optimus.native_backend
    import optimus.elevation
    import optimus.gui_instance
    deny = Mock(side_effect=AssertionError('A TLS probe must not initialize application services.'))
    monkeypatch.setattr(optimus.backend, 'Backend', deny)
    monkeypatch.setattr(optimus.native_backend, 'NativeBackend', deny)
    monkeypatch.setattr(optimus.elevation, 'startup_administrator', deny)
    monkeypatch.setattr(optimus.gui_instance, 'GuiInstance', deny)
    monkeypatch.setattr(app, '_run_gui', deny)
    return deny


@pytest.mark.parametrize('system', ['win32', 'darwin', 'linux', 'freebsd'])
def test_tls_probe_dispatches_before_backend_gui_uac_and_platform_gate(monkeypatch, capsys, forbidden_services, system):
    probe = Mock(return_value=ready_probe())
    monkeypatch.setattr(tls_context, 'trust_probe', probe)
    monkeypatch.setattr(app.sys, 'platform', system)
    assert app.main(['--tls-probe']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value == dict(ready_probe(), schema=1, command='tls-probe')
    probe.assert_called_once_with()
    forbidden_services.assert_not_called()


def test_tls_probe_report_is_small_exclusive_and_does_not_create_app_data(monkeypatch, tmp_path, capsys, forbidden_services):
    monkeypatch.setattr(tls_context, 'trust_probe', lambda: ready_probe())
    report_directory = tmp_path / 'explicit-report'
    assert app.main(['--tls-probe', '--output', str(report_directory)]) == 0
    value = json.loads(capsys.readouterr().out)
    reports = list(report_directory.glob('tls-trust-*.json'))
    assert len(reports) == 1 and value['report'] == str(reports[0])
    assert reports[0].stat().st_size < 16384
    assert json.loads(reports[0].read_text()) == dict(ready_probe(), schema=1, command='tls-probe')
    assert list(tmp_path.iterdir()) == [report_directory]
    forbidden_services.assert_not_called()


def test_real_cli_probe_proves_bundled_roots_with_empty_default_routes_and_no_network(monkeypatch, tmp_path, capsys, forbidden_services):
    empty = tmp_path / 'empty-default.pem'
    empty.write_bytes(b'')
    empty_directory = tmp_path / 'empty-default-directory'
    empty_directory.mkdir()
    monkeypatch.setenv('SSL_CERT_FILE', str(empty))
    monkeypatch.setenv('SSL_CERT_DIR', str(empty_directory))
    deny_network = Mock(side_effect=AssertionError('The TLS probe must never connect to a network.'))
    monkeypatch.setattr(socket, 'create_connection', deny_network)
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    assert app.main(['--tls-probe', '--output', str(tmp_path / 'report')]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == 'ready' and value['bundled_ca_count'] > 0
    assert value['trusted_ca_count'] > 0 and len(value['bundle_sha256']) == 64
    assert value['hostname_checking'] is True and value['certificate_verification'] == 'CERT_REQUIRED'
    assert value['network_requests'] == 0 and value['maintenance_applied'] is False
    deny_network.assert_not_called()
    forbidden_services.assert_not_called()


def test_existing_probe_report_is_never_overwritten(monkeypatch, tmp_path, capsys, forbidden_services):
    monkeypatch.setattr(tls_context, 'trust_probe', lambda: ready_probe())
    monkeypatch.setattr(app.uuid, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    old = tmp_path / ('tls-trust-' + 'a' * 32 + '.json')
    old.write_bytes(b'preserve existing report')
    assert app.main(['--tls-probe', '--output', str(tmp_path)]) == 2
    assert old.read_bytes() == b'preserve existing report'
    assert json.loads(capsys.readouterr().err)['status'] == 'error'
    forbidden_services.assert_not_called()


@pytest.mark.parametrize('key,value', [('status', 'unknown'), ('hostname_checking', False),
    ('certificate_verification', 'CERT_NONE'), ('bundled_ca_count', 0),
    ('trusted_ca_count', True), ('network_requests', True), ('network_requests', 1),
    ('maintenance_applied', True), ('bundle_sha256', 'invalid'),
    ('bundle_size_bytes', 16 * 1024 * 1024 + 1)])
def test_unproven_tls_result_fails_without_app_initialization(monkeypatch, tmp_path, capsys, forbidden_services, key, value):
    payload = ready_probe()
    payload[key] = value
    monkeypatch.setattr(tls_context, 'trust_probe', lambda: payload)
    output = tmp_path / 'not-created'
    assert app.main(['--tls-probe', '--output', str(output)]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result['status'] == 'error'
    assert result['network_requests'] == 0 and result['maintenance_applied'] is False
    assert not output.exists()
    forbidden_services.assert_not_called()


@pytest.mark.parametrize('error', [ImportError('certifi missing'), OSError('bundle missing'), ValueError('no CA roots')])
def test_probe_dependency_failure_exits_without_gui_or_preferences(monkeypatch, capsys, forbidden_services, error):
    monkeypatch.setattr(tls_context, 'trust_probe', Mock(side_effect=error))
    assert app.main(['--tls-probe']) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'
    forbidden_services.assert_not_called()


def test_oversized_probe_result_is_not_exported(monkeypatch, tmp_path, capsys, forbidden_services):
    payload = dict(ready_probe(), unexpected_padding='x' * 17000)
    monkeypatch.setattr(tls_context, 'trust_probe', lambda: payload)
    assert app.main(['--tls-probe', '--output', str(tmp_path / 'unused')]) == 2
    assert 'report limit' in json.loads(capsys.readouterr().err)['error']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('other', [['--smoke-test'], ['--startup-probe'], ['--audit'],
    ['--self-update-worker', 'a' * 32], ['--self-update-resume', 'a' * 32]])
def test_tls_probe_cannot_be_combined_with_an_app_action(other):
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--tls-probe', *other])
