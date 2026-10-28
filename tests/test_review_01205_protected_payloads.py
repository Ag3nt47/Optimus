"""Adversarial payload fixtures; no host cache or installers are accessed."""
from pathlib import Path
import hashlib

from optimus.update_payloads import PayloadWorkspace


def test_inno_uninstaller_is_not_a_disposable_download(tmp_path):
    data = tmp_path / 'Data'
    data.mkdir()
    attempt = PayloadWorkspace.create(data, 'winget', 'Fixture.Inno')
    payload = Path(attempt.env_overrides()['TEMP'])
    uninstaller = payload / 'unins000.exe'
    uninstaller.write_bytes(b'fixture recovery executable, not a download')
    installer = payload / 'installer.exe'
    installer.write_bytes(b'catalog-verified download fixture')
    download_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
    receipt = attempt.finish({'exitCode': 0, 'installer_started': True,
        'outcomeKnown': True, 'running': False}, verified=True,
        quiescent=True, restart_pending=False, download_hashes=(download_hash,))
    assert uninstaller.exists(), 'A standard Inno uninstaller must stay out of download cleanup'
    assert not installer.exists(), 'The positive catalog-hash deletion path must have run'
    assert receipt['workspace_owned'] is True
    assert receipt['removed_files'] == 1
    assert receipt['removed_bytes'] == len(b'catalog-verified download fixture')
