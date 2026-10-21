"""Validation for saved, target-scoped maintenance approvals."""
from __future__ import annotations
import re

APPROVAL_DEFAULTS = {
    'duplicate_policy': 'review', 'duplicate_changes_approved': False,
    'duplicate_max_recycle': 25, 'duplicate_min_age_days': 7,
    'cloud_policy': 'review', 'cloud_changes_approved': False,
    'cloud_drive_id': '', 'cloud_folder_ids': [],
    'cloud_max_recycle': 25, 'cloud_min_age_days': 7,
    'startup_policy': 'review', 'startup_changes_approved': False,
    'startup_approved_entries': [],
    'driver_policy': 'review', 'driver_changes_approved': False,
    'driver_update_agreements_accepted': False, 'driver_approved_updates': [],
    'driver_approved_devices': [],
}
_CLOUD_ID = re.compile(r'^[A-Za-z0-9_!.\-]{1,256}$')


def validate_approvals(settings):
    """Return normalized scopes; only a saved current setup can enable applies."""
    from .automation import CURRENT_SETUP_VERSION
    from .automated_system import validate_startup_approvals, validate_driver_approvals, validate_driver_devices
    for key in ('duplicate_policy', 'cloud_policy'):
        if settings[key] not in ('review', 'keep_oldest', 'keep_newest'):
            raise ValueError('Choose review, keep oldest, or keep newest for duplicate recycling.')
    if settings['startup_policy'] not in ('review', 'disable_selected'):
        raise ValueError('Choose review or disable selected startup entries.')
    if settings['driver_policy'] not in ('review', 'install_approved', 'install_matching'):
        raise ValueError('Choose review, selected driver updates, or approved device rules.')
    for prefix in ('duplicate', 'cloud'):
        maximum, age = settings[prefix + '_max_recycle'], settings[prefix + '_min_age_days']
        if type(maximum) is not int or not 1 <= maximum <= 500:
            raise ValueError('Recycling limits must be between 1 and 500 files per run.')
        if type(age) is not int or not 1 <= age <= 3650:
            raise ValueError('Minimum duplicate age must be between 1 and 3650 days.')
    drive = settings['cloud_drive_id']
    if not isinstance(drive, str) or (drive and not _CLOUD_ID.fullmatch(drive)):
        raise ValueError('Use a verified personal OneDrive drive identity.')
    folders = settings['cloud_folder_ids']
    if type(folders) is not list or len(folders) > 20 or any(not isinstance(item, str) or not _CLOUD_ID.fullmatch(item) or item.casefold() == 'root' for item in folders):
        raise ValueError('Select at most 20 verified concrete OneDrive folder identities.')
    if len(set(folders)) != len(folders):
        raise ValueError('Approved OneDrive folder identities must be unique.')
    settings['startup_approved_entries'] = validate_startup_approvals(settings['startup_approved_entries'])
    settings['driver_approved_updates'] = validate_driver_approvals(settings['driver_approved_updates'])
    settings['driver_approved_devices'] = validate_driver_devices(settings['driver_approved_devices'])
    actions = (
        ('duplicates_enabled', 'duplicate_policy', 'duplicate_changes_approved'),
        ('cloud_review_enabled', 'cloud_policy', 'cloud_changes_approved'),
        ('startup_review_enabled', 'startup_policy', 'startup_changes_approved'),
        ('driver_updates_enabled', 'driver_policy', 'driver_changes_approved'),
    )
    for enabled, policy, approval in actions:
        if settings[enabled] and settings[policy] != 'review':
            if settings['setup_version'] != CURRENT_SETUP_VERSION or settings[approval] is not True:
                raise ValueError('Complete the updated setup and explicitly approve each automatic change.')
    if settings['duplicates_enabled'] and settings['duplicate_policy'] != 'review' and not settings['duplicate_roots']:
        raise ValueError('Approve at least one local folder before automatic duplicate recycling.')
    if settings['cloud_review_enabled'] and settings['cloud_policy'] != 'review' and (not drive or not folders):
        raise ValueError('Bind automatic cloud recycling to a verified drive and at least one folder.')
    if settings['startup_review_enabled'] and settings['startup_policy'] != 'review' and not settings['startup_approved_entries']:
        raise ValueError('Select exact startup registrations before automatic disabling.')
    if settings['driver_updates_enabled'] and settings['driver_policy'] != 'review':
        if settings['driver_update_agreements_accepted'] is not True:
            raise ValueError('Approve the driver update agreements before automatic installation.')
        key = 'driver_approved_updates' if settings['driver_policy'] == 'install_approved' else 'driver_approved_devices'
        if not settings[key]:
            raise ValueError('Select exact driver updates or complete device/provider rules.')
    return settings
