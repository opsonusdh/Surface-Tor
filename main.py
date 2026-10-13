import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import colorama
import requests

script_dir = Path(__file__).resolve().parent
replica_dir = script_dir / "Replica"
GIST_FILENAME = "surface_tor.json"
LOCAL_STATE_PATH = script_dir / GIST_FILENAME

# Environment variables that must never be forwarded to spawned child
# processes (Replica/uvicorn, cloudflared). A token set via --github-token
# or GITHUB_TOKEN is only needed by this launcher process to talk to the
# GitHub API directly; neither child needs it, and proxied origins or
# tunnel diagnostics should never have a path to it.
_SENSITIVE_ENV_KEYS = ("GITHUB_TOKEN",)

if os.name == 'nt':
    system = "windows"
elif os.name == 'posix':
    system = "linux"
else:
    system = "unknown"


def require_replica_dir() -> None:
    """Exit with a clear message if the Replica checkout is missing.

    Checked when the launcher actually runs rather than on import, so the
    module (and its tests) can be imported before setup has been run.
    """
    if not (replica_dir.exists() and replica_dir.is_dir()):
        print(f"{colorama.Fore.RED}[ERROR]{colorama.Style.RESET_ALL} Replica directory not found at {replica_dir}. "
              f"Run {'setup.sh' if system == 'linux' else 'setup.ps1'} first; it clones and patches Replica.")
        sys.exit(1)


def resolve_probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "::0", "") else host


def _sanitized_child_env() -> dict[str, str]:
    """A copy of this process's environment with sensitive keys stripped.

    Used as the base environment for every child process we spawn (Replica
    and cloudflared), so secrets this launcher holds (e.g. a GitHub token)
    are never visible to a proxied origin or leaked into tunnel diagnostics.
    """
    env = os.environ.copy()
    for key in _SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    return env


def build_proxy_environment(target: str, user_agent: str | None = None, impersonate: str | None = None) -> dict[str, str]:
    env = _sanitized_child_env()
    env["TARGET_ORIGIN"] = target
    env["PYTHONUNBUFFERED"] = "1"
    # The two variables below are read by the small patch that
    # scripts/patch_replica.py applies to Replica. Clear any stale value
    # exported in the parent shell first, so the CLI flags are the only
    # thing that can turn the override on.
    env.pop("UPSTREAM_USER_AGENT", None)
    env.pop("IMPERSONATE_PROFILE", None)
    if user_agent:
        # Every request this node makes upstream carries exactly this
        # User-Agent header, replacing whatever the visitor's browser sent.
        env["UPSTREAM_USER_AGENT"] = user_agent
    if impersonate:
        # Advanced: force a specific TLS/HTTP2 fingerprint profile instead
        # of deriving chrome/firefox from the User-Agent string.
        env["IMPERSONATE_PROFILE"] = impersonate
    return env


_REPLICA_PATCH_MARKER = "Surface Tor patch v2"


def replica_is_patched() -> bool:
    """True when Replica carries this project's upstream-user-agent patch."""
    try:
        return _REPLICA_PATCH_MARKER in (replica_dir / "replica" / "proxy.py").read_text(encoding="utf-8")
    except OSError:
        return False


def ensure_replica_patched() -> bool:
    """Make sure Replica supports -u/--user-agent, applying the patch if needed.

    Replica is a separate, git-ignored clone, so an existing checkout made
    before this feature existed won't have the patch. Rather than letting
    -u silently do nothing, apply scripts/patch_replica.py on demand and
    report whether Replica ended up patched.
    """
    if replica_is_patched():
        return True
    patch_script = script_dir / "scripts" / "patch_replica.py"
    if patch_script.exists():
        subprocess.run([sys.executable, str(patch_script)], check=False)
    return replica_is_patched()


def resolve_cloudflared_path() -> str | None:
    env_path = os.getenv("CLOUDFLARED_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            return str(candidate)

    for name in ("cloudflared", "cloudflared.exe"):
        found = shutil.which(name)
        if found:
            return found

    for name in ("cloudflared", "cloudflared.exe"):
        candidate = script_dir / name
        if candidate.exists():
            return str(candidate)

    return None


def save_json(path: str | Path, data: dict, indent: int = 2) -> Path:
    """Write *data* as JSON to *path* atomically.

    Writes to a temp file in the same directory, then renames it onto the
    target path. os.replace() is atomic on both POSIX and Windows, so a
    crash, Ctrl+C, or power loss mid-write can never leave a truncated or
    corrupt surface_tor.json behind — the reader either sees the old
    complete file or the new complete file, never a partial one.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def make_seeded_key(seed: int, index: int) -> str:
    return hashlib.sha256(f"{seed}:{index}".encode("utf-8")).hexdigest()


def canonicalize_site_target(target: str) -> str:
    value = str(target).strip()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target must be an HTTP or HTTPS URL with a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    if not hostname or re.search(r"[\s<>\"'`\\^{|}]", hostname):
        raise ValueError("Target hostname contains invalid characters")
    port = parsed.port
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    return f"{parsed.scheme.lower()}://{hostname}"


def make_site_key(target: str, final_url: str) -> str:
    key_data = json.dumps([0, canonicalize_site_target(target), final_url.strip()], separators=(",", ":"))
    return hashlib.sha256(key_data.encode("utf-8")).hexdigest()


class _SiteMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.in_title = False
        self._svg_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "svg":
            self._svg_depth += 1
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "title" and self._svg_depth == 0:
            self.in_title = True
        elif tag == "meta" and attributes.get("name", "").lower() == "description":
            self.description = attributes.get("content", "").strip()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "svg" and self._svg_depth > 0:
            self._svg_depth -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)


def _validate_public_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Metadata URL must be a public HTTP or HTTPS URL")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Metadata URL hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Metadata requests to private or non-public hosts are blocked")


def _decode_html_bytes(raw: bytes, content_type: str = "") -> str:
    """Decode fetched page bytes to text, without trusting requests' default.

    When a server sends ``Content-Type: text/html`` with no charset,
    ``requests`` falls back to ISO-8859-1 (the old HTTP/1.1 default) even
    though nearly every server that omits a charset is really sending UTF-8;
    decoding that as Latin-1 garbles any non-ASCII title or description.

    This works purely from the bytes already read (``response.content`` is
    unavailable once the stream has been consumed). Order of preference:
    an explicit ``charset=`` in the Content-Type header, then strict UTF-8
    (which legacy-encoded pages almost never satisfy by accident), then a
    ``<meta charset>`` declaration near the top of the document, then UTF-8
    with replacement characters as a last resort.
    """
    header_match = re.search(r"charset=([^\s;]+)", content_type or "", re.IGNORECASE)
    if header_match:
        try:
            return raw.decode(header_match.group(1).strip("\"'"), errors="replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    meta_match = re.search(rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9_\-:.]+)", raw[:4096], re.IGNORECASE)
    if meta_match:
        try:
            return raw.decode(meta_match.group(1).decode("ascii"), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return raw.decode("utf-8", errors="replace")


def fetch_site_metadata(target: str, user_agent: str | None = None) -> tuple[str, str, str, str]:
    """Fetch the page title and meta description for *target*.

    Returns a 4-tuple ``(name, description, name_source, desc_source)`` where
    the source fields indicate provenance so the caller can report it:

    * ``name_source`` is ``"metadata"`` when the value came from the page
      ``<title>`` tag, ``"domain"`` when no title was found and the domain
      name was used as a fallback.
    * ``desc_source`` is ``"metadata"`` when the value came from a
      ``<meta name="description">`` tag, ``"unavailable"`` when no usable
      description was found.

    ``user_agent`` is the same string passed to the launcher's ``-u`` flag:
    a site that rejects unfamiliar user agents would otherwise refuse this
    lookup too and silently fall back to showing just the domain name.
    """
    initial_url = str(target).strip()
    if "://" not in initial_url:
        initial_url = f"https://{initial_url}"
    fallback_name = urlsplit(initial_url).hostname or "Website"
    current_url = initial_url

    try:
        for _ in range(6):
            _validate_public_http_url(current_url)
            response = requests.get(
                current_url,
                headers={"User-Agent": user_agent or "SurfaceTor/1.0"},
                timeout=(3, 8),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    break
                current_url = urljoin(current_url, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                response.close()
                return fallback_name, "", "domain", "unavailable"

            content = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                content.extend(chunk)
                if len(content) >= 1024 * 1024:
                    del content[1024 * 1024:]
                    break
            page_content_type = response.headers.get("Content-Type", "")
            response.close()
            parser = _SiteMetadataParser()
            parser.feed(_decode_html_bytes(bytes(content), page_content_type))
            title_value = " ".join(" ".join(parser.title_parts).split())[:300]
            title = title_value or fallback_name
            description = " ".join(html.unescape(parser.description).split())[:1000]
            name_source = "metadata" if title_value else "domain"
            desc_source = "metadata" if description else "unavailable"
            return title, description, name_source, desc_source
        return fallback_name, "", "domain", "unavailable"
    except (requests.RequestException, ValueError, OSError, socket.gaierror):
        return fallback_name, "", "domain", "unavailable"


def finalize_tunnel_records(records: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    seen_urls: set[str] = set()

    for record in records:
        if not isinstance(record, dict):
            continue

        if record.get("healthy") is False:
            continue

        status = str(record.get("status") or "").lower()
        if status in {"stale", "dead", "obsolete", "failed", "unhealthy"}:
            continue

        public_url = str(record.get("public_url") or "").strip()
        if not public_url:
            continue
        if public_url in seen_urls:
            continue
        seen_urls.add(public_url)

        local_port = record.get("local_port")
        target = record.get("target")
        timestamp = record.get("timestamp")

        cleaned.append({
            "local_port": local_port,
            "public_url": public_url,
            "target": target,
            "timestamp": timestamp,
            "name": record.get("name"),
            "desc": record.get("desc"),
            "origin_target": record.get("origin_target"),
        })

    return cleaned


def serialize_tunnel_state(records: list[dict], name_override: str | None = None, desc_override: str | None = None, user_agent: str | None = None) -> tuple[dict, dict]:
    """Build the on-disk record dict for the given routing chain.

    Returns ``(record, source_info)`` where *record* maps the site key to
    ``{"name", "desc", "url"}`` and *source_info* describes where the name
    and description originated — ``"metadata"``, ``"domain"``,
    ``"unavailable"``, or ``"user"`` (when ``--name``/``--desc`` overrides
    were supplied).
    """
    cleaned_records = finalize_tunnel_records(records)
    if not cleaned_records:
        return {}, {}
    origin_target = cleaned_records[0].get("origin_target") or cleaned_records[0].get("target")
    final_url = cleaned_records[-1].get("public_url")
    if not origin_target or not final_url:
        return {}, {}
    key = make_site_key(origin_target, final_url)
    name, description, name_source, desc_source = fetch_site_metadata(origin_target, user_agent=user_agent)
    if name_override is not None:
        name = name_override
        name_source = "user"
    if desc_override is not None:
        description = desc_override
        desc_source = "user"
    record = {key: {"name": name, "desc": description, "url": final_url}}
    source_info = {
        "name": name,
        "desc": description,
        "name_source": name_source,
        "desc_source": desc_source,
    }
    return record, source_info


def _hostname(value: str) -> str | None:
    candidate = str(value).strip()
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    return parsed.hostname.lower().rstrip(".") if parsed.hostname else None


def remove_legacy_site_records(stored_records: dict, origin_target: str) -> None:
    pending_hosts = [_hostname(origin_target)]
    visited_hosts: set[str] = set()
    keys_to_remove: set[str] = set()
    while pending_hosts:
        current_host = pending_hosts.pop()
        if not current_host or current_host in visited_hosts:
            continue
        visited_hosts.add(current_host)
        for key, record in list(stored_records.items()):
            if not isinstance(record, dict):
                continue
            if str(record.get("desc") or "").strip().lower() != "reverse proxy tunnel":
                continue
            if _hostname(record.get("name", "")) != current_host:
                continue
            keys_to_remove.add(key)
            next_host = _hostname(record.get("url", ""))
            if next_host:
                pending_hosts.append(next_host)
    for key in keys_to_remove:
        stored_records.pop(key, None)


def load_local_state(path: str | Path | None = None) -> dict:
    target = Path(path) if path else LOCAL_STATE_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        if "records" in data and isinstance(data["records"], dict):
            return data
        if data and all(isinstance(value, dict) for value in data.values()):
            return {"records": data}
    return {}


def persist_tunnel_state(records: list[dict], local_path: str | Path | None = None, gist_token: str | None = None, gist_id: str | None = None, name_override: str | None = None, desc_override: str | None = None, attached_url: str | None = None, user_agent: str | None = None) -> tuple[str | None, dict]:
    """Merge this chain's record into local state and (optionally) the Gist.

    ``attached_url`` is the public URL of an *existing* chain this device is
    extending (its ``--target`` when ``--origin`` is given). Any published
    record pointing at that URL belonged to the earlier chain's final hop, which
    is now an internal hop, so it is replaced rather than listed alongside the
    extended chain's new final URL.
    """
    local_state = Path(local_path) if local_path else LOCAL_STATE_PATH
    current_state = load_local_state(local_state)
    existing_id = gist_id or current_state.get("gist_id")
    gist_data: dict = {}
    payload = dict(current_state)

    if gist_token and existing_id:
        # A failure to fetch/parse the existing Gist must not abort the whole
        # save — surface_tor.json is always written locally regardless of
        # Gist reachability. Fall back to local state as the merge base so
        # this run's record still lands somewhere even if GitHub is down.
        try:
            gist_data = get_gist(gist_token, existing_id)
            gist_state = parse_gist_state(gist_data)
            payload = {**current_state, **gist_state}
            payload["records"] = {**current_state.get("records", {}), **gist_state.get("records", {})}
            payload["target_index"] = {**current_state.get("target_index", {}), **gist_state.get("target_index", {})}
        except Exception as exc:
            print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Could not read existing Gist: {exc}. Continuing with local state only.")
            payload = dict(current_state)

    cleaned_records = finalize_tunnel_records(records)
    serialized, source_info = serialize_tunnel_state(cleaned_records, name_override=name_override, desc_override=desc_override, user_agent=user_agent)
    if serialized:
        origin_target = cleaned_records[0].get("origin_target") or cleaned_records[0].get("target")
        if not origin_target:
            raise ValueError("A site target is required to persist tunnel state")
        canonical_target = canonicalize_site_target(origin_target)
        new_key, new_record = next(iter(serialized.items()))
        target_index = dict(payload.get("target_index", {}))
        stored_records = dict(payload.get("records", {}))
        remove_legacy_site_records(stored_records, origin_target)
        previous_key = target_index.get(canonical_target)
        if previous_key and previous_key != new_key:
            stored_records.pop(previous_key, None)
        if attached_url:
            attached = str(attached_url).strip().rstrip("/")
            superseded = {
                key for key, rec in stored_records.items()
                if isinstance(rec, dict) and str(rec.get("url", "")).strip().rstrip("/") == attached
            }
            for key in superseded:
                stored_records.pop(key, None)
            for origin_key, rec_key in list(target_index.items()):
                if rec_key in superseded:
                    target_index.pop(origin_key, None)
        target_index[canonical_target] = new_key
        stored_records[new_key] = new_record
        payload["records"] = stored_records
        payload["target_index"] = target_index

    payload["gist_id"] = existing_id
    if gist_token:
        # target_index maps *origin* URLs to record keys. It's purely a
        # local bookkeeping structure (so this launcher can find and replace
        # a site's old record when its tunnel URL changes) and reveals the
        # real origin behind each tunnel — it has no reason to be published
        # to the public Gist the search page reads, so it's stripped from
        # the uploaded content. It stays in full in the local JSON file.
        gist_payload = {k: v for k, v in payload.items() if k != "target_index"}
        gist_content = json.dumps(gist_payload, indent=2, ensure_ascii=False)
        try:
            if existing_id:
                # If we have an existing gist, update it (if public) or recreate it
                if gist_data.get("public", False):
                    response = edit_gist(gist_token, existing_id, description="surface_tor", files={GIST_FILENAME: {"content": gist_content}})
                    existing_id = response.get("id", existing_id)
                else:
                    response = create_gist(gist_token, "surface_tor", GIST_FILENAME, gist_content, public=True)
                    existing_id = response.get("id")
            else:
                response = create_gist(gist_token, "surface_tor", GIST_FILENAME, gist_content, public=True)
                existing_id = response.get("id")
            payload["gist_id"] = existing_id
        except Exception as exc:
            print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Gist sync failed: {exc}. Local data saved anyway.")
            # Keep whatever gist id we already had (None stays None; a real
            # id we already knew about is preserved) so a single transient
            # failure doesn't make the next successful run mint a brand-new
            # Gist and orphan the one the published search page points at.
            payload["gist_id"] = existing_id

    save_json(local_state, payload)
    return existing_id, source_info


def parse_gist_state(gist_data: dict) -> dict:
    file_data = gist_data.get("files", {}).get(GIST_FILENAME)
    if not file_data:
        return {"records": {}, "target_index": {}}
    content = file_data.get("content")
    if file_data.get("truncated") and file_data.get("raw_url"):
        response = requests.get(file_data["raw_url"], timeout=20)
        response.raise_for_status()
        content = response.text
    if not content:
        return {"records": {}, "target_index": {}}
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("Existing Gist state must be a JSON object")
    parsed.setdefault("records", {})
    parsed.setdefault("target_index", {})
    return parsed


def get_gist(token: str, gist_id: str) -> dict:
    response = requests.get(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def create_gist(token: str, description: str, file_name: str, content: str, public: bool = True) -> dict:
    if not token:
        raise ValueError("GitHub token is required to create a gist.")

    payload = {
        "description": description,
        "public": public,
        "files": {
            file_name: {"content": content}
        },
    }
    response = requests.post(
        "https://api.github.com/gists",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def edit_gist(token: str, gist_id: str, description: str | None = None, files: dict | None = None) -> dict:
    if not token:
        raise ValueError("GitHub token is required to edit a gist.")

    payload = {}
    if description is not None:
        payload["description"] = description
    if files is not None:
        payload["files"] = files

    response = requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


_started_processes: list[subprocess.Popen] = []


def register_process(proc: subprocess.Popen) -> None:
    if proc is not None:
        _started_processes.append(proc)


def _kill_process_group(proc: subprocess.Popen, sig: int) -> None:
    """Send a signal to the entire process group of *proc*.

    Processes started with ``start_new_session=True`` run in their own
    process group, so :func:`os.killpg` reaches both *proc* and any
    children it spawned, without having to track those children
    individually.

    If the process happens to share the current process group (e.g. in
    unit tests that start a subprocess without ``start_new_session``),
    we fall back to signalling *just* that process to avoid killing the
    test runner.
    """
    try:
        pgid = os.getpgid(proc.pid)
        if pgid == os.getpgrp():
            # Same group as us — kill only the target process
            proc.send_signal(sig)
        else:
            os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        # Process already gone or not killable — try direct signal
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def cleanup_started_processes() -> None:
    for proc in list(_started_processes):
        if proc.poll() is not None:
            _started_processes.remove(proc)
            continue
        try:
            _kill_process_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            if proc in _started_processes:
                _started_processes.remove(proc)


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            _kill_process_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        if proc in _started_processes:
            _started_processes.remove(proc)


def start_reverse_proxy(host: str, port: int, target: str, user_agent: str | None = None, impersonate: str | None = None) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", "Replica.replica.main:app", "--host", host, "--port", str(port)]
    env = build_proxy_environment(target, user_agent=user_agent, impersonate=impersonate)
    proc = subprocess.Popen(cmd, cwd=str(script_dir), env=env, start_new_session=True)
    register_process(proc)
    return proc


def install_shutdown_handlers() -> None:
    def _handle_signal(signum, frame):
        print("\nStopping spawned reverse proxies...")
        cleanup_started_processes()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


# Cloudflare owns a handful of fixed subdomains under trycloudflare.com for
# its own control-plane endpoints (provisioning API, the --allowed-mail auth
# flow, the update checker). A generated quick-tunnel hostname is never one
# of these, so when scanning cloudflared's stderr for "the" tunnel URL we
# must exclude them — otherwise a *failure* message like
# `dial tcp: lookup api.trycloudflare.com` is misread as a successfully
# established tunnel at https://api.trycloudflare.com.
_CLOUDFLARED_RESERVED_SUBDOMAINS = {"api", "login", "update", "dash", "www"}
_CLOUDFLARED_URL_RE = re.compile(r'https://([a-z0-9-]+)\.trycloudflare\.com')


def extract_tunnel_url_from_stderr(stderr_log: Path) -> str | None:
    """Fallback: parse the captured cloudflared stderr log for the quick-tunnel URL.

    The metrics API (``/quicktunnel``) is the primary way to discover the tunnel
    endpoint, but it can be unreliable — especially when the metrics server lags
    behind the tunnel creation. Cloudflared always prints the hostname to stderr
    when a quick tunnel is created, so we can recover it with a simple regex over
    the captured log. We skip any match against a reserved Cloudflare-owned
    subdomain (see above) since those never denote an actual tunnel.
    """
    try:
        content = stderr_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _CLOUDFLARED_URL_RE.finditer(content):
        if match.group(1) in _CLOUDFLARED_RESERVED_SUBDOMAINS:
            continue
        return match.group(0)
    return None


def start_tunnel_for_port(host: str, port: int, metrics_port: int, cloudflared_bin: str, protocol: str | None = None) -> tuple[subprocess.Popen, str | None]:
    probe_host = resolve_probe_host(host)

    cmd_tunnel = [
        cloudflared_bin,
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        f"127.0.0.1:{metrics_port}",
    ]
    if protocol:
        cmd_tunnel += ["--protocol", protocol]
    cmd_tunnel += ["--url", f"http://{probe_host}:{port}"]

    # Capture cloudflared stderr to a log file for diagnostics when the tunnel fails
    stderr_log = script_dir / "workspace" / f"cloudflared_{port}.stderr"
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_fd = open(stderr_log, "w")
    try:
        proc = subprocess.Popen(cmd_tunnel, stdout=subprocess.DEVNULL, stderr=stderr_fd, env=_sanitized_child_env(), start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        stderr_fd.write(f"Failed to start cloudflared: {exc}\n")
        stderr_fd.close()
        _tunnel_stderr_logs[port] = stderr_log
        return None, None
    stderr_fd.close()
    register_process(proc)

    tunnel_url = None
    retries = 10
    while retries > 0:
        if proc.poll() is not None:
            break  # cloudflared process exited prematurely
        time.sleep(1.5)
        try:
            response = requests.get(f"http://127.0.0.1:{metrics_port}/quicktunnel", timeout=2)
            if response.status_code == 200:
                data = response.json()
                if "hostname" in data:
                    tunnel_url = f"https://{data['hostname']}"
                    break
        except requests.exceptions.RequestException:
            pass
        # Fallback: cloudflared prints the URL to stderr as soon as the tunnel
        # is created, even if the metrics API hasn't served it yet.  Check
        # the captured stderr log so we don't miss an already-established tunnel.
        if tunnel_url is None:
            tunnel_url = extract_tunnel_url_from_stderr(stderr_log)
            if tunnel_url:
                break
        retries -= 1

    if tunnel_url is None:
        # One last attempt: read the stderr log after the loop, in case the
        # metrics API timing was off or cloudflared exited after printing the URL.
        tunnel_url = extract_tunnel_url_from_stderr(stderr_log)

    if tunnel_url is None:
        _tunnel_stderr_logs[port] = stderr_log

    return proc, tunnel_url


_tunnel_stderr_logs: dict[int, Path] = {}


def _is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Check whether a TCP port is accepting connections (no HTTP).

    A successful TCP connect means a process is listening on the port.
    We deliberately discard any HTTP response — we only care whether
    the *process* is up, not whether the upstream target responds 200.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def tunnel_is_healthy(record: dict) -> bool:
    """Check whether the tunnel infrastructure is alive.

    The multi-hop chain consists of two components per layer:

    * a reverse proxy process (Replica/uvicorn) listening on ``local_port``
    * a cloudflared tunnel process exposing a public URL

    A layer is considered healthy when **both** components are structurally
    alive — i.e. the proxy is still listening on its local port and the
    cloudflared process has not exited.

    This deliberately avoids HTTP status codes.  When the upstream target
    server is unreachable or unconfigured, the proxy may return 502, but
    that does **not** mean the tunnel is broken — only the upstream is
    unreachable.  Restarting the tunnel would not fix it, so we must not
    treat a 502 as a health-check failure.
    """
    local_port = record.get("local_port")
    public_url = record.get("public_url")
    proxy_proc = record.get("proxy_proc")
    tunnel_proc = record.get("tunnel_proc")

    # --- Proxy process check ------------------------------------------------
    if proxy_proc is not None:
        if proxy_proc.poll() is not None:
            return False  # proxy process has exited

    if local_port is not None:
        probe_host = resolve_probe_host(record.get("host", "0.0.0.0"))
        if not _is_port_open(probe_host, local_port):
            return False  # nothing listening on the local proxy port

    # --- Tunnel process + public URL check ----------------------------------
    if tunnel_proc is not None:
        if tunnel_proc.poll() is not None:
            return False  # cloudflared tunnel process has exited

    if not public_url:
        return False

    return True


def restart_proxy_record(record: dict, host: str, target: str) -> None:
    stop_process(record.get("proxy_proc"))
    record["proxy_proc"] = start_reverse_proxy(
        host, record["local_port"], target,
        user_agent=record.get("user_agent"), impersonate=record.get("impersonate"),
    )
    record["target"] = target
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def restart_tunnel_process(record: dict, host: str, cloudflared_bin: str, protocol: str | None = None) -> None:
    stop_process(record.get("tunnel_proc"))
    metrics_port = record.get("metrics_port", record["local_port"] + 10000)
    tunnel_proc, public_url = start_tunnel_for_port(host, record["local_port"], metrics_port, cloudflared_bin, protocol=protocol)
    if not public_url:
        stop_process(tunnel_proc)
        raise RuntimeError(f"Tunnel did not report a URL for port {record['local_port']}")
    record["tunnel_proc"] = tunnel_proc
    record["public_url"] = public_url
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def recover_tunnel_record(index: int, records: list[dict], host: str, cloudflared_bin: str, protocol: str | None = None) -> None:
    """Recover whichever half of this layer actually failed.

    A layer has two independent components (its Replica proxy process and
    its cloudflared tunnel process), and they fail independently:

    * If only the **proxy** died, restarting just the proxy on the same
      local port is enough — the existing tunnel keeps pointing at that
      port and reconnects on its own once something is listening there
      again. No new public URL is needed, so nothing downstream changes.
    * If the **tunnel** died (or never reported a URL), it must be
      restarted, which mints a new public URL. Anything downstream that
      was pointed at the old URL (the next layer's proxy, if this isn't
      the final layer) must be rebound to the new one.

    Checking each independently — rather than assuming "final layer
    restarts everything, every other layer restarts only its tunnel" —
    means a crashed intermediate proxy actually gets restarted instead of
    being silently left dead while its tunnel keeps getting rotated every
    health-check cycle.
    """
    record = records[index]
    probe_host = resolve_probe_host(record.get("host", host))

    proxy_proc = record.get("proxy_proc")
    proxy_dead = proxy_proc is not None and proxy_proc.poll() is not None
    proxy_unreachable = record.get("local_port") is not None and not _is_port_open(probe_host, record["local_port"])

    if proxy_dead or proxy_unreachable:
        restart_proxy_record(record, host, record["target"])

    tunnel_proc = record.get("tunnel_proc")
    tunnel_dead = tunnel_proc is not None and tunnel_proc.poll() is not None
    url_missing = not record.get("public_url")

    if tunnel_dead or url_missing:
        restart_tunnel_process(record, host, cloudflared_bin, protocol=protocol)
        if index + 1 < len(records):
            downstream_record = records[index + 1]
            restart_proxy_record(downstream_record, host, record["public_url"])

    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def format_routing_table(routing_table: dict) -> str:
    records = sorted(routing_table.items(), key=lambda item: item[1].get("local_port", 0))
    if not records:
        return "Routing chain is empty."

    lines = [f"{colorama.Fore.CYAN}Routing chain (origin -> final tunnel):{colorama.Style.RESET_ALL}"]
    for index, (node, record) in enumerate(records):
        source_url = (record.get("target") or record.get("origin_target")) if index == 0 else records[index - 1][1].get("public_url")
        source = _hostname(source_url or "") or str(source_url or "unknown")
        destination_url = record.get("public_url") or "unknown"
        destination = _hostname(destination_url) or destination_url
        lines.append(
            f"  {colorama.Fore.MAGENTA}{node}{colorama.Style.RESET_ALL}  "
            f"{colorama.Fore.CYAN}{source}{colorama.Style.RESET_ALL}  "
            f"{colorama.Fore.YELLOW}->{colorama.Style.RESET_ALL}  "
            f"{colorama.Fore.GREEN}{destination}{colorama.Style.RESET_ALL}"
        )

    final_url = records[-1][1].get("public_url")
    if final_url:
        lines.append(f"  {colorama.Fore.GREEN}Final URL: {final_url}{colorama.Style.RESET_ALL}")
    return "\n".join(lines)


def _print_cloudflared_diagnostic(port: int) -> None:
    """Read and print the captured cloudflared stderr log for diagnostics."""
    stderr_log_path = _tunnel_stderr_logs.get(port)
    if stderr_log_path and stderr_log_path.exists():
        stderr_content = stderr_log_path.read_text(encoding="utf-8", errors="replace").strip()
        if stderr_content:
            print(f"{colorama.Fore.YELLOW}[DETAIL] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} cloudflared output:")
            for line in stderr_content.splitlines():
                print(f"  {line}")


def cleanup_stderr_logs() -> None:
    """Remove cloudflared stderr log files from the workspace."""
    workspace = script_dir / "workspace"
    if not workspace.exists():
        return
    for log_file in workspace.glob("cloudflared_*.stderr"):
        try:
            log_file.unlink()
        except Exception:
            pass
    _tunnel_stderr_logs.clear()


def _print_persist_summary(source_info: dict, gist_id: str | None, prev_gist_id: str | None, gist_token: str | None) -> None:
    """Print a human-readable summary of the site metadata and gist sync status.

    *source_info* comes from :func:`serialize_tunnel_state` and carries the
    resolved name, description, and where each value originated
    (``"metadata"``, ``"domain"``, ``"unavailable"``, or ``"user"``).
    """
    ts = time.strftime('%Y-%m-%d %H:%M:%S')

    source_labels = {
        "metadata": "webpage metadata (title/meta tag)",
        "domain": "domain name fallback",
        "user": "user-specified (-N/--name, -d/--desc)",
        "unavailable": "not found on webpage",
    }

    if source_info:
        name = source_info.get("name", "")
        desc = source_info.get("desc", "")
        name_raw = source_info.get("name_source", "unknown")
        desc_raw = source_info.get("desc_source", "unknown")
        name_label = source_labels.get(name_raw, name_raw)
        desc_label = source_labels.get(desc_raw, desc_raw)

        print(f"{colorama.Fore.CYAN}[INFO] [{ts}]{colorama.Style.RESET_ALL} "
              f"Site name: \"{name}\" (source: {name_label})")
        if desc:
            print(f"{colorama.Fore.CYAN}[INFO] [{ts}]{colorama.Style.RESET_ALL} "
                  f"Site description: \"{desc}\" (source: {desc_label})")
        else:
            print(f"{colorama.Fore.CYAN}[INFO] [{ts}]{colorama.Style.RESET_ALL} "
                  f"Site description: none (source: {desc_label})")

    if gist_token:
        if gist_id:
            if prev_gist_id and prev_gist_id != gist_id:
                gist_action = "Gist recreated (previous was private)"
            elif prev_gist_id:
                gist_action = "Gist updated"
            else:
                gist_action = "Gist created"
            print(f"{colorama.Fore.GREEN}[SUCCESS] [{ts}]{colorama.Style.RESET_ALL} "
                  f"{gist_action} — ID: {gist_id}")
        else:
            print(f"{colorama.Fore.YELLOW}[WARN] [{ts}]{colorama.Style.RESET_ALL} "
                  f"Gist sync failed (see warning above). State saved locally to {GIST_FILENAME}.")
    else:
        print(f"{colorama.Fore.CYAN}[INFO] [{ts}]{colorama.Style.RESET_ALL} "
              f"No GitHub token provided — state saved locally to {GIST_FILENAME} (gist not synced).")


def main():
    colorama.just_fix_windows_console()
    install_shutdown_handlers()
    parser = argparse.ArgumentParser(description="Run a chain of reverse proxies, each exposed through its own Cloudflare Quick Tunnel, and optionally publish the final URL.")
    parser.add_argument('-t', '--target', type=str, default="https://example.com",
                         help='The URL this device proxies to. This can be a real origin site, or '
                              'the public tunnel URL of a chain already running on another device '
                              '(see --origin) to extend that chain further from here.')
    parser.add_argument('-n', '--number', type=int, default=3, help='The number of reverse proxies in the chain on this device. Must be at least 1.')
    parser.add_argument('-g', '--github-token', type=str, required=False, help='Specify the GitHub token.')
    parser.add_argument('--gist-id', type=str, default=None,
                         help='Sync into this existing Gist ID instead of one recorded in a local '
                              'surface_tor.json. Lets a fresh device (no local state yet) attach to a '
                              'Gist another device already created — pass the same --github-token.')
    parser.add_argument('--origin', type=str, default=None,
                         help="The true original site this chain represents, when --target is itself "
                              "another device's public tunnel URL rather than the real origin. When set, "
                              "the site record is keyed and named against --origin instead of --target, "
                              "so this device's published URL replaces the earlier tunnel URL as the "
                              "canonical entry for the real site, instead of creating a separate one.")
    parser.add_argument('--no-publish', action='store_true',
                         help='Relay mode: run this device\'s part of the chain but write nothing to '
                              'surface_tor.json or any Gist. Use it on every device except the last one, '
                              'then give this device\'s final URL to the next device as its --target.')
    parser.add_argument('-N', '--name', type=str, default=None, help='Override the display name for the tunnel site. If not given, the name is fetched from the target URL (or the domain is used as fallback).')
    parser.add_argument('-d', '--desc', type=str, default=None, help='Override the description for the tunnel site. If not given, the description is fetched from the target URL (or empty if unavailable).')
    parser.add_argument('-u', '--user-agent', type=str, default=None,
                         help='Send exactly this User-Agent string to the target on every upstream request, '
                              'instead of forwarding each visitor\'s own. Useful when the origin rejects or '
                              'behaves differently for unusual or bot-like User-Agents. Also used for the launcher\'s own '
                              'title/description lookup of the site. The TLS fingerprint '
                              'is matched to it (Firefox if the string says Firefox, otherwise Chrome) so '
                              'the two stay consistent. Example: -u "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) '
                              'Gecko/20100101 Firefox/128.0"')
    parser.add_argument('--impersonate', type=str, default=None,
                         help='Advanced: force a specific TLS/HTTP2 fingerprint profile (e.g. chrome, firefox, '
                              'safari) instead of deriving it from -u. Must be a profile name your installed '
                              'curl_cffi supports; an unsupported name makes every proxied request fail with 502.')
    parser.add_argument('--host', '-H', type=str, default="127.0.0.1", help='Specify the host to bind the local proxy servers. Loopback is all cloudflared needs; widen only for direct LAN access.')
    parser.add_argument('--port', '-p', type=int, default=8000, help='First local port to bind. Node i uses PORT + i, so PORT .. PORT + NUMBER - 1 must all be free.')
    parser.add_argument('--metrics', '-m', type=int, default=10000, help='Offset added to each node\'s port to get the local port of its cloudflared metrics endpoint (default 10000: the node on 8000 uses 18000). Must keep every result at or below 65535.')
    parser.add_argument('--protocol', type=str, default=None, choices=['quic', 'http2', 'h2mux', ''], help='Cloudflare Tunnel protocol to use. Default lets cloudflared auto-select (usually quic).')
    args = parser.parse_args()

    require_replica_dir()

    if args.number < 1:
        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} --number must be at least 1; a chain with zero nodes has nothing to run.")
        sys.exit(2)

    if args.origin:
        try:
            canonicalize_site_target(args.origin)
        except ValueError as exc:
            print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} --origin: {exc}")
            sys.exit(2)

    if args.gist_id is not None and not re.fullmatch(r"[A-Za-z0-9]{6,64}", args.gist_id.strip()):
        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} --gist-id must be the alphanumeric ID from the Gist's URL, not a full URL.")
        sys.exit(2)
    if args.gist_id is not None:
        args.gist_id = args.gist_id.strip()

    last_node_port = args.port + args.number - 1
    if args.port < 1 or args.metrics < 1 or last_node_port + args.metrics > 65535:
        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Ports out of range: nodes would use {args.port}..{last_node_port} and metrics ports up to {last_node_port + args.metrics}, but the maximum is 65535. Lower --port or --metrics.")
        sys.exit(2)

    if args.user_agent is not None:
        args.user_agent = args.user_agent.strip()
        if not args.user_agent or any(ch in args.user_agent for ch in "\r\n\x00"):
            print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} --user-agent must be a non-empty single-line string.")
            sys.exit(2)

    if (args.user_agent or args.impersonate) and not ensure_replica_patched():
        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} --user-agent/--impersonate need the Replica patch, and it could not be applied automatically. Run `python scripts/patch_replica.py` and check its output.")
        sys.exit(2)

    if args.github_token:
        os.environ["GITHUB_TOKEN"] = args.github_token

    for i in range(args.number):
        port_to_check = args.port + i
        probe_host = resolve_probe_host(args.host)
        if _is_port_open(probe_host, port_to_check, timeout=0.5):
            raise ValueError(
                f"Port {port_to_check} is already in use. Please specify a different starting port using the --port argument."
            )

    routing_table = {}
    target = args.target
    origin_for_records = args.origin or args.target
    attached_url = args.target if args.origin else None
    gist_token = None if args.no_publish else (args.github_token or os.getenv("GITHUB_TOKEN"))

    # Validate the GitHub token early so the user gets clear feedback
    if gist_token:
        try:
            check_resp = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {gist_token}", "Accept": "application/vnd.github+json"},
                timeout=10,
            )
            if check_resp.status_code != 200:
                print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} GITHUB_TOKEN appears invalid (HTTP {check_resp.status_code}). Gists will not be saved to GitHub. Local JSON will still be saved. Get a valid token at https://github.com/settings/tokens (needs 'gist' scope).")
                gist_token = None
        except requests.RequestException:
            print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Could not verify GITHUB_TOKEN (network error). Gists will not be saved to GitHub. Local JSON will still be saved.")
            gist_token = None

    local_state = load_local_state()
    gist_state_id = args.gist_id or (local_state.get("gist_id") if isinstance(local_state, dict) else None)

    try:
        for i in range(args.number):
            port = args.port + i
            proxy_proc = start_reverse_proxy(args.host, port, target, user_agent=args.user_agent, impersonate=args.impersonate)
            print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Started reverse proxy on port {port}")

            metrics_port = port + args.metrics
            cloudflared_bin = resolve_cloudflared_path()
            if cloudflared_bin is None:
                print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} cloudflared is not installed or not in PATH.")
                print(f"{colorama.Fore.YELLOW}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Install cloudflared or download it next to this script, then restart.")
                continue

            tunnel_proc, tunnel_url = start_tunnel_for_port(args.host, port, metrics_port, cloudflared_bin, protocol=args.protocol)

            if tunnel_url:
                print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel established on port {port} -> {tunnel_url}")
                routing_table[f"node_{port}"] = {
                    "local_port": port,
                    "metrics_port": metrics_port,
                    "public_url": tunnel_url,
                    "target": target,
                    "origin_target": origin_for_records,
                    "host": args.host,
                    "user_agent": args.user_agent,
                    "impersonate": args.impersonate,
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "proxy_proc": proxy_proc,
                    "tunnel_proc": tunnel_proc,
                }
                target = tunnel_url
            else:
                stop_process(proxy_proc)
                stop_process(tunnel_proc)
                error_msg = f"Tunnel failed to report an endpoint on port {port}"
                print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} {error_msg}")
                _print_cloudflared_diagnostic(port)
                print(f"{colorama.Fore.YELLOW}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Nothing was published. Check that this device can reach api.trycloudflare.com (network and DNS), then run again.")
                sys.exit(1)

        if len(routing_table) == args.number and routing_table:
            if args.no_publish:
                relay_url = list(routing_table.values())[-1]["public_url"]
                print(f"{colorama.Fore.CYAN}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Relay mode (--no-publish): nothing is written to {GIST_FILENAME} or any Gist. Give this URL to the next device as its --target: {relay_url}")
            else:
                prev_gist_id = gist_state_id
                gist_state_id, source_info = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id, name_override=args.name, desc_override=args.desc, attached_url=attached_url, user_agent=args.user_agent)
                _print_persist_summary(source_info, gist_state_id, prev_gist_id, gist_token)

        if routing_table:
            print(format_routing_table(routing_table))

        last_final_url = list(routing_table.values())[-1]["public_url"] if routing_table else None
        while True:
            time.sleep(10)
            records = list(routing_table.values())
            for index, (key, record) in enumerate(list(routing_table.items())):
                if not tunnel_is_healthy(record):
                    print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel unhealthy for port {record['local_port']}; restarting.")
                    try:
                        cloudflared_bin = resolve_cloudflared_path()
                        if cloudflared_bin is None:
                            raise RuntimeError("cloudflared is not installed or not in PATH")
                        recover_tunnel_record(index, records, args.host, cloudflared_bin, protocol=args.protocol)
                        if len(routing_table) == args.number:
                            if args.no_publish:
                                new_final_url = list(routing_table.values())[-1]["public_url"]
                                if new_final_url != last_final_url:
                                    print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} This chain's final URL changed to {new_final_url}. Any device attached downstream must be restarted with the new --target.")
                                    last_final_url = new_final_url
                            else:
                                gist_state_id, _ = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id, name_override=args.name, desc_override=args.desc, attached_url=attached_url, user_agent=args.user_agent)
                        print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel recovered for port {record['local_port']}: {record['public_url']}")
                    except Exception as exc:
                        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Could not recover tunnel on port {record['local_port']}: {exc}")
                        _print_cloudflared_diagnostic(record['local_port'])
    except KeyboardInterrupt:
        print("\nStopping spawned reverse proxies...")
        cleanup_started_processes()
        raise SystemExit(0)
    finally:
        cleanup_started_processes()
        cleanup_stderr_logs()


if __name__ == "__main__":
    main()
