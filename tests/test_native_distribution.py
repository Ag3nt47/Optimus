"""Native artifact identity and packaging guards; never build or alter the OS."""
import hashlib
import importlib.util
import json
import plistlib
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest


TOOLS = Path(__file__).resolve().parents[1] / 'tools'


def load_tool(name):
    spec = importlib.util.spec_from_file_location('test_distribution_' + name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOLS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(TOOLS))
    return module


build = load_tool('build_native')
publish = load_tool('publish_release')


@pytest.mark.parametrize('system,machine,expected', [
    ('win32', 'AMD64', 'Windows-x64'), ('darwin', 'arm64', 'macOS-arm64'),
    ('darwin', 'x86_64', 'macOS-x64'), ('linux', 'x86_64', 'Linux-x86_64')])
def test_artifact_identity_uses_actual_platform_architecture(system, machine, expected):
    assert build.target_name(system, machine) == expected
    command = build.build_command(Path('project'), Path('dist'), system, machine)
    assert '--collect-submodules' in command
    assert 'send2trash' in command
    assert command[command.index('--collect-data') + 1] == 'certifi'
    if system == 'darwin':
        assert command[command.index('--target-arch') + 1] == machine


@pytest.mark.parametrize('system,machine', [('win32', 'arm64'), ('linux', 'arm64'), ('freebsd', 'x86_64')])
def test_unsupported_builds_cannot_be_labeled_as_a_published_platform(system, machine):
    with pytest.raises(ValueError, match='No published native build'):
        build.target_name(system, machine)


def test_mac_payload_uses_bundle_resources_and_the_real_binary(tmp_path):
    bundle, resources, executable = build.bundle_paths(tmp_path, 'darwin')
    assert bundle.name == 'Optimus.app'
    assert resources == bundle / 'Contents/Resources'
    assert executable == bundle / 'Contents/MacOS/Optimus'


def test_linux_archive_keeps_executable_permission(tmp_path):
    bundle = tmp_path / 'Optimus'
    bundle.mkdir()
    executable = bundle / 'Optimus'
    executable.write_bytes(b'fixture only')
    executable.chmod(0o755)
    archive_path = build.archive_bundle(bundle, tmp_path / 'assets', 'linux', 'x86_64')
    with tarfile.open(archive_path) as archive:
        info = archive.getmember('Optimus/Optimus')
        assert info.isfile()
        # Windows does not retain a POSIX executable bit; the real Linux
        # runner additionally validates this invariant on its native FS.
        if sys.platform != 'win32':
            assert info.mode & 0o111


def test_source_asset_excludes_local_data_and_credentials(tmp_path):
    source = tmp_path / 'project'
    (source / 'src').mkdir(parents=True)
    (source / 'src/app.py').write_text('VALUE = 1\n')
    (source / 'src/.env').write_text('PRIVATE_FIXTURE=1')
    (source / 'private').mkdir()
    (source / 'private/report.json').write_text('{}')
    archive_path = build.write_source_archive(source, tmp_path / 'assets')
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ['Optimus-Source/', 'Optimus-Source/src/', 'Optimus-Source/src/app.py']


def test_release_requires_all_native_assets_and_matching_bytes(tmp_path):
    for name in publish.ASSETS - {'SHA256SUMS.txt'}:
        (tmp_path / name).write_bytes(name.encode('ascii'))
    build.write_checksums(tmp_path)
    assert {path.name for path in publish.validated_assets(tmp_path)} == publish.ASSETS
    (tmp_path / 'Optimus-macOS-arm64.zip').write_bytes(b'changed fixture')
    with pytest.raises(ValueError, match='does not match'):
        publish.validated_assets(tmp_path)


def test_missing_native_target_blocks_release_publication(tmp_path):
    (tmp_path / 'Optimus-Windows-x64.zip').write_bytes(b'fixture')
    with pytest.raises(ValueError, match='exactly these files'):
        publish.validated_assets(tmp_path)


def test_checksums_are_for_archives_not_the_previous_checksum_file(tmp_path):
    (tmp_path / 'one.zip').write_bytes(b'one')
    (tmp_path / 'SHA256SUMS.txt').write_text('old fixture')
    result = build.write_checksums(tmp_path)
    assert result.read_text() == hashlib.sha256(b'one').hexdigest() + '  one.zip\n'


def ready_tls_payload():
    return {'schema': 1, 'command': 'tls-probe', 'status': 'ready',
            'trust_source': 'system_plus_bundled_mozilla', 'bundle_sha256': 'a' * 64,
            'bundle_size_bytes': 1000, 'bundled_ca_count': 10, 'trusted_ca_count': 12,
            'hostname_checking': True, 'certificate_verification': 'CERT_REQUIRED',
            'network_requests': 0, 'maintenance_applied': False}


def fake_packaged_commands(monkeypatch, payload, tls_payload=None):
    commands = []
    # Keep standalone fixture runs independent of platform.machine()'s native
    # subprocess fallback while subprocess.run is intentionally mocked below.
    monkeypatch.setattr(build, 'target_name', lambda: 'Fixture-native-target')
    def run(argv, **options):
        commands.append((argv, options))
        if '--screenshot' in argv:
            image = Path(argv[argv.index('--screenshot') + 1])
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b'disposable screenshot fixture')
        if '--process-scan' in argv:
            destination = Path(argv[argv.index('--output') + 1])
            destination.mkdir(parents=True)
            (destination / 'fixture.json').write_text(json.dumps(payload), encoding='utf-8')
        if '--tls-probe' in argv:
            destination = Path(argv[argv.index('--output') + 1])
            destination.mkdir(parents=True)
            (destination / 'tls-trust-fixture.json').write_text(json.dumps(
                ready_tls_payload() if tls_payload is None else tls_payload), encoding='utf-8')
            assert Path(options['env']['SSL_CERT_FILE']).read_bytes() == b''
            assert not list(Path(options['env']['SSL_CERT_DIR']).iterdir())
            assert 'REQUESTS_CA_BUNDLE' not in options['env']
            assert 'CURL_CA_BUNDLE' not in options['env']
    monkeypatch.setattr(build.subprocess, 'run', run)
    return commands


def test_packaged_verification_uses_only_read_only_commands_and_real_report(monkeypatch, tmp_path):
    calls = fake_packaged_commands(monkeypatch, {'processes': [{'pid': 123}], 'complete': False,
                                               'actions': [{'selected': False}]})
    result = build.verify_package(tmp_path / 'Optimus', output=tmp_path / 'evidence.json')
    assert [arguments[1] for arguments, _ in calls] == ['--smoke-test', '--startup-probe', '--process-scan', '--tls-probe']
    assert all(options['check'] and options['timeout'] == 150 for _, options in calls)
    assert result['process_rows'] == 1 and result['inventory_complete'] == [False]
    assert result['maintenance_applied'] is False
    assert result['tls_trust'] == ready_tls_payload()
    assert result['checks'][-1] == 'bundled TLS trust probe'
    assert '--screenshot' in calls[0][0] and (tmp_path / 'evidence.png').is_file()
    assert json.loads((tmp_path / 'evidence.json').read_text()) == result


def test_macos_bundle_declares_browser_consent_and_real_release_version(tmp_path):
    bundle = tmp_path / 'Optimus.app'
    (bundle / 'Contents').mkdir(parents=True)
    path = bundle / 'Contents/Info.plist'
    path.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'io.github.Ag3nt47.Optimus'}))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nversion="0.7.0"\n', encoding='utf-8')
    build.configure_macos_metadata(bundle, source)
    value = plistlib.loads(path.read_bytes())
    assert value['CFBundleShortVersionString'] == '0.7.0'
    assert value['CFBundleVersion'] == '0.7.0'
    assert 'Close and clean' in value['NSAppleEventsUsageDescription']


@pytest.mark.parametrize('payload,error', [
    ({'processes': [], 'actions': []}, 'no process inventory'),
    ({'processes': [{'pid': 123}], 'actions': [{'selected': True}]}, 'preselected a mutation')])
def test_packaged_verification_rejects_empty_inventory_or_preselected_changes(monkeypatch, tmp_path, payload, error):
    fake_packaged_commands(monkeypatch, payload)
    with pytest.raises(RuntimeError, match=error):
        build.verify_package(tmp_path / 'Optimus')


@pytest.mark.parametrize('key,value', [('hostname_checking', False),
    ('certificate_verification', 'CERT_NONE'), ('bundle_sha256', 'unknown'),
    ('bundled_ca_count', 0), ('trusted_ca_count', False), ('bundle_size_bytes', 0),
    ('network_requests', True), ('network_requests', 1), ('maintenance_applied', True),
    ('trust_source', 'host_only'), ('schema', True)])
def test_packaged_verification_rejects_tls_trust_without_strict_bundled_evidence(monkeypatch, tmp_path, key, value):
    tls = ready_tls_payload()
    tls[key] = value
    fake_packaged_commands(monkeypatch, {'processes': [{'pid': 123}]}, tls)
    with pytest.raises(RuntimeError, match='strict bundled certificate trust'):
        build.verify_package(tmp_path / 'Optimus')


def test_tls_probe_uses_isolated_empty_default_routes_without_mutating_builder_environment(tmp_path):
    original = {'HOME': 'fixture-home', 'SSL_CERT_FILE': 'host-ca.pem',
                'SSL_CERT_DIR': 'host-ca-dir', 'REQUESTS_CA_BUNDLE': 'requests-ca',
                'CURL_CA_BUNDLE': 'curl-ca', 'UNCHANGED': 'yes'}
    snapshot = dict(original)
    result = build._tls_probe_environment(original, tmp_path)
    assert original == snapshot
    assert result['HOME'] == 'fixture-home' and result['UNCHANGED'] == 'yes'
    assert Path(result['SSL_CERT_FILE']).read_bytes() == b''
    assert not list(Path(result['SSL_CERT_DIR']).iterdir())
    assert 'REQUESTS_CA_BUNDLE' not in result and 'CURL_CA_BUNDLE' not in result


@pytest.mark.parametrize('contents', [b'not json', b'x' * 16385])
def test_tls_report_is_bounded_and_malformed_evidence_cannot_pass(tmp_path, contents):
    (tmp_path / 'tls-trust-fixture.json').write_bytes(contents)
    with pytest.raises(RuntimeError, match='JSON|input limit'):
        build._verified_tls_report(tmp_path)


def test_missing_or_ambiguous_tls_report_cannot_pass(tmp_path):
    with pytest.raises(RuntimeError, match='one ordinary report'):
        build._verified_tls_report(tmp_path)
    for name in ('one', 'two'):
        (tmp_path / ('tls-trust-' + name + '.json')).write_text(json.dumps(ready_tls_payload()))
    with pytest.raises(RuntimeError, match='one ordinary report'):
        build._verified_tls_report(tmp_path)


def release_files(tmp_path):
    for name in publish.ASSETS - {'SHA256SUMS.txt'}:
        (tmp_path / name).write_bytes(name.encode('ascii'))
    build.write_checksums(tmp_path)
    return tmp_path


def fake_github_cli(monkeypatch, directory, *, existing=None, draft=False, different=False, fail_upload=False):
    commands = []
    def run(argv, **options):
        commands.append(argv)
        operation = argv[2]
        if operation == 'view':
            return SimpleNamespace(returncode=1 if existing is None else 0,
                stdout=json.dumps({'assets': [{'name': name} for name in existing or []], 'isDraft': draft}))
        if operation == 'download':
            name = argv[argv.index('--pattern') + 1]
            target = Path(argv[argv.index('--dir') + 1]) / name
            target.write_bytes(b'conflicting remote fixture' if different else (directory / name).read_bytes())
        if operation == 'upload' and fail_upload:
            raise publish.subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(publish.subprocess, 'run', run)
    return commands


def test_release_stays_draft_until_every_asset_uploads(monkeypatch, tmp_path):
    directory = release_files(tmp_path)
    commands = fake_github_cli(monkeypatch, directory)
    assert publish.main(['--tag', 'v0.7.0', '--assets', str(directory), '--repo', 'Fixture/Optimus']) == 0
    operations = [command[2] for command in commands]
    assert operations == ['view', 'create', *(['upload'] * len(publish.ASSETS)), 'edit']
    assert '--draft' in commands[1] and '--verify-tag' in commands[1]
    assert '--draft=false' in commands[-1] and '--latest' in commands[-1]


def test_release_upload_failure_does_not_publish_incomplete_downloads(monkeypatch, tmp_path):
    directory = release_files(tmp_path)
    commands = fake_github_cli(monkeypatch, directory, fail_upload=True)
    with pytest.raises(publish.subprocess.CalledProcessError):
        publish.main(['--tag', 'v0.7.0', '--assets', str(directory)])
    assert '--draft' in commands[1]
    assert all(command[2] != 'edit' for command in commands)


def test_release_conflict_is_checked_before_any_missing_asset_upload(monkeypatch, tmp_path):
    directory = release_files(tmp_path)
    commands = fake_github_cli(monkeypatch, directory, existing=['Optimus-Windows-x64.zip'], different=True)
    with pytest.raises(RuntimeError, match='refusing to overwrite'):
        publish.main(['--tag', 'v0.7.0', '--assets', str(directory)])
    assert [command[2] for command in commands] == ['view', 'download']


def test_published_matching_release_retry_has_no_mutations(monkeypatch, tmp_path):
    directory = release_files(tmp_path)
    commands = fake_github_cli(monkeypatch, directory, existing=sorted(publish.ASSETS))
    assert publish.main(['--tag', 'v0.7.0', '--assets', str(directory)]) == 0
    assert [command[2] for command in commands] == ['view', *(['download'] * len(publish.ASSETS))]
