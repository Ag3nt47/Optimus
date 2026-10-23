"""Explicit retention choices and review text; no files or services are accessed."""

RETENTION_SCOPES = (
    ('codex_archives', 'Delete archived Codex chats (personal history; irreversible)',
     'Archived chats contain personal conversation history. Deletion is permanent and has no automatic undo. '
     'Only supported, verified archived conversations older than your selected age are eligible; active and unarchived chats stay.'),
    ('angerona_logs', 'Remove old inactive Angerona logs',
     'Only eligible inactive log files in verified Angerona log locations are included. Active, open, protected and unrecognized files are skipped.'),
    ('failed_downloads', 'Remove incomplete download leftovers',
     'Only old top-level .crdownload, .part and .partial fragments in the verified Downloads folder are eligible. '
     'This also discards old paused downloads. Completed downloads and installed software are preserved; active or unknown download state is skipped.'),
)


def validate_retention_scope(settings, *, require_all=False):
    for prefix, title, _ in RETENTION_SCOPES:
        enabled, age = prefix + '_cleanup_enabled', prefix + '_retention_days'
        if require_all and (enabled not in settings or age not in settings):
            raise ValueError('Review and save the current retention cleanup choices before approving unattended Auto Clean.')
        if type(settings.get(enabled, False)) is not bool:
            raise ValueError(title + ' must be explicitly on or off.')
        value = settings.get(age, 7)
        if type(value) is not int or not 1 <= value <= 3650:
            raise ValueError(title + ' requires an independent retention age from 1 to 3650 days.')
    return True


def retention_actions(settings):
    """Describe only explicitly enabled deletion scopes for setup and launch."""
    descriptions = {
        'codex_archives': 'Permanently delete verified archived Codex conversations older than {age} day(s). '
            'This removes personal conversation history with no automatic undo; active and unarchived chats are preserved.',
        'angerona_logs': 'Permanently remove eligible inactive Angerona log files older than {age} day(s), '
            'only in verified supported log locations. Active/open and protected files are skipped.',
        'failed_downloads': 'Permanently remove supported incomplete-download leftovers older than {age} day(s), '
            'only top-level .crdownload, .part and .partial fragments in the verified Downloads folder. '
            'Old paused-download fragments are also discarded. Completed downloads, installed software and active/unknown downloads are preserved.',
    }
    return [descriptions[prefix].format(age=settings.get(prefix + '_retention_days', 7))
        for prefix, _, _ in RETENTION_SCOPES if settings.get(prefix + '_cleanup_enabled') is True]
