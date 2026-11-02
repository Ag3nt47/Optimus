"""The Windows collector runs against fake CIM providers, never actual hardware."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope='module')
def collector_receipts(tmp_path_factory):
    if os.name != 'nt':
        pytest.skip('Native Windows PowerShell fake-CIM fixture.')
    directory = tmp_path_factory.mktemp('hardware-health-native')
    receipts = directory / 'receipts.json'
    script = Path(__file__).with_name('Test-HardwareHealth.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(script), '-ReceiptsPath', str(receipts)],
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary['Result'] == 'PASS' and summary['Assertions'] >= 60
    assert summary['RealQueries'] == summary['RealTests'] == summary['RealRepairs'] == 0
    assert summary['RawSerialsExported'] == 0
    return json.loads(receipts.read_text(encoding='utf-8-sig'))


def test_physical_drive_identifiers_and_volume_partitions_are_separate(collector_receipts):
    disks = collector_receipts['good']['storage']
    assert disks[0]['device_id'] == '12' and disks[0]['disk_number'] == 2
    assert disks[0]['volumes'] == ['C:', 'D:']
    assert disks[1]['volumes'] == ['F:']
    assert disks[0]['reliability_source'] == 'MSFT_PhysicalDiskToStorageReliabilityCounter'
    assert disks[0]['read_errors_total_source'].endswith('.ReadErrorsTotal')


def test_provider_counters_preserve_real_zero_and_wear_beyond_estimated_life(collector_receipts):
    disks = collector_receipts['good']['storage']
    assert disks[0]['wear_percent_used'] == disks[0]['uncorrected_read_errors'] == 0
    assert disks[1]['wear_percent_used'] == 120
    assert disks[1]['counter_capabilities']['wear_percent_used'] == 'not_applicable'
    assert all('health_percent' not in disk and 'remaining_life_percent' not in disk for disk in disks)


def test_battery_capacity_ratio_is_distinct_from_current_charge_and_keeps_units(collector_receipts):
    battery = collector_receipts['good']['batteries'][0]
    assert battery['design_capacity'] == 1000 and battery['full_charge_capacity'] == 800
    assert battery['full_charge_vs_design_percent'] == 80
    assert battery['capacity_units'] == 'mWh' and battery['cycle_count'] == 20
    assert 'charge_percent' not in battery


def test_cim_battery_fallback_reports_only_its_own_documented_capacity_fields(collector_receipts):
    battery = collector_receipts['fallback']['batteries'][0]
    assert battery['source'] == 'Win32_Battery' and battery['capacity_units'] == 'mWh'
    assert battery['full_charge_vs_design_percent'] == 75
    assert battery['identity_quality'] == 'unavailable' and 'identity_hash' not in battery
    assert 'charge_percent' not in battery
    unsupported = collector_receipts['unsupported_fallback']['batteries'][0]
    assert not any(key in unsupported for key in ('full_charge_vs_design_percent', 'charge_percent', 'design_capacity'))


def test_unknown_and_all_ones_values_are_absent_rather_than_healthy_zeroes(collector_receipts):
    disk = collector_receipts['sentinels']['storage'][0]
    assert disk['reliability_available'] is False
    assert not any(field in disk for field in ('temperature_c', 'maximum_operating_temperature_c',
        'wear_percent_used', 'power_on_hours', 'uncorrected_read_errors', 'health_status'))


def test_health_native_source_keeps_inbox_queries_read_only_and_bounded():
    source = (Path(__file__).parents[1] / 'src/optimus/engine/SystemDiagnostics.ps1').read_text()
    collector = source.split('function Get-OptimusSystemDiagnostics')[0]
    assert 'Get-CimInstance' in collector and 'Get-CimAssociatedInstance' in collector
    assert "'MSFT_PhysicalDiskToStorageReliabilityCounter'" in collector or '-Association MSFT_PhysicalDiskToStorageReliabilityCounter' in collector
    assert not any(command in collector for command in ('Set-CimInstance', 'Invoke-CimMethod',
        'Remove-CimInstance', 'Reset-PhysicalDisk', 'Repair-Volume', 'Start-Process', 'smartctl'))
    assert "'/batteryreport /xml /duration 1 /output" in collector
    assert "[IO.Directory]::Delete($directory,$false)" in collector
    assert 'DtdProcessing]::Prohibit' in collector
    assert 'UseShellExecute=$false' in collector
    assert '$process.Kill()' in collector and '$process.WaitForExit(50)' in collector
    assert 'OperationTimeoutSec $seconds' in collector and 'Select-Object -First ($Limit+1)' in collector


def test_physical_hardware_identity_is_only_an_opaque_local_hash(collector_receipts):
    payload = json.dumps(collector_receipts['good'])
    assert not any(private in payload for private in ('do-not-export', 'Drive-Unique', 'FixtureBattery-instance'))
    for row in collector_receipts['good']['storage'] + collector_receipts['good']['batteries']:
        assert row['identity_quality'] == 'stable' and len(row['identity_hash']) == 64
        assert all(character in '0123456789abcdef' for character in row['identity_hash'])


def test_expanded_native_drive_counters_keep_units_provenance_and_missing_values(collector_receipts):
    disk = collector_receipts['good']['storage'][0]
    assert disk['read_errors_total'] == disk['corrected_read_errors'] == 2
    assert disk['maximum_read_latency_ms'] == 300 and disk['maximum_write_latency_ms'] == 0
    assert disk['start_stop_cycles'] == 20 and disk['rated_start_stop_cycles'] == 600000
    assert disk['manufacture_year_week'] == '2018-W42'
    assert disk['manufacture_year_week_source'].endswith('.ManufactureDate')
    assert collector_receipts['good']['storage'][1]['counter_capabilities']['uncorrected_write_errors'] == 'not_reported'


def test_inbox_battery_fallback_excludes_raw_report_identity_and_usage(collector_receipts):
    report = collector_receipts['inbox_fallback']
    assert report['capabilities']['battery_inbox_report'] == 'used'
    assert report['batteries'][0]['source'] == 'powercfg_batteryreport'
    assert report['batteries'][0]['full_charge_vs_design_percent'] == 70
    assert report['batteries'][0]['cycle_count'] == 30
    payload = json.dumps(report)
    assert not any(value in payload for value in ('private-report-serial', 'private-computer-name', 'Fixture battery model'))
