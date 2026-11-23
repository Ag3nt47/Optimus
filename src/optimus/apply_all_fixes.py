"""Apply every reviewed security recommendation without opening dialogs.

Used when the saved ``apply_all_fixes_without_review`` choice is on. It does
what the Security review dialog does after the user ticks every supported row
and answers Yes to each question:

* only rows whose presentation is ``Apply`` or ``Install`` are run; ``Open`` /
  ``View`` / ``Review`` rows would launch Windows UI, so they are listed as
  manual items instead;
* Defender threat remediation gets the same consent object the dialog builds
  (all current active threat IDs plus the fresh fingerprint); if that list is
  incomplete it is skipped, exactly as in the dialog;
* after each result ``recommendation_continuation_reason`` decides whether the
  remaining rows must wait (restart pending, uncertain installer, ...).

Process actions are never applied automatically (``post_run_fixes`` rules them
out by design); they are only counted in the summary.
"""
from __future__ import annotations

import re
from typing import Any, Callable

SETTING = 'apply_all_fixes_without_review'


def remediation_consent(review: dict) -> dict | None:
    threats = review.get('threats') if isinstance(review, dict) else None
    if not isinstance(threats, dict) or threats.get('active_ids_complete') is not True:
        return None
    identities, fingerprint = threats.get('active_ids'), threats.get('active_fingerprint')
    if (not isinstance(identities, list) or not 0 < len(identities) <= 1000
            or any(type(identity) is not int or not 0 <= identity <= 2 ** 63 - 1 for identity in identities)
            or len(set(identities)) != len(identities)
            or threats.get('active_fingerprint_complete') is not True
            or not isinstance(fingerprint, str) or re.fullmatch('[0-9a-f]{64}', fingerprint) is None):
        return None
    return {'confirmed': True, 'scope': 'all_current_active_defender_threats',
            'threat_ids': sorted(identities), 'resource_fingerprint': fingerprint}


def apply_all_recommendations(backend: Any, result: dict, settings: dict, *, cancel_event: Any = None,
                              progress_callback: Callable[[dict], None] | None = None) -> dict:
    from .recommendation_presentation import (recommendation_continuation_reason, recommendation_presentation,
                                              recommendation_result_text)
    review = result.get('security_review') if isinstance(result, dict) else None
    recommendations = review.get('recommendations') if isinstance(review, dict) else None
    recommendations = [row for row in recommendations or [] if isinstance(row, dict) and isinstance(row.get('id'), str)]
    install_approved = settings.get('windows_security_updates_install_approved') is True
    apply = getattr(backend, 'apply_security_recommendation', None)
    summary = {'status': 'completed', 'applied': [], 'manual': [], 'skipped': [], 'deferred': [],
               'process_suggestions': 0}
    processes = result.get('process_review') if isinstance(result, dict) else None
    if isinstance(processes, dict):
        rows = processes.get('processes') or processes.get('items') or []
        summary['process_suggestions'] = len(rows) if isinstance(rows, list) else 0
    queue = []
    for row in recommendations:
        presentation = recommendation_presentation(row, windows_install_approved=install_approved)
        title = str(row.get('title') or row['id'])
        # Threat removal is labelled "Review" in the dialog, but this saved choice
        # explicitly covers it (with the dialog's own fresh-threat-list consent).
        runnable = presentation['selection_label'] in ('Apply', 'Install') or row['id'] == 'remediate_defender_threats'
        if not runnable or not callable(apply):
            summary['manual'].append({'id': row['id'], 'title': title, 'action': presentation['action_label']})
            continue
        queue.append((row, presentation, title))
    for index, (row, presentation, title) in enumerate(queue):
        identifier = row['id']
        if cancel_event is not None and cancel_event.is_set():
            summary['deferred'].append({'id': identifier, 'title': title, 'reason': 'Cancelled.'})
            continue
        arguments: dict[str, Any] = {'cancel_event': cancel_event}
        if identifier == 'review_windows_update':
            arguments['allow_saved_update_install'] = presentation['selection_label'] == 'Install'
        if identifier == 'remediate_defender_threats':
            consent = remediation_consent(review)
            if consent is None:
                summary['skipped'].append({'id': identifier, 'title': title,
                                           'reason': 'Fresh active-threat list required; no removal requested.'})
                continue
            arguments['remediation_consent'] = consent
        if progress_callback is not None:
            progress_callback({'current': index, 'total': len(queue), 'description': 'Applying: ' + title})
        try:
            outcome = apply(identifier, **arguments)
            text = recommendation_result_text(outcome)
        except Exception as error:  # one failure is reported, never hidden
            outcome, text = {'status': 'failed', 'error': str(error)[:300]}, 'Failed: ' + str(error)[:200]
        summary['applied'].append({'id': identifier, 'title': title, 'result': text, 'raw': outcome})
        reason = recommendation_continuation_reason(outcome)
        if reason:
            for later, _presentation, later_title in queue[index + 1:]:
                summary['deferred'].append({'id': later['id'], 'title': later_title, 'reason': reason})
            break
    if summary['deferred']:
        summary['status'] = 'deferred'
    if progress_callback is not None:
        progress_callback({'current': len(queue), 'total': len(queue), 'description': 'All fixes applied'})
    summary['text'] = summary_text(summary)
    return summary


def summary_text(summary: dict) -> str:
    lines = []
    for row in summary['applied']:
        lines.append(f"• {row['title']}: {row['result']}")
    for row in summary['skipped']:
        lines.append(f"• {row['title']}: skipped — {row['reason']}")
    for row in summary['deferred']:
        lines.append(f"• {row['title']}: deferred — {row['reason']}")
    for row in summary['manual']:
        lines.append(f"• {row['title']}: manual step ({row['action']}) — not opened automatically")
    if summary['process_suggestions']:
        lines.append(f"• {summary['process_suggestions']} process suggestion(s) listed in the report; "
                     'processes are never ended automatically.')
    head = (f"Applied {len(summary['applied'])} fix(es) without review"
            + (f", {len(summary['deferred'])} deferred" if summary['deferred'] else '')
            + (f", {len(summary['manual'])} manual" if summary['manual'] else '') + '.')
    return head + ('\n' + '\n'.join(lines) if lines else '')
