"""Read-only nHaystack / Haystack grid checker.

Compares an expected point-binding table (device id, object id, optional Brick
class and Haystack tags) with a Haystack grid. The grid may be a saved fixture
(JSON, Zinc, or CSV) or a live GET of ``/haystack/read?filter=point``.

Live calls are GET-only (about, read filters, nav). Credentials may be loaded
from an env file (``NIAGARA_USER`` / ``NIAGARA_PASS``) or the environment.
TLS verification is skipped only with an explicit insecure flag. Credentials,
Authorization headers, and cookie values are never written into reports.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import ssl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from rdflib import Graph

from .refs import bindings_from_graph, normalize_object_type

MARKER_TRUE = {"", "m", "marker", "true", "x", "✓"}

ENV_USER_KEYS = ("NIAGARA_USER", "NIAGARA_STATION_LOGIN", "BASK_223P_USER")
ENV_PASS_KEYS = ("NIAGARA_PASS", "NIAGARA_STATION_PASS", "BASK_223P_PASS")
LIVE_READS = (
    ("about", "about"),
    ("point", "read?filter=point"),
    ("equip", "read?filter=equip"),
    ("nav", "nav"),
)


def load_env_credentials(env_file: Path | None = None) -> tuple[str | None, str | None]:
    """Load station username/password from an env file and/or process env.

    Values are never logged. Returns (username, password) or (None, None).
    """
    data: dict[str, str] = {}
    if env_file is not None:
        text = env_file.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    def pick(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            if data.get(key):
                return data[key]
            env_val = os.environ.get(key)
            if env_val:
                return env_val
        return None
    return pick(ENV_USER_KEYS), pick(ENV_PASS_KEYS)


def _basic_authorization(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _auth_scheme(www: str | None) -> str:
    if not www:
        return ""
    low = www.lower()
    parts = []
    if "scram" in low:
        parts.append("scram")
        if "sha-256" in low or "sha256" in low:
            parts.append("sha-256")
    if "basic" in low:
        parts.append("basic")
    return " ".join(parts) or "other"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _ssl_context(insecure: bool):
    if insecure:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _about_info(rows: list[dict[str, Any]]) -> dict[str, str]:
    if not rows:
        return {}
    raw = rows[0].get("raw") or {}
    info = {}
    for key in ("productName", "productVersion", "serverName", "vendorName",
                "moduleName", "moduleVersion", "version", "haystackVersion",
                "whoami", "projName"):
        val = raw.get(key) or rows[0].get(key)
        if val not in (None, ""):
            info[key] = str(val)
    return info




def split_tags(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().casefold() for item in value if str(item).strip()}
    text = str(value).strip()
    if not text:
        return set()
    return {part.strip().casefold() for part in re.split(r"[\s,;|]+", text) if part.strip()}


def _cell(row: dict, *names: str) -> str:
    lower = {str(k).casefold(): v for k, v in row.items() if k is not None}
    for name in names:
        if name.casefold() in lower and lower[name.casefold()] not in (None, ""):
            val = lower[name.casefold()]
            if isinstance(val, dict):
                return str(val.get("val") or val.get("dis") or val.get("name") or "")
            return str(val).strip()
    return ""



def normalize_expected_row(row: dict[str, str]) -> dict[str, str]:
    out = {str(k): ('' if v is None else str(v).strip()) for k, v in row.items() if k}
    typ = out.get('object_type') or ''
    inst = out.get('object_instance') or ''
    if typ and inst and not out.get('object_identifier'):
        try:
            out['object_identifier'] = f'{normalize_object_type(typ)},{inst}'
        except ValueError:
            out['object_identifier'] = f'{typ},{inst}'
    if not out.get('bacnet_uri') and out.get('device_instance') and out.get('object_identifier'):
        out['bacnet_uri'] = f"bacnet://{out['device_instance']}/{out['object_identifier']}"
    return out


def load_expected(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix in {".ttl", ".turtle", ".rdf", ".nt"}:
        graph = Graph()
        graph.parse(path, format="turtle")
        return [normalize_expected_row(r) for r in bindings_from_graph(graph)]
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "rows" in payload:
            payload = payload["rows"]
        if not isinstance(payload, list):
            raise ValueError("Expected JSON binding list")
        return [normalize_expected_row({str(k): "" if v is None else str(v) for k, v in row.items()}) for row in payload]
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    if not rows:
        raise ValueError(f"{path} has no binding rows")
    return [normalize_expected_row(row) for row in rows]


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("_kind") == "marker" or value.get("kind") == "marker":
            return "M"
        if "val" in value:
            return value.get("val")
        if "dis" in value and len(value) <= 3:
            return value.get("dis")
    return value


def _normalize_row(row: dict) -> dict[str, Any]:
    flat = {str(k): _unwrap(v) for k, v in row.items() if k is not None}
    tags = split_tags(flat.get("tags") or flat.get("haystack_tags") or flat.get("haystackTags"))
    # Marker columns in a Haystack grid become tags.
    for key, val in list(flat.items()):
        key_l = key.casefold()
        if key_l in {"id", "dis", "kind", "unit", "tz", "mod", "bacnetcur", "bacnet_uri",
                     "device_instance", "object_identifier", "object_name", "description",
                     "tags", "haystack_tags", "brick_class"}:
            continue
        text = "" if val is None else str(val).strip().casefold()
        if text in MARKER_TRUE or val is True:
            tags.add(key.casefold())
    if _cell(flat, "point") or "point" in tags:
        tags.add("point")
    uri = _cell(flat, "bacnetCur", "bacnet_uri", "bacnetUri", "bacnetURI")
    object_id = _cell(flat, "object_identifier", "objectIdentifier", "bacnetObject")
    device_instance = _cell(flat, "device_instance", "deviceInstance", "bacnetDevice")
    if uri and uri.lower().startswith("bacnet://"):
        rest = uri.split("://", 1)[1]
        if "/" in rest:
            dev, obj = rest.split("/", 1)
            device_instance = device_instance or dev
            object_id = object_id or obj
    if object_id and "," in object_id:
        typ, inst = object_id.split(",", 1)
        try:
            object_id = f"{normalize_object_type(typ)},{inst}"
        except ValueError:
            pass
    return {
        "id": _cell(flat, "id"),
        "dis": _cell(flat, "dis", "object_name", "name", "navName"),
        "bacnet_uri": uri,
        "device_instance": device_instance,
        "object_identifier": object_id,
        "object_name": _cell(flat, "object_name", "dis", "name"),
        "brick_class": _cell(flat, "brick_class", "brickClass"),
        "tags": tags,
        "raw": flat,
    }


def parse_hayson(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "rows" in payload:
        return [_normalize_row(row) for row in payload["rows"]]
    if isinstance(payload, list):
        return [_normalize_row(row) for row in payload]
    raise ValueError("Unsupported Haystack JSON grid")


def parse_zinc(text: str) -> list[dict[str, Any]]:
    """Parse a minimal Zinc grid (header + rows). Enough for nHaystack read-back fixtures."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.strip().startswith("//")]
    if not lines or not lines[0].lower().startswith("ver:"):
        raise ValueError("Zinc grid must start with ver:")
    # skip metadata lines until the column header (no leading ver: and contains a comma or id)
    idx = 1
    while idx < len(lines) and lines[idx].lower().startswith("ver:"):
        idx += 1
    if idx >= len(lines):
        return []
    header = _split_zinc(lines[idx])
    rows = []
    for line in lines[idx + 1:]:
        values = _split_zinc(line)
        row = {header[i]: (values[i] if i < len(values) else "") for i in range(len(header))}
        rows.append(_normalize_row(row))
    return rows


def _split_zinc(line: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == '"' and i + 1 < len(line) and line[i + 1] == '"':
                current.append('"')
                i += 2
                continue
            if ch == '"':
                in_str = False
                i += 1
                continue
            current.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == ",":
            parts.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current).strip())
    cleaned = []
    for part in parts:
        if part in {"M", "✔"}:
            cleaned.append("M")
        elif part.startswith("@"):
            cleaned.append(part[1:])
        elif part in {"N", ""}:
            cleaned.append("")
        else:
            cleaned.append(part)
    return cleaned


def load_grid(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix in {".zinc", ".txt"} or text.lstrip().lower().startswith("ver:"):
        return parse_zinc(text)
    if suffix == ".json":
        return parse_hayson(json.loads(text))
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    return [_normalize_row(row) for row in rows]


def _expected_uri(row: dict[str, str]) -> str:
    uri = (row.get("bacnet_uri") or "").strip()
    if uri:
        return uri
    dev, oid = row.get("device_instance") or "", row.get("object_identifier") or ""
    if dev and oid:
        return f"bacnet://{dev}/{oid}"
    return ""


def _match(expected: dict[str, str], actual: dict[str, Any]) -> bool:
    exp_uri = _expected_uri(expected).casefold()
    act_uri = (actual.get("bacnet_uri") or "").casefold()
    if exp_uri and act_uri and exp_uri == act_uri:
        return True
    if (expected.get("device_instance") and expected.get("object_identifier")
            and str(actual.get("device_instance") or "") == str(expected["device_instance"])
            and str(actual.get("object_identifier") or "").casefold()
            == str(expected["object_identifier"]).casefold()):
        return True
    exp_name = (expected.get("object_name") or "").strip().casefold()
    if exp_name and exp_name in {
        (actual.get("object_name") or "").strip().casefold(),
        (actual.get("dis") or "").strip().casefold(),
        (actual.get("id") or "").strip().casefold(),
    }:
        return True
    return False


def _bound(expected: dict[str, str], actual: dict[str, Any]) -> bool:
    exp_uri = _expected_uri(expected).casefold()
    act_uri = (actual.get("bacnet_uri") or "").casefold()
    if exp_uri and act_uri:
        return exp_uri == act_uri
    if expected.get("device_instance") and expected.get("object_identifier"):
        return (str(actual.get("device_instance") or "") == str(expected["device_instance"])
                and str(actual.get("object_identifier") or "").casefold()
                == str(expected["object_identifier"]).casefold())
    return False


def _tagged(expected: dict[str, str], actual: dict[str, Any]) -> bool:
    required = split_tags(expected.get("haystack_tags"))
    required.add("point")
    tags = actual.get("tags") or set()
    return required <= set(tags)


def check_grid(expected: list[dict[str, str]], actual: list[dict[str, Any]]) -> dict:
    findings = []
    used: set[int] = set()
    for row in expected:
        match_idx = next((i for i, act in enumerate(actual) if i not in used and _match(row, act)), None)
        name = row.get("object_name") or row.get("bacnet_uri") or "?"
        if match_idx is None:
            findings.append({
                "object_name": name,
                "device_instance": row.get("device_instance", ""),
                "object_identifier": row.get("object_identifier", ""),
                "bacnet_uri": _expected_uri(row),
                "status": "missing",
                "present": False, "tagged": False, "bound": False,
                "detail": "required point not present in the Haystack grid",
            })
            continue
        used.add(match_idx)
        act = actual[match_idx]
        bound = _bound(row, act)
        tagged = _tagged(row, act)
        if bound and tagged:
            status = "ok"
            detail = "present, tagged, and bound to the expected BACnet object"
        elif not bound and not tagged:
            status = "wrong_binding_and_untagged"
            detail = "matched by name but BACnet binding and tags do not match"
        elif not bound:
            status = "wrong_binding"
            detail = (f"expected {_expected_uri(row) or (row.get('device_instance') + '/' + row.get('object_identifier', ''))}"
                      f" but grid has {act.get('bacnet_uri') or act.get('object_identifier') or 'no BACnet binding'}")
        else:
            status = "untagged"
            missing = sorted((split_tags(row.get("haystack_tags")) | {"point"}) - set(act.get("tags") or set()))
            detail = "missing tags: " + ", ".join(missing)
        findings.append({
            "object_name": name,
            "device_instance": row.get("device_instance", ""),
            "object_identifier": row.get("object_identifier", ""),
            "bacnet_uri": _expected_uri(row),
            "haystack_id": act.get("id") or "",
            "status": status,
            "present": True, "tagged": tagged, "bound": bound,
            "detail": detail,
        })
    extras = []
    for i, act in enumerate(actual):
        if i not in used:
            extras.append({
                "object_name": act.get("object_name") or act.get("dis") or "",
                "haystack_id": act.get("id") or "",
                "bacnet_uri": act.get("bacnet_uri") or "",
                "status": "extra",
            })
    ok = sum(1 for f in findings if f["status"] == "ok")
    missing = sum(1 for f in findings if f["status"] == "missing")
    untagged = sum(1 for f in findings if f["status"] == "untagged")
    wrong = sum(1 for f in findings if "wrong_binding" in f["status"])
    summary = {
        "required": len(expected),
        "grid_rows": len(actual),
        "ok": ok,
        "missing": missing,
        "untagged": untagged,
        "wrong_binding": wrong,
        "extra": len(extras),
        "passed": missing == 0 and untagged == 0 and wrong == 0,
        "findings": findings,
        "extras": extras,
    }
    return summary


def fetch_haystack_grid(base_url: str, timeout: float = 8.0,
                        env_file: Path | None = None, insecure: bool = False,
                        username: str | None = None, password: str | None = None
                        ) -> tuple[list[dict[str, Any]] | None, dict]:
    """GET-only live nHaystack reads. Never logs credentials or auth headers."""
    base = base_url if base_url.endswith("/") else base_url + "/"
    user, pw = username, password
    if not user or not pw:
        file_user, file_pw = load_env_credentials(env_file)
        user = user or file_user
        pw = pw or file_pw
    meta: dict[str, Any] = {
        "base_url": base_url, "about_status": None, "read_status": None,
        "reachable": False, "login_required": False, "auth_mode": "none",
        "auth_scheme_offered": "", "insecure_tls": bool(insecure),
        "product": {}, "reads": {}, "detail": "",
    }
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        meta["detail"] = "Haystack URL must be http or https"
        return None, meta
    if parsed.scheme == "https" and not insecure:
        # Local stations usually use a self-signed cert; require an explicit flag.
        pass
    ctx = _ssl_context(insecure)
    opener = build_opener(_NoRedirect, HTTPSHandler(context=ctx))
    if user and pw:
        meta["auth_mode"] = "basic"
        auth_header = _basic_authorization(user, pw)
    else:
        auth_header = None

    def get(url: str, accept: str = "text/zinc") -> tuple[int, str, str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "bask-223p-haystack-check",
        }
        if auth_header:
            headers["Authorization"] = auth_header
        req = Request(url, method="GET", headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, resp.headers.get("Content-Type") or "", resp.headers.get("Location") or "", body
        except HTTPError as exc:
            location = exc.headers.get("Location") or "" if exc.headers else ""
            www = exc.headers.get("WWW-Authenticate") or "" if exc.headers else ""
            ctype = exc.headers.get("Content-Type") or "" if exc.headers else ""
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            # Stash offered scheme on 401 without storing the raw header (may contain tokens).
            if exc.code == 401 and www and not meta.get("auth_scheme_offered"):
                meta["auth_scheme_offered"] = _auth_scheme(www)
            return exc.code, ctype, location, body
        except URLError as exc:
            raise ConnectionError(str(exc.reason or exc)) from exc

    def is_login(status: int, location: str, body: str) -> bool:
        if status in {401, 403}:
            return True
        if status in {301, 302, 303, 307, 308}:
            return "login" in (location or "").casefold() or True
        if "prelogin" in body.casefold() or "/login" in body.casefold():
            return True
        return False

    def parse_body(body: str) -> list[dict[str, Any]]:
        text = body.lstrip()
        if not text:
            return []
        if text.startswith("{") or text.startswith("["):
            return parse_hayson(json.loads(body))
        return parse_zinc(body)

    try:
        about_status, _, about_loc, about_body = get(urljoin(base, "about"))
        meta["about_status"] = about_status
        point_url = urljoin(base, "read?filter=point")
        read_status, _, read_loc, read_body = get(point_url)
        meta["read_status"] = read_status
    except (ConnectionError, TimeoutError, OSError) as exc:
        meta["detail"] = f"could not reach {base_url}: {exc}"
        return None, meta

    if meta.get("auth_scheme_offered") == "scram sha-256" or meta.get("auth_scheme_offered") == "scram":
        meta["login_required"] = True
        meta["auth_mode"] = "scram_required"
        meta["detail"] = (
            "Server offered SCRAM authentication; HTTP Basic was not accepted. "
            "SCRAM-SHA-256 client support is not used because the station accepted or offered a different path."
        )
        # If we actually got a 401 scram after sending Basic, report clearly.
        if about_status in {401, 403} or read_status in {401, 403}:
            return None, meta

    if is_login(about_status, about_loc, about_body) or is_login(read_status, read_loc, read_body):
        meta["login_required"] = True
        offered = meta.get("auth_scheme_offered") or "form-login"
        creds = "HTTP Basic credentials were sent" if auth_header else "No credentials were sent"
        meta["detail"] = (
            f"Haystack REST at {base_url} redirected to the station login or requires authentication "
            f"(about={about_status}, read={read_status}, offered={offered}). {creds}."
        )
        return None, meta
    if read_status != 200 or about_status != 200:
        meta["detail"] = f"Haystack GET returned about={about_status} read={read_status}"
        return None, meta

    try:
        about_rows = parse_body(about_body)
        rows = parse_body(read_body)
    except (ValueError, json.JSONDecodeError) as exc:
        meta["detail"] = f"could not parse Haystack grid: {exc}"
        return None, meta

    meta["product"] = _about_info(about_rows)
    meta["reads"]["about"] = {"status": about_status, "rows": len(about_rows)}
    meta["reads"]["point"] = {"status": read_status, "rows": len(rows)}
    meta["about_rows"] = about_rows
    # Additional GET-only filters; failures are recorded, not fatal.
    extra_grids: dict[str, list[dict[str, Any]]] = {}
    for name, rel in LIVE_READS:
        if name in {"about", "point"}:
            continue
        status, _, loc, body = get(urljoin(base, rel))
        entry: dict[str, Any] = {"status": status}
        if is_login(status, loc, body):
            entry["login_required"] = True
        elif status == 200:
            try:
                extra_grids[name] = parse_body(body)
                entry["rows"] = len(extra_grids[name])
            except (ValueError, json.JSONDecodeError) as exc:
                entry["error"] = f"parse failed: {type(exc).__name__}"
        meta["reads"][name] = entry
    meta["extra_grids"] = extra_grids
    meta["reachable"] = True
    meta["detail"] = f"read {len(rows)} point rows from {point_url} using {meta['auth_mode']}"
    return rows, meta


def write_check_report(summary: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "haystack-check.json"
    csv_path = out / "haystack-check.csv"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    fields = ["object_name", "device_instance", "object_identifier", "bacnet_uri",
              "haystack_id", "status", "present", "tagged", "bound", "detail"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary.get("findings") or [])
    return {"check_json": str(json_path), "check_csv": str(csv_path)}


def run_check(expected_path: Path, out: Path, grid_path: Path | None = None,
              url: str | None = None, env_file: Path | None = None,
              insecure: bool = False) -> dict:
    expected = load_expected(expected_path)
    probe = None
    if grid_path is not None:
        actual = load_grid(grid_path)
        source = str(grid_path)
    elif url:
        actual, probe = fetch_haystack_grid(url, env_file=env_file, insecure=insecure)
        source = url
        if actual is None:
            result = {
                "passed": False,
                "required": len(expected),
                "reachable": False,
                "login_required": bool(probe and probe.get("login_required")),
                "source": source,
                "probe": probe,
                "detail": (probe or {}).get("detail") or "Haystack grid was not reachable",
            }
            out.mkdir(parents=True, exist_ok=True)
            (out / "haystack-check.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["output"] = str(out / "haystack-check.json")
            return result
    else:
        raise ValueError("Provide --grid (fixture) or --url (live read-only)")
    summary = check_grid(expected, actual)
    summary["source"] = source
    if probe:
        # Drop in-memory grids from the written probe (they are saved as sibling files).
        extra = probe.pop("extra_grids", {}) if probe else {}
        about_rows = probe.pop("about_rows", None) if probe else None
        summary["probe"] = {k: v for k, v in probe.items() if k not in {"extra_grids", "about_rows"}}
        summary["reachable"] = probe.get("reachable", True)
        summary["login_required"] = probe.get("login_required", False)
        summary["auth_mode"] = probe.get("auth_mode")
        summary["product"] = probe.get("product") or {}
        out.mkdir(parents=True, exist_ok=True)
        (out / "point-grid.json").write_text(json.dumps(actual, indent=2, default=str), encoding="utf-8")
        if about_rows is not None:
            (out / "about-grid.json").write_text(json.dumps(about_rows, indent=2, default=str), encoding="utf-8")
        for name, rows in extra.items():
            (out / f"{name}-grid.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    else:
        summary["reachable"] = True
        summary["login_required"] = False
    paths = write_check_report(summary, out)
    public = {k: v for k, v in summary.items() if k not in {"findings", "extras"}}
    public.update(paths)
    public["findings_preview"] = summary["findings"][:8]
    return public


def grid_from_bindings(rows: list[dict[str, str]]) -> dict:
    """Build a Hayson grid fixture that matches a binding table (for tests and demos)."""
    cols = ["id", "dis", "point", "bacnetCur", "tags"]
    hay_rows = []
    for row in rows:
        name = row.get("object_name") or row.get("object_identifier") or "point"
        tags = " ".join(sorted(split_tags(row.get("haystack_tags")) | {"point"}))
        hay_rows.append({
            "id": f"p:{name}",
            "dis": name,
            "point": {"_kind": "marker"},
            "bacnetCur": _expected_uri(row),
            "tags": tags,
        })
    return {"_kind": "grid", "meta": {"ver": "3.0"}, "cols": [{"name": c} for c in cols], "rows": hay_rows}
