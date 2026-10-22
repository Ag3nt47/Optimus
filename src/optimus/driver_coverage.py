"""Read-only correlation of present Windows devices and applicable WUA offers.

No offer proves only Windows Update coverage, never that a vendor has no newer
release. This module does not download, install, disable or delete any device.
"""
from __future__ import annotations

import math
import re

DRIVER_SEARCH_CRITERIA = "IsInstalled=0 and Type='Driver' and IsHidden=0"


def driver_scan_evidence(scan, *, eligible_count=None):
    """Bounded scan proof; empty offers are conclusive only after valid success.

    Device inventory completeness and Windows Update availability are separate
    observations. Neither is evidence of every vendor's latest driver release.
    """
    from .automated_system import driver_descriptor
    value = scan if isinstance(scan, dict) else {}
    errors = []
    def error(message, source='Windows Update'):
        if len(errors) < 20:
            errors.append({'source': str(source)[:120], 'error': str(message)[:500]})
    if not isinstance(scan, dict):
        error('No usable driver scan report was returned.')
    code = value.get('resultCode')
    if type(code) is not int or code != 2:
        error('Windows Update did not report a complete successful scan (result code 2).')
    updates = value.get('updates')
    invalid = 0
    if not isinstance(updates, list):
        error('The driver offer collection is missing or malformed.')
    else:
        identities = set()
        for row in updates:
            try:
                candidate = driver_descriptor(row)
                identity = (candidate['id'], candidate['revision'])
                if identity in identities:
                    raise ValueError('Ambiguous duplicate driver identity/revision.')
                identities.add(identity)
            except (ValueError, TypeError):
                invalid += 1
        if invalid:
            error(f'{invalid} driver offer records are malformed or ambiguous; availability remains unverified.')
    if value.get('error'):
        error(value['error'])
    for field in ('errors', 'warnings'):
        rows = value.get(field, [])
        if not isinstance(rows, list):
            error('The scan ' + field + ' collection is malformed.')
        else:
            for row in rows[:20]:
                if isinstance(row, dict):
                    error(row.get('error') or row.get('message') or str(row), row.get('source', 'Windows Update'))
                else:
                    error(row)
    online = value.get('online') if type(value.get('online')) is bool else None
    if online is False:
        error('This was an offline catalog query; online driver availability was not verified.')
    if 'online' in value and online is None:
        error('The online query state is malformed.')
    if value.get('criteria') and value['criteria'] != DRIVER_SEARCH_CRITERIA:
        error('The driver query did not cover the expected installed/hidden driver scope.')
    if 'returned_offer_count' in value and (type(value['returned_offer_count']) is not int
            or not isinstance(updates, list) or value['returned_offer_count'] != len(updates)):
        error('The returned driver offer count does not match the scan collection.')
    complete = not errors
    coverage = value.get('coverage') if isinstance(value.get('coverage'), dict) else {}
    counts = coverage.get('summary') if isinstance(coverage.get('summary'), dict) else {}
    def count(name):
        number = counts.get(name)
        return number if type(number) is int and 0 <= number <= 1000000 else None
    offers_count = len(updates) if isinstance(updates, list) else None
    eligible = eligible_count if type(eligible_count) is int and eligible_count >= 0 else None
    if complete:
        summary = (f'Windows Update search completed: {offers_count} applicable driver offers'
                   + (f'; {eligible} eligible under saved approvals' if eligible is not None else '') + '.')
        if offers_count == 0:
            summary += ' No applicable drivers were offered by the configured Windows Update service.'
    else:
        summary = 'Driver search incomplete; available updates could not be verified.'
    devices = count('devices')
    if devices is not None:
        summary += f' {devices} present devices inventoried.'
    if coverage.get('inventoryComplete') is not True:
        summary += ' Device inventory coverage is incomplete.'
    summary += ' OEM tools may offer other driver or firmware releases.'
    duration = value.get('duration_seconds')
    duration = (float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and math.isfinite(duration) and 0 <= duration < 3155760000 else None)
    status = 'completed' if complete else 'failed' if type(code) is int and code in (4, 5) else 'incomplete'
    return {'schema': 1, 'complete': complete, 'status': status,
            'source': str(value.get('source') or 'Windows Update')[:200],
            'criteria': str(value.get('criteria') or DRIVER_SEARCH_CRITERIA)[:512],
            'online': online, 'started_utc': str(value.get('started_utc') or '')[:80],
            'completed_utc': str(value.get('completed_utc') or '')[:80], 'duration_seconds': duration,
            'result_code': code if type(code) is int else None,
            'offers_count': offers_count, 'eligible_count': eligible, 'invalid_offer_count': invalid,
            'inventory_complete': coverage.get('inventoryComplete') is True,
            'devices_checked': devices, 'device_problems': count('deviceProblems'),
            'summary': summary, 'errors': errors}

# Fixed navigation destinations; never URLs supplied by driver/update metadata.
_SUPPORT = {
    'nvidia': ('NVIDIA driver downloads', 'https://www.nvidia.com/en-us/drivers/'),
    'amd': ('AMD drivers and support', 'https://www.amd.com/en/support/download/drivers.html'),
    'intel': ('Intel driver downloads', 'https://www.intel.com/content/www/us/en/download-center/home.html'),
    'dell': ('Dell drivers and downloads', 'https://www.dell.com/support/home/en-us?app=drivers'),
    'hp': ('HP software and drivers', 'https://support.hp.com/us-en/drivers'),
    'lenovo': ('Lenovo PC support', 'https://pcsupport.lenovo.com/us/en/'),
    'asus': ('ASUS download center', 'https://www.asus.com/support/download-center/'),
    'msi': ('MSI product support', 'https://www.msi.com/support'),
    'gigabyte': ('GIGABYTE product support', 'https://www.gigabyte.com/Support'),
    'asrock': ('ASRock product support', 'https://www.asrock.com/support/'),
    'acer': ('Acer drivers and manuals', 'https://www.acer.com/us-en/support/drivers-and-manuals'),
    'microsoft': ('Microsoft device support', 'https://support.microsoft.com/'),
}
_BRANDS = {
    'nvidia': r'\bnvidia\b', 'amd': r'\bamd\b|\badvanced micro devices\b',
    'intel': r'\bintel\b', 'dell': r'\bdell\b',
    'hp': r'\bhp\b|\bhewlett.packard\b', 'lenovo': r'\blenovo\b',
    'asus': r'\basus\b|\basustek\b', 'msi': r'\bmsi\b|\bmicro.star\b',
    'gigabyte': r'\bgigabyte\b|\bgiga.byte\b', 'asrock': r'\basrock\b',
    'acer': r'\bacer\b',
}
_PCI_VENDORS = {'10DE': 'nvidia', '1002': 'amd', '1022': 'amd', '8086': 'intel'}
_PROBLEMS = {
    0: 'Windows reports no device problem.',
    1: 'Device is not configured correctly.',
    2: 'Windows cannot load the device driver.',
    10: 'Device cannot start.',
    12: 'Device has insufficient resources.',
    14: 'Device requires a Windows restart.',
    18: 'Device driver must be reinstalled.',
    22: 'Device is disabled; an update may not be needed.',
    24: 'Device is absent, not working, or incompletely installed.',
    28: 'Device drivers are not installed.',
    31: 'Windows cannot load the required device drivers.',
    43: 'Windows stopped the device because it reported problems.',
    45: 'Device is not connected.',
    52: 'Windows cannot verify the driver signature.',
}


def _rows(value):
    if isinstance(value, dict):
        return [value]
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _ids(value):
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result = {}
    for item in values:
        if _text(item):
            result.setdefault(_text(item).casefold(), _text(item))
    return list(result.values())


def is_trusted_support_url(url):
    """Allow only exact built-in HTTPS support routes, including fixed paths."""
    return isinstance(url, str) and url in {route[1] for route in _SUPPORT.values()}


def support_links(manufacturer='', hardware_ids=None, *, system=False):
    """Return navigation hints, not a claim that a download is applicable."""
    brands = []
    maker = _text(manufacturer)
    for brand, pattern in _BRANDS.items():
        if re.search(pattern, maker, re.IGNORECASE):
            brands.append(brand)
    if system and re.search(r'\bmicrosoft\b', maker, re.IGNORECASE):
        brands.append('microsoft')
    for identity in _ids(hardware_ids):
        match = re.match(r'PCI\\VEN_([0-9A-F]{4})&', identity, re.IGNORECASE)
        if match and match[1].upper() in _PCI_VENDORS:
            brands.append(_PCI_VENDORS[match[1].upper()])
    return [dict(label=_SUPPORT[brand][0], url=_SUPPORT[brand][1]) for brand in dict.fromkeys(brands)]


def installed_device_rules(device):
    """Normalize future-update approvals from known installed device metadata.

    These rules never authorize a guessed publisher/class or a wildcard ID. A
    compatible ID can cover several devices; the installer rechecks every matching
    installed driver and will not downgrade any of them.
    """
    from .automated_system import validate_driver_devices
    rules = {}
    for installed in _rows(device.get('installedDrivers')):
        provider = _text(installed.get('provider'))
        driver_class = _text(installed.get('deviceClass'))
        floor = _text(installed.get('date'))
        for hardware_id in _ids(device.get('hardwareIds')) + _ids(device.get('compatibleIds')):
            candidate = dict(hardwareId=hardware_id, provider=provider, **{
                'class': driver_class, 'notOlderThanDriverDate': floor})
            try:
                candidate = validate_driver_devices([candidate])[0]
            except ValueError:
                continue
            key = tuple(candidate[field].casefold() for field in ('hardwareId', 'provider', 'class'))
            if key not in rules or candidate['notOlderThanDriverDate'] > rules[key]['notOlderThanDriverDate']:
                rules[key] = candidate
    return list(rules.values())



def build_driver_coverage(inventory, scan):
    """Join offers by exact hardware/compatible IDs; preserve uncertainty."""
    inventory = inventory if isinstance(inventory, dict) else {}
    scan = scan if isinstance(scan, dict) else {}
    inventory_ok = inventory.get('complete') is True
    scan_evidence = driver_scan_evidence(scan)
    scan_ok = scan_evidence['complete']
    offers = _rows(scan.get('updates'))
    offered_by_hardware = {}
    for index, offer in enumerate(offers):
        identity = _text(offer.get('hardwareId')).casefold()
        if identity:
            offered_by_hardware.setdefault(identity, []).append((index, offer))
    matched = set()
    devices = []
    for raw in _rows(inventory.get('devices')):
        if raw.get('present') is False:
            continue
        device = dict(raw)
        device['deviceId'] = _text(raw.get('deviceId'))
        device['displayName'] = _text(raw.get('displayName')) or device['deviceId'] or 'Unnamed device'
        device['deviceClass'] = _text(raw.get('deviceClass')) or 'Unknown'
        device['manufacturer'] = _text(raw.get('manufacturer'))
        device['hardwareIds'] = _ids(raw.get('hardwareIds'))
        device['compatibleIds'] = _ids(raw.get('compatibleIds'))
        device['installedDrivers'] = _rows(raw.get('installedDrivers'))
        code = raw.get('problemCode')
        code = code if type(code) is int and code >= 0 else None
        device['problemCode'] = code
        device['problem'] = _PROBLEMS.get(code, 'Windows reports device problem code '+str(code)+'.' if code else 'Device problem status is unavailable.')
        local_offers = {}
        for identity in device['hardwareIds'] + device['compatibleIds']:
            for index, offer in offered_by_hardware.get(identity.casefold(), []):
                matched.add(index)
                local_offers[index] = dict(offer)
        device['updateOffers'] = list(local_offers.values())
        device['hasProblem'] = code is not None and code != 0
        device['missingDriver'] = code == 28
        device['driverMetadataMissing'] = not device['installedDrivers']
        device['approvalRules'] = installed_device_rules(device) if inventory_ok else []
        device['approvalUnavailableReason'] = '' if device['approvalRules'] else ('A complete present-device and installed-driver inventory is required.' if not inventory_ok else 'An exact hardware/compatible ID and known installed driver provider, class and ISO date are required.')
        if device['hasProblem']:
            status, description = 'device_problem', device['problem']
            if local_offers:
                description += ' Windows Update offers a matching driver; this does not guarantee repair.'
        elif not inventory_ok or not scan_ok:
            status, description = 'scan_incomplete', 'Coverage is incomplete; update availability is unknown.'
        elif local_offers:
            status, description = 'offered', 'Windows Update offers a matching applicable driver.'
        else:
            status, description = 'no_offer', 'No matching Windows Update offer. A newer vendor release may exist.'
        device['status'], device['statusDescription'] = status, description
        device['supportLinks'] = support_links(device['manufacturer'], device['hardwareIds'])
        devices.append(device)
    devices.sort(key=lambda item: (not item['hasProblem'], not bool(item['updateOffers']), item['displayName'].casefold(), item['deviceId'].casefold()))
    system = inventory.get('system') if isinstance(inventory.get('system'), dict) else {}
    routes = support_links(system.get('manufacturer'), system=True) + support_links(system.get('baseboardManufacturer'))
    routes = list({route['url']: route for route in routes}.values())
    unmatched = [dict(offer) for index, offer in enumerate(offers) if index not in matched]
    errors = _rows(inventory.get('errors'))
    errors.extend(row for row in scan_evidence['errors'] if row not in errors)
    return {
        'devices': devices, 'inventoryComplete': inventory_ok, 'updateScanComplete': scan_ok,
        'inventoryErrors': errors, 'unmatchedUpdates': unmatched,
        'system': dict(system), 'systemSupport': routes,
        'summary': {
            'devices': len(devices), 'offered': sum(bool(item['updateOffers']) for item in devices),
            'deviceProblems': sum(item['hasProblem'] for item in devices),
            'missingDriver': sum(item['missingDriver'] for item in devices),
            'driverMetadataMissing': sum(item['driverMetadataMissing'] for item in devices),
            'noOffer': sum(item['status'] == 'no_offer' for item in devices),
            'scanIncomplete': sum(item['status'] == 'scan_incomplete' for item in devices),
            'unmatchedUpdates': len(unmatched),
        },
        'notes': [
            'Inventory covers present Plug and Play devices and devices whose presence is unknown. Disconnected devices are excluded.',
            'Windows Update checks available required and optional drivers from your configured update service; organization policy can restrict this source.',
            'No Windows Update offer does not mean every driver is current. OEM tools and support sites may offer other releases, BIOS or firmware.',
            'An exact hardware or compatible ID can match several device instances. Offers are not guessed from device names.',
            'Missing signed-driver metadata alone does not prove a driver is missing; Windows problem code 28 identifies an uninstalled device driver.',
            'Support links open official product-selection pages. They do not download drivers or verify that a vendor package is compatible.',
            'If no vendor route is recognized, use your PC or motherboard manufacturer support page and the exact model/hardware IDs shown here; Device Manager can inspect the device.',
            'Existing saved driver approvals, backup requirement, restart guards and installation permissions still apply.',
        ],
    }



