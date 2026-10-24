"""Consume eligible reboot intents within one separately approved full workflow.

An optimize request already authorizes its selected maintenance scope. A saved
reboot plan authorizes only update continuation. This wrapper joins those two
receipts around one fresh workflow, never an extra queue before/after cleanup.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .automation import RunLease
from .update_resume import UpdateResumeService, _scope


def run_with_reboot_resume(backend, settings, progress_callback, cancel_event=None, *, browser_prompt=None):
    """Run an approved optimization once; resume never starts a second queue.

    Disabled mode follows the original entry point without a resume directory,
    boot probe or extra lease. Enabled mode owns the lease until both the full
    workflow and any claimed continuation receipt are durably finalized.
    """
    from .workflow import Preferences, atomic_json
    validator = getattr(getattr(backend, 'preference_store', None), 'validate', Preferences.validate)
    configuration = validator(settings)
    if configuration.get('toolkit_settings', {}).get('resume', {}).get('enabled') is not True:
        return backend.workflow.run(configuration, progress_callback, cancel_event, browser_prompt=browser_prompt)
    with RunLease(backend.data_dir) as lease:
        service = UpdateResumeService(backend)
        executed, workflow_result = False, None
        saved_configuration = validator(backend.preferences())
        try:
            saved_scope = _scope(saved_configuration)[1]
            requested_scope = _scope(configuration)[1]
            claim_allowed = saved_scope == requested_scope
        except (ValueError, TypeError):
            claim_allowed = False
        claiming_intent = False
        callback_entered = False

        def fresh_workflow(current_configuration):
            nonlocal executed, workflow_result, callback_entered
            callback_entered = True
            if executed:
                raise RuntimeError('This maintenance request already ran its update queue.')
            if claiming_intent and validator(backend.preferences()) != saved_configuration:
                raise ValueError('Saved maintenance approvals changed before reboot continuation could start. Review the current choices on a fresh run.')
            executed = True
            workflow_result = backend.workflow.run(current_configuration, progress_callback,
                cancel_event, browser_prompt=browser_prompt, _lease=lease)
            return workflow_result

        try:
            if claim_allowed:
                claiming_intent = True
                continuation = service.resume(configuration, fresh_workflow, lease=lease, maintenance_approved=True)
                claiming_intent = False
            else:
                continuation = {'status': 'approval_changed',
                    'reason': 'The requested update scope differs from the current saved approvals; no reboot intent was consumed.'}
        except Exception as error:
            if executed or callback_entered:
                raise  # Preserve any started intent and its real unknown outcome.
            claiming_intent = False
            continuation = {'status': 'unavailable', 'reason': type(error).__name__ + ': ' + str(error)[:500]}
        if not executed:
            # No eligible intent does not cancel a separately approved optimize
            # request. Its ordinary native barriers still guard all mutations.
            fresh_workflow(configuration)
        if isinstance(workflow_result, dict):
            metadata = {key: copy.deepcopy(value) for key, value in continuation.items() if key != 'result'}
            metadata.update(schema=1, workflow_runs_started=1, additional_update_queues_started=0,
                maintenance_authorization='Current explicit optimize request; the reboot intent does not authorize additional cleanup.')
            workflow_result['automatic_reboot_resume'] = metadata
            path = workflow_result.get('log_path')
            identifier = workflow_result.get('id')
            if (isinstance(path, str) and isinstance(identifier, str)
                    and Path(path) == Path(backend.data_dir) / 'runs' / (identifier + '.json')):
                atomic_json(Path(path), workflow_result)
        return workflow_result
