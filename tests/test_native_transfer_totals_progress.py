"""Measured transfer metadata survives native ticks without inventing size."""
from unittest.mock import Mock
import pytest

from optimus.item_progress import BoundItemProgress, normalize_item_progress
from optimus.winget_progress import WinGetProgressParser


def transfer(**fields):
    return dict(key='software:update:Vendor.App', label='Vendor.App', phase='download',
                percent=25, downloaded_bytes=256, download_total_bytes=1024,
                bytes_source='winget_native', bytes_estimated=True, **fields)


@pytest.mark.parametrize('done,total,source,approximate', [
    (0, 0, 'windows_update', False), (512, 1024, 'windows_update', False),
    (2**40, 2**41, 'winget_native', True)])
def test_valid_measured_transfer_fields_are_retained(done,total,source,approximate):
    value=transfer();value.update(downloaded_bytes=done,download_total_bytes=total,
                                 bytes_source=source,bytes_estimated=approximate)
    result=normalize_item_progress(value)
    assert result['downloaded_bytes']==done and result['download_total_bytes']==total
    assert result['bytes_source']==source and result['bytes_estimated'] is approximate


@pytest.mark.parametrize('field,value', [
    ('downloaded_bytes', True), ('downloaded_bytes', -1), ('downloaded_bytes', 2**50+1),
    ('downloaded_bytes', '256'), ('downloaded_bytes', float('nan')),
    ('download_total_bytes', 255), ('download_total_bytes', True),
    ('download_total_bytes', float('inf')), ('bytes_source', 'unknown'),
    ('bytes_estimated', 1)])
def test_malformed_transfer_metadata_never_replaces_valid_percentage(field,value):
    payload=transfer();payload[field]=value
    result=normalize_item_progress(payload)
    assert result['percent']==25 and 'downloaded_bytes' not in result


def test_install_percent_cannot_be_reinterpreted_as_transferred_or_installed_bytes():
    value=transfer();value.update(phase='install',percent=73)
    result=normalize_item_progress(value)
    assert result['percent']==73 and 'downloaded_bytes' not in result


def test_bound_native_callback_preserves_counter_and_exact_item_identity():
    seen=[];reporter=BoundItemProgress(seen.append,'driver:one','Device driver')
    reporter({'phase':'downloading','percent':32,'downloaded_bytes':320,
              'download_total_bytes':1000,'bytes_source':'windows_update'})
    current=seen[-1]['item_progress']
    assert current['key']=='driver:one' and current['downloaded_bytes']==320
    assert current['download_total_bytes']==1000 and current['bytes_estimated'] is False
    reporter.close();count=len(seen)
    reporter({'phase':'downloading','percent':90,'downloaded_bytes':900,
              'bytes_source':'windows_update'})
    assert len(seen)==count


def test_winget_independently_formatted_units_produce_a_continuous_percentage():
    events=[];parser=WinGetProgressParser(events.append)
    parser.feed('Downloading https://example.invalid/app\r512 KB / 1 MB\r0.75 MB / 1024 KB\r')
    bars=[row for row in events if 'downloaded_bytes' in row]
    assert [row['percent'] for row in bars]==[50.,75.]
    assert [row['downloaded_bytes'] for row in bars]==[512*1024,768*1024]
    assert all(row['download_total_bytes']==1024**2 for row in bars)
    assert all(row['bytes_estimated'] is True for row in bars)


def test_cached_or_percent_only_progress_does_not_invent_transfer_bytes():
    events=[];parser=WinGetProgressParser(events.append)
    parser.feed('Downloading https://example.invalid/app\r25%\r100%\r'
                'Successfully verified installer hash\nStarting package install...\n73%\r')
    assert any(row['phase']=='install' and row['percent']==73 for row in events)
    assert all('downloaded_bytes' not in row for row in events)
    assert all(row['percent']!=100 for row in events)


def test_driver_terminal_download_receipt_is_bound_to_exact_offer():
    from optimus.backend import Backend
    backend=Backend.__new__(Backend);backend.bridge=Mock()
    backend.preferences=Mock(return_value={})
    payload={'phase':'downloading','percent':99,'downloaded_bytes':200,
             'download_total_bytes':200,'bytes_source':'windows_update','bytes_estimated':False}
    backend.bridge.request.return_value={'resultCode':2,'download_progress':payload}
    result=backend.driver_update_apply_unattended({'id':'driver-guid','revision':4,'title':'GPU'},accept_eula=True)
    observed=result['download_progress']
    assert observed['key']=='driver:update:driver-guid:4' and observed['label']=='GPU'
    assert observed['downloaded_bytes']==200
    assert 'key' not in payload
