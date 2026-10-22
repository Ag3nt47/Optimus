"""Local browser privacy cleanup with review, SQLite backups and guarded undo.

Plans contain counts and schema metadata, never cookie values or visited URLs.
Backups are private local database copies and must never be bundled or uploaded.
"""
from __future__ import annotations
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from typing import Any
import psutil
from optimus.browser_paths import native_locations, process_matches

_PLATFORM = sys.platform

_CATEGORIES = frozenset(("history", "cookies", "downloads"))
_HEX_ID = re.compile(r"^[a-f0-9]{32}$")
_DOM_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$")


def _identity(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size, "mtime": stat.st_mtime_ns}


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="privacy-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


@contextmanager
def _ro(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    try:
        yield connection
    finally:
        connection.close()


def _domains(values: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("Keep domains must be a list of plain host names.")
    result = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("Keep domains must be plain host names.")
        try:
            domain = raw.strip().lstrip(".").rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("A keep domain is invalid.") from error
        if not domain or len(domain) > 253 or not all(_DOM_LABEL.fullmatch(label) for label in domain.split(".")):
            raise ValueError("Use a plain domain such as example.com, without a URL, wildcard or path.")
        if domain not in result:
            result.append(domain)
    if len(result) > 100:
        raise ValueError("At most 100 keep domains can be selected.")
    return result


def _cookie_where(column: str, keep: list[str]) -> tuple[str, list[str]]:
    if not keep:
        return "1", []
    terms, parameters = [], []
    for domain in keep:
        terms.append(f"(ltrim(lower({column}), '.') = ? OR ltrim(lower({column}), '.') LIKE ?)")
        parameters.extend((domain, "%." + domain))
    return "NOT (" + " OR ".join(terms) + ")", parameters


class BrowserPrivacy:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.backup_root = self.data_dir / "BrowserPrivacyBackups"
        self._profiles: dict[str, dict[str, Any]] = {}
        self._plans: dict[str, dict[str, Any]] = {}

    def _discover(self) -> list[dict[str, Any]]:
        if _PLATFORM != 'win32':
            found = []
            for browser in native_locations(_PLATFORM):
                root = browser['profile_root']
                try:
                    for profile in root.iterdir():
                        if not profile.is_dir():
                            continue
                        if browser['family'] == 'firefox':
                            recognized = (profile / 'places.sqlite').is_file()
                        else:
                            recognized = profile.name == 'Default' or bool(re.fullmatch(r'Profile \d+', profile.name))
                        if recognized:
                            found.append({'browser': browser['name'], 'browser_id': browser['id'],
                                'name': profile.name, 'path': str(profile.absolute()), 'root': str(root.absolute()),
                                'family': browser['family'], 'process': browser['process'],
                                'process_names': list(browser['process_names'])})
                except OSError:
                    continue
            return found
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        roots = (("Microsoft Edge", local / "Microsoft" / "Edge" / "User Data", "msedge.exe"),
                 ("Google Chrome", local / "Google" / "Chrome" / "User Data", "chrome.exe"),
                 ("Chromium", local / "Chromium" / "User Data", "chromium.exe"),
                 ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data", "brave.exe"),
                 ("Vivaldi", local / "Vivaldi" / "User Data", "vivaldi.exe"))
        found = []
        for browser, root, process in roots:
            if not root.is_dir():
                continue
            for profile in root.iterdir():
                if profile.is_dir() and (profile.name == "Default" or re.fullmatch(r"Profile \d+", profile.name)):
                    found.append({"browser": browser, "name": profile.name, "path": str(profile.absolute()), "root": str(root.absolute()), "family": "chromium", "process": process})
        firefox = roaming / "Mozilla" / "Firefox" / "Profiles"
        if firefox.is_dir():
            for profile in firefox.iterdir():
                if profile.is_dir() and (profile / "places.sqlite").is_file():
                    found.append({"browser": "Mozilla Firefox", "name": profile.name, "path": str(profile.absolute()), "root": str(firefox.absolute()), "family": "firefox", "process": "firefox.exe"})
        return found

    def profiles(self) -> list[dict[str, Any]]:
        result = []
        for profile in self._discover():
            profile_id = hashlib.sha256((profile["browser"] + "|" + os.path.normcase(profile["path"])).encode("utf-8")).hexdigest()[:32]
            item = dict(profile, id=profile_id)
            self._profiles[profile_id] = item
            result.append({"id": profile_id, "browser": item["browser"], "name": item["name"], "path": item["path"], "family": item["family"], "supportedCategories": ["history", "cookies", "downloads"] if item["family"] == "chromium" else ["history", "cookies"]})
        return result

    def _profile(self, profile_id: str) -> dict[str, Any]:
        if profile_id not in self._profiles:
            self.profiles()
        if profile_id not in self._profiles:
            raise ValueError("Choose an existing recognized browser profile.")
        profile = self._profiles[profile_id]
        path, root = Path(profile["path"]), Path(profile["root"])
        if not path.is_dir() or path.parent != root or _link(root) or _link(path):
            raise ValueError("The browser profile moved or uses an indirect path; rescan profiles.")
        return profile

    @staticmethod
    def _closed(profile: dict[str, Any]) -> None:
        wanted = profile["process"].lower()
        try:
            for process in psutil.process_iter(["pid", "name"]):
                name = process.info.get('name')
                if not isinstance(name, str) or not name:
                    raise RuntimeError('A running process name is inaccessible; browser closure cannot be verified.')
                if name.lower() == wanted or profile.get('browser_id') and process_matches(profile['browser_id'], name, _PLATFORM):
                    raise RuntimeError(f"Close every {profile['browser']} window and its background processes before scanning, cleaning or restoring this profile.")
        except (psutil.AccessDenied, psutil.Error) as error:
            raise RuntimeError("Running browser processes could not be checked safely.") from error

    @staticmethod
    def _specs(profile: dict[str, Any], categories: list[str]) -> list[dict[str, Any]]:
        path = Path(profile["path"])
        result = []
        history_categories = [item for item in categories if item != "cookies"]
        if history_categories:
            relative = "History" if profile["family"] == "chromium" else "places.sqlite"
            if (path / relative).is_file():
                result.append({"relative": relative, "kind": "history", "categories": history_categories})
        if "cookies" in categories:
            if profile["family"] == "firefox":
                relative = "cookies.sqlite"
            else:
                relative = "Network/Cookies" if (path / "Network" / "Cookies").is_file() else "Cookies"
            if (path / relative).is_file():
                result.append({"relative": relative, "kind": "cookies", "categories": ["cookies"]})
        return result

    @staticmethod
    def _path(profile: dict[str, Any], relative: str) -> Path:
        permitted = {"History", "Cookies", "Network/Cookies"} if profile["family"] == "chromium" else {"places.sqlite", "cookies.sqlite"}
        if relative not in permitted:
            raise ValueError("A database target is outside the approved profile schema.")
        result = Path(profile["path"]) / relative
        if _link(result) or _link(result.parent):
            raise ValueError("Linked browser databases cannot be cleaned or restored.")
        return result

    @staticmethod
    def _files(path: Path) -> dict[str, Any]:
        return {"database": _identity(path), "wal": _identity(Path(str(path) + "-wal")), "shm": _identity(Path(str(path) + "-shm")), "journal": _identity(Path(str(path) + "-journal"))}

    @staticmethod
    def _no_pending_journal(path: Path) -> None:
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise RuntimeError("The database has a pending WAL or rollback journal. Open and close this browser normally before retrying; no data was changed.")

    @staticmethod
    def _schema(connection: sqlite3.Connection, family: str, spec: dict[str, Any]) -> dict[str, set[str]]:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required: dict[str, set[str]] = {}
        if spec["kind"] == "cookies":
            required = {"cookies": {"host_key", "name"}} if family == "chromium" else {"moz_cookies": {"host", "name", "value"}}
        elif family == "firefox":
            required = {"moz_historyvisits": {"id", "place_id"}, "moz_places": {"id", "url"}, "moz_bookmarks": {"id", "fk"}}
        else:
            if "history" in spec["categories"]:
                required.update({"urls": {"id", "url"}, "visits": {"id", "url"}})
            if "downloads" in spec["categories"]:
                required["downloads"] = {"id"}
                required["downloads_url_chains"] = {"id", "url"}
        schema = {}
        for table in tables:
            # Table names from the database are quoted as identifiers; never evaluated.
            quoted = table.replace('"', '""')
            schema[table] = {row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')}
        for table, columns in required.items():
            if table not in schema or not columns <= schema[table]:
                raise ValueError(f"Unsupported browser database schema for {table}; use the browser's own privacy controls.")
        relevant = set(required) | {"visit_source", "keyword_search_terms", "downloads_slices"}
        if any(row[0] in relevant for row in connection.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger'")):
            raise ValueError("This database has unrecognized persistent cleanup triggers; use native browser controls.")
        return schema

    @staticmethod
    def _counts(connection: sqlite3.Connection, family: str, spec: dict[str, Any], keep: list[str]) -> dict[str, int]:
        result = {}
        if spec["kind"] == "cookies":
            table, column = ("cookies", "host_key") if family == "chromium" else ("moz_cookies", "host")
            where, parameters = _cookie_where(column, keep)
            result["cookies"] = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", parameters).fetchone()[0]
            result["cookiesKept"] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - result["cookies"]
        else:
            if "history" in spec["categories"]:
                table = "visits" if family == "chromium" else "moz_historyvisits"
                result["history"] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if "downloads" in spec["categories"]:
                result["downloads"] = connection.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        return result

    def scan(self, profile_id: str, categories: tuple[str, ...] | list[str] = ("history", "cookies", "downloads"), keep_domains: list[str] | None = None) -> dict[str, Any]:
        profile = self._profile(profile_id)
        self._closed(profile)  # Must precede every database connection.
        chosen = list(dict.fromkeys(categories))
        if not chosen or any(item not in _CATEGORIES for item in chosen):
            raise ValueError("Choose history, cookies or download metadata.")
        keep = _domains(keep_domains or [])
        unsupported = []
        if profile["family"] == "firefox" and "downloads" in chosen:
            chosen.remove("downloads")
            unsupported.append({"category": "downloads", "reason": "Firefox download annotations vary by version; use Firefox native Clear Recent History for downloads."})
        counts = {item: 0 for item in chosen}
        specs = self._specs(profile, chosen)
        for spec in specs:
            path = self._path(profile, spec["relative"])
            self._no_pending_journal(path)
            before = self._files(path)
            with _ro(path) as connection:
                self._schema(connection, profile["family"], spec)
                counts.update(self._counts(connection, profile["family"], spec, keep))
            if self._files(path) != before:
                raise RuntimeError("The browser database changed during its scan. Close the browser and scan again.")
            spec["files"] = before
        plan_id = uuid.uuid4().hex
        cached = {"id": plan_id, "profileId": profile_id, "profilePath": profile["path"], "created": time.time(), "categories": chosen, "keep": keep, "specs": specs, "counts": counts}
        self._plans[plan_id] = cached
        return {"planId": plan_id, "profileId": profile_id, "profile": {"browser": profile["browser"], "name": profile["name"]}, "categories": chosen, "counts": counts, "unsupported": unsupported, "canApply": bool(specs), "expiresUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cached["created"] + 900)), "estimatedBackupBytes": sum(spec["files"]["database"]["size"] for spec in specs), "note": "Download cleanup removes the browser list, not downloaded files. Firefox bookmark/place rows remain. Password stores and bookmark files are never opened. Cleanup is logical deletion; private local backups retain the original data."}

    @staticmethod
    def _delete(connection: sqlite3.Connection, family: str, spec: dict[str, Any], keep: list[str]) -> int:
        schema = BrowserPrivacy._schema(connection, family, spec)
        before = connection.total_changes
        if spec["kind"] == "cookies":
            table, column = ("cookies", "host_key") if family == "chromium" else ("moz_cookies", "host")
            where, parameters = _cookie_where(column, keep)
            connection.execute(f"DELETE FROM {table} WHERE {where}", parameters)
        elif family == "firefox":
            connection.execute("DELETE FROM moz_historyvisits")
            setters = []
            if "visit_count" in schema["moz_places"]:
                setters.append("visit_count=0")
            if "last_visit_date" in schema["moz_places"]:
                setters.append("last_visit_date=NULL")
            if setters:
                connection.execute("UPDATE moz_places SET " + ",".join(setters))
        else:
            if "downloads" in spec["categories"]:
                for table in ("downloads_slices", "downloads_url_chains", "downloads"):
                    if table in schema:
                        connection.execute("DELETE FROM " + table)
            if "history" in spec["categories"]:
                for table in ("visit_source", "keyword_search_terms", "visits", "urls"):
                    if table in schema:
                        connection.execute("DELETE FROM " + table)
        return connection.total_changes - before

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(plan, dict) or plan.get("confirmed") is not True:
            raise ValueError("Confirm the displayed privacy cleanup before applying it.")
        plan_id = plan.get("planId")
        if not isinstance(plan_id, str) or not _HEX_ID.fullmatch(plan_id):
            raise ValueError("Invalid browser privacy preview identity.")
        cached = self._plans.get(plan_id)
        if not cached or time.time() - cached["created"] > 900:
            raise ValueError("This privacy preview expired or belongs to another session. Scan again.")
        profile = self._profile(cached["profileId"])
        self._closed(profile)
        selected = plan.get("selectedCategories", cached["categories"])
        if not isinstance(selected, list) or not selected or any(item not in cached["categories"] for item in selected):
            raise ValueError("Select only the categories shown in this preview.")
        specs = [dict(spec, categories=[item for item in spec["categories"] if item in selected]) for spec in cached["specs"] if any(item in selected for item in spec["categories"])]
        if not specs:
            raise ValueError("No supported existing database was selected.")
        for spec in specs:
            path = self._path(profile, spec["relative"])
            if self._files(path) != spec["files"]:
                raise ValueError("A browser database changed since preview. Scan again before cleaning.")
            self._no_pending_journal(path)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        required = sum(spec["files"]["database"]["size"] for spec in specs) + 64 * 1024 * 1024
        if shutil.disk_usage(self.backup_root).free < required:
            raise RuntimeError("There is not enough free space for private rollback backups. No records were deleted.")
        backup_id = uuid.uuid4().hex
        folder = self.backup_root / backup_id
        folder.mkdir(mode=0o700)
        manifest = {"backupId": backup_id, "profileId": cached["profileId"], "profilePath": profile["path"], "browser": profile["browser"], "profileName": profile["name"], "createdUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "categories": selected, "state": "prepared", "databases": [], "note": "Private local original browser data; never include this folder in public app packages or reports."}
        connections: list[tuple[dict[str, Any], sqlite3.Connection]] = []
        committed: list[str] = []
        failure = None
        self._plans.pop(plan_id, None)  # A plan can be applied at most once.
        try:
            for index, spec in enumerate(specs):
                path = self._path(profile, spec["relative"])
                connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)
                connections.append((spec, connection))
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("BEGIN IMMEDIATE")
                if _identity(path) != spec["files"]["database"]:
                    raise RuntimeError("A database changed while acquiring its cleanup lock.")
                self._schema(connection, profile["family"], spec)
                backup_name = f"database-{index}.sqlite"
                backup_path = folder / backup_name
                # A separate read-only source avoids backing up a connection that
                # owns an uncommitted write transaction. BEGIN IMMEDIATE holds
                # other writers away while SQLite copies the committed snapshot.
                with _ro(path) as original, closing(sqlite3.connect(str(backup_path))) as destination:
                    original.backup(destination)
                manifest["databases"].append({"relative": spec["relative"], "file": backup_name, "sha256": _digest(backup_path), "before": spec["files"], "committed": False})
            _json_write(folder / "manifest.json", manifest)  # Required before any DELETE.
            self._closed(profile)
            for spec, connection in connections:
                self._delete(connection, profile["family"], spec, cached["keep"])
            for spec, connection in connections:
                connection.commit()
                committed.append(spec["relative"])
        except Exception as error:
            failure = error
            for _, connection in connections:
                connection.rollback()
        finally:
            for _, connection in connections:
                connection.close()
            for record in manifest["databases"]:
                record["committed"] = record["relative"] in committed
                record["after"] = self._files(self._path(profile, record["relative"]))
            manifest["state"] = "applied" if failure is None else ("partial" if committed else "failed")
            if failure is not None:
                manifest["error"] = str(failure)
            _json_write(folder / "manifest.json", manifest)
        if failure is not None:
            raise RuntimeError(f"Privacy cleanup {manifest['state']}; backup {backup_id} was retained. {failure}") from failure
        return {"backupId": backup_id, "state": "applied", "categories": selected, "counts": {key: value for key, value in cached["counts"].items() if key in selected}, "note": "Original data is retained only in the private local rollback backup. New browser activity prevents automatic restore so newer history and bookmarks are not overwritten."}

    def list_backups(self) -> list[dict[str, Any]]:
        result = []
        if not self.backup_root.is_dir():
            return result
        for folder in self.backup_root.iterdir():
            if not _HEX_ID.fullmatch(folder.name) or _link(folder):
                continue
            try:
                data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                if data.get("backupId") != folder.name:
                    continue
                result.append({key: data.get(key) for key in ("backupId", "profileId", "browser", "profileName", "createdUtc", "categories", "state")})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda item: item.get("createdUtc") or "", reverse=True)

    def restore_backup(self, backup_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("Confirm restoration of this private browser backup.")
        if not _HEX_ID.fullmatch(backup_id):
            raise ValueError("Invalid browser backup identity.")
        folder = self.backup_root / backup_id
        if _link(folder) or _link(folder / "manifest.json"):
            raise ValueError("Linked backups cannot be restored.")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("backupId") != backup_id or manifest.get("state") not in ("applied", "partial"):
            raise ValueError("This backup is not an applied cleanup that can be restored.")
        profile = self._profile(manifest.get("profileId"))
        self._closed(profile)
        if profile["path"] != manifest.get("profilePath"):
            raise ValueError("The backup belongs to a different profile path.")
        records = [record for record in manifest["databases"] if record.get("committed")]
        if not records:
            raise ValueError("This backup has no committed changes to restore.")
        for record in records:
            filename = record.get("file", "")
            if not re.fullmatch(r"database-\d+\.sqlite", filename):
                raise ValueError("Invalid backup database name.")
            backup = folder / filename
            path = self._path(profile, record["relative"])
            if _link(backup) or _digest(backup) != record["sha256"]:
                raise ValueError("A private backup database changed; restore was stopped.")
            if self._files(path) != record["after"]:
                raise ValueError("Browser data changed after cleanup. Automatic restore would overwrite newer data, so it was stopped.")
            self._no_pending_journal(path)
        safety = folder / ("before-restore-" + uuid.uuid4().hex)
        safety.mkdir(mode=0o700)
        for record in records:
            path = self._path(profile, record["relative"])
            with _ro(path) as current, closing(sqlite3.connect(str(safety / record["file"]))) as destination:
                current.backup(destination)
        self._closed(profile)
        for record in records:
            path = self._path(profile, record["relative"])
            if self._files(path) != record["after"]:
                raise ValueError("Browser data changed while preparing restore. No restore was started.")
        restored = []
        try:
            for record in records:
                path = self._path(profile, record["relative"])
                # SQLite's backup API writes through its own transaction and
                # handles journal/WAL state; database files are never replaced.
                with _ro(folder / record["file"]) as original, closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as destination:
                    original.backup(destination)
                restored.append(record["relative"])
        except Exception:
            manifest["state"] = "restore-partial"
            manifest["restoredDatabases"] = restored
            manifest["restoreSafetyFolder"] = safety.name
            _json_write(folder / "manifest.json", manifest)
            raise
        manifest["state"] = "restored"
        manifest["restoreSafetyFolder"] = safety.name
        _json_write(folder / "manifest.json", manifest)
        return {"backupId": backup_id, "state": "restored", "databases": len(restored), "note": "Restored to the same approved profile using SQLite. A private pre-restore safety snapshot was retained."}
