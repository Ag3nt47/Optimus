from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from optimus.apply_all_fixes import apply_all_recommendations, remediation_consent  # noqa: E402
from optimus.workflow import DEFAULTS  # noqa: E402

FINGERPRINT = 'a' * 64


def result(*ids, threats=True):
    review = {'recommendations': [{'id': identifier, 'title': identifier.replace('_', ' ')} for identifier in ids]}
    if threats:
        review['threats'] = {'active_ids_complete': True, 'active_ids': [7, 3], 'active_fingerprint': FINGERPRINT,
                             'active_fingerprint_complete': True}
    return {'status': 'completed', 'security_review': review, 'process_review': {'processes': [{}, {}]}}


class Backend:
    def __init__(self, outcomes=None):
        self.calls, self.outcomes = [], outcomes or {}

    def apply_security_recommendation(self, identifier, **kwargs):
        self.calls.append((identifier, kwargs))
        return self.outcomes.get(identifier, {'status': 'completed', 'outcomeKnown': True, 'running': False})


class ApplyAllTests(unittest.TestCase):
    def test_setting_defaults_off(self):
        self.assertIs(DEFAULTS['apply_all_fixes_without_review'], False)

    def test_applies_every_supported_row_and_lists_the_rest(self):
        from optimus.recommendation_presentation import recommendation_presentation
        ids = ['enable_defender_realtime', 'remediate_defender_threats', 'open_windows_security', 'unknown_thing']
        supported = [i for i in ids if recommendation_presentation({'id': i})['selection_label'] in ('Apply', 'Install')
                     or i == 'remediate_defender_threats']
        backend = Backend()
        summary = apply_all_recommendations(backend, result(*ids), {})
        self.assertEqual([call[0] for call in backend.calls], supported)
        self.assertEqual(supported, ['enable_defender_realtime', 'remediate_defender_threats'])
        self.assertEqual({row['id'] for row in summary['manual']}, {'open_windows_security', 'unknown_thing'})
        self.assertEqual(summary['process_suggestions'], 2)
        self.assertIn('never ended automatically', summary['text'])
        for identifier, kwargs in backend.calls:
            if identifier == 'remediate_defender_threats':
                self.assertEqual(kwargs['remediation_consent']['threat_ids'], [3, 7])

    def test_threat_removal_skipped_without_a_complete_threat_list(self):
        backend = Backend()
        summary = apply_all_recommendations(backend, result('remediate_defender_threats', threats=False), {})
        self.assertEqual(backend.calls, [])
        self.assertIsNone(remediation_consent({}))
        self.assertIn('Fresh active-threat list', summary['skipped'][0]['reason'])

    def test_restart_required_defers_remaining_rows(self):
        from optimus.recommendation_presentation import recommendation_presentation
        ids = [i for i in ('enable_defender_realtime', 'enable_firewall', 'update_defender_signatures', 'remediate_defender_threats')
               if recommendation_presentation({'id': i})['selection_label'] in ('Apply', 'Install')]
        if len(ids) < 2:
            self.skipTest('needs two supported recommendation identifiers')
        backend = Backend({ids[0]: {'status': 'completed', 'restartNeeded': True, 'outcomeKnown': True, 'running': False}})
        summary = apply_all_recommendations(backend, result(*ids), {})
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(summary['status'], 'deferred')
        self.assertEqual(len(summary['deferred']), len(ids) - 1)

    def test_cancel_and_exceptions_are_reported(self):
        class Boom(Backend):
            def apply_security_recommendation(self, identifier, **kwargs):
                raise RuntimeError('policy blocked')
        from optimus.recommendation_presentation import recommendation_presentation
        ids = [i for i in ('enable_defender_realtime', 'enable_firewall') if recommendation_presentation({'id': i})['selection_label'] == 'Apply']
        if not ids:
            self.skipTest('needs a supported recommendation identifier')
        summary = apply_all_recommendations(Boom(), result(ids[0]), {})
        self.assertIn('policy blocked', summary['applied'][0]['result'])
        cancel = threading.Event()
        cancel.set()
        backend = Backend()
        summary = apply_all_recommendations(backend, result(*ids), {}, cancel_event=cancel)
        self.assertEqual(backend.calls, [])
        self.assertEqual(len(summary['deferred']), len(ids))


if __name__ == '__main__':
    unittest.main()
