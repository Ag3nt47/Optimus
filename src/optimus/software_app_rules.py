"""Pure exact-package app rules. Reading or editing rules never runs a package manager."""
from __future__ import annotations

import copy
import re

APP_RULE_DEFAULTS = {'software_app_rules': []}
SOURCES = ('winget', 'brew', 'apt', 'dnf', 'pacman')
UPDATE_POLICIES = ('follow_global', 'always_update', 'always_ignore')
PRESENCE_POLICIES = ('keep_current', 'keep_installed', 'keep_uninstalled')
_WINDOWS_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}\Z')
_NATIVE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9+_.@:/-]{0,255}\Z')
_FIELDS = frozenset(('id', 'source', 'update_policy', 'presence'))


def rule_identity(package_id, source, *, native=False):
    if not isinstance(source, str) or source not in SOURCES or not native and source != 'winget':
        raise ValueError('Choose a supported exact package source.')
    if not isinstance(package_id, str):
        raise ValueError('An exact package identity is required.')
    if source == 'winget':
        valid = _WINDOWS_ID.fullmatch(package_id) is not None
    else:
        valid = (_NATIVE_ID.fullmatch(package_id) is not None and package_id.startswith(source + ':')
                 and len(package_id) > len(source) + 1 and '..' not in package_id.split(':', 1)[1].split('/'))
    if not valid:
        raise ValueError('Use the complete package identity from its source; paths and commands are not package IDs.')
    return source.casefold(), package_id.casefold()


def validate_app_rules(value, *, native=False):
    if type(value) is not list or len(value) > 100:
        raise ValueError('Save at most 100 exact per-app rules.')
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError('Each app rule requires only id, source, update_policy and presence.')
        identity = rule_identity(row['id'], row['source'], native=native)
        if identity in seen:
            raise ValueError('App rules must have unique exact source/package identities.')
        if row['update_policy'] not in UPDATE_POLICIES or row['presence'] not in PRESENCE_POLICIES:
            raise ValueError('Choose a supported app update and presence policy.')
        if row['presence'] == 'keep_uninstalled' and row['update_policy'] != 'always_ignore':
            raise ValueError('Always uninstall requires Always ignore updates for the same app.')
        seen.add(identity)
        result.append(copy.deepcopy(row))
    return result


def rule_for(rules, package_id, source='winget'):
    identity = rule_identity(package_id, source, native=True)
    return next((copy.deepcopy(row) for row in validate_app_rules(rules, native=True)
                 if rule_identity(row['id'], row['source'], native=True) == identity), None)


def apply_update_rules(settings, rules=None):
    """Map explicit rules into an effective copy of existing update controls.

    Exclusions, agreements, pins, version checks and holds retain their gates.
    This does not enable the stage or grant agreement/all-supported approvals.
    """
    result = copy.deepcopy(settings)
    rows = validate_app_rules(settings.get('software_app_rules', []) if rules is None else rules, native=True)
    exclusions = list(result.get('software_update_exclude_ids', []))
    excluded = {value.casefold() for value in exclusions}
    for row in rows:
        if row['update_policy'] == 'always_ignore' and row['id'].casefold() not in excluded:
            exclusions.append(row['id']); excluded.add(row['id'].casefold())
    always = [row['id'] for row in rows if row['update_policy'] == 'always_update'
              and row['presence'] != 'keep_uninstalled' and row['id'].casefold() not in excluded]
    policy = result.get('software_update_policy', 'review')
    selected = list(result.get('software_update_ids', [])) if policy != 'review' else []
    selected_keys = {value.casefold() for value in selected}
    for identity in always if policy != 'all_supported' else []:
        if identity.casefold() not in selected_keys:
            selected.append(identity); selected_keys.add(identity.casefold())
    if len(selected) > 100 or len(exclusions) > 200:
        raise ValueError('The combined saved selections and per-app rules exceed the bounded app scope.')
    if policy == 'review' and always:
        result['software_update_policy'] = 'selected'
    result.update(software_update_ids=selected, software_update_exclude_ids=exclusions, software_app_rules=rows)
    return result


def app_rules_summary(settings):
    """Exact saved rule scopes for approval text; this performs no host lookup."""
    rows = validate_app_rules(settings.get('software_app_rules', []), native=True)
    scope = {key: [] for key in ('always_update', 'always_ignore', 'keep_installed', 'keep_uninstalled')}
    for row in rows:
        label = row['id'] + ' [' + row['source'] + ']'
        if row['update_policy'] in scope:
            scope[row['update_policy']].append(label)
        if row['presence'] in scope:
            scope[row['presence']].append(label)
    scope['enabled'] = settings.get('software_updates_enabled') is True
    scope['automatic_actions'] = scope['enabled'] and bool(scope['always_update'] or scope['keep_installed'] or scope['keep_uninstalled'])
    return scope
