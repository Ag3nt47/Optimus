"""Recognized browser locations and process names for each native platform.

Only browser cache folders are returned as cache roots. Profile locations are
separate because their databases require the privacy engine's backup workflow.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def browser_definitions(platform: str | None = None) -> dict[str, dict]:
    platform = platform or sys.platform
    if platform == 'win32':
        names = {'edge': ('msedge.exe',), 'chrome': ('chrome.exe',),
                 'chromium': ('chromium.exe',), 'brave': ('brave.exe',),
                 'vivaldi': ('vivaldi.exe',), 'firefox': ('firefox.exe',)}
    elif platform == 'darwin':
        names = {'edge': ('Microsoft Edge', 'Microsoft Edge Helper'),
                 'chrome': ('Google Chrome', 'Google Chrome Helper'),
                 'chromium': ('Chromium', 'Chromium Helper'),
                 'brave': ('Brave Browser', 'Brave Browser Helper'),
                 'vivaldi': ('Vivaldi', 'Vivaldi Helper'),
                 'firefox': ('firefox', 'Firefox', 'plugin-container')}
    else:
        names = {'edge': ('msedge', 'microsoft-edge', 'microsoft-edge-stable'),
                 'chrome': ('chrome', 'google-chrome', 'google-chrome-stable'),
                 'chromium': ('chromium', 'chromium-browser'),
                 'brave': ('brave', 'brave-browser'), 'vivaldi': ('vivaldi-bin', 'vivaldi'),
                 'firefox': ('firefox', 'firefox-bin')}
    labels = {'edge': 'Microsoft Edge', 'chrome': 'Google Chrome',
              'chromium': 'Chromium', 'brave': 'Brave', 'vivaldi': 'Vivaldi',
              'firefox': 'Mozilla Firefox'}
    bundles = {'edge': 'com.microsoft.edgemac', 'chrome': 'com.google.Chrome',
               'chromium': 'org.chromium.Chromium', 'brave': 'com.brave.Browser',
               'vivaldi': 'com.vivaldi.Vivaldi', 'firefox': 'org.mozilla.firefox'}
    return {key: {'name': labels[key], 'process': values[0], 'process_names': values,
                  'bundle_id': bundles[key]} for key, values in names.items()}


def process_matches(browser_id: str, name: str, platform: str | None = None, *, allow_truncated: bool = True) -> bool:
    platform = platform or sys.platform
    definition = browser_definitions(platform).get(browser_id)
    if not definition or not isinstance(name, str):
        return False
    if any(name.casefold() == allowed.casefold() for allowed in definition['process_names']):
        return True
    if allow_truncated and platform != 'win32' and len(name) == 15 and any(allowed.startswith(name) for allowed in definition['process_names']):
        # Some kernels expose only the 15-character comm name. This broadens a
        # blocker during inventory, never authorization: closure still verifies
        # the full executable basename, owner and process creation time.
        return True
    # macOS Chromium helpers append an explicit role to the known helper name.
    return platform == 'darwin' and any(
        name.startswith(allowed + ' (') and name.endswith(')')
        for allowed in definition['process_names'] if allowed.endswith(' Helper'))


def native_locations(platform: str | None = None, home: Path | None = None) -> list[dict]:
    platform, home = platform or sys.platform, home or Path.home()
    definitions = browser_definitions(platform)
    if platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
        roaming = Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
        bases = {'edge': ('Microsoft/Edge/User Data', 'Microsoft/Edge/User Data'),
                 'chrome': ('Google/Chrome/User Data', 'Google/Chrome/User Data'),
                 'chromium': ('Chromium/User Data', 'Chromium/User Data'),
                 'brave': ('BraveSoftware/Brave-Browser/User Data', 'BraveSoftware/Brave-Browser/User Data'),
                 'vivaldi': ('Vivaldi/User Data', 'Vivaldi/User Data')}
        roots = [(key, local / profile, local / cache) for key, (profile, cache) in bases.items()]
        roots.append(('firefox', roaming / 'Mozilla/Firefox/Profiles', local / 'Mozilla/Firefox/Profiles'))
    elif platform == 'darwin':
        support, caches = home / 'Library/Application Support', home / 'Library/Caches'
        bases = {'edge': 'Microsoft Edge', 'chrome': 'Google/Chrome', 'chromium': 'Chromium',
                 'brave': 'BraveSoftware/Brave-Browser', 'vivaldi': 'Vivaldi'}
        roots = [(key, support / value, caches / value) for key, value in bases.items()]
        roots.append(('firefox', support / 'Firefox/Profiles', caches / 'Firefox/Profiles'))
    else:
        config = Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config')))
        cache = Path(os.environ.get('XDG_CACHE_HOME', str(home / '.cache')))
        # Relative XDG values are invalid according to the base-directory spec.
        config = config if config.is_absolute() else home / '.config'
        cache = cache if cache.is_absolute() else home / '.cache'
        bases = {'edge': 'microsoft-edge', 'chrome': 'google-chrome', 'chromium': 'chromium',
                 'brave': 'BraveSoftware/Brave-Browser', 'vivaldi': 'vivaldi'}
        roots = [(key, config / value, cache / value) for key, value in bases.items()]
        roots.append(('firefox', home / '.mozilla/firefox', cache / 'mozilla/firefox'))
        # Sandboxed Firefox and Chromium retain their own fixed user locations.
        roots.extend((('firefox', home / 'snap/firefox/common/.mozilla/firefox', home / 'snap/firefox/common/.cache/mozilla/firefox'),
                      ('firefox', home / '.var/app/org.mozilla.firefox/.mozilla/firefox', home / '.var/app/org.mozilla.firefox/cache/mozilla/firefox'),
                      ('chromium', home / 'snap/chromium/common/chromium', home / 'snap/chromium/common/.cache/chromium'),
                      ('chromium', home / '.var/app/org.chromium.Chromium/config/chromium', home / '.var/app/org.chromium.Chromium/cache/chromium')))
    return [dict(definitions[key], id=key, profile_root=profile, cache_root=cache,
                 family='firefox' if key == 'firefox' else 'chromium')
            for key, profile, cache in roots]
