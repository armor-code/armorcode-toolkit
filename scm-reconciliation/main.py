#!/usr/bin/env python3
"""
ArmorCode SCM Workspace Reconciler — single standalone script.

Identifies SCM workspaces/orgs missing from ArmorCode and auto-creates them.
Supports: GitHub (PAT), Bitbucket (Cloud Basic + OnPrem Token/Basic), Azure DevOps (Cloud + OnPrem).

Usage:
  ARMORCODE_API_TOKEN=<token> python main.py [--config /path/to/config.json]

Docker:
  docker run -v /path/to/config.json:/config/config.json -v /path/to/output:/output -e ARMORCODE_API_TOKEN=<token> armorcode-scm-reconciler
"""

import argparse
import base64
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# Disable SSL verification globally
requests.packages.urllib3.disable_warnings()
_SSL_VERIFY = False

# ── Constants ────────────────────────────────────────────────────────────────

_TIMEOUT = 30
_DEFAULT_CONFIG = "/config/config.json"
_BASE_DIR = Path("/output") if Path("/output").exists() else Path(tempfile.gettempdir()) / "armorcode-scm-reconciler"
_LOG_DIR = Path(tempfile.gettempdir()) / "armorcode" / "log"
_DATE_FMT = "%Y-%m-%d"

_CLOUD_HOST_DEFAULTS = {
    "GITHUB": "https://github.com",
    "BITBUCKET": "https://bitbucket.org",
    "AZURE_REPOS": "https://dev.azure.com/",
}

_INSTALL_CONFIG_DEFAULTS = {
    "GITHUB": {"authType": "TOKEN", "subProductMappingConfig": ["INACTIVE", "DORMANT"], "githubIssuesEnabled": False},
    "BITBUCKET": {"subProductMappingConfig": ["INACTIVE", "DORMANT"]},
    "AZURE_REPOS": {"subProductMappingConfig": ["INACTIVE", "DORMANT"]},
}

log = logging.getLogger("reconciler")


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _request_with_retry(method: str, url: str, session=None, max_retries: int = 3, **kwargs) -> requests.Response:
    """
    Makes an HTTP request with rate-limit-aware retry.
    Respects X-Rate-Limit-Retry-After-Seconds and X-Rate-Limit-Reset headers.
    """
    import time
    kwargs.setdefault("timeout", _TIMEOUT)
    kwargs.setdefault("verify", _SSL_VERIFY)
    caller = session or requests
    func = getattr(caller, method)

    for attempt in range(max_retries + 1):
        resp = func(url, **kwargs)
        if resp.status_code != 429:
            return resp

        # Rate limited — check headers for wait time
        retry_after = resp.headers.get("X-Rate-Limit-Retry-After-Seconds") or resp.headers.get("Retry-After")
        reset_time = resp.headers.get("X-Rate-Limit-Reset")

        if retry_after:
            wait = int(retry_after)
        elif reset_time:
            wait = max(0, int(reset_time) - int(time.time()))
        else:
            wait = min(5 * (2 ** attempt), 60)  # exponential backoff, max 60s

        if attempt < max_retries:
            log.warning("Rate limited (429) on %s, waiting %ds (attempt %d/%d)", url, wait, attempt + 1, max_retries)
            time.sleep(wait)
        else:
            return resp  # last attempt, return 429 response for caller to handle

    return resp


# ── Config ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """Loads, validates, and fills defaults for config.json."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"[CONFIG ERROR] File not found: {path}")
    try:
        cfg = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"[CONFIG ERROR] Invalid JSON: {e}")

    if "armorcode" not in cfg or "base_url" not in cfg["armorcode"]:
        sys.exit("[CONFIG ERROR] Missing armorcode.base_url")
    if not cfg.get("scm"):
        sys.exit("[CONFIG ERROR] No SCM entries under 'scm'")

    for i, entry in enumerate(cfg["scm"]):
        pfx = f"[CONFIG ERROR] scm[{i}]"
        scm_type = entry.get("type", "").upper()
        hosting = entry.get("hosting_type", "").lower()

        if scm_type not in _CLOUD_HOST_DEFAULTS:
            sys.exit(f"{pfx} Unknown type '{scm_type}'")
        if hosting not in ("cloud", "onprem"):
            sys.exit(f"{pfx} hosting_type must be Cloud or OnPrem")

        if hosting == "cloud" and not entry.get("host_url"):
            entry["host_url"] = _CLOUD_HOST_DEFAULTS[scm_type]
        if hosting == "onprem" and not entry.get("host_url"):
            sys.exit(f"{pfx} host_url is required for OnPrem")

        if scm_type == "GITHUB" and not entry.get("pat"):
            sys.exit(f"{pfx} Missing 'pat' for GITHUB")
        if scm_type == "BITBUCKET" and hosting == "cloud":
            for f in ("username", "password"):
                if not entry.get(f):
                    sys.exit(f"{pfx} Missing '{f}' for BITBUCKET Cloud")
        if scm_type == "BITBUCKET" and hosting == "onprem":
            if not entry.get("token") and not (entry.get("username") and entry.get("password")):
                sys.exit(f"{pfx} BITBUCKET OnPrem requires 'token' or 'username'+'password'")
        if scm_type == "AZURE_REPOS" and not entry.get("token"):
            sys.exit(f"{pfx} Missing 'token' for AZURE_REPOS")
        if scm_type == "AZURE_REPOS" and hosting == "onprem" and not entry.get("collection"):
            sys.exit(f"{pfx} Missing 'collection' for AZURE_REPOS OnPrem")

    return cfg


# ── Storage ──────────────────────────────────────────────────────────────────

def init_storage() -> Path:
    _BASE_DIR.mkdir(parents=True, exist_ok=True)
    (_BASE_DIR / "data").mkdir(exist_ok=True)
    _cleanup_old(_BASE_DIR / "data")
    return _BASE_DIR


def _cleanup_old(parent: Path, days: int = 30) -> None:
    cutoff = date.today() - timedelta(days=days)
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        try:
            if date.fromisoformat(child.name) < cutoff:
                shutil.rmtree(child)
        except ValueError:
            pass


def _today() -> str:
    return date.today().strftime(_DATE_FMT)


def scm_data_dir(base: Path, scm_key: str, run_ts: str) -> Path:
    d = base / "data" / _today() / run_ts / scm_key
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── SCM Clients ──────────────────────────────────────────────────────────────

# GitHub

def github_fetch_orgs(pat: str, host_url: str) -> set[str]:
    is_cloud = "github.com" in host_url
    base_api = "https://api.github.com" if is_cloud else host_url.rstrip("/") + "/api/v3"
    url = f"{base_api}/user/orgs?per_page=100"
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    orgs: set[str] = set()

    while url:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT, verify=_SSL_VERIFY)
        resp.raise_for_status()
        for org in resp.json():
            if login := org.get("login"):
                orgs.add(login)
        link = resp.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None

    log.info("GitHub: fetched %d orgs from %s", len(orgs), host_url)
    return orgs


# Bitbucket

def _bb_basic_token(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def bb_fetch_workspaces_cloud(username: str, password: str) -> set[str]:
    headers = {"Authorization": f"Basic {_bb_basic_token(username, password)}", "Accept": "application/json"}
    url = "https://api.bitbucket.org/2.0/user/workspaces?pagelen=100"
    workspaces: set[str] = set()

    while url:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT, verify=_SSL_VERIFY)
        resp.raise_for_status()
        body = resp.json()
        for ws in body.get("values", []):
            if slug := ws.get("slug"):
                workspaces.add(slug)
        url = body.get("next")

    log.info("Bitbucket Cloud: fetched %d workspaces", len(workspaces))
    return workspaces


def bb_fetch_projects_onprem(host_url: str, token: str = None,
                             username: str = None, password: str = None) -> set[str]:
    base = host_url.rstrip("/")
    if token:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    else:
        headers = {"Authorization": f"Basic {_bb_basic_token(username, password)}", "Accept": "application/json"}

    start, limit = 0, 100
    projects: set[str] = set()

    while True:
        resp = requests.get(f"{base}/rest/api/latest/projects?limit={limit}&start={start}",
                            headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        for proj in body.get("values", []):
            if key := proj.get("key"):
                projects.add(key)
        if body.get("isLastPage", True):
            break
        start += limit

    log.info("Bitbucket OnPrem: fetched %d projects from %s", len(projects), host_url)
    return projects


# Azure DevOps

def _ado_basic_auth(token: str) -> str:
    return base64.b64encode(f":{token}".encode()).decode()


def ado_fetch_orgs_cloud(token: str) -> set[str]:
    headers = {"Authorization": f"Basic {_ado_basic_auth(token)}", "Accept": "application/json"}

    member_id = None
    try:
        resp = requests.get(
            "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0",
            headers=headers, timeout=_TIMEOUT, verify=_SSL_VERIFY,
        )
        if resp.status_code == 200:
            member_id = resp.json().get("id")
    except Exception as e:
        log.warning("Azure Cloud: profile fetch failed: %s", e)

    if not member_id:
        log.warning("Azure Cloud: could not get profile from global vssps (PAT may be org-scoped)")
        return set()

    try:
        resp = requests.get(
            f"https://app.vssps.visualstudio.com/_apis/accounts?memberId={member_id}&api-version=7.0",
            headers=headers, timeout=_TIMEOUT, verify=_SSL_VERIFY,
        )
        if resp.status_code == 200:
            data = resp.json()
            values = data.get("value", data) if isinstance(data, dict) else data
            orgs = {a["accountName"] for a in values if isinstance(a, dict) and a.get("accountName")}
            log.info("Azure Cloud: fetched %d orgs via global vssps", len(orgs))
            return orgs
    except Exception:
        pass

    log.warning("Azure Cloud: global accounts API failed. Use a global PAT to auto-discover orgs.")
    return set()


def ado_fetch_projects_onprem(host_url: str, collection: str, token: str) -> set[str]:
    base = host_url.rstrip("/")
    headers = {"Authorization": f"Basic {_ado_basic_auth(token)}", "Accept": "application/json"}
    top, skip = 100, 0
    projects: set[str] = set()

    while True:
        resp = requests.get(f"{base}/{collection}/_apis/projects?$top={top}&$skip={skip}&api-version=6.0",
                            headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        values = resp.json().get("value", [])
        for proj in values:
            if name := proj.get("name"):
                projects.add(name)
        if len(values) < top:
            break
        skip += top

    log.info("Azure OnPrem: fetched %d projects from %s/%s", len(projects), host_url, collection)
    return projects


# ── ArmorCode Client ─────────────────────────────────────────────────────────

class ArmorcodeClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._s = requests.Session()
        self._s.verify = _SSL_VERIFY
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def fetch_installations(self, repo_type: str, hosting: str = "cloud") -> set[str]:
        resp = _request_with_retry("get", f"{self._base}/user/tools/git/gitInstallation/repoType/{repo_type}",
                                   session=self._s)
        resp.raise_for_status()
        results = set()
        for e in resp.json():
            if repo_type == "GITHUB":
                val = e.get("account")
            elif repo_type == "AZURE_REPOS" and hosting == "onprem":
                val = e.get("name")  # ADO OnPrem: one installation per project, name=project
            else:
                val = e.get("workspace")
            if not val:
                continue
            if "/" in val and repo_type != "GITHUB":
                results.add(val.rstrip("/").rsplit("/", 1)[-1])
            else:
                results.add(val)
        return results

    def create_github_bulk(self, missing: list[str], all_orgs: list[str],
                           scm_cfg: dict, token: str, install_cfg: dict) -> list:
        payload = [
            {
                "hostUrl": scm_cfg["host_url"],
                "token": token,
                "organisation": all_orgs,
                "repoType": "GITHUB",
                "account": org,
            }
            for org in missing
        ]
        resp = _request_with_retry("post", f"{self._base}/user/tools/git/installation/bulk",
                                   session=self._s, json=payload)
        resp.raise_for_status()
        r = resp.json()
        return r if isinstance(r, list) else [r]

    def create_single(self, payload: dict, install_cfg: dict) -> dict:
        """Creates installation, then PUTs config."""
        resp = _request_with_retry("post", f"{self._base}/user/tools/git/gitInstallation",
                                   session=self._s, json=payload)
        resp.raise_for_status()
        created = resp.json()
        if iid := created.get("id"):
            try:
                cfg_resp = _request_with_retry("put",
                                               f"{self._base}/user/tools/git/installation/{iid}/config",
                                               session=self._s, json=install_cfg)
                cfg_resp.raise_for_status()
                created["_config"] = cfg_resp.json()
            except Exception as e:
                log.warning("Could not set config for installation %s: %s", iid, e)
        return created


# ── Reconciler ───────────────────────────────────────────────────────────────

def _resolve_install_config(scm_type: str, scm_entry: dict, cfg_defaults: dict | None) -> dict:
    result = {**_INSTALL_CONFIG_DEFAULTS.get(scm_type, {})}
    if cfg_defaults and scm_type in cfg_defaults:
        result.update(cfg_defaults[scm_type])
    if scm_entry.get("install_config"):
        result.update(scm_entry["install_config"])
    return result


def _fetch_scm_orgs(entry: dict, scm_type: str, hosting: str) -> set[str]:
    if scm_type == "GITHUB":
        return github_fetch_orgs(entry["pat"], entry["host_url"])
    if scm_type == "BITBUCKET":
        if hosting == "cloud":
            return bb_fetch_workspaces_cloud(entry["username"], entry["password"])
        return bb_fetch_projects_onprem(entry["host_url"], token=entry.get("token"),
                                        username=entry.get("username"), password=entry.get("password"))
    if scm_type == "AZURE_REPOS":
        if hosting == "cloud":
            return ado_fetch_orgs_cloud(entry["token"])
        return ado_fetch_projects_onprem(entry["host_url"], entry["collection"], entry["token"])
    raise ValueError(f"Unsupported SCM type: {scm_type}")


def _build_payload(org: str, entry: dict, scm_type: str, hosting: str, install_cfg: dict) -> dict:
    ht = "Cloud" if hosting == "cloud" else "OnPrem"

    if scm_type == "AZURE_REPOS":
        payload = {"name": org, "hostingType": ht, "secretKey": entry["token"],
                   "workspace": entry.get("collection", org), "repoType": "AZURE_REPOS"}
        if ht == "OnPrem":
            payload["hostUrl"] = entry["host_url"]
        return payload

    if hosting == "cloud":
        tok = _bb_basic_token(entry["username"], entry["password"])
        return {"name": org, "hostingType": "Cloud", "username": entry["username"],
                "password": entry["password"],
                "workspace": f"https://api.bitbucket.org/2.0/workspaces/{org}",
                "repoType": "BITBUCKET", "token": tok}

    # BITBUCKET OnPrem
    host = entry["host_url"].rstrip("/")
    p = {"name": org, "hostingType": "OnPrem", "hostUrl": host,
         "workspace": f"{host}/projects/{org}", "repoType": "BITBUCKET"}
    if entry.get("token"):
        p["onPremToken"] = entry["token"]
        p["token"] = entry["token"]
    else:
        p["username"] = entry["username"]
        p["password"] = entry["password"]
        p["token"] = _bb_basic_token(entry["username"], entry["password"])
    return p


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2))


def reconcile(entry: dict, ac: ArmorcodeClient, data_dir: Path,
              install_cfg_defaults: dict | None, auto_create: bool = False) -> dict:
    scm_type = entry["type"].upper()
    hosting = entry["hosting_type"].lower()
    scm_key = f"{scm_type}_{hosting.upper()}"
    d = data_dir / scm_key
    d.mkdir(parents=True, exist_ok=True)

    summary = {"scm_key": scm_key, "missing_count": 0, "created_count": 0, "ghost_count": 0, "errors": []}

    try:
        ac_orgs = ac.fetch_installations(scm_type, hosting)
    except Exception as e:
        log.error("Failed to fetch AC installations for %s: %s", scm_type, e)
        summary["errors"].append(str(e))
        return summary
    _write_json(d / "ac_orgs.json", sorted(ac_orgs))

    try:
        scm_orgs = _fetch_scm_orgs(entry, scm_type, hosting)
    except Exception as e:
        log.error("Failed to fetch SCM orgs for %s: %s", scm_key, e)
        summary["errors"].append(str(e))
        return summary
    _write_json(d / "scm_orgs.json", sorted(scm_orgs))

    missing = scm_orgs - ac_orgs
    ghosts = ac_orgs - scm_orgs
    _write_json(d / "missing_in_ac.json", sorted(missing))
    _write_json(d / "present_in_ac_missing_in_scm.json", sorted(ghosts))
    summary["missing_count"] = len(missing)
    summary["ghost_count"] = len(ghosts)

    if ghosts:
        log.warning("%s: %d orgs in ArmorCode but absent from SCM: %s", scm_key, len(ghosts), sorted(ghosts))

    install_cfg = _resolve_install_config(scm_type, entry, install_cfg_defaults)
    created = []

    if not auto_create:
        if missing:
            log.info("%s: [DRY-RUN] %d installations would be created: %s", scm_key, len(missing), sorted(missing))
        _write_json(d / "would_create.json", sorted(missing))
        _write_json(d / "created.json", [])
        return summary

    for org in sorted(missing):
        try:
            if scm_type == "GITHUB":
                result = ac.create_github_bulk([org], sorted(scm_orgs), entry, entry["pat"], install_cfg)
            else:
                result = ac.create_single(_build_payload(org, entry, scm_type, hosting, install_cfg), install_cfg)
            created.append({"org": org, "result": result})
            summary["created_count"] += 1
            log.info("%s: created installation for '%s'", scm_key, org)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                log.info("%s: '%s' already exists (409), skipping", scm_key, org)
                summary["created_count"] += 1
            else:
                log.error("Failed to create '%s': %s", org, e)
                summary["errors"].append(f"{org}: {e}")
        except Exception as e:
            log.error("Failed to create '%s': %s", org, e)
            summary["errors"].append(f"{org}: {e}")

    _write_json(d / "created.json", created)
    return summary


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ArmorCode SCM Workspace Reconciler")
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help="Path to config.json")
    parser.add_argument("--armorcode-api-token", required=False, help="ArmorCode API Bearer token (or set ARMORCODE_API_TOKEN env var)")
    parser.add_argument("--auto-create", action="store_true", default=False, help="Actually create missing installations. Default: dry-run (report only)")
    args = parser.parse_args()

    api_token = args.armorcode_api_token or os.environ.get("ARMORCODE_API_TOKEN")
    if not api_token:
        sys.exit("[ERROR] --armorcode-api-token argument or ARMORCODE_API_TOKEN env var is required")

    cfg = load_config(args.config)
    base = init_storage()

    # Setup logging → stdout + tempdir()/armorcode/log/run-YYYY-MM-DD.log
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"run-{_today()}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG)
    log.addHandler(logging.StreamHandler(sys.stdout))
    log.addHandler(logging.FileHandler(log_file))
    for h in log.handlers:
        h.setFormatter(fmt)

    log.info("Starting reconciliation. Config: %s | Log: %s | Mode: %s",
             args.config, log_file, "AUTO-CREATE" if args.auto_create else "DRY-RUN")

    ac = ArmorcodeClient(cfg["armorcode"]["base_url"], api_token)
    install_cfg_defaults = cfg.get("install_config_defaults")
    run_ts = datetime.now().strftime("%H-%M-%S")

    summaries = []
    for entry in cfg["scm"]:
        scm_key = f"{entry['type'].upper()}_{entry['hosting_type'].upper()}"
        data = scm_data_dir(base, scm_key, run_ts)
        log.info("Reconciling %s ...", scm_key)
        summaries.append(reconcile(entry, ac, data.parent, install_cfg_defaults, args.auto_create))

    print(f"\n{'=' * 60}")
    print(f"{'SCM':<30} {'Missing':>8} {'Created':>8} {'Ghosts':>8} {'Errors':>8}")
    print(f"{'-' * 60}")
    for s in summaries:
        print(f"{s['scm_key']:<30} {s['missing_count']:>8} {s['created_count']:>8}"
              f" {s['ghost_count']:>8} {len(s['errors']):>8}")
    print(f"{'=' * 60}")

    log.info("Done. Results in: %s", base / "data")


if __name__ == "__main__":
    main()
