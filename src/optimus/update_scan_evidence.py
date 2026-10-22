"""Bounded source evidence for app discovery, separate from installer outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
import time


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')


def app_scan_evidence(scan=None, *, native=False, started=None, finished=None,
                      selected=None, result=None, error=None, not_started=None):
    """Never turn an empty unrecognized response into a zero-update finding.

    Counts describe the last discovery response, not a promise that all installed
    software is current. Successful installers do not establish zero remaining
    offers without another discovery pass.
    """
    scan = scan if isinstance(scan, dict) else {}
    rows = scan.get('updates', scan.get('packages'))
    rows_valid = isinstance(rows, list) and len(rows) <= 1000 and all(isinstance(row, dict) for row in rows)
    source = scan.get('source')
    source = source if isinstance(source, str) and len(source) <= 120 else ('native package manager' if native else 'winget')
    code = scan.get('exitCode')
    excluded = scan.get('excluded', [])
    excluded = excluded if isinstance(excluded, list) else ['Invalid exclusion metadata.']
    reported_raw = scan.get('source_reported_offers_count')
    reported = reported_raw if type(reported_raw) is int and 0 <= reported_raw <= 1000000 else None
    counts_consistent = (reported_raw is None or
                         reported is not None and rows_valid and reported == len(rows))
    diagnostic = scan.get('stderr')
    has_diagnostic = bool(diagnostic.strip()) if isinstance(diagnostic, str) else diagnostic is not None
    parse_complete = scan.get('complete', True) is True
    complete = (not not_started and error is None and type(code) is int and code == 0
                and scan.get('structured') is True and parse_complete and rows_valid
                and not excluded and counts_consistent and not has_diagnostic and (native or source == 'winget'))
    status = ('not_started' if not_started else 'failed' if error is not None or type(code) is int and code != 0
              else 'completed' if complete else 'incomplete')
    recognized = len(rows) if rows_valid else 0
    offers = recognized if complete else None
    eligible = len(selected) if selected is not None else None
    result = result if isinstance(result, dict) else {}
    items = result.get('items', [])
    items = items if isinstance(items, list) else []
    counts = {name: sum(isinstance(row, dict) and row.get('status') == name for row in items)
              for name in ('completed', 'failed', 'skipped', 'cancelled')}
    verified = sum(isinstance(row, dict) and isinstance(row.get('result'), dict)
                   and row['result'].get('verified') is True for row in items)
    deferred = result.get('deferred_ids', [])
    deferred_count = len(deferred) if isinstance(deferred, list) else 0
    installed = scan.get('installed_packages_checked')
    installed = installed if type(installed) is int and 0 <= installed <= 1000000 else None
    coverage = ('Only applications registered with the WinGet source are covered; Store-only, portable, '
                'unregistered and publisher-only updaters may not be included.' if not native else
                'Only packages registered with this native package manager and its current repository metadata '
                'are covered; other applications and publisher-only updaters are not included.')
    if not_started:
        summary = str(not_started)[:500]
    elif complete:
        summary = f'{source}: {offers} offered update' + ('' if offers == 1 else 's') + ' found'
        if eligible is not None:
            summary += f'; {eligible} eligible under saved approvals'
        if items or deferred_count:
            summary += f'; {counts["completed"]} installer success, {counts["failed"]} failed, {counts["skipped"]} skipped, {deferred_count} deferred'
        summary += '. No more updates were offered by this source at scan time.' if offers == 0 else '.'
    else:
        summary = f'{source}: update search ' + ('failed' if status == 'failed' else 'incomplete')
        if reported is not None:
            summary += f'; {reported} offers reported by the source'
        summary += f'; {recognized} recognized offer' + ('' if recognized == 1 else 's') if rows_valid and recognized else ''
        summary += '; the number of available updates is unknown.'
    errors = []
    if error is not None:
        errors.append({'source': source, 'error': (type(error).__name__ + ': ' + str(error))[:1000]})
    if status in ('failed', 'incomplete'):
        if type(code) is int and code != 0:
            errors.append({'source': source, 'error': f'Package manager returned status {code}.'})
        for reason in excluded[:8]:
            errors.append({'source': source, 'error': str(reason)[:500]})
        if not counts_consistent:
            errors.append({'source': source, 'error': 'The source-reported offer count does not match recognized update rows.'})
        if has_diagnostic:
            errors.append({'source': source, 'error': 'The package manager reported diagnostic output; source coverage is not confirmed.'})
        if not errors:
            errors.append({'source': source, 'error': str(scan.get('note') or 'The source response was not recognized completely.')[:1000]})
    metadata = scan.get('discovery', {})
    metadata = metadata if isinstance(metadata, dict) else {}
    begin = metadata.get('started_at', started)
    end = metadata.get('completed_at', finished if finished is not None else time.time())
    valid_time = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 32503680000
    return {'schema': 1, 'complete': bool(complete), 'status': status, 'source': source,
            'criteria': ('Native manager offered updates using current repository metadata' if native else
                         'winget upgrade --source winget --disable-interactivity'),
            'started_utc': _utc(begin) if valid_time(begin) else None,
            'completed_utc': _utc(end) if valid_time(end) and not not_started else None,
            'duration_seconds': round(max(0, end - begin), 3) if valid_time(begin) and valid_time(end) else None,
            'offers_count': offers, 'recognized_offers_count': recognized,
            'source_reported_offers_count': reported,
            'eligible_count': eligible, 'installed_packages_checked': installed,
            'completed_count': counts['completed'], 'verified_count': verified,
            'failed_count': counts['failed'], 'skipped_count': counts['skipped'],
            'deferred_count': deferred_count, 'remaining_count': 0 if complete and offers == 0 else None,
            'no_more_offered': bool(complete and offers == 0), 'coverage_limit': coverage,
            'summary': summary, 'errors': errors}


def with_app_scan(result, *, native=False, started=None, selected=None, error=None, not_started=None):
    result['app_scan'] = app_scan_evidence(result.get('scan'), native=native, started=started,
        selected=selected, result=result, error=error, not_started=not_started)
    return result


def attach_app_scan_error(error, context, *, native=False):
    """Retain the last completed discovery when a later checkpoint raises.

    This adds discovery evidence only. It deliberately supplies no installer
    lifecycle claims: a callback may fail after an earlier installer ran.
    """
    if isinstance(getattr(error, 'app_scan', None), dict):
        return
    outcome = context.get('outcome')
    if isinstance(outcome, dict) and isinstance(outcome.get('app_scan'), dict):
        error.app_scan = outcome['app_scan']
    elif isinstance(context.get('scan'), dict):
        selected = context.get('selected')
        items = context.get('items', [])
        processed = {row.get('id') for row in items if isinstance(row, dict) and isinstance(row.get('id'), str)}
        selected_ids = [row if isinstance(row, str) else row.get('id') for row in (selected or [])
                        if isinstance(row, (str, dict))]
        deferred = [identity for identity in selected_ids if isinstance(identity, str) and identity not in processed]
        error.app_scan = app_scan_evidence(context['scan'], native=native, started=context.get('started'),
            selected=selected, result={'items': items, 'deferred_ids': deferred})
