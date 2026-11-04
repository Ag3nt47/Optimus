"""Exact, saved Windows application-removal exclusions."""
from __future__ import annotations

import re
import unicodedata

EXCLUSIONS_KEY = 'app_removal_exclusions'
WINDOWS_APP_NAME = 'MicrosoftCorporationII.Windows365'
# Microsoft documents the version-independent Package Family Name.
# https://learn.microsoft.com/windows-app/configure-uwf-exclusions
WINDOWS_APP_FAMILY = WINDOWS_APP_NAME + '_8wekyb3d8bbwe'
WINDOWS_APP_STORE_ID = '9N1F85V9T8BN'
WINDOWS_APP_RECORD = {'kind': 'store', 'id': WINDOWS_APP_FAMILY,
                      'name': WINDOWS_APP_NAME, 'label': 'Microsoft Windows App'}
_STORE_NAME = re.compile(r'[A-Za-z0-9.-]{3,50}\Z')
_PUBLISHER_ID = re.compile(r'[A-Za-z0-9]{13}\Z')
_EDGE_DESKTOP = ('Microsoft Edge', 'Microsoft Edge browser')
_EDGE_STORE = 'Microsoft.MicrosoftEdge.Stable'


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
            and not any(unicodedata.category(char) in ('Cc', 'Cs') for char in value))


def validate_app_removal_exclusions(rows):
    if type(rows) is not list or len(rows) > 500:
        raise ValueError('Save at most 500 exact application removal exclusions.')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'kind', 'id', 'name', 'label'}:
            raise ValueError('Each application removal exclusion requires an installed app identity and label.')
        kind, identity, name, label = (row[key] for key in ('kind', 'id', 'name', 'label'))
        if kind not in ('store', 'desktop') or not all(map(_text, (identity, name, label))):
            raise ValueError('An application removal exclusion contains invalid inventory metadata.')
        if kind == 'store':
            package_name, separator, publisher_id = identity.rpartition('_')
            if (not separator or not _STORE_NAME.fullmatch(name) or package_name.casefold() != name.casefold()
                    or not _PUBLISHER_ID.fullmatch(publisher_id)):
                raise ValueError('Store app protection requires its complete Package Family Name.')
        key = (kind, identity.casefold())
        if key in seen:
            raise ValueError('Application removal exclusions must have unique exact identities.')
        seen.add(key)
        result.append(dict(row))
    return result


def removal_app_record(inventory_row):
    """Translate a Windows installed-app row, omitting incomplete identities."""
    if not isinstance(inventory_row, dict):
        return None
    inventory_type = inventory_row.get('Type')
    kind = {'Store': 'store', 'Desktop': 'desktop'}.get(inventory_type) if isinstance(inventory_type, str) else None
    identity, name = (inventory_row.get(key) for key in ('Identity', 'Name'))
    label = inventory_row.get('DisplayName')
    if kind == 'store' and isinstance(identity, str) and identity.casefold() == WINDOWS_APP_FAMILY.casefold():
        label = WINDOWS_APP_RECORD['label']
    label = label.strip() if isinstance(label, str) else ''
    try:
        return validate_app_removal_exclusions([{'kind': kind, 'id': identity, 'name': name,
                                                 'label': label if _text(label) else name}])[0]
    except ValueError:
        return None


def is_removal_protected(rows, *, identity='', names=(), kind=None):
    """Match trusted aliases exactly. With an AppX family use it exclusively."""
    identity = identity.casefold() if isinstance(identity, str) else ''
    aliases = {name.casefold() for name in names if isinstance(name, str) and name}
    for row in rows:
        if kind is not None and row['kind'] != kind:
            continue
        key = row['id'].casefold()
        if identity and identity == key:
            return True
        if kind == 'store' and identity:
            continue
        row_names = {row['name'].casefold()}
        if kind is None and row['kind'] == 'store':
            row_names.add(row['label'].casefold())
            if key == WINDOWS_APP_FAMILY.casefold():
                row_names.update(('windows app', 'microsoft windows app'))
                if identity == WINDOWS_APP_STORE_ID.casefold():
                    return True
        if row_names.intersection(aliases):
            return True
    return False


def _appx_family(target):
    explicit = target.get('appIdentity')
    if isinstance(explicit, str) and explicit:
        return explicit
    # Older saved plans and provisioned packages can contain only a full name.
    full_name, name = target.get('packageFullName'), target.get('name')
    if isinstance(full_name, str) and isinstance(name, str):
        parts = full_name.split('_')
        if len(parts) == 5 and parts[0].casefold() == name.casefold() and _PUBLISHER_ID.fullmatch(parts[-1]):
            return parts[0] + '_' + parts[-1]
    return ''


def debloat_target_protected(rows, target):
    kind, name = target.get('kind'), target.get('name')
    if kind in ('appx', 'provisioned'):
        return is_removal_protected(rows, identity=_appx_family(target), names=(name,), kind='store')
    if kind in ('edge', 'desktop-edge-script'):
        return (is_removal_protected(rows, identity='Microsoft.Edge', names=_EDGE_DESKTOP, kind='desktop')
                or is_removal_protected(rows, names=(_EDGE_STORE,), kind='store'))
    if kind == 'office-dialog':
        return is_removal_protected(rows, identity=target.get('appIdentity'), names=(name,), kind='desktop')
    return False


def protected_debloat_result(target):
    return {'id': target['id'], 'name': target.get('name') or target['id'],
            'status': 'Protected from removal in Settings', 'reason': 'app_removal_excluded',
            'removed': False, 'protected': True, 'skipped': True, 'restartNeeded': False}
