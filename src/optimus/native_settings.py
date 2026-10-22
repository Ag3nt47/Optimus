"""Native settings retain explicit scopes without importing Windows path rules."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path, PurePosixPath

from .automation import CURRENT_SETUP_VERSION
from .workflow import Preferences, DEFAULTS, full_optimization_settings

WINDOWS_ONLY = (
    'request_admin_on_startup', 'consumer_debloat_enabled', 'm365_debloat_enabled',
    'outlook_debloat_enabled', 'edge_debloat_enabled', 'privacy_advertising_enabled',
    'privacy_diagnostics_enabled', 'spyware_scan_enabled', 'windows_security_updates_enabled',
    'windows_security_updates_install_approved', 'windows_security_update_agreements_accepted',
    'driver_updates_enabled', 'legacy_patch_review_enabled', 'cloud_review_enabled',
    'codex_priority_enabled',
    'update_compatibility_mode_enabled', 'windows_cpu_compatibility_enabled',
    'drive_optimization_enabled',
)
PACKAGE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9@+_.:/-]{0,255}\Z')


def full_native_settings(settings=None):
    result = full_optimization_settings(settings)
    for key in WINDOWS_ONLY:
        result[key] = False
    result['cleanup_category_ids'] = [key for key in result['cleanup_category_ids']
                                      if key not in ('windows-temp', 'canonical-user-temp')]
    result['cloud_policy'] = result['driver_policy'] = 'review'
    if settings is None:
        result['browser_cleanup_browsers'].append('chromium')
    return result


def validate_native_path(value, *, folder=False):
    if (not isinstance(value, str) or not value.startswith('/') or len(value) > 4096
            or re.search(r'[\x00-\x1f]', value) or '..' in PurePosixPath(value).parts):
        raise ValueError('Select an absolute native path without parent traversal.')
    path = PurePosixPath(value)
    if path == PurePosixPath('/') or folder and len(path.parts) < 3:
        raise ValueError('Select a specific personal folder below the filesystem root.')
    return str(path)


def startup_descriptor(record):
    if (not isinstance(record, dict) or record.get('Source') != 'native-startup'
            or not isinstance(record.get('Name'), str) or not 0 < len(record['Name']) <= 512
            or not re.fullmatch(r'[a-f0-9]{64}', str(record.get('SHA256', '')))
            or type(record.get('Enabled')) is not bool):
        raise ValueError('Approve an exact native startup file from a current inventory.')
    return {'Source': 'native-startup', 'Name': record['Name'],
            'Path': validate_native_path(record.get('Path')), 'SHA256': record['SHA256'],
            'Enabled': record['Enabled']}


class NativePreferences(Preferences):
    def load(self):
        with self.lock:
            try:
                return self.validate(json.loads(self.path.read_text(encoding='utf-8')))
            except FileNotFoundError:
                return self.validate(full_native_settings())
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return self.validate(self.safe_defaults())

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise ValueError('Settings must be an object.')
        raw = copy.deepcopy(DEFAULTS)
        raw.update(copy.deepcopy(value))
        roots = raw['duplicate_roots']
        if type(roots) is not list or len(roots) > 20:
            raise ValueError('Choose at most 20 native duplicate folders.')
        roots = [validate_native_path(root, folder=True) for root in roots]
        if len(set(roots)) != len(roots):
            raise ValueError('Duplicate folders must be unique.')
        entries = raw['startup_approved_entries']
        if type(entries) is not list or len(entries) > 100:
            raise ValueError('Approve at most 100 exact native startup entries.')
        entries = [startup_descriptor(item) for item in entries]
        if len({item['Path'] for item in entries}) != len(entries):
            raise ValueError('Native startup approvals must be unique.')
        packages = {}
        for key in ('software_update_ids', 'software_update_exclude_ids'):
            items = raw[key]
            if (type(items) is not list or len(items) > 100
                    or any(not isinstance(item, str) or not PACKAGE_ID.fullmatch(item)
                           or '..' in item.split(':', 1)[-1].split('/') for item in items)
                    or len(set(items)) != len(items)):
                raise ValueError('Choose at most 100 unique exact native package identities.')
            packages[key] = items
        # Validate shared types, browser choices, bounds and policy names once.
        # Windows-specific descriptor validators never receive native records.
        common = copy.deepcopy(raw)
        for key in WINDOWS_ONLY:
            common[key] = False
        common.update(duplicate_roots=[], duplicates_enabled=False,
                      startup_approved_entries=[], startup_review_enabled=False,
                      startup_policy='review', driver_policy='review', cloud_policy='review',
                      software_update_ids=[], software_update_exclude_ids=[],
                      software_updates_enabled=False)
        result = Preferences.validate(common)
        result.update(packages)
        result.update(duplicate_roots=roots, startup_approved_entries=entries)
        for key in ('duplicates_enabled', 'startup_review_enabled', 'software_updates_enabled'):
            if type(raw[key]) is not bool:
                raise ValueError(key + ' must be on or off.')
            result[key] = raw[key]
        if raw['startup_policy'] not in ('review', 'disable_selected'):
            raise ValueError('Choose review or disable selected native startup entries.')
        result['startup_policy'] = raw['startup_policy']
        current = result['setup_version'] == CURRENT_SETUP_VERSION
        if result['duplicates_enabled'] and result['duplicate_policy'] != 'review':
            if not current or not result['duplicate_changes_approved'] or not roots:
                raise ValueError('Approve selected native duplicate folders and recycling before automatic changes.')
        if result['startup_review_enabled'] and result['startup_policy'] != 'review':
            if not current or not result['startup_changes_approved'] or not entries:
                raise ValueError('Approve exact native startup files before automatic disabling.')
        if result['software_updates_enabled'] and result['software_update_policy'] != 'review':
            if not current or not result['software_update_agreements_accepted']:
                raise ValueError('Complete setup and accept native package-manager agreements before automatic updates.')
            if result['software_update_policy'] == 'selected' and not packages['software_update_ids']:
                raise ValueError('Choose exact native package IDs before automatic updates.')
            if result['software_update_policy'] == 'all_supported' and not result['software_update_all_approved']:
                raise ValueError('Approve the all-supported native update scope explicitly.')
        return result
