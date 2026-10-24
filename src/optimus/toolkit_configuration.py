"""Versioned, default-off maintenance extensions within the saved scope."""
from __future__ import annotations
import copy
import ntpath
import os
from pathlib import Path

from .adaptive_maintenance import DEFAULTS as ADAPTIVE, validate_configuration as validate_adaptive
from .application_caches import DEFAULTS as CACHES, validate_configuration as validate_caches
from .storage_growth import DEFAULTS as STORAGE, validate_configuration as validate_storage
from .recovery_retention import DEFAULTS as RETENTION, validate_configuration as validate_retention
from .additional_packages import DEFAULTS as PROVIDERS, validate_configuration as validate_providers

DEFAULTS = {'adaptive': copy.deepcopy(ADAPTIVE), 'application_caches': copy.deepcopy(CACHES),
            'storage_growth': copy.deepcopy(STORAGE), 'recovery_retention': copy.deepcopy(RETENTION),
            'history': {'enabled': False, 'sample_seconds': 5},
            'diagnostics': {'enabled': False, 'integrity_symptoms': []},
            'low_space_cleaning': {'enabled': False},
            'installer_space': {'enabled': False, 'destination': '', 'required_bytes': None},
            'resume': {'enabled': False, 'resident_handoff_enabled': False}, 'providers': copy.deepcopy(PROVIDERS)}


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Use the supported maintenance toolkit choices.')
    result = copy.deepcopy(DEFAULTS)
    for key, section in value.items():
        if not isinstance(section, dict) or set(section) - set(DEFAULTS[key]):
            raise ValueError('Unsupported ' + key + ' toolkit choices.')
        result[key].update(copy.deepcopy(section))
    for key, validator in (('adaptive', validate_adaptive), ('application_caches', validate_caches),
                           ('storage_growth', validate_storage), ('recovery_retention', validate_retention),
                           ('providers', validate_providers)):
        result[key] = validator(result[key])
    for key in ('history', 'diagnostics', 'installer_space', 'resume', 'low_space_cleaning'):
        if type(result[key]['enabled']) is not bool:
            raise ValueError(key + ' must be on or off.')
    if type(result['resume']['resident_handoff_enabled']) is not bool:
        raise ValueError('Experimental resident handoff must be explicitly on or off.')
    duration = result['history']['sample_seconds']
    if type(duration) is not int or not 1 <= duration <= 30:
        raise ValueError('Observe resources for 1 to 30 seconds.')
    from .system_diagnostics import SYMPTOMS
    symptoms = result['diagnostics']['integrity_symptoms']
    if type(symptoms) is not list or len(symptoms) > len(SYMPTOMS) or any(type(item) is not str or item not in SYMPTOMS for item in symptoms) or len(set(symptoms)) != len(symptoms):
        raise ValueError('Choose exact supported Windows integrity symptoms.')
    destination, size = result['installer_space']['destination'], result['installer_space']['required_bytes']
    if not isinstance(destination, str) or len(destination) > 1024 or any(ord(char) < 32 for char in destination):
        raise ValueError('Choose an ordinary local installer destination.')
    if destination and (not Path(destination).is_absolute() or destination.startswith(('\\\\', '//'))
                        or '..' in Path(destination).parts
                        or os.name == 'nt' and (not ntpath.splitdrive(destination)[0]
                            or ':' in ntpath.splitdrive(destination)[1]
                            or any(part.endswith((' ', '.')) for part in Path(destination).parts[1:]))):
        raise ValueError('Installer destinations must use an absolute ordinary local path.')
    if size is not None and (type(size) is not int or not 1 <= size <= 1024**4):
        raise ValueError('An installer estimate must be 1 byte to 1 TiB, or unknown.')
    return result


def launch_descriptions(settings):
    config = validate_configuration(settings.get('toolkit_settings', {}))
    actions, checks = [], []
    cache, retention = config['application_caches'], config['recovery_retention']
    if config['low_space_cleaning']['enabled']:
        actions.append('While Optimus is open after launch approval, clean only already selected eligible junk/cache categories on verified local volumes below the saved '
                       + str(settings.get('drive_space_guard_target_gib', 2.0))
                       + ' GiB target. Saved ages, cooldown and scope limits apply; open-browser data and new cleanup categories are excluded. The drive-space guard must remain on; sufficient space is not guaranteed.')
    if cache['enabled']:
        actions.append('Delete eligible rebuildable application caches for ' + ', '.join(cache['apps'])
                       + f"; at least {cache['min_age_days']} days old, within saved file/byte limits. Active or uncertain applications are skipped.")
    if retention['enabled']:
        actions.append(f"Expire eligible Optimus recovery/report data at least {retention['min_age_days']} days old.")
        if retention.get('browser_backups'):
            actions.append('Permanently remove eligible completed browser backup groups; their history/cookie undo will be lost. Driver, restore-point and unresolved update records are protected.')
    if config['providers']['enabled']:
        actions.append('Update only these saved Chocolatey package IDs through the approved provider: ' + ', '.join(config['providers']['ids']) + '.')
    for key, text in (('adaptive', 'Apply saved idle, power, resource-pressure, network and maintenance-hour limits before new work.'),
                      ('history', 'Record bounded sustained performance observations in local history.'),
                      ('storage_growth', 'Compare the selected folder sizes with prior complete local measurements.'),
                      ('diagnostics', 'Read available disk, battery, boot and pagefile diagnostics; symptom checks do not approve repairs.'),
                      ('installer_space', 'Check free space on actual system, temporary and selected destination volumes against known estimates.'),
                      ('resume', 'Save eligible remaining update work for revalidation after a user-initiated restart.')):
        if config[key]['enabled']:
            checks.append(text)
    return actions, checks
