# -*- coding: utf-8 -*-
# IGAC - Migrador vectorial de FileGDB para ArcGIS Server 11.3.
# Procedimiento, configuración y pseudocódigo: README.md.


import argparse
import csv
import getpass
import json
import os
import re
import shutil
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
import requests
import arcpy

# 1. Constantes


FILEGDB_TOKEN = "FileGDBWorkspaceFactory"


# 2. Utilidades generales


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def norm_unc(value):
    if value is None:
        return ""
    return str(value).replace("/", "\\").rstrip("\\").lower()


def path_starts(value, root):
    if not value or not root:
        return False
    value_n = norm_unc(value)
    root_n = norm_unc(root)
    return value_n == root_n or value_n.startswith(root_n + "\\")


def replace_ci(text, old, new):
    if not isinstance(text, str):
        return (text, 0)
    pattern = re.compile(re.escape(old), flags=re.IGNORECASE)
    return pattern.subn(lambda _: new, text)


def recursive_replace(obj, old, new):
    total = 0
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            new_value, count = recursive_replace(value, old, new)
            result[key] = new_value
            total += count
        return (result, total)
    if isinstance(obj, list):
        result = []
        for value in obj:
            new_value, count = recursive_replace(value, old, new)
            result.append(new_value)
            total += count
        return (result, total)
    if isinstance(obj, str):
        return replace_ci(obj, old, new)
    return (obj, 0)


def recursive_count(obj, target):
    total = 0
    if isinstance(obj, dict):
        for value in obj.values():
            total += recursive_count(value, target)
    elif isinstance(obj, list):
        for value in obj:
            total += recursive_count(value, target)
    elif isinstance(obj, str):
        total += len(re.findall(re.escape(target), obj, flags=re.IGNORECASE))
    return total


def read_text(path):
    for encoding in ("utf-8-sig", "utf-8", "utf-16-le", "utf-16-be", "cp1252"):
        try:
            return (Path(path).read_text(encoding=encoding), encoding)
        except UnicodeError:
            continue
    raise RuntimeError(f"No fue posible leer como texto: {path}")


def atomic_write_text(path, text, encoding):
    path = Path(path)
    tmp = path.with_name(path.name + ".igac_tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def database_from_connection(connection_string):
    if not connection_string:
        return ""
    match = re.search("DATABASE=(.+?\\.gdb)", connection_string, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def replace_root(path, old_root, new_root):
    if not path_starts(path, old_root):
        return ""
    suffix = path[len(old_root) :]
    return new_root.rstrip("\\") + suffix


# 3. Cliente ArcGIS Server Admin y REST


class ArcGISAdmin:

    def __init__(self, admin_url, rest_url, username, password, verify_ssl=True, timeout=60):
        self.admin_url = admin_url.rstrip("/")
        self.rest_url = rest_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()
        self.token = self.generate_token(username, password)

    def generate_token(self, username, password):
        response = self.session.post(
            f"{self.admin_url}/generateToken",
            data={
                "username": username,
                "password": password,
                "client": "requestip",
                "expiration": 60,
                "f": "json",
            },
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        result = response.json()
        if "token" not in result:
            raise RuntimeError(
                "No fue posible generar token: " + json.dumps(result, ensure_ascii=False)
            )
        return result["token"]

    def get(self, path, params=None):
        params = dict(params or {})
        params.update({"token": self.token, "f": "json"})
        response = self.session.get(
            f"{self.admin_url}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def post(self, path, data=None):
        data = dict(data or {})
        data.update({"token": self.token, "f": "json"})
        response = self.session.post(
            f"{self.admin_url}/{path.lstrip('/')}",
            data=data,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def rest_json(self, path, params=None):
        params = dict(params or {})
        params.update({"f": "json", "token": self.token})
        response = self.session.get(
            f"{self.rest_url}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def service_status(self, service_path):
        return self.get(f"services/{service_path}/status")

    def list_services(self, folder=""):
        path = "services" if not folder else f"services/{folder}"
        result = self.get(path)
        return result.get("services") or []

    def list_folders(self):
        result = self.get("services")
        return result.get("folders") or []

    def service_manifest(self, service_path):
        return self.get(f"services/{service_path}/iteminfo/manifest/manifest.json")

    def service_operation(self, service_path, operation):
        result = self.post(f"services/{service_path}/{operation}")
        if str(result.get("status", "")).lower() == "error":
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result

    def wait_state(self, service_path, desired, timeout_seconds):
        desired = desired.upper()
        end_time = time.time() + timeout_seconds
        last_status = {}
        while time.time() < end_time:
            last_status = self.service_status(service_path)
            configured = str(last_status.get("configuredState", "")).upper()
            realtime = str(last_status.get("realTimeState", "")).upper()
            print(f"    {service_path}: {configured}/{realtime}")
            if configured == desired and realtime == desired:
                return (True, last_status)
            time.sleep(2)
        return (False, last_status)


# 4. Auditoría de Registered Folders


def find_file_shares(admin):
    items = []
    searches = [
        {"parentPath": "/fileShares", "types": "folder"},
        {"ancestorPath": "/fileShares", "types": "folder"},
    ]
    seen = set()
    for params in searches:
        try:
            result = admin.get("data/findItems", params)
        except Exception:
            continue
        candidates = result.get("items") or result.get("dataItems") or []
        for item in candidates:
            path = item.get("path")
            if not path:
                continue
            if path.lower() in seen:
                continue
            seen.add(path.lower())
            try:
                complete = admin.get(f"data/items{path}")
                if isinstance(complete, dict) and complete.get("path"):
                    item = complete
            except Exception:
                pass
            items.append(item)
    return items


def datastore_audit(admin, cfg):
    old_root = cfg["old_root"]
    new_root = cfg["new_root"]
    rows = []
    for item in find_file_shares(admin):
        info = item.get("info") or {}
        server_path = info.get("path") or ""
        publisher_path = item.get("clientPath") or server_path
        old_count = recursive_count(item, old_root)
        new_count = recursive_count(item, new_root)
        rows.append(
            {
                "path": item.get("path"),
                "type": item.get("type"),
                "publisher_path": publisher_path,
                "server_path": server_path,
                "old_occurrences": old_count,
                "new_occurrences": new_count,
                "matches_old_root": old_count > 0
                or path_starts(publisher_path, old_root)
                or path_starts(server_path, old_root),
                "matches_new_root": new_count > 0
                or path_starts(publisher_path, new_root)
                or path_starts(server_path, new_root),
                "item": item,
            }
        )
    return rows


# 5. Asociación entre servicios y Registered Folders


def evaluate_datastore_readiness(service, datastore_rows, cfg):
    old_root = cfg["old_root"]
    new_root = cfg["new_root"]
    relations = []
    for workspace in service.get("workspaces", []):
        connection = (
            workspace.get("onServerConnectionString")
            or workspace.get("onPremiseConnectionString")
            or ""
        )
        database = database_from_connection(connection)
        if not database:
            continue
        expected_database = replace_root(database, old_root, new_root)
        candidates = []
        for datastore in datastore_rows:
            publisher = datastore.get("publisher_path") or ""
            server = datastore.get("server_path") or ""
            old_match = path_starts(database, publisher) or path_starts(database, server)
            new_match = expected_database and (
                path_starts(expected_database, publisher) or path_starts(expected_database, server)
            )
            if not (old_match or new_match):
                continue
            specificity = max(len(norm_unc(publisher)), len(norm_unc(server)))
            candidates.append((specificity, datastore, old_match, new_match))
        candidates.sort(key=lambda x: x[0], reverse=True)
        if not candidates:
            relations.append(
                {
                    "database": database,
                    "expected_database": expected_database,
                    "datastore": None,
                    "status": "REGISTERED_FOLDER_NOT_FOUND",
                    "ready": False,
                }
            )
            continue
        _, datastore, old_match, new_match = candidates[0]
        publisher = datastore.get("publisher_path") or ""
        server = datastore.get("server_path") or ""
        server_ready = (
            expected_database
            and path_starts(expected_database, server)
            and path_starts(server, new_root)
        )
        publisher_ready = (
            expected_database
            and path_starts(expected_database, publisher)
            and path_starts(publisher, new_root)
        )
        ready = bool(server_ready or publisher_ready)
        relations.append(
            {
                "database": database,
                "expected_database": expected_database,
                "datastore_path": datastore.get("path"),
                "publisher_path": publisher,
                "server_path": server,
                "ready": ready,
                "status": "READY" if ready else "NOT_UPDATED",
            }
        )
    if not relations:
        return {"ready": False, "status": "DATASTORE_NOT_FOUND", "relations": []}
    all_ready = all((x.get("ready") for x in relations))
    return {
        "ready": all_ready,
        "status": "READY" if all_ready else "DATASTORE_NOT_READY",
        "relations": relations,
    }


# 6. Descubrimiento y clasificación de servicios


def is_vector_service_dir(path):
    name = Path(path).name
    return name.endswith(".MapServer") or name.endswith(".FeatureServer")


def service_identity(service_dir, arcgisinput_root):
    relative = Path(service_dir).relative_to(Path(arcgisinput_root))
    name = relative.name
    if name.endswith(".MapServer"):
        service_type = "MapServer"
        service_name = name[: -len(".MapServer")]
    else:
        service_type = "FeatureServer"
        service_name = name[: -len(".FeatureServer")]
    folder = "/".join(relative.parts[:-1])
    prefix = folder + "/" if folder else ""
    return {
        "folder": folder,
        "service_name": service_name,
        "service_type": service_type,
        "admin_path": f"{prefix}{service_name}.{service_type}",
        "rest_path": f"{prefix}{service_name}/{service_type}",
        "relative_service_dir": str(relative),
    }


def find_manifest_json(service_dir):
    candidates = [
        Path(service_dir) / "extracted" / "manifest.json",
        Path(service_dir) / "esriinfo" / "manifest" / "manifest.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = list(Path(service_dir).rglob("manifest.json"))
    if found:
        return found[0]
    return None


def classify_manifest(manifest, old_root):
    workspaces = []
    databases = manifest.get("databases") or []
    for db in databases:
        factory = str(db.get("onServerWorkspaceFactoryProgID", ""))
        if FILEGDB_TOKEN not in factory:
            continue
        by_reference = db.get("byReference")
        server_conn = str(db.get("onServerConnectionString", ""))
        premise_conn = str(db.get("onPremiseConnectionString", ""))
        database = database_from_connection(server_conn or premise_conn)
        if not database:
            continue
        if not path_starts(database, old_root):
            continue
        workspaces.append(
            {
                "factory": factory,
                "byReference": by_reference,
                "onServerConnectionString": server_conn,
                "onPremiseConnectionString": premise_conn,
                "onServerName": db.get("onServerName"),
            }
        )
    if not workspaces:
        return ("NO_MATCH", [])
    if all((x.get("byReference") is not False for x in workspaces)):
        return ("FILEGDB_REFERENCED", workspaces)
    return ("FILEGDB_COPIED_OR_MIXED", workspaces)


def rest_service_identity(folder, item, cfg):
    service_name = (
        item.get("serviceName")
        or item.get("servicename")
        or item.get("name")
    )
    service_type = item.get("type")
    if not service_name or service_type not in ("MapServer", "FeatureServer"):
        return None
    folder = folder.strip("/")
    prefix = folder + "/" if folder else ""
    relative = (
        Path(folder) / f"{service_name}.{service_type}"
        if folder
        else Path(f"{service_name}.{service_type}")
    )
    return {
        "folder": folder,
        "service_name": service_name,
        "service_type": service_type,
        "admin_path": f"{prefix}{service_name}.{service_type}",
        "rest_path": f"{prefix}{service_name}/{service_type}",
        "relative_service_dir": str(relative),
        "arcgisinput_dir": str(Path(cfg["arcgisinput_root"]) / relative),
        "configstore_dir": str(Path(cfg["configstore_services_root"]) / relative),
        "manifest_path": "ArcGIS Server Admin REST",
    }


def discover_services_from_admin(admin, cfg):
    old_root = cfg["old_root"]
    include_regex = re.compile(cfg.get("include_regex", ".*"), flags=re.IGNORECASE)
    exclude_regex = re.compile(
        cfg.get("exclude_regex", r"(^|[\\/])Hosted([\\/]|$)"),
        flags=re.IGNORECASE,
    )
    folders = [""] + [
        folder for folder in admin.list_folders()
        if folder.lower() not in ("system", "utilities")
    ]
    services = []

    for folder in folders:
        if folder and exclude_regex.search(folder):
            continue
        try:
            items = admin.list_services(folder)
        except Exception as exc:
            services.append(
                {
                    "folder": folder,
                    "service_name": "",
                    "service_type": "",
                    "admin_path": folder,
                    "rest_path": folder,
                    "relative_service_dir": folder,
                    "arcgisinput_dir": "",
                    "configstore_dir": "",
                    "manifest_path": "ArcGIS Server Admin REST",
                    "classification": "SERVICE_LIST_ERROR",
                    "workspaces": [{"error": str(exc)}],
                    "manifest_old_root_occurrences": 0,
                }
            )
            continue

        for item in items:
            identity = rest_service_identity(folder, item, cfg)
            if not identity:
                continue
            if not include_regex.search(identity["admin_path"]):
                continue
            if exclude_regex.search(identity["admin_path"]):
                continue

            try:
                manifest = admin.service_manifest(identity["admin_path"])
                classification, workspaces = classify_manifest(manifest, old_root)
                old_count = recursive_count(manifest, old_root)
            except Exception as exc:
                classification = "MANIFEST_ERROR"
                workspaces = [{"error": str(exc)}]
                old_count = 0

            services.append(
                {
                    **identity,
                    "classification": classification,
                    "workspaces": workspaces,
                    "manifest_old_root_occurrences": old_count,
                }
            )

    return services


def discover_services_from_filesystem(cfg):
    arcgisinput_root = Path(cfg["arcgisinput_root"])
    configstore_root = Path(cfg["configstore_services_root"])
    old_root = cfg["old_root"]
    include_regex = re.compile(cfg.get("include_regex", ".*"), flags=re.IGNORECASE)
    exclude_regex = re.compile(
        cfg.get("exclude_regex", r"(^|[\\/])Hosted([\\/]|$)"),
        flags=re.IGNORECASE,
    )
    services = []

    for root, dirs, files in os.walk(arcgisinput_root):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not exclude_regex.search(str(root_path / d))
        ]
        if not is_vector_service_dir(root_path):
            continue

        identity = service_identity(root_path, arcgisinput_root)

        if exclude_regex.search(identity["admin_path"]):
            dirs[:] = []
            continue
        if not include_regex.search(identity["admin_path"]):
            dirs[:] = []
            continue

        manifest_path = find_manifest_json(root_path)
        if not manifest_path:
            services.append(
                {
                    **identity,
                    "classification": "NO_MANIFEST",
                    "arcgisinput_dir": str(root_path),
                    "configstore_dir": str(
                        configstore_root / identity["relative_service_dir"]
                    ),
                    "manifest_path": "",
                    "workspaces": [],
                    "manifest_old_root_occurrences": 0,
                }
            )
            dirs[:] = []
            continue

        manifest = None
        try:
            manifest = load_json(manifest_path)
            classification, workspaces = classify_manifest(manifest, old_root)
        except Exception as exc:
            classification = "MANIFEST_ERROR"
            workspaces = [{"error": str(exc)}]

        services.append(
            {
                **identity,
                "classification": classification,
                "arcgisinput_dir": str(root_path),
                "configstore_dir": str(
                    configstore_root / identity["relative_service_dir"]
                ),
                "manifest_path": str(manifest_path),
                "workspaces": workspaces,
                "manifest_old_root_occurrences": (
                    recursive_count(manifest, old_root)
                    if manifest is not None
                    else 0
                ),
            }
        )
        dirs[:] = []

    return services


def discover_services(admin, cfg):
    mode = str(cfg.get("discovery_mode", "admin_rest")).lower()
    if mode in ("filesystem", "file_system", "files"):
        return discover_services_from_filesystem(cfg)
    return discover_services_from_admin(admin, cfg)


# 7. Escaneo de artefactos


def candidate_files(service):
    roots = [Path(service["arcgisinput_dir"]), Path(service["configstore_dir"])]
    result = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if not (
                name == "manifest.json"
                or name == "manifest.xml"
                or name == "serviceconfiguration.json"
                or name.endswith(".mapx")
                or name.endswith(".msd")
            ):
                continue
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
    return result


def count_json_file(path, old_root):
    try:
        return recursive_count(load_json(path), old_root)
    except Exception:
        text, _ = read_text(path)
        return len(re.findall(re.escape(old_root), text, flags=re.IGNORECASE))


def scan_msd(path, old_root):
    rows = []
    total = 0
    if not zipfile.is_zipfile(path):
        return (rows, total)
    with zipfile.ZipFile(path, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith((".json", ".xml")):
                continue
            raw = archive.read(info.filename)
            text = None
            for encoding in ("utf-8-sig", "utf-8", "utf-16-le", "utf-16-be", "cp1252"):
                try:
                    text = raw.decode(encoding)
                    break
                except Exception:
                    continue
            if text is None:
                continue
            count = 0
            if info.filename.lower().endswith(".json"):
                try:
                    obj = json.loads(text)
                    count = recursive_count(obj, old_root)
                except Exception:
                    pass
            if count == 0:
                count = len(re.findall(re.escape(old_root), text, flags=re.IGNORECASE))
            if count:
                rows.append({"entry": info.filename, "occurrences": count})
                total += count
    return (rows, total)


def scan_service(service, old_root):
    rows = []
    total = 0
    for path in candidate_files(service):
        name = path.name.lower()
        if name.endswith(".msd"):
            entries, count = scan_msd(path, old_root)
            if count:
                rows.append(
                    {"file": str(path), "kind": "MSD", "occurrences": count, "entries": entries}
                )
                total += count
            continue
        if name.endswith(".json") or name.endswith(".mapx"):
            count = count_json_file(path, old_root)
        else:
            text, _ = read_text(path)
            count = len(re.findall(re.escape(old_root), text, flags=re.IGNORECASE))
        if count:
            rows.append(
                {
                    "file": str(path),
                    "kind": (
                        "JSON" if name.endswith(".json") or name.endswith(".mapx") else "XML/TEXT"
                    ),
                    "occurrences": count,
                }
            )
            total += count
    return (rows, total)


def scan_service_files(service, old_root):
    rows = []
    total = 0
    for path in candidate_files(service):
        name = path.name.lower()
        if name.endswith(".msd"):
            entries, count = scan_msd(path, old_root)
            if count:
                rows.append(
                    {
                        "file": str(path),
                        "kind": "MSD",
                        "occurrences": count,
                        "entries": entries,
                    }
                )
                total += count
            continue

        if name.endswith(".json") or name.endswith(".mapx"):
            count = count_json_file(path, old_root)
        else:
            content, _ = read_text(path)
            count = len(
                re.findall(
                    re.escape(old_root),
                    content,
                    flags=re.IGNORECASE,
                )
            )

        if count:
            rows.append(
                {
                    "file": str(path),
                    "kind": (
                        "JSON"
                        if name.endswith(".json") or name.endswith(".mapx")
                        else "XML/TEXT"
                    ),
                    "occurrences": count,
                }
            )
            total += count
    return (rows, total)


def configstore_manifest_json(service):
    return (
        Path(service["configstore_dir"])
        / "esriinfo"
        / "manifest"
        / "manifest.json"
    )


def wait_manifest_writable(service, timeout_seconds=60, stable_seconds=3):
    """
    Espera a que el manifest del config-store esté estable y pueda abrirse
    en modo lectura/escritura.

    No depende de que manifest.json.lock desaparezca, porque ArcGIS Server
    puede mantener ese archivo .lock aun cuando manifest.json ya sea editable.
    """
    manifest = configstore_manifest_json(service)
    deadline = time.time() + timeout_seconds
    stable_since = None
    last_signature = None
    last_error = None

    while time.time() < deadline:
        if not manifest.exists():
            last_error = "manifest.json no existe"
            stable_since = None
            time.sleep(1)
            continue

        try:
            stat = manifest.stat()
            signature = (stat.st_size, stat.st_mtime_ns)

            # Abrir r+b obliga a comprobar acceso real de escritura
            # sin modificar el contenido.
            with open(manifest, "r+b") as handle:
                handle.seek(0, os.SEEK_END)

            if signature == last_signature:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_seconds:
                    return {
                        "path": str(manifest),
                        "writable": True,
                        "stable_seconds": stable_seconds,
                        "lock_file_exists": manifest.with_name(
                            manifest.name + ".lock"
                        ).exists(),
                    }
            else:
                stable_since = time.time()
                last_signature = signature

            last_error = None

        except (OSError, PermissionError) as exc:
            last_error = str(exc)
            stable_since = None

        time.sleep(1)

    raise RuntimeError(
        "manifest.json del config-store no quedó estable/escribible "
        f"en {timeout_seconds}s: {manifest}"
        + (f" | último error: {last_error}" if last_error else "")
    )


def verify_manifest_paths(path, old_root, new_root):
    old_count = count_json_file(path, old_root)
    new_count = count_json_file(path, new_root)
    return {
        "file": str(path),
        "old_root_occurrences": old_count,
        "new_root_occurrences": new_count,
    }


def apply_service_files_except_manifest(
    service,
    old_root,
    new_root,
    work_root,
):
    primary_manifest = configstore_manifest_json(service)
    rows = []
    total = 0

    for path in candidate_files(service):
        try:
            if path.resolve() == primary_manifest.resolve():
                continue
        except Exception:
            if str(path).lower() == str(primary_manifest).lower():
                continue

        name = path.name.lower()

        if name.endswith(".msd"):
            count, entries = update_msd(
                path,
                old_root,
                new_root,
                work_root,
            )
            if count:
                rows.append(
                    {
                        "file": str(path),
                        "kind": "MSD",
                        "replacements": count,
                        "entries": entries,
                    }
                )
                total += count
            continue

        if name.endswith(".json") or name.endswith(".mapx"):
            count = update_json_file(
                path,
                old_root,
                new_root,
            )
        else:
            count = update_text_file(
                path,
                old_root,
                new_root,
            )

        if count:
            rows.append(
                {
                    "file": str(path),
                    "kind": (
                        "JSON"
                        if name.endswith(".json")
                        or name.endswith(".mapx")
                        else "XML/TEXT"
                    ),
                    "replacements": count,
                }
            )
            total += count

    return (rows, total)


# 8. Actualización de artefactos


def update_json_file(path, old_root, new_root):
    try:
        data = load_json(path)
        modified, count = recursive_replace(data, old_root, new_root)
        if count:
            tmp = Path(path).with_name(Path(path).name + ".igac_tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(modified, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return count
    except Exception:
        text, encoding = read_text(path)
        modified, count = replace_ci(text, old_root, new_root)
        if count:
            atomic_write_text(path, modified, encoding)
        return count


def update_text_file(path, old_root, new_root):
    text, encoding = read_text(path)
    modified, count = replace_ci(text, old_root, new_root)
    if count:
        atomic_write_text(path, modified, encoding)
    return count


def update_msd(path, old_root, new_root, work_root=None):
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"MSD inválido: {path}")
    # El temporal se crea junto al MSD para permitir el reemplazo atómico
    # en la misma unidad o recurso UNC.
    temp = path.with_name(path.name + ".igac_tmp")
    total = 0
    changes = []
    try:
        with zipfile.ZipFile(path, "r") as source:
            comment = source.comment
            with zipfile.ZipFile(temp, "w", allowZip64=True) as target:
                target.comment = comment
                for info in source.infolist():
                    raw = source.read(info.filename)
                    new_raw = raw
                    count = 0
                    if not info.is_dir() and info.filename.lower().endswith((".json", ".xml")):
                        text = None
                        encoding_used = None
                        for encoding in ("utf-8-sig", "utf-8", "utf-16-le", "utf-16-be", "cp1252"):
                            try:
                                text = raw.decode(encoding)
                                encoding_used = encoding
                                break
                            except Exception:
                                continue
                        if text is not None:
                            if info.filename.lower().endswith(".json"):
                                try:
                                    data = json.loads(text)
                                    modified, count = recursive_replace(data, old_root, new_root)
                                    if count:
                                        new_text = json.dumps(
                                            modified, ensure_ascii=False, indent=2
                                        )
                                        new_raw = new_text.encode(encoding_used)
                                except Exception:
                                    modified, count = replace_ci(text, old_root, new_root)
                                    if count:
                                        new_raw = modified.encode(encoding_used)
                            else:
                                modified, count = replace_ci(text, old_root, new_root)
                                if count:
                                    new_raw = modified.encode(encoding_used)
                    target.writestr(info, new_raw)
                    if count:
                        changes.append({"entry": info.filename, "replacements": count})
                        total += count
        with zipfile.ZipFile(temp, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError("MSD generado corrupto: " + bad)
        if total:
            os.replace(temp, path)
        else:
            temp.unlink(missing_ok=True)
        return (total, changes)
    except Exception:
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass
        raise


def apply_service_files(service, old_root, new_root, work_root):
    rows = []
    total = 0
    for path in candidate_files(service):
        name = path.name.lower()
        if name.endswith(".msd"):
            count, entries = update_msd(path, old_root, new_root, work_root)
            if count:
                rows.append(
                    {"file": str(path), "kind": "MSD", "replacements": count, "entries": entries}
                )
                total += count
            continue
        if name.endswith(".json") or name.endswith(".mapx"):
            count = update_json_file(path, old_root, new_root)
        else:
            count = update_text_file(path, old_root, new_root)
        if count:
            rows.append(
                {
                    "file": str(path),
                    "kind": (
                        "JSON" if name.endswith(".json") or name.endswith(".mapx") else "XML/TEXT"
                    ),
                    "replacements": count,
                }
            )
            total += count
    return (rows, total)


# 9. Copia de seguridad y restauración


def backup_service(service, backup_root):
    key = service["admin_path"].replace("/", "__")
    destination = Path(backup_root) / key
    destination.mkdir(parents=True, exist_ok=True)
    mappings = []
    sources = [
        (Path(service["arcgisinput_dir"]), "arcgisinput"),
        (Path(service["configstore_dir"]), "configstore"),
    ]
    for source, label in sources:
        if not source.exists():
            continue
        target = destination / label
        shutil.copytree(source, target)
        mappings.append({"source": str(source), "backup": str(target)})
    save_json(
        destination / "backup_metadata.json",
        {
            "service": service,
            "directories": mappings,
            "created": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return destination


def restore_service_backup(backup_dir):
    metadata = load_json(Path(backup_dir) / "backup_metadata.json")
    for item in metadata["directories"]:
        source = Path(item["source"])
        backup = Path(item["backup"])
        if source.exists():
            shutil.rmtree(source)
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup, source)


# 10. Validación REST


def validate_rest_service(admin, service):
    result = {"root_ok": False, "query_ok": None, "layer_id": None, "detail": []}
    root = admin.rest_json(service["rest_path"])
    if "error" in root:
        result["detail"].append(root["error"])
        return (False, result)
    result["root_ok"] = True
    candidates = root.get("layers") or root.get("tables") or []
    if not candidates:
        result["query_ok"] = False
        result["detail"].append("Servicio sin layers/tables")
        return (False, result)
    layer_id = candidates[0].get("id")
    result["layer_id"] = layer_id
    query = admin.rest_json(
        f"{service['rest_path']}/{layer_id}/query", {"where": "1=1", "returnCountOnly": "true"}
    )
    if "error" in query:
        result["query_ok"] = False
        result["detail"].append(query["error"])
        return (False, result)
    result["query_ok"] = "count" in query
    result["detail"].append("count=" + str(query.get("count")))
    return (result["query_ok"], result)


def validate_rest_service_retry(
    admin,
    service,
    timeout_seconds=120,
    interval_seconds=5,
):
    """
    Reintenta la validación REST posterior al START.

    ArcGIS Server puede reportar STARTED/STARTED antes de que las capas
    del MapServer hayan terminado de inicializarse. Esta función evita
    hacer rollback por una validación demasiado temprana.
    """
    deadline = time.time() + timeout_seconds
    attempts = []
    last_result = None

    while time.time() < deadline:
        try:
            ok, result = validate_rest_service(admin, service)
        except Exception as exc:
            ok = False
            result = {
                "root_ok": False,
                "query_ok": False,
                "layer_id": None,
                "detail": [str(exc)],
            }

        attempts.append(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "ok": ok,
                "result": result,
            }
        )

        if ok:
            return True, result, attempts

        last_result = result
        time.sleep(interval_seconds)

    return False, last_result, attempts


def path_is_under(path, root):
    p = str(Path(path)).replace("/", "\\").lower().rstrip("\\")
    r = str(Path(root)).replace("/", "\\").lower().rstrip("\\")
    return p == r or p.startswith(r + "\\")


def diagnostic_stages(service):
    """
    Devuelve las etapas de modificación incremental.
    Cada etapa agrega únicamente un tipo de artefacto.
    """
    files = candidate_files(service)
    config_root = Path(service["configstore_dir"])
    input_root = Path(service["arcgisinput_dir"])

    stages = [
        {
            "name": "CONFIGSTORE_MANIFEST_JSON",
            "label": "config-store manifest.json",
            "paths": [
                p for p in files
                if path_is_under(p, config_root)
                and p.name.lower() == "manifest.json"
            ],
        },
        {
            "name": "CONFIGSTORE_MANIFEST_XML",
            "label": "config-store manifest.xml",
            "paths": [
                p for p in files
                if path_is_under(p, config_root)
                and p.name.lower() == "manifest.xml"
            ],
        },
        {
            "name": "ARCGISINPUT_MANIFEST_JSON",
            "label": "arcgisinput manifest.json",
            "paths": [
                p for p in files
                if path_is_under(p, input_root)
                and p.name.lower() == "manifest.json"
            ],
        },
        {
            "name": "ARCGISINPUT_MANIFEST_XML",
            "label": "arcgisinput manifest.xml",
            "paths": [
                p for p in files
                if path_is_under(p, input_root)
                and p.name.lower() == "manifest.xml"
            ],
        },
        {
            "name": "MAPX",
            "label": "MAPX",
            "paths": [
                p for p in files
                if p.name.lower().endswith(".mapx")
            ],
        },
        {
            "name": "MSD",
            "label": "MSD",
            "paths": [
                p for p in files
                if p.name.lower().endswith(".msd")
            ],
        },
        {
            "name": "SERVICECONFIGURATION_JSON",
            "label": "serviceconfiguration.json",
            "paths": [
                p for p in files
                if p.name.lower() == "serviceconfiguration.json"
            ],
        },
    ]
    return [stage for stage in stages if stage["paths"]]


def apply_diagnostic_stage(stage, old_root, new_root, work_root):
    changes = []
    total = 0

    for path in stage["paths"]:
        name = path.name.lower()

        if name.endswith(".msd"):
            count, entries = update_msd(
                path,
                old_root,
                new_root,
                work_root,
            )
            if count:
                changes.append(
                    {
                        "file": str(path),
                        "kind": "MSD",
                        "replacements": count,
                        "entries": entries,
                    }
                )
                total += count
            continue

        if name.endswith(".json") or name.endswith(".mapx"):
            count = update_json_file(path, old_root, new_root)
            kind = "JSON"
        else:
            count = update_text_file(path, old_root, new_root)
            kind = "XML/TEXT"

        if count:
            changes.append(
                {
                    "file": str(path),
                    "kind": kind,
                    "replacements": count,
                }
            )
            total += count

    return changes, total


# 11. Procesamiento de servicios


def process_service(admin, cfg, service, run_dir):
    old_root = cfg["old_root"]
    new_root = cfg["new_root"]

    result = {
        "service": service["admin_path"],
        "result": "",
        "error": "",
    }

    baseline_ok, baseline_rest = validate_rest_service(
        admin,
        service,
    )

    if not baseline_ok:
        result["result"] = "SKIPPED_BASELINE_REST_FAILED"
        result["error"] = json.dumps(
            baseline_rest,
            ensure_ascii=False,
        )
        return (
            result,
            {
                "service": service,
                "baseline_rest": baseline_rest,
                "datastore_status": service.get("datastore_status"),
            },
        )

    before_files, before_hits = scan_service(
        service,
        old_root,
    )

    if before_hits == 0:
        result["result"] = "NO_OLD_ROOT_FOUND"
        return (result, {})

    backup = backup_service(
        service,
        Path(run_dir) / "backups",
    )

    evidence = {
        "before_files": before_files,
        "baseline_rest": baseline_rest,
        "backup": str(backup),
        "diagnostic_stages": [],
    }

    stages = diagnostic_stages(service)

    print()
    print("ETAPAS DE DIAGNÓSTICO:")
    for index, stage in enumerate(stages, 1):
        print(
            f"  {index}. {stage['label']} "
            f"({len(stage['paths'])} archivo(s))"
        )

    total_replacements = 0
    current_stage = None

    try:
        # El servicio comienza STARTED porque la validación baseline ya pasó.
        for index, stage in enumerate(stages, 1):
            current_stage = stage

            print()
            print(
                f"[ETAPA {index}/{len(stages)}] "
                f"{stage['label']}"
            )

            print("STOP:", service["admin_path"])
            stop_response = admin.service_operation(
                service["admin_path"],
                "stop",
            )

            stopped, stop_status = admin.wait_state(
                service["admin_path"],
                "STOPPED",
                int(cfg.get("stop_timeout_seconds", 180)),
            )

            if not stopped:
                raise RuntimeError(
                    f"{stage['name']}: servicio no alcanzó "
                    "STOPPED/STOPPED"
                )

            stage_evidence = {
                "stage": stage["name"],
                "label": stage["label"],
                "files": [str(p) for p in stage["paths"]],
                "stop_response": stop_response,
                "stop_status": stop_status,
            }

            # Para etapas del config-store, comprobar que el manifest
            # principal ya está estable antes de escribir.
            if stage["name"].startswith("CONFIGSTORE_"):
                stage_evidence["manifest_ready"] = (
                    wait_manifest_writable(
                        service,
                        timeout_seconds=int(
                            cfg.get(
                                "manifest_wait_timeout_seconds",
                                60,
                            )
                        ),
                        stable_seconds=int(
                            cfg.get(
                                "manifest_stable_seconds",
                                3,
                            )
                        ),
                    )
                )

            changes, replacements = apply_diagnostic_stage(
                stage,
                old_root,
                new_root,
                Path(run_dir)
                / "work"
                / service["admin_path"].replace("/", "__")
                / stage["name"],
            )

            total_replacements += replacements
            stage_evidence["changes"] = changes
            stage_evidence["replacements"] = replacements

            print(
                f"  Reemplazos en etapa: {replacements}"
            )

            # Registrar cuántas referencias OLD quedan físicamente,
            # pero NO abortar todavía por ellas: el objetivo es saber
            # después de qué artefacto el servicio deja de funcionar.
            remaining_files, remaining_count = scan_service_files(
                service,
                old_root,
            )
            stage_evidence["remaining_old_root"] = {
                "count": remaining_count,
                "files": remaining_files,
            }

            print("START:", service["admin_path"])
            start_response = admin.service_operation(
                service["admin_path"],
                "start",
            )

            started, start_status = admin.wait_state(
                service["admin_path"],
                "STARTED",
                int(cfg.get("start_timeout_seconds", 300)),
            )

            stage_evidence["start_response"] = start_response
            stage_evidence["start_status"] = start_status

            if not started:
                evidence["diagnostic_stages"].append(
                    stage_evidence
                )
                raise RuntimeError(
                    f"{stage['name']}: servicio no alcanzó "
                    "STARTED/STARTED"
                )

            # Validación REST por etapa. 30 s por defecto, configurable.
            rest_ok, rest_after, rest_attempts = (
                validate_rest_service_retry(
                    admin,
                    service,
                    timeout_seconds=int(
                        cfg.get(
                            "diagnostic_rest_timeout_seconds",
                            30,
                        )
                    ),
                    interval_seconds=int(
                        cfg.get(
                            "diagnostic_rest_interval_seconds",
                            5,
                        )
                    ),
                )
            )

            stage_evidence["rest_ok"] = rest_ok
            stage_evidence["rest_after"] = rest_after
            stage_evidence["rest_attempts"] = rest_attempts

            # También capturar manifest vivo en cada etapa.
            try:
                live_manifest = admin.service_manifest(
                    service["admin_path"]
                )
                stage_evidence["admin_manifest"] = {
                    "old_root_occurrences": recursive_count(
                        live_manifest,
                        old_root,
                    ),
                    "new_root_occurrences": recursive_count(
                        live_manifest,
                        new_root,
                    ),
                    "manifest": live_manifest,
                }
            except Exception as manifest_exc:
                stage_evidence["admin_manifest_error"] = str(
                    manifest_exc
                )

            evidence["diagnostic_stages"].append(stage_evidence)

            if rest_ok:
                print(
                    f"  [OK] {stage['label']} -> "
                    "servicio REST funcional"
                )
            else:
                print(
                    f"  [FALLÓ] {stage['label']} -> "
                    f"{rest_after}"
                )
                raise RuntimeError(
                    "ETAPA_CAUSANTE="
                    + stage["name"]
                    + " | REST dejó de funcionar después de "
                    + stage["label"]
                )

        result["replacements"] = total_replacements
        result["result"] = "SUCCESS_DIAGNOSTIC_ALL_STAGES"

        # Validación final de referencias físicas.
        final_files, final_old = scan_service_files(
            service,
            old_root,
        )
        evidence["final_physical_old_root"] = {
            "count": final_old,
            "files": final_files,
        }

        print()
        print(
            "DIAGNÓSTICO COMPLETADO: todas las etapas "
            "mantuvieron el servicio funcional."
        )

        return (result, evidence)

    except Exception as exc:
        result["error"] = str(exc)
        result["replacements"] = total_replacements

        if current_stage:
            result["failed_stage"] = current_stage["name"]

        print()
        print("ERROR -> ROLLBACK:", exc)

        try:
            # Asegurar STOP antes de restaurar.
            try:
                status = admin.service_status(
                    service["admin_path"]
                )
                if (
                    str(status.get("configuredState", "")).upper()
                    != "STOPPED"
                    or str(status.get("realTimeState", "")).upper()
                    != "STOPPED"
                ):
                    admin.service_operation(
                        service["admin_path"],
                        "stop",
                    )
                    rollback_stopped, rollback_stop_status = (
                        admin.wait_state(
                            service["admin_path"],
                            "STOPPED",
                            int(
                                cfg.get(
                                    "stop_timeout_seconds",
                                    180,
                                )
                            ),
                        )
                    )
                    evidence["rollback_stop_status"] = (
                        rollback_stop_status
                    )

                    if not rollback_stopped:
                        raise RuntimeError(
                            "Rollback: servicio no alcanzó "
                            "STOPPED/STOPPED"
                        )
            except Exception as stop_exc:
                raise RuntimeError(
                    "Rollback no pudo detener el servicio: "
                    + str(stop_exc)
                )

            try:
                evidence["manifest_ready_before_rollback"] = (
                    wait_manifest_writable(
                        service,
                        timeout_seconds=int(
                            cfg.get(
                                "manifest_wait_timeout_seconds",
                                60,
                            )
                        ),
                        stable_seconds=int(
                            cfg.get(
                                "manifest_stable_seconds",
                                3,
                            )
                        ),
                    )
                )
            except Exception as wait_exc:
                evidence["manifest_wait_rollback_warning"] = str(
                    wait_exc
                )

            restore_service_backup(backup)

            admin.service_operation(
                service["admin_path"],
                "start",
            )

            started, rollback_start_status = admin.wait_state(
                service["admin_path"],
                "STARTED",
                int(cfg.get("start_timeout_seconds", 300)),
            )

            rollback_rest_ok, rollback_rest, rollback_attempts = (
                validate_rest_service_retry(
                    admin,
                    service,
                    timeout_seconds=int(
                        cfg.get(
                            "diagnostic_rest_timeout_seconds",
                            30,
                        )
                    ),
                    interval_seconds=int(
                        cfg.get(
                            "diagnostic_rest_interval_seconds",
                            5,
                        )
                    ),
                )
            )

            evidence["rollback"] = {
                "started": started,
                "start_status": rollback_start_status,
                "rest_ok": rollback_rest_ok,
                "rest": rollback_rest,
                "attempts": rollback_attempts,
            }

            if started and rollback_rest_ok:
                result["result"] = (
                    "FAILED_STAGE_IDENTIFIED_ROLLBACK_OK"
                )
            else:
                result["result"] = (
                    "FAILED_ROLLBACK_ATTENTION_REQUIRED"
                )

        except Exception as rollback_exc:
            result["result"] = (
                "FAILED_ROLLBACK_ATTENTION_REQUIRED"
            )
            result["error"] += (
                " | Rollback: " + str(rollback_exc)
            )

        return (result, evidence)


# 12. Inventario, reportes y ejecución principal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["audit", "apply"], default="audit")
    parser.add_argument("--only-service")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    cfg = load_json(args.config)
    if not cfg.get("verify_ssl", True):
        requests.packages.urllib3.disable_warnings()
    print("=" * 90)
    print("IGAC - MIGRADOR VECTORIAL ARCGIS SERVER 11.3 v1.7 DIAGNOSTICO")
    print("=" * 90)
    print("Modo     :", args.mode.upper())
    print("OLD_ROOT :", cfg["old_root"])
    print("NEW_ROOT :", cfg["new_root"])
    print("=" * 90)
    username = input("Usuario siteadmin/administrador: ").strip()
    password = getpass.getpass("Contraseña: ")
    admin = ArcGISAdmin(
        cfg["admin_url"],
        cfg["rest_url"],
        username,
        password,
        verify_ssl=cfg.get("verify_ssl", True),
        timeout=int(cfg.get("http_timeout_seconds", 60)),
    )
    run_dir = Path(cfg["output_root"]) / (stamp() + "_" + args.mode)
    run_dir.mkdir(parents=True, exist_ok=True)
    datastore_rows = datastore_audit(admin, cfg)
    save_json(run_dir / "datastores_audit.json", datastore_rows)
    services = discover_services(admin, cfg)
    if args.only_service:
        services = [
            service
            for service in services
            if service["admin_path"].lower() == args.only_service.lower()
        ]
    inventory = []
    eligible = []
    for service in services:
        artifacts, old_hits = scan_service(service, cfg["old_root"])
        datastore_status = evaluate_datastore_readiness(service, datastore_rows, cfg)
        record = {
            **service,
            "old_root_occurrences": old_hits,
            "artifacts": artifacts,
            "datastore_status": datastore_status,
        }
        inventory.append(record)
        if (
            service["classification"] == "FILEGDB_REFERENCED"
            and old_hits > 0
            and datastore_status["ready"]
        ):
            service["datastore_status"] = datastore_status
            eligible.append(service)
    save_json(run_dir / "inventory.json", inventory)
    print()
    print("Servicios vectoriales encontrados :", len(services))
    print()
    for record in inventory:
        ds = record.get("datastore_status", {})
        print(f"{record['admin_path']:<55} {record['classification']:<24} {ds.get('status')}")
    print()
    print("Servicios READY para migración :", len(eligible))
    csv_path = run_dir / "inventory.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "admin_path",
            "folder",
            "service_name",
            "service_type",
            "classification",
            "old_root_occurrences",
            "datastore_ready",
            "datastore_status",
            "arcgisinput_dir",
            "configstore_dir",
            "manifest_path",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for record in inventory:
            ds = record.get("datastore_status", {})
            writer.writerow(
                {
                    "admin_path": record.get("admin_path"),
                    "folder": record.get("folder"),
                    "service_name": record.get("service_name"),
                    "service_type": record.get("service_type"),
                    "classification": record.get("classification"),
                    "old_root_occurrences": record.get("old_root_occurrences"),
                    "datastore_ready": ds.get("ready"),
                    "datastore_status": ds.get("status"),
                    "arcgisinput_dir": record.get("arcgisinput_dir"),
                    "configstore_dir": record.get("configstore_dir"),
                    "manifest_path": record.get("manifest_path"),
                }
            )
    if args.mode == "audit":
        print()
        print("AUDIT completado.")
        print("No se modificó ningún Data Store ni servicio.")
        print("Evidencia:", run_dir)
        return
    if not eligible:
        print()
        print("NO HAY SERVICIOS READY.")
        print("Revise datastores_audit.json y actualice manualmente los Registered Folders.")
        return
    if args.limit > 0:
        eligible = eligible[: args.limit]
    print()
    print("Servicios que se procesarán:", len(eligible))
    confirmation = input("Escriba APLICAR-VECTOR-IGAC para continuar: ").strip()
    if confirmation != "APLICAR-VECTOR-IGAC":
        print("Cancelado.")
        return
    results = []
    for index, service in enumerate(eligible, 1):
        print()
        print("#" * 90)
        print(f"[{index}/{len(eligible)}] {service['admin_path']}")
        print("#" * 90)
        result, evidence = process_service(admin, cfg, service, run_dir)
        results.append(result)
        save_json(
            run_dir / "evidence" / (service["admin_path"].replace("/", "__") + ".json"), evidence
        )
        save_json(run_dir / "report.json", results)
        if result["result"] == "FAILED_ROLLBACK_ATTENTION_REQUIRED":
            print()
            print("LOTE DETENIDO.")
            break
    print()
    print("=" * 90)
    print("FINALIZADO")
    print("=" * 90)
    for result in results:
        print(result["service"], "->", result["result"])
    print()
    print("Evidencia:", run_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
    except Exception as exc:
        print("\nERROR FINAL:", exc)
        traceback.print_exc()
        sys.exit(1)
