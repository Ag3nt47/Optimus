"""Independent, preview-first Windows maintenance controls for Optimus.

Only explicit apply methods mutate Windows. Preview methods never remove apps.
CCleaner code, licenses and personal inventory are not part of this module.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any
from .automated_software import is_newer_version


_ACTIONS = (
    {"id": "consumer-apps", "name": "Optional Windows apps", "description": "Review a curated exact-name list. Store, App Installer, security, frameworks, Xbox and OneDrive are preserved.", "admin": False},
    {"id": "m365", "name": "Microsoft 365 consumer apps", "description": "Review Microsoft 365 hub/companion Store apps. Desktop Office, Outlook and OneDrive are separate.", "admin": False},
    {"id": "outlook", "name": "New Outlook app", "description": "Review the optional Microsoft.OutlookForWindows Store app separately. Local app data can be removed.", "admin": False},
    {"id": "edge", "name": "Microsoft Edge browser", "description": "Use an installed uninstaller. Windows may restrict removal. Shared WebView2 and update services are preserved.", "admin": False},
    {"id": "office-desktop", "name": "Desktop Microsoft Office uninstall", "description": "Find registered desktop Office products and open Windows Programs and Features for their uninstall flow.", "admin": False},
    {"id": "desktop-edge-script", "name": "Existing Desktop Edge removal script", "description": "Optional integration: review the existing script on the Windows Desktop, then launch its visible administrator console with its own typed APPLY gate. It changes region/update policies and has no complete undo.", "admin": True, "external": True},
)
_ALLOWED_ACTIONS = frozenset(item["id"] for item in _ACTIONS)
_PLAN_ID = re.compile(r"^[a-f0-9]{32}$")
_FINGERPRINT = re.compile(r"^[a-fA-F0-9]{64}$")
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$")
_TASK_NAME = re.compile(r"^\\?Optimus-Audit-[A-Za-z0-9\-]{1,60}$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class UpdatePreflightError(ValueError):
    """A rejected update attempt that did not start a package installer."""
    installer_started = False
    outcomeKnown = True
    running = False


class UpdateOutcomeError(RuntimeError):
    """An installer was launched but its final state cannot be established."""
    installer_started = True
    outcomeKnown = False
    running = True


class Maintenance:
    """Bridge-backed optional app removals and native Windows entry points.

    debloat_apply requires {planId, selectedIds: [...], confirmed: True}.
    The caller must obtain confirmation after displaying the concrete preview.
    Caller-supplied command strings, target payloads and action changes are ignored.
    """

    def __init__(self, bridge: Any, data_dir: str | Path):
        self.bridge = bridge
        self.data_dir = Path(data_dir)
        self.plan_dir = self.data_dir / "MaintenancePlans"
        self.log_dir = self.data_dir / "MaintenanceHistory"
        self._last_update_ids: set[str] = set()
        self._last_update_packages: dict[str, dict[str, str]] = {}
        self._last_update_scan = 0.0

    @staticmethod
    def debloat_actions() -> list[dict[str, Any]]:
        return [dict(item) for item in _ACTIONS]

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="optimus-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _request(self, operation: str, **arguments: Any) -> Any:
        return self.bridge.request(operation, **arguments)

    def debloat_preview(self, action: str | dict[str, Any]) -> dict[str, Any]:
        options = {"action": action} if isinstance(action, str) else dict(action)
        action_id = options.get("action", options.get("id"))
        if action_id not in _ALLOWED_ACTIONS:
            raise ValueError("Unknown optional-app action.")
        provisioned = options.get("includeProvisioned", False)
        if not isinstance(provisioned, bool):
            raise ValueError("includeProvisioned must be true or false.")
        if provisioned and action_id in {"edge", "office-desktop", "desktop-edge-script"}:
            raise ValueError("Provisioning applies only to optional Store-app actions.")
        response = self._request("maintenance_preview", action=action_id, includeProvisioned=provisioned)
        if not isinstance(response, dict):
            raise RuntimeError("The maintenance preview returned an invalid response.")
        targets = response.get("targets") or []
        if not isinstance(targets, list) or len(targets) > 100:
            raise RuntimeError("The maintenance preview returned an invalid target list.")
        identifiers: set[str] = set()
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get("id"), str):
                raise RuntimeError("A maintenance target has no valid identity.")
            if target["id"] in identifiers or not _FINGERPRINT.fullmatch(str(target.get("fingerprint", ""))):
                raise RuntimeError("A maintenance target has an invalid or repeated fingerprint.")
            identifiers.add(target["id"])
            target["selected"] = False
        plan = {
            "planId": uuid.uuid4().hex,
            "action": action_id,
            "summary": response.get("summary", "Review the optional apps before selecting any removal."),
            "targets": targets,
            "commands": response.get("commands", []),
            "recovery": response.get("recovery", "Reinstallation does not restore deleted app-local data."),
            "restartNeeded": bool(response.get("restartNeeded", False)),
            "includeProvisioned": provisioned,
            "createdUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "createdEpoch": time.time(),
            "state": "preview",
            "canApply": bool(targets),
        }
        if response.get("externalPreview") is not None:
            plan["externalPreview"] = response["externalPreview"]
            if response["externalPreview"].get("exitCode") != 0:
                plan["canApply"] = False
                plan["summary"] += " The external script preview failed; resolve that before applying."
        self._write_json(self.plan_dir / (plan["planId"] + ".json"), plan)
        return plan

    def debloat_apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("confirmed") is not True:
            raise ValueError("Confirm the displayed removal plan explicitly before applying it.")
        plan_id = str(plan.get("planId", ""))
        if not _PLAN_ID.fullmatch(plan_id):
            raise ValueError("Invalid maintenance plan identity.")
        path = self.plan_dir / (plan_id + ".json")
        with path.open("r", encoding="utf-8") as stream:
            saved = json.load(stream)
        if saved.get("planId") != plan_id or saved.get("action") not in _ALLOWED_ACTIONS:
            raise ValueError("The saved maintenance plan is invalid.")
        if saved.get("state") != "preview" or not saved.get("canApply"):
            raise ValueError("Create a fresh preview before applying another removal.")
        if time.time() - float(saved.get("createdEpoch", 0)) > 1800:
            raise ValueError("The maintenance preview expired. Preview the installed apps again.")
        selected = plan.get("selectedIds")
        if not isinstance(selected, list) or not selected or not all(isinstance(item, str) for item in selected):
            raise ValueError("Select at least one reviewed target.")
        if len(selected) != len(set(selected)):
            raise ValueError("A removal target was selected more than once.")
        known = {item["id"]: item for item in saved["targets"]}
        if any(item not in known for item in selected):
            raise ValueError("The selection includes a target outside this preview.")
        # Persist the exact reviewed inventory before the first Windows mutation.
        saved["selectedIds"] = selected
        saved["state"] = "applying"
        self._write_json(path, saved)
        try:
            result = self._request(
                "maintenance_apply", action=saved["action"],
                includeProvisioned=bool(saved.get("includeProvisioned", False)),
                targets=[known[item] for item in selected], confirmation="REMOVE",
            )
        except Exception as error:
            saved["state"] = "failed"
            saved["error"] = str(error)
            self._write_json(path, saved)
            self._write_json(self.log_dir / (plan_id + ".json"), saved)
            raise
        saved["state"] = "completed"
        saved["result"] = result
        self._write_json(path, saved)
        self._write_json(self.log_dir / (plan_id + ".json"), saved)
        if isinstance(result, dict):
            result["planId"] = plan_id
            result["historyFile"] = str(self.log_dir / (plan_id + ".json"))
        return result

    def registry_scan(self) -> Any:
        """Read-only diagnostics; no bulk registry repair is exposed."""
        return self._request("registry_audit")

    def registry_export(self, destination: str | Path | None = None) -> dict[str, Any]:
        report = self.registry_scan()
        path = Path(destination) if destination else self.data_dir / "Reports" / ("registry-audit-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
        self._write_json(path, report)
        return {"path": str(path), "report": report}

    def drivers_get(self) -> Any:
        return self._request("drivers_list")

    def driver_updates_scan(self) -> Any:
        return self._request("driver_updates_scan")

    def driver_update_apply(self, update_id: str, revision: int, *, confirmed: bool = False) -> Any:
        if confirmed is not True:
            raise ValueError("Confirm the selected Windows Update driver before installing it.")
        try:
            parsed = str(uuid.UUID(update_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Invalid Windows Update driver identity.") from error
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("Invalid driver update revision.")
        return self._request("driver_update_apply", id=parsed, revision=revision)

    @staticmethod
    def _run(arguments: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @staticmethod
    def _winget() -> str:
        executable = shutil.which("winget.exe")
        if not executable:
            raise RuntimeError("Windows Package Manager is not available. Install or update Microsoft App Installer from Microsoft Store.")
        return executable

    @staticmethod
    def _parse_update_table(output: str, *, source_context: str | None = None) -> tuple[list[dict[str, str]], bool, list[str]]:
        """Accept only complete identities from the recognized English CLI table.

        WinGet does not document JSON output for upgrade/list. Unknown layouts
        stay text; fixed column boundaries plus explicit source prevent guessing.
        Source-scoped WinGet versions omit Source: only the native caller that
        fixed --source winget may supply that narrow four-column context.
        """
        lines = output.splitlines()
        headers = [(index, line) for index, line in enumerate(lines)
                   if (re.fullmatch(r"Name +Id +Version +Available +Source *", line)
                       or source_context == "winget" and re.fullmatch(r"Name {2,}Id {2,}Version {2,}Available *", line))]
        if len(headers) != 1:
            return [], False, ["Unsupported, missing or repeated update-table header."]
        index, header = headers[0]
        if index + 1 >= len(lines) or not re.fullmatch(r"-{10,}", lines[index + 1].strip()):
            return [], False, ["Update-table separator was not recognized."]
        has_source = bool(re.fullmatch(r"Name +Id +Version +Available +Source *", header))
        columns = ("Name", "Id", "Version", "Available", "Source") if has_source else ("Name", "Id", "Version", "Available")
        starts = [header.index(key) for key in columns]
        rows: list[dict[str, str]] = []
        skipped: list[str] = []
        counts: dict[str, int] = {}
        version_pattern = re.compile(r"[vV]?[0-9][0-9A-Za-z._+~\-]{0,127}")
        for line in lines[index + 2:]:
            if not line.strip():
                continue
            if re.fullmatch(r"\d+ upgrades? available\.", line.strip()):
                continue
            if re.fullmatch(r"\d+ package\(s\) have version numbers that cannot be determined\. Use --include-unknown to see all results\.", line.strip()):
                skipped.append("Installed packages with unknown versions require their publisher updater or version review.")
                continue
            values = [line[start:starts[position + 1] if position < len(starts) - 1 else len(line)].strip()
                      for position, start in enumerate(starts)]
            name, package_id, version, available = values[:4]
            source = values[4] if has_source else source_context
            # Footers/progress are not data. IDs, versions and source must be
            # complete and aligned; truncated names are harmless display text.
            if not _PACKAGE_ID.fullmatch(package_id) or package_id.endswith(".") or "..." in package_id or not name:
                skipped.append("An unrecognized or truncated update row was excluded.")
                continue
            if source != "winget":
                skipped.append("A row without the exact winget source was excluded.")
                continue
            key = package_id.casefold()
            counts[key] = counts.get(key, 0) + 1
            if (not version_pattern.fullmatch(version) or not version_pattern.fullmatch(available)
                    or version.endswith(".") or available.endswith(".") or ".." in version or ".." in available):
                skipped.append("A package with unknown, truncated or unsupported versions was excluded.")
                continue
            if not is_newer_version(version, available):
                skipped.append("A package without a provably newer comparable version was excluded.")
                continue
            rows.append({"name": name, "id": package_id, "package_id": package_id,
                         "version": version, "available": available, "source": source})
        duplicates = {key for key, count in counts.items() if count != 1}
        if duplicates:
            skipped.append("Ambiguous repeated package IDs were excluded.")
            rows = [row for row in rows if row["id"].casefold() not in duplicates]
        return rows, True, skipped

    def _read_only_update_search(self, cancel_event):
        """Cancel only our exact WinGet listing child; never an installer.

        A request remains pending until the child has exited. Native update
        apply commands continue using the non-interruptible installer runner.
        """
        arguments = [self._winget(), 'upgrade', '--source', 'winget', '--disable-interactivity']
        if cancel_event.is_set():
            return subprocess.CompletedProcess(arguments, 130, '', 'Read-only update search cancelled.')
        child = subprocess.Popen(arguments, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 120
        requested = False
        stop_deadline = None
        while True:
            try:
                output, errors = child.communicate(timeout=0.1)
                return subprocess.CompletedProcess(arguments, 130 if requested else child.returncode,
                    output, errors or ('Read-only update search cancelled.' if requested else ''))
            except subprocess.TimeoutExpired:
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    if not requested:
                        requested = True
                        stop_deadline = time.monotonic() + 2
                        try: child.terminate()
                        except OSError: pass  # Retain the child and wait for proven exit.
                    elif stop_deadline is not None and time.monotonic() >= stop_deadline:
                        try: child.kill()
                        except OSError: pass  # Failed termination never establishes quiescence.
                        stop_deadline = None
                    # communicate() must prove this direct read-only child is
                    # quiescent before the caller clears its pending request.

    def updates_scan(self, *, cancel_event=None) -> dict[str, Any]:
        """Read the winget source only; never accept agreements during a scan."""
        self._last_update_ids = set()
        self._last_update_packages = {}
        self._last_update_scan = 0.0
        started = time.time()
        result = (self._read_only_update_search(cancel_event) if cancel_event is not None else
                  self._run([self._winget(), "upgrade", "--source", "winget", "--disable-interactivity"]))
        output = _ANSI.sub("", result.stdout or "").replace("\r", "")
        stderr = _ANSI.sub("", result.stderr or "")
        rows, structured, skipped = self._parse_update_table(output, source_context="winget")
        reported = re.findall(r"^(\d+) upgrades? available\.$", output, re.M)
        reported_count = int(reported[0]) if len(reported) == 1 and len(reported[0]) <= 6 else None
        if reported and (reported_count is None or reported_count != len(rows)):
            skipped.append("The source-reported upgrade count does not match the recognized update rows.")
        if stderr.strip():
            skipped.append("WinGet reported diagnostic output; source coverage requires review.")
        # ReportListResult(true) emits this single line, without a table, when
        # a successful source-scoped listing has no selectable upgrades. Do not
        # mistake the normal empty state for an unsupported table or generalize
        # single-package no-upgrade HRESULTs into successful source scans.
        no_applicable_upgrades = (result.returncode == 0 and not stderr.strip()
                                  and output.strip() == "No installed package found matching input criteria.")
        if no_applicable_upgrades:
            rows, structured, skipped = [], True, []
        if result.returncode != 0:
            rows, structured = [], False
            skipped.append("WinGet returned a nonzero status; no automatic candidates are trusted.")
        self._last_update_ids = {row["id"] for row in rows}
        self._last_update_packages = {row["id"]: dict(row) for row in rows}
        self._last_update_scan = time.time() if structured else 0.0
        return {"updates": rows, "packages": [dict(row) for row in rows], "text": output, "output": output,
                "stderr": stderr, "exitCode": result.returncode,
                "structured": structured, "source": "winget", "excluded": skipped,
                "source_reported_offers_count": reported_count,
                "discovery": {"started_at": started, "completed_at": time.time(),
                              "command": "winget upgrade --source winget --disable-interactivity"},
                "no_applicable_upgrades": no_applicable_upgrades,
                "note": "Only recognized complete winget-source rows are selectable. Localized or changed output remains text; an empty parsed list does not establish that no updates exist."}

    @staticmethod
    def _parse_pins(output: str) -> tuple[list[dict[str, str]], bool]:
        """Recognize English pin output; missing or partial output is never no pins.

        Installed-version pins use product/family keys rather than repository IDs.
        Their full names conservatively protect matching installed applications.
        """
        lines = [line for line in output.splitlines() if line.strip()]
        if len(lines) == 1 and lines[0].strip() == 'There are no pins configured.':
            return [], True
        headers = [(index, line) for index, line in enumerate(lines)
                   if re.fullmatch(r'Name +Id +Version +Source +Pin type +Pinned version *', line)]
        if len(headers) != 1:
            return [], False
        index, header = headers[0]
        if index + 1 >= len(lines) or not re.fullmatch(r'-{10,}', lines[index + 1].strip()):
            return [], False
        labels = ('Name', 'Id', 'Version', 'Source', 'Pin type', 'Pinned version')
        starts = [header.index(label) for label in labels]
        rows = []
        for line in lines[index + 2:]:
            values = [line[start:starts[position + 1] if position < 5 else len(line)].strip()
                      for position, start in enumerate(starts)]
            name, identity, version, source, kind, pinned_version = values
            if not identity or not source or not kind or any('...' in part or '\u2026' in part for part in values):
                return [], False
            if source == 'Installed' and not name:
                return [], False
            if source != 'Installed' and (not _PACKAGE_ID.fullmatch(identity) or identity.endswith('.')):
                return [], False
            rows.append({'name': name, 'id': identity, 'version': version,
                         'source': source, 'kind': kind, 'pinned_version': pinned_version})
        # An empty table can hide orphaned pin records; only the explicit message
        # proves there were no configured pins.
        return rows, bool(rows)

    def _pin_status(self, package: dict[str, str]) -> tuple[bool, str, bool]:
        """A fresh check covers ordinary pins exact WinGet upgrades can bypass."""
        try:
            result = self._run([self._winget(), 'pin', 'list', '--disable-interactivity'])
        except (OSError, subprocess.TimeoutExpired):
            return False, 'The current WinGet pin inventory could not be read; no installer was started.', True
        if result.returncode != 0:
            return False, 'The current WinGet pin inventory failed; no installer was started.', True
        rows, recognized = self._parse_pins(_ANSI.sub('', result.stdout or '').replace('\r', ''))
        if not recognized:
            return False, 'The current WinGet pin inventory is unrecognized or incomplete; no installer was started.', True
        for row in rows:
            if row['id'].casefold() == package['id'].casefold():
                return False, 'This application has a WinGet pin; Optimus preserves it.', False
            if row['source'] == 'Installed':
                name = package.get('name', '')
                if not name or '...' in name or '\u2026' in name:
                    return False, 'An installed-version pin cannot be compared with this truncated application name.', True
                if row['name'].casefold() == name.casefold():
                    return False, 'This application has an installed-version WinGet pin; Optimus preserves it.', False
        return True, '', False
    def updates_apply(self, package_id: str, *, confirmed: bool = False, unattended: bool = False, cancel_event=None, compatibility_mode: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise UpdatePreflightError("Confirm the selected software update before installing it.")
        if type(unattended) is not bool:
            raise UpdatePreflightError("Choose an explicit unattended mode.")
        if type(compatibility_mode) is not bool:
            raise UpdatePreflightError("Choose an explicit non-security compatibility mode.")
        if not isinstance(package_id, str) or not _PACKAGE_ID.fullmatch(package_id):
            raise UpdatePreflightError("Select an exact package ID from a fresh update scan.")
        current = self._last_update_packages.get(package_id)
        age = time.time() - self._last_update_scan
        if (package_id not in self._last_update_ids or not current or current.get("source") != "winget"
                or not is_newer_version(current.get('version'), current.get('available'))
                or self._last_update_scan <= 0 or age < 0):
            raise UpdatePreflightError("Select an unambiguous winget package from a fresh update scan.")
        # Retain the exact previously selected version pair, even when a prior
        # installer took over fifteen minutes. Fresh revalidation below must
        # still match that pair; an aged cache never authorizes a broader offer.
        expected = dict(current)
        def cancelled_result():
            if cancel_event is not None and cancel_event.is_set():
                return {"id": package_id, "package_id": package_id, "source": "winget", "status": "cancelled",
                        "cancelled": True, "skipped": True, "exitCode": None, "unattended": unattended,
                        "installer_started": False, "outcomeKnown": True, "running": False,
                        "from_version": (current or expected).get("version"), "to_version": (current or expected).get("available"),
                        "note": "Cancelled during update safety checks; no installer was started."}
            return None
        if (interrupted := cancelled_result()) is not None:
            return interrupted
        def held_result():
            if not unattended:
                return None
            from .update_holds import load_update_holds, matching_update_hold, held_update_result
            hold = matching_update_hold(current, load_update_holds(self.data_dir))
            return held_update_result(current, hold) if hold else None
        # Avoid a known bad exact offer before any network/pin revalidation.
        # Read again at the final launch boundary in case an operator added a
        # hold while the source or pin checks were running.
        if (held := held_result()) is not None:
            return held
        # Revalidate both manual and unattended updates. An external updater may
        # have already installed the offered version since the preview.
        try:
            fresh = self.updates_scan()
        except Exception as error:
            raise UpdatePreflightError("The update revalidation failed before any installer started (" + type(error).__name__ + ").") from error
        if not isinstance(fresh, dict) or fresh.get('structured') is not True or type(fresh.get('exitCode')) is not int or fresh['exitCode'] != 0:
            raise UpdatePreflightError("The update revalidation was incomplete; no installer was started.")
        current = self._last_update_packages.get(package_id)
        if (interrupted := cancelled_result()) is not None:
            return interrupted
        if not current or any(current.get(field) != expected.get(field) for field in ('id', 'version', 'available', 'source')):
            return {"id": package_id, "package_id": package_id, "source": "winget", "status": "skipped", "skipped": True,
                    "exitCode": None, "unattended": unattended, "failureKind": "version_changed",
                    "installer_started": False, "outcomeKnown": True, "running": False,
                    "from_version": (current or expected).get("version"), "to_version": (current or expected).get("available"),
                    "note": "The installed or available version changed during revalidation; no installer was started."}
        try:
            unpinned, reason, needs_review = self._pin_status(current)
        except Exception as error:
            raise UpdatePreflightError("The pin safety check failed before any installer started (" + type(error).__name__ + ").") from error
        if (interrupted := cancelled_result()) is not None:
            return interrupted
        if not unpinned:
            return {"id": package_id, "package_id": package_id, "source": "winget", "status": "skipped", "skipped": True,
                    "exitCode": None, "unattended": unattended, "note": reason, "requiresReview": needs_review,
                    "installer_started": False, "outcomeKnown": True, "running": False,
                    "failureKind": "pin_inventory_untrusted" if needs_review else "pinned",
                    "from_version": current["version"], "to_version": current["available"]}
        try:
            executable = self._winget()
        except Exception as error:
            raise UpdatePreflightError("WinGet became unavailable before any installer started.") from error
        arguments = [executable, "upgrade", "--id", package_id, "--exact", "--source", "winget", "--version", current['available']]
        if compatibility_mode:
            arguments += ["--force"]
        if unattended:
            arguments += ["--silent", "--disable-interactivity", "--accept-source-agreements", "--accept-package-agreements"]
        else:
            arguments += ["--disable-interactivity"]
        try:
            if (interrupted := cancelled_result()) is not None:
                return interrupted
            if (held := held_result()) is not None:
                return held
            result = self._run(arguments, timeout=1800)
        except subprocess.TimeoutExpired:
            # The vendor child can outlive WinGet. Never retry or replay here.
            raise UpdateOutcomeError("The update timed out. A vendor installer may still be running; verify its outcome before trying again.") from None
        except (FileNotFoundError, PermissionError) as error:
            # These launch errors occur before CreateProcess starts WinGet;
            # a missing/replaced executable is not a detached installer.
            raise UpdatePreflightError("WinGet could not be started; no installer was launched.") from error
        output = _ANSI.sub("", result.stdout or "")
        stderr = _ANSI.sub("", result.stderr or "")
        native_code = result.returncode & 0xffffffff
        # WinGet can translate an installer's 3010 into successful exit 0 while
        # reporting the required restart as text. Recognize that documented case.
        restart = (native_code in (3010, 1641, 0x8A150109, 0x8A15010A, 0x8A15010B)
                   or any(message in (output + '\n' + stderr).casefold() for message in (
                       'restart your pc to finish installation.', 'your pc will restart to finish installation.',
                       'installation failed. restart your pc then try again.')))
        installer_running = (native_code in (1618, 0x8A150102)
                             or 'another installation is already in progress.' in (output + '\n' + stderr).casefold())
        return {"id": package_id, "package_id": package_id, "source": "winget", "exitCode": result.returncode,
                "status": "completed" if result.returncode in (0, 3010, 1641) else "failed", "unattended": unattended,
                "compatibility_mode": compatibility_mode,
                "restartNeeded": True if restart else None, "running": installer_running,
                "installer_started": True, "outcomeKnown": not installer_running,
                "from_version": current["version"], "to_version": current["available"],
                "text": output, "stderr": stderr,
                "note": (("The selected mode accepts vendor/source agreements and requests a silent installer. Elevation or app closure may still be required. No automatic-reboot permission or retry was requested."
                          if unattended else "A vendor installer can request administrator approval or a restart. No license/source agreement was accepted automatically.")
                         + (" Saved compatibility mode adds WinGet --force for non-security issues only. Hash/signature verification and installer hard blocks remain in effect." if compatibility_mode else ""))}

    @staticmethod
    def windows_update_open() -> dict[str, str]:
        if os.name != "nt":
            raise RuntimeError("Windows Update settings are available only on Windows.")
        os.startfile("ms-settings:windowsupdate")
        return {"status": "Opened Windows Update settings"}

    def schedules_list(self) -> list[dict[str, str]]:
        result = self._run(["schtasks.exe", "/Query", "/FO", "CSV"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Windows Task Scheduler could not be queried.")
        rows = list(csv.reader(io.StringIO(result.stdout)))
        return [{"name": row[0], "nextRun": row[1], "state": row[2]} for row in rows[1:] if len(row) >= 3 and _TASK_NAME.fullmatch(row[0])]

    def schedule_create(self, hour: int = 12, minute: int = 0, *, confirmed: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("Confirm creation of the displayed daily audit task.")
        if not isinstance(hour, int) or not isinstance(minute, int) or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Choose a valid local time for the audit.")
        if not getattr(sys, "frozen", False):
            raise RuntimeError("Install the packaged Optimus app before scheduling it. A development Python invocation is not a stable task target.")
        executable = str(Path(sys.executable).resolve(strict=True))
        name = "Optimus-Audit-" + uuid.uuid4().hex[:12]
        target = subprocess.list2cmdline([executable, "--audit"])
        result = self._run(["schtasks.exe", "/Create", "/TN", name, "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}", "/TR", target, "/IT", "/RL", "LIMITED"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task creation failed.")
        return {"name": name, "time": f"{hour:02d}:{minute:02d}", "action": "Read-only Optimus --audit", "status": "Created; runs only while this user is signed in"}

    def schedule_delete(self, name: str, *, confirmed: bool = False) -> dict[str, str]:
        if confirmed is not True:
            raise ValueError("Confirm deletion of the selected Optimus audit schedule.")
        if not _TASK_NAME.fullmatch(name):
            raise ValueError("Only Optimus audit tasks can be deleted here.")
        result = self._run(["schtasks.exe", "/Delete", "/TN", name, "/F"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Task deletion failed.")
        return {"name": name, "status": "Deleted"}
