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
import datetime
from pathlib import Path
import requests
import arcpy

# 1. Constantes


FILEGDB_TOKEN = "FileGDBWorkspaceFactory"


# 2. Utilidades generales


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


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



def recursive_find_string_refs(value, needle, path="$"):
    """
    Devuelve las ubicaciones JSON donde aparece needle.
    Sirve para saber exactamente qué campos del Admin REST manifest
    siguen conservando OLD_ROOT.
    """
    refs = []

    if isinstance(value, dict):
        for key, child in value.items():
            refs.extend(
                recursive_find_string_refs(
                    child,
                    needle,
                    f"{path}.{key}",
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.extend(
                recursive_find_string_refs(
                    child,
                    needle,
                    f"{path}[{index}]",
                )
            )
    elif isinstance(value, str):
        count = value.lower().count(str(needle).lower())
        if count:
            refs.append(
                {
                    "path": path,
                    "value": value,
                    "occurrences": count,
                }
            )

    return refs


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
        self.username = username
        self.password = password

        # Token administrativo: se usa exclusivamente contra /admin.
        self.token = self.generate_token(username, password)

        # Token REST independiente. Solo se genera si /rest/services
        # realmente exige autenticacion.
        self.rest_token = None

    def generate_token(self, username=None, password=None):
        username = username or self.username
        password = password or self.password
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
                "No fue posible generar token administrativo: "
                + json.dumps(result, ensure_ascii=False)
            )
        return result["token"]

    def get(self, path, params=None):
        base_params = dict(params or {})
        base_params["f"] = "json"
        for attempt in range(2):
            request_params = dict(base_params)
            request_params["token"] = self.token
            response = self.session.get(
                f"{self.admin_url}/{path.lstrip('/')}",
                params=request_params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()
            error = result.get("error") if isinstance(result, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code != 498:
                return result
            if attempt == 0:
                print("    Token ADMIN invalido/expirado. Renovando...")
                self.token = self.generate_token()
        return result

    def post(self, path, data=None):
        base_data = dict(data or {})
        base_data["f"] = "json"
        for attempt in range(2):
            request_data = dict(base_data)
            request_data["token"] = self.token
            response = self.session.post(
                f"{self.admin_url}/{path.lstrip('/')}",
                data=request_data,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()

            error = result.get("error") if isinstance(result, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code is None and isinstance(result, dict):
                try:
                    code = int(result.get("code"))
                except (TypeError, ValueError):
                    code = None

            if code != 498:
                return result

            if attempt == 0:
                print("    Token ADMIN invalido/expirado. Renovando...")
                self.token = self.generate_token()

        return result

    def generate_rest_token(self):
        """
        Genera un token independiente para REST usando client=referer.

        No reutiliza el token administrativo client=requestip porque,
        detras de Web Adaptor/proxy/balanceador, puede ser rechazado
        por /rest/services con error 498 Invalid Token.
        """
        response = self.session.post(
            f"{self.admin_url}/generateToken",
            data={
                "username": self.username,
                "password": self.password,
                "client": "referer",
                "referer": self.rest_url,
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
                "No fue posible generar token REST: "
                + json.dumps(result, ensure_ascii=False)
            )

        self.rest_token = result["token"]
        return self.rest_token

    def rest_json(self, path, params=None):
        """
        Consulta REST:
        1. Primero sin token.
        2. Si ArcGIS responde 499, genera token REST con client=referer.
        3. Si el token REST devuelve 498, lo renueva una vez.
        """
        url = f"{self.rest_url}/{path.lstrip('/')}"
        base_params = dict(params or {})
        base_params["f"] = "json"

        # Primer intento: anonimo. Ideal para servicios publicos.
        response = self.session.get(
            url,
            params=base_params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        result = response.json()

        error = result.get("error") if isinstance(result, dict) else None
        code = error.get("code") if isinstance(error, dict) else None

        if code not in (498, 499):
            return result

        # Si REST exige autenticacion, usar un token separado.
        if self.rest_token is None:
            self.generate_rest_token()

        auth_params = dict(base_params)
        auth_params["token"] = self.rest_token

        response = self.session.get(
            url,
            params=auth_params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        result = response.json()

        error = result.get("error") if isinstance(result, dict) else None
        code = error.get("code") if isinstance(error, dict) else None

        if code == 498:
            self.generate_rest_token()
            auth_params["token"] = self.rest_token
            response = self.session.get(
                url,
                params=auth_params,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            result = response.json()

        return result

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
        empty_state_count = 0

        while time.time() < end_time:
            last_status = self.service_status(service_path)

            if not isinstance(last_status, dict):
                raise RuntimeError(
                    "Respuesta de status no valida para "
                    + service_path
                    + ": "
                    + repr(last_status)
                )

            # Detectar errores ArcGIS antes de interpretar el estado.
            error = last_status.get("error")
            if isinstance(error, dict):
                raise RuntimeError(
                    "Error consultando status de "
                    + service_path
                    + ": "
                    + json.dumps(last_status, ensure_ascii=False)
                )

            if str(last_status.get("status", "")).lower() == "error":
                raise RuntimeError(
                    "Error consultando status de "
                    + service_path
                    + ": "
                    + json.dumps(last_status, ensure_ascii=False)
                )

            # ArcGIS Server normalmente los devuelve en la raiz, pero
            # algunos endpoints/proxies pueden envolverlos en 'status'.
            state_obj = last_status
            nested = last_status.get("status")
            if isinstance(nested, dict):
                state_obj = nested

            configured = str(
                state_obj.get("configuredState", "")
            ).upper()
            realtime = str(
                state_obj.get("realTimeState", "")
            ).upper()

            if not configured and not realtime:
                empty_state_count += 1
                print(
                    f"    {service_path}: estado no interpretable "
                    f"({empty_state_count}/3)"
                )
                if empty_state_count >= 3:
                    raise RuntimeError(
                        "El endpoint /status no devolvio configuredState/"
                        "realTimeState para "
                        + service_path
                        + ". Respuesta: "
                        + json.dumps(last_status, ensure_ascii=False)
                    )
                time.sleep(2)
                continue

            empty_state_count = 0
            print(f"    {service_path}: {configured}/{realtime}")

            # configuredState es el objetivo administrativo. realTimeState
            # puede tardar unos segundos adicionales en sincronizarse.
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
                "datasets": [
                    dataset.get("onServerName")
                    for dataset in (db.get("datasets") or [])
                    if dataset.get("onServerName")
                ],
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
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        },
    )
    return destination


def restore_service_backup(backup_dir):
    """
    Restauración NO DESTRUCTIVA del backup.

    Solo crea directorios faltantes y copia/reemplaza archivos desde
    el backup. Nunca elimina la carpeta del servicio ni purga contenido.
    """
    metadata = load_json(Path(backup_dir) / "backup_metadata.json")

    restored = {
        "files_copied": 0,
        "directories": [],
        "configstore_restored": False,
        "arcgisinput_restored": False,
        "restart_arcgis_server_service_may_be_required": False,
    }

    for item in metadata["directories"]:
        source = Path(item["source"])
        backup = Path(item["backup"])

        if not backup.exists():
            raise RuntimeError(
                f"Backup inexistente durante rollback: {backup}"
            )

        source.mkdir(parents=True, exist_ok=True)

        source_text = str(source).lower()
        if "config-store" in source_text:
            restored["configstore_restored"] = True
            restored[
                "restart_arcgis_server_service_may_be_required"
            ] = True

        if "arcgisinput" in source_text:
            restored["arcgisinput_restored"] = True

        copied_here = 0

        for root, dirs, files in os.walk(backup):
            root_path = Path(root)
            relative = root_path.relative_to(backup)
            target_root = source / relative
            target_root.mkdir(parents=True, exist_ok=True)

            for dirname in dirs:
                (target_root / dirname).mkdir(
                    parents=True,
                    exist_ok=True,
                )

            for filename in files:
                backup_file = root_path / filename
                target_file = target_root / filename

                last_error = None
                for attempt in range(3):
                    try:
                        shutil.copy2(backup_file, target_file)
                        copied_here += 1
                        restored["files_copied"] += 1
                        last_error = None
                        break
                    except (PermissionError, OSError) as exc:
                        last_error = exc
                        time.sleep(1)

                if last_error is not None:
                    raise RuntimeError(
                        "No fue posible restaurar archivo durante rollback: "
                        f"{backup_file} -> {target_file}. Error: {last_error}"
                    )

        restored["directories"].append(
            {
                "backup": str(backup),
                "target": str(source),
                "files_copied": copied_here,
            }
        )

    return restored



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
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "ok": ok,
                "result": result,
            }
        )

        if ok:
            return True, result, attempts

        last_result = result
        time.sleep(interval_seconds)

    return False, last_result, attempts


def validate_destination_source(service, cfg):
    """
    Valida la fuente FileGDB destino antes de modificar el servicio.

    IMPORTANTE:
    arcpy.Exists(<ruta .gdb>) se conserva solo como dato diagnostico.
    NO bloquea por si solo la migracion.

    Criterios reales:
      1. La carpeta .gdb debe existir en el sistema operativo.
      2. ArcPy debe poder inventariar su contenido con arcpy.da.Walk o
         ListFeatureClasses/ListDatasets.
      3. Todos los datasets requeridos por el manifest deben existir
         dentro de la GDB destino.
    """
    old_root = cfg["old_root"]
    new_root = cfg["new_root"]

    report = {
        "ok": True,
        "relations": [],
    }

    for workspace in service.get("workspaces", []):
        connection = (
            workspace.get("onServerConnectionString")
            or workspace.get("onPremiseConnectionString")
            or ""
        )

        database = database_from_connection(connection)

        if not database:
            continue

        expected_database = replace_root(
            database,
            old_root,
            new_root,
        )

        relation = {
            "source_database": database,
            "expected_database": expected_database,
            "os_exists": False,
            "arcpy_exists": False,
            "ok": False,
        }

        try:
            relation["os_exists"] = os.path.isdir(
                expected_database
            )
        except Exception as exc:
            relation["os_error"] = str(exc)

        try:
            relation["arcpy_exists"] = bool(
                arcpy.Exists(expected_database)
            )
        except Exception as exc:
            relation["arcpy_exists_error"] = str(exc)

        # Criterio simple:
        # si la carpeta .gdb existe físicamente, se considera válida.
        relation["ok"] = relation["os_exists"]

        if not relation["ok"]:
            relation["error"] = (
                "La FileGDB destino no existe o no es accesible: "
                + str(expected_database)
            )
            report["ok"] = False

        report["relations"].append(relation)

    if not report["relations"]:
        report["ok"] = False
        report["error"] = (
            "No se encontraron workspaces FileGDB para validar"
        )

    return report


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
        evidence = {
            "service": service,
            "baseline_rest": baseline_rest,
            "datastore_status": service.get("datastore_status"),
        }
        return (result, evidence)

    before_files, before_hits = scan_service(
        service,
        old_root,
    )
    if before_hits == 0:
        result["result"] = "NO_OLD_ROOT_FOUND"
        return (result, {})

    print("VALIDANDO FUENTE DESTINO:", service["admin_path"])
    destination_source = validate_destination_source(
        service,
        cfg,
    )

    for relation in destination_source.get("relations", []):
        print("  GDB:", relation.get("expected_database"))
        print(
            "    OS:",
            relation.get("os_exists"),
            "| ArcPy Exists GDB:",
            relation.get("arcpy_exists"),
            "| Workspace legible:",
            relation.get("workspace_readable"),
        )

        for warning in relation.get("warnings", []):
            print("    WARNING:", warning)

        print(
            "    Feature Datasets:",
            len(relation.get("feature_datasets", [])),
            "| Feature Classes:",
            len(relation.get("feature_classes", [])),
        )

        for dataset in relation.get("datasets", []):
            detail = (
                "    Dataset: "
                + str(dataset.get("name"))
                + " | Encontrado: "
                + str(dataset.get("exists"))
            )
            if dataset.get("path"):
                detail += " | " + str(dataset.get("path"))
            print(detail)

        if relation.get("missing_datasets"):
            print(
                "    FALTANTES:",
                ", ".join(relation["missing_datasets"])
            )

    if not destination_source.get("ok"):
        result["result"] = "SKIPPED_DESTINATION_SOURCE_INVALID"
        result["error"] = (
            "La fuente de datos destino no existe, no es "
            "accesible o no contiene los datasets requeridos"
        )
        evidence = {
            "service": service,
            "before_files": before_files,
            "baseline_rest": baseline_rest,
            "destination_source_validation": destination_source,
            "datastore_status": service.get("datastore_status"),
        }
        print(
            "  [BLOQUEADO] No se modificará el servicio."
        )
        return (result, evidence)

    print("  [OK] Fuente destino validada")

    backup = backup_service(
        service,
        Path(run_dir) / "backups",
    )
    evidence = {
        "before_files": before_files,
        "baseline_rest": baseline_rest,
        "destination_source_validation": destination_source,
        "backup": str(backup),
    }

    service_started_after_change = False
    mutation_started = False

    try:
        print("\nSTOP:", service["admin_path"])
        evidence["stop_response"] = admin.service_operation(
            service["admin_path"],
            "stop",
        )

        stopped, stop_status = admin.wait_state(
            service["admin_path"],
            "STOPPED",
            int(cfg.get("stop_timeout_seconds", 180)),
        )

        evidence["stop_status"] = stop_status

        if not stopped:
            raise RuntimeError(
                "Servicio no alcanzó STOPPED/STOPPED"
            )
        
        print("  Esperando liberación de locks...")
        time.sleep(
            int(cfg.get("post_stop_wait_seconds", 20))
        )
        

        # En IGAC se observó que STOPPED/STOPPED puede ocurrir antes de
        # que manifest.json quede realmente estable para escritura.
        manifest_ready = wait_manifest_writable(
            service,
            timeout_seconds=int(
                cfg.get("manifest_wait_timeout_seconds", 60)
            ),
            stable_seconds=int(
                cfg.get("manifest_stable_seconds", 3)
            ),
        )
        evidence["manifest_ready_before_change"] = manifest_ready

        # Desde este punto existen cambios físicos que sí requieren
        # restauración del backup si ocurre una falla posterior.
        mutation_started = True

        # Primero modificar todos los artefactos excepto el manifest.json
        # principal del config-store.
        changes, replacements = apply_service_files_except_manifest(
            service,
            old_root,
            new_root,
            Path(run_dir)
            / "work"
            / service["admin_path"].replace("/", "__"),
        )
        evidence["changes"] = changes
        result["replacements"] = replacements

        # El manifest principal se modifica al final, una vez estabilizado.
        primary_manifest = configstore_manifest_json(service)
        if not primary_manifest.exists():
            raise RuntimeError(
                "No existe manifest.json principal del config-store: "
                + str(primary_manifest)
            )

        manifest_replacements = update_json_file(
            primary_manifest,
            old_root,
            new_root,
        )
        result["replacements"] += manifest_replacements
        evidence["configstore_manifest_change"] = {
            "file": str(primary_manifest),
            "replacements": manifest_replacements,
        }

        if result["replacements"] == 0:
            raise RuntimeError(
                "No se realizaron reemplazos"
            )

        # Validación inmediata del manifest editado.
        manifest_check = verify_manifest_paths(
            primary_manifest,
            old_root,
            new_root,
        )
        evidence["configstore_manifest_check_immediate"] = (
            manifest_check
        )

        if manifest_check["old_root_occurrences"] != 0:
            raise RuntimeError(
                "El manifest.json principal conserva "
                f"{manifest_check['old_root_occurrences']} "
                "referencias OLD_ROOT inmediatamente después "
                "de escribirlo"
            )

        if manifest_check["new_root_occurrences"] == 0:
            raise RuntimeError(
                "El manifest.json principal no contiene NEW_ROOT "
                "después de la actualización"
            )

        # Esperar unos segundos y verificar de nuevo para detectar
        # regeneración/reversión antes de arrancar el servicio.
        time.sleep(
            int(cfg.get("manifest_post_write_verify_seconds", 5))
        )

        manifest_check_delayed = verify_manifest_paths(
            primary_manifest,
            old_root,
            new_root,
        )
        evidence["configstore_manifest_check_delayed"] = (
            manifest_check_delayed
        )

        if manifest_check_delayed["old_root_occurrences"] != 0:
            raise RuntimeError(
                "ArcGIS Server revirtió/regeneró manifest.json: "
                f"{manifest_check_delayed['old_root_occurrences']} "
                "referencias OLD_ROOT reaparecieron antes del START"
            )

        # Validar todos los artefactos físicos.
        physical_after_files, physical_after = scan_service_files(
            service,
            old_root,
        )
        evidence["physical_after"] = physical_after_files

        if physical_after:
            detail = "; ".join(
                f"{row['file']} ({row['occurrences']})"
                for row in physical_after_files
            )
            raise RuntimeError(
                "Persisten "
                f"{physical_after} referencias OLD_ROOT "
                f"en archivos físicos: {detail}"
            )

        print("START:", service["admin_path"])
        evidence["start_response"] = admin.service_operation(
            service["admin_path"],
            "start",
        )
        service_started_after_change = True

        started, start_status = admin.wait_state(
            service["admin_path"],
            "STARTED",
            int(cfg.get("start_timeout_seconds", 300)),
        )
        evidence["start_status"] = start_status

        if not started:
            raise RuntimeError(
                "Servicio no alcanzó STARTED/STARTED"
            )

        # El manifest vivo puede tardar en refrescarse después del START.
        manifest_refreshed, live_manifest, manifest_attempts = (
            wait_live_manifest_refresh(
                admin,
                service["admin_path"],
                old_root,
                new_root,
                timeout_seconds=int(
                    cfg.get(
                        "admin_manifest_refresh_timeout_seconds",
                        60,
                    )
                ),
                interval_seconds=int(
                    cfg.get(
                        "admin_manifest_refresh_interval_seconds",
                        5,
                    )
                ),
            )
        )

        live_old_refs = recursive_find_string_refs(
            live_manifest,
            old_root,
        )
        live_new_refs = recursive_find_string_refs(
            live_manifest,
            new_root,
        )

        live_manifest_old = sum(
            item["occurrences"]
            for item in live_old_refs
        )
        live_manifest_new = sum(
            item["occurrences"]
            for item in live_new_refs
        )

        evidence["admin_manifest_after"] = {
            "refreshed": manifest_refreshed,
            "old_root_occurrences": live_manifest_old,
            "new_root_occurrences": live_manifest_new,
            "old_root_refs": live_old_refs,
            "attempts": manifest_attempts,
            "manifest": live_manifest,
        }

        if live_manifest_old:
            detail = "; ".join(
                f"{item['path']} = {item['value']}"
                for item in live_old_refs
            )
            raise RuntimeError(
                "Persisten "
                f"{live_manifest_old} referencias OLD_ROOT "
                "en Admin REST manifest después de esperar "
                f"{cfg.get('admin_manifest_refresh_timeout_seconds', 60)}s. "
                f"Campos: {detail}"
            )

        if live_manifest_new == 0:
            raise RuntimeError(
                "Admin REST manifest no refleja NEW_ROOT "
                "después del reinicio"
            )

        rest_ok, rest_after, rest_attempts = validate_rest_service_retry(
            admin,
            service,
            timeout_seconds=int(
                cfg.get("rest_post_start_timeout_seconds", 120)
            ),
            interval_seconds=int(
                cfg.get("rest_post_start_interval_seconds", 5)
            ),
        )
        evidence["rest_after"] = rest_after
        evidence["rest_after_attempts"] = rest_attempts

        if not rest_ok:
            raise RuntimeError(
                "Validación REST posterior falló después de reintentos"
            )

        result["result"] = "SUCCESS"
        return (result, evidence)

    except Exception as exc:
        result["error"] = str(exc)
        print("ERROR:", exc)

        # Si falló antes de tocar archivos, NO se restaura nada.
        if not mutation_started:
            print(
                "Falla previa a cambios físicos: no se ejecutará "
                "rollback de archivos."
            )
            try:
                admin.token = admin.generate_token()
                start_response = admin.service_operation(
                    service["admin_path"],
                    "start",
                )
                started, start_status = admin.wait_state(
                    service["admin_path"],
                    "STARTED",
                    int(cfg.get("start_timeout_seconds", 300)),
                )
                evidence["recovery_no_changes"] = {
                    "start_response": start_response,
                    "start_status": start_status,
                    "started": started,
                }
                result["result"] = (
                    "FAILED_NO_CHANGES_SERVICE_RESTARTED"
                    if started
                    else "FAILED_NO_CHANGES_ATTENTION_REQUIRED"
                )
            except Exception as recovery_exc:
                result["result"] = (
                    "FAILED_NO_CHANGES_ATTENTION_REQUIRED"
                )
                result["error"] += (
                    " | Recuperación: " + str(recovery_exc)
                )
            return (result, evidence)

        # Si ya hubo cambios físicos, el rollback solo se ejecuta
        # después de confirmar que el servicio quedó detenido.
        print("ERROR -> ROLLBACK SEGURO:", exc)

        try:
            admin.token = admin.generate_token()
            stop_response = admin.service_operation(
                service["admin_path"],
                "stop",
            )
            rollback_stopped, rollback_stop_status = (
                admin.wait_state(
                    service["admin_path"],
                    "STOPPED",
                    int(cfg.get("stop_timeout_seconds", 180)),
                )
            )

            evidence["rollback_stop"] = {
                "response": stop_response,
                "status": rollback_stop_status,
                "stopped": rollback_stopped,
            }

            if not rollback_stopped:
                result["result"] = (
                    "FAILED_ROLLBACK_NOT_ATTEMPTED_SERVICE_NOT_STOPPED"
                )
                result["error"] += (
                    " | Rollback de archivos NO ejecutado porque "
                    "no se confirmó STOPPED. Backup conservado en: "
                    + str(backup)
                )
                return (result, evidence)

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
                evidence["manifest_wait_rollback_warning"] = (
                    str(wait_exc)
                )

            rollback_restore = restore_service_backup(backup)
            evidence["rollback_restore"] = rollback_restore

            if rollback_restore.get(
                "restart_arcgis_server_service_may_be_required"
            ):
                print(
                    "  AVISO: se restauró config-store. Si el servicio "
                    "no aparece en Manager/Admin, reinicia el servicio "
                    "de Windows ArcGIS Server."
                )

            admin.token = admin.generate_token()
            rollback_start_response = admin.service_operation(
                service["admin_path"],
                "start",
            )
            started, rollback_start_status = admin.wait_state(
                service["admin_path"],
                "STARTED",
                int(cfg.get("start_timeout_seconds", 300)),
            )

            rest_ok = False
            rollback_rest = {}
            if started:
                try:
                    rest_ok, rollback_rest = validate_rest_service(
                        admin,
                        service,
                    )
                except Exception as rest_exc:
                    rollback_rest = {"error": str(rest_exc)}

            evidence["rollback"] = {
                "start_response": rollback_start_response,
                "started": started,
                "start_status": rollback_start_status,
                "rest_ok": rest_ok,
                "rest": rollback_rest,
                "windows_service_restart_may_be_required": (
                    rollback_restore.get(
                        "restart_arcgis_server_service_may_be_required",
                        False,
                    )
                ),
            }

            if started and rest_ok:
                result["result"] = "FAILED_ROLLBACK_OK"
            else:
                result["result"] = (
                    "FAILED_ROLLBACK_RESTORED_RESTART_MAY_BE_REQUIRED"
                )
                result["error"] += (
                    " | Backup restaurado sin borrar carpetas. "
                    "Si el servicio no aparece, reinicia el servicio "
                    "de Windows ArcGIS Server."
                )

        except Exception as rollback_exc:
            result["result"] = "FAILED_ROLLBACK_ATTENTION_REQUIRED"
            result["error"] += (
                " | Rollback seguro: " + str(rollback_exc)
                + " | Backup conservado en: " + str(backup)
            )

        return (result, evidence)



def wait_live_manifest_refresh(
    admin,
    service_path,
    old_root,
    new_root,
    timeout_seconds=60,
    interval_seconds=5,
):
    """
    Reconsulta el manifest vivo después del START.

    ArcGIS Server puede tardar algunos segundos en reflejar el manifest
    recién actualizado. No se dispara rollback por la primera lectura.
    """
    deadline = time.time() + timeout_seconds
    attempts = []
    last_manifest = None

    while time.time() <= deadline:
        manifest = admin.service_manifest(service_path)
        last_manifest = manifest

        old_refs = recursive_find_string_refs(
            manifest,
            old_root,
        )
        new_refs = recursive_find_string_refs(
            manifest,
            new_root,
        )

        attempt = {
            "timestamp": datetime.datetime.now().isoformat(
                timespec="seconds"
            ),
            "old_root_occurrences": sum(
                item["occurrences"]
                for item in old_refs
            ),
            "new_root_occurrences": sum(
                item["occurrences"]
                for item in new_refs
            ),
            "old_root_refs": old_refs,
        }
        attempts.append(attempt)

        if attempt["old_root_occurrences"] == 0:
            return True, manifest, attempts

        time.sleep(interval_seconds)

    return False, last_manifest, attempts



# 12. Inventario, reportes y ejecución principal


def main():

    print("=" * 80)
    print("MODO SAFE: rollback no destructivo")
    print("No se eliminan carpetas de config-store ni arcgisinput")
    print("=" * 80)
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
    print("IGAC - MIGRADOR VECTORIAL ARCGIS SERVER 11.3 v1.16 SAFE")
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