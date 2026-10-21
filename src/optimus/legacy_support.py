"""Local inventory and navigation for vendor-only/legacy patch coverage.

Provider discovery is not a verified patch offer or installation authority.
No community binary, driver, certificate or compatibility bypass is installed.
"""
from __future__ import annotations
import re
from .driver_coverage import support_links

CATALOG_REVIEWED = '2026-10-21'
PROVIDERS = (
    {'id': 'microsoft-catalog', 'name': 'Microsoft Update Catalog', 'kind': 'official',
     'url': 'https://www.catalog.update.microsoft.com/',
     'scope': 'Published Microsoft out-of-band and archived update packages; applicability, prerequisites, licensing and support must be verified.'},
    {'id': '0patch', 'name': 'ACROS 0patch', 'kind': 'third-party micropatching',
     'url': 'https://0patch.com/',
     'compatibility_url': 'https://support.0patch.com/hc/en-us/articles/4403751356050-Which-Windows-products-has-0patch-security-adopted',
     'licensing_url': 'https://support.0patch.com/hc/en-us/articles/360009436920-Are-post-EOS-Windows-7-Windows-Server-2008-R2-and-Windows-10-micropatches-also-available-to-home-personal-users',
     'cost': 'Post-end-of-support coverage requires the provider paid license. Optimus does not include or bypass it.',
     'scope': 'Provider-maintained patches for specified Windows/Office builds. Exact binary/build compatibility and current coverage require provider verification; this is not a universal replacement for updates.'},
    {'id': 'legacy-update', 'name': 'Legacy Update', 'kind': 'community open-source',
     'url': 'https://legacyupdate.net/', 'source_url': 'https://github.com/LegacyUpdate/LegacyUpdate',
     'scope': 'Restores access to existing update infrastructure on supported legacy Windows versions. It does not create new security fixes for every unsupported vulnerability. Optimus itself requires Windows 10/11.'},
)


def review_legacy_support(context=None, applications=None, devices=None):
    context = context if isinstance(context, dict) else {}
    apps = applications if isinstance(applications, list) else []
    inventory = devices if isinstance(devices, dict) else {}
    rows = inventory.get('devices', [])
    rows = rows if isinstance(rows, list) else []
    errors = []
    inventory_errors = inventory.get('errors', [])
    if inventory.get('complete') is not True:
        errors.append('Device inventory was incomplete or could not be verified; missing devices are not evidence of complete patch coverage.')
    if not isinstance(inventory_errors, list):
        errors.append('Device inventory returned invalid error metadata.')
    else:
        for error in inventory_errors[:100]:
            source = error.get('source') if isinstance(error, dict) else None
            label = source if source in ('Win32_PnPEntity', 'Win32_PnPSignedDriver', 'Win32_ComputerSystem', 'Win32_BaseBoard') else 'Windows device inventory'
            errors.append(label + ' could not be fully read; review Drivers before treating this inventory as complete.')
        if len(inventory_errors) > 100:
            errors.append('Additional device-inventory errors exceeded the bounded review limit.')
    if len(rows) > 2000:
        errors.append('The device review limit was reached; additional devices were not reviewed.')
    matches = []
    for app in apps[:3000]:
        if not isinstance(app, dict):
            continue
        name = app.get('Name', app.get('name', ''))
        publisher = app.get('Publisher', app.get('publisher', ''))
        if not isinstance(name, str) or not isinstance(publisher, str):
            continue
        if (re.search(r'\bMicrosoft\b', publisher, re.I)
                and re.search(r'\b(?:Microsoft\s+)?Office\b.*\b(?:2010|2013|2016|2019)\b', name, re.I)):
            matches.append({'product': name, 'reported_version': app.get('Version', app.get('version')),
                'provider': '0patch', 'status': 'compatibility_not_verified',
                'note': 'The product family appears in the provider adoption list. Installed patch level, component hashes, prerequisites and entitlement have not been verified.'})
    build = context.get('build')
    if type(build) is int and 10240 <= build <= 19045:
        matches.append({'product': 'Windows build ' + str(build), 'provider': '0patch',
            'status': 'edition_and_esu_not_verified',
            'lifecycle_url': 'https://learn.microsoft.com/lifecycle/products/windows-10-home-and-pro',
            'note': 'Review the exact edition, release, LTSC/ESU entitlement and provider-compatible patch level. Build range alone does not establish that this system is unsupported.'})
    links = {}
    device_notes = []
    for device in rows[:2000]:
        if not isinstance(device, dict):
            continue
        manufacturer = device.get('manufacturer', device.get('Manufacturer', ''))
        routes = support_links(manufacturer, device.get('hardwareIds', []), system=True)
        for route in routes:
            links.setdefault(route['url'], route)
        if routes:
            device_notes.append({'device': device.get('displayName') or device.get('name') or device.get('Name') or 'Device',
                'manufacturer': manufacturer, 'routes': routes,
                'status': 'vendor_catalog_not_queried',
                'note': 'A support route does not prove a compatible driver/BIOS/firmware patch exists. Match the exact model, hardware ID, OS and architecture.'})
    return {'review_only': True, 'status': 'partial' if errors else 'review', 'failed_items': len(errors), 'errors': errors,
        'catalog_reviewed': CATALOG_REVIEWED,
        'providers': [dict(provider) for provider in PROVIDERS],
        'possible_product_matches': matches, 'official_hardware_routes': list(links.values()),
        'device_review': device_notes, 'device_inventory_complete': inventory.get('complete') is True and not errors,
        'recommendations': [{'id': 'review_legacy_patches', 'title': 'Review vendor-only and legacy/community patch coverage',
            'reason': 'Updates outside Windows Update can require an OEM updater, out-of-band vendor package or separately licensed micropatching provider. Review compatibility and rollback before selecting an installer.',
            'action': 'Show the local provider and hardware-support report; no package is downloaded or installed.'}],
        'note': 'This is a bounded local inventory and source catalog, not a live universal community-patch search or a vulnerability/compatibility assessment. No available compatible patch is asserted from a missing Windows Update offer. Unknown or discontinued products can remain unpatched.'}
