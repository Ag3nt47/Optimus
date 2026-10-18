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
        return subprocess.run(arguments, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @staticmethod
    def _winget() -> str:
        executable = shutil.which("winget.exe")
        if not executable:
            raise RuntimeError("Windows Package Manager is not available. Install or update Microsoft App Installer from Microsoft Store.")
        return executable

    def updates_scan(self) -> dict[str, Any]:
        """Use installed winget; source agreements are never accepted silently."""
        result = self._run([self._winget(), "upgrade", "--disable-interactivity"])
        output = _ANSI.sub("", result.stdout or "").replace("\r", "")
        rows: list[dict[str, str]] = []
        lines = output.splitlines()
        # English winget column output is parsed only when its complete header is recognized.
        header = next((line for line in lines if re.search(r"\bName\s+Id\s+Version\s+Available\b", line)), "")
        if header:
            columns = [(key, header.index(key)) for key in ("Name", "Id", "Version", "Available")]
            source_start = header.find("Source")
            for line in lines[lines.index(header) + 1:]:
                if len(line) <= columns[-1][1] or set(line.strip()) <= {"-", " "}:
                    continue
                values = {}
                for index, (key, start) in enumerate(columns):
                    end = columns[index + 1][1] if index + 1 < len(columns) else (source_start if source_start >= 0 else len(line))
                    values[key.lower()] = line[start:end].strip()
                if _PACKAGE_ID.fullmatch(values.get("id", "")) and values.get("available"):
                    rows.append(values)
        self._last_update_ids = {row["id"] for row in rows}
        return {"updates": rows, "text": output, "stderr": _ANSI.sub("", result.stderr or ""), "exitCode": result.returncode, "structured": bool(header), "source": "Installed Windows Package Manager", "note": "Localized or changed output remains available as text. Do not infer that no updates exist from an empty parsed list."}

    def updates_apply(self, package_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("Confirm the selected software update before installing it.")
        if not _PACKAGE_ID.fullmatch(package_id) or package_id not in self._last_update_ids:
            raise ValueError("Select an exact package from a fresh update scan.")
        result = self._run([self._winget(), "upgrade", "--id", package_id, "--exact", "--disable-interactivity"], timeout=1800)
        return {"id": package_id, "exitCode": result.returncode, "text": _ANSI.sub("", result.stdout or ""), "stderr": _ANSI.sub("", result.stderr or ""), "note": "A vendor installer can request administrator approval or a restart. No license/source agreement was accepted automatically."}

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
