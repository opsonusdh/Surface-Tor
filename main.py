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
from urllib.parse import urljoin, urlsplit, urlunsplit

import colorama
import requests

script_dir = Path(__file__).resolve().parent
replica_dir = script_dir / "Replica"
GIST_FILENAME = "surface_tor.json"
LOCAL_STATE_PATH = script_dir / GIST_FILENAME

if os.name == 'nt':
    system = "windows"
elif os.name == 'posix':
    system = "linux"
else:
    system = "unknown"

if not (replica_dir.exists() and replica_dir.is_dir()):
    raise FileNotFoundError(
        f"Replica directory not found at {replica_dir}. Please ensure the 'Replica' folder exists in the same directory as this script. Run {'setup.sh' if system == 'linux' else 'setup.ps1'}"
    )


def resolve_probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "::0", "") else host


def build_proxy_environment(target: str) -> dict[str, str]:
    env = os.environ.copy()
    env["TARGET_ORIGIN"] = target
    env["PYTHONUNBUFFERED"] = "1"
    return env


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


def resolve_proot_path() -> str | None:
    """Find the proot binary.

    Needed on systems where /etc/resolv.conf is unavailable (e.g., Android/Termux)
    so we can provide a writable rootfs with DNS config for Go binaries like cloudflared.
    """
    found = shutil.which("proot")
    if found:
        return found
    candidate = Path("/data/data/com.termux/files/usr/bin/proot")
    if candidate.exists():
        return str(candidate)
    return None


def _dns_resolution_broken() -> bool:
    """Check whether Go-style DNS resolution is likely to fail.

    Go binaries read /etc/resolv.conf. On Android/Termux the /etc partition is
    a read-only symlink to /system/etc and typically lacks resolv.conf, so Go's
    resolver falls back to [::1]:53 where no DNS server listens.  Python's
    getaddrinfo works because the NDK resolver reads Termux's own resolv.conf.
    """
    return not Path("/etc/resolv.conf").exists()


# Module-level proot rootfs path (created once, reused for all tunnels)
_proot_rootfs: Path | None = None
_tunnel_stderr_logs: dict[int, Path] = {}


def prepare_proot_rootfs() -> Path | None:
    """Create a proot rootfs with working DNS config and CA certificates.

    Returns the rootfs path if proot is needed and available, None otherwise.
    """
    global _proot_rootfs
    if _proot_rootfs is not None:
        return _proot_rootfs

    if not _dns_resolution_broken():
        return None

    proot_bin = resolve_proot_path()
    if not proot_bin:
        return None

    rootfs = script_dir / "workspace" / ".proot_rootfs"
    etc_dir = rootfs / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)

    # Write resolv.conf with working DNS servers
    resolv_conf = etc_dir / "resolv.conf"
    if resolv_conf.exists():
        return rootfs  # Already created
    termux_resolv = Path("/data/data/com.termux/files/usr/etc/resolv.conf")
    if termux_resolv.exists():
        shutil.copy2(termux_resolv, resolv_conf)
    else:
        resolv_conf.write_text(
            "nameserver 8.8.8.8\nnameserver 8.8.4.4\nnameserver 1.1.1.1\n"
        )

    # Copy CA certificates so cloudflared can verify HTTPS
    ssl_certs_dir = etc_dir / "ssl" / "certs"
    ssl_certs_dir.mkdir(parents=True, exist_ok=True)
    cert_sources = [
        Path("/data/data/com.termux/files/usr/etc/tls/cert.pem"),
        Path("/etc/ssl/certs/ca-certificates.crt"),
    ]
    for src in cert_sources:
        if src.exists():
            shutil.copy2(src, ssl_certs_dir / "ca-certificates.crt")
            break

    _proot_rootfs = rootfs
    return rootfs


def _build_proot_prefix(proot_bin: str, rootfs: Path, cloudflared_bin: str) -> list[str]:
    """Build the proot command prefix that wraps the cloudflared invocation."""
    cloudflared_dir = str(Path(cloudflared_bin).resolve().parent)
    script_dir_resolved = str(script_dir.resolve())
    binds = [proot_bin, "-r", str(rootfs), "-b", "/dev", "-b", "/proc", "-b", str(script_dir)]
    if cloudflared_dir != script_dir_resolved:
        binds += ["-b", cloudflared_dir]
    return binds


def cleanup_proot_rootfs() -> None:
    """Remove the proot rootfs directory."""
    global _proot_rootfs
    if _proot_rootfs is not None and _proot_rootfs.exists():
        shutil.rmtree(_proot_rootfs, ignore_errors=True)
        _proot_rootfs = None


def save_json(path: str | Path, data: dict, indent: int = 2) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    return target


def make_seeded_key(seed: int, index: int) -> str:
    return hashlib.sha256(f"{seed}:{index}".encode("utf-8")).hexdigest()


def canonicalize_site_target(target: str) -> str:
    value = str(target).strip()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target must be an HTTP or HTTPS URL with a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
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

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta" and attributes.get("name", "").lower() == "description":
            self.description = attributes.get("content", "").strip()

    def handle_endtag(self, tag):
        if tag.lower() == "title":
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


def fetch_site_metadata(target: str) -> tuple[str, str, str, str]:
    """Fetch the page title and meta description for *target*.

    Returns a 4-tuple ``(name, description, name_source, desc_source)`` where
    the source fields indicate provenance so the caller can report it:

    * ``name_source`` is ``"metadata"`` when the value came from the page
      ``<title>`` tag, ``"domain"`` when no title was found and the domain
      name was used as a fallback.
    * ``desc_source`` is ``"metadata"`` when the value came from a
      ``<meta name="description">`` tag, ``"unavailable"`` when no usable
      description was found.
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
                headers={"User-Agent": "SurfaceTor/1.0"},
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
            encoding = response.encoding or "utf-8"
            response.close()
            parser = _SiteMetadataParser()
            parser.feed(bytes(content).decode(encoding, errors="replace"))
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


def serialize_tunnel_state(records: list[dict], name_override: str | None = None, desc_override: str | None = None) -> tuple[dict, dict]:
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
    name, description, name_source, desc_source = fetch_site_metadata(origin_target)
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


def persist_tunnel_state(records: list[dict], local_path: str | Path | None = None, gist_token: str | None = None, gist_id: str | None = None, name_override: str | None = None, desc_override: str | None = None) -> tuple[str | None, dict]:
    local_state = Path(local_path) if local_path else LOCAL_STATE_PATH
    current_state = load_local_state(local_state)
    existing_id = gist_id or current_state.get("gist_id")
    gist_data: dict = {}
    payload = dict(current_state)
    if gist_token and existing_id:
        gist_data = get_gist(gist_token, existing_id)
        gist_state = parse_gist_state(gist_data)
        payload = {**current_state, **gist_state}
        payload["records"] = {**current_state.get("records", {}), **gist_state.get("records", {})}
        payload["target_index"] = {**current_state.get("target_index", {}), **gist_state.get("target_index", {})}

    cleaned_records = finalize_tunnel_records(records)
    serialized, source_info = serialize_tunnel_state(cleaned_records, name_override=name_override, desc_override=desc_override)
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
        target_index[canonical_target] = new_key
        stored_records[new_key] = new_record
        payload["records"] = stored_records
        payload["target_index"] = target_index

    payload["gist_id"] = existing_id
    if gist_token:
        try:
            if existing_id:
                # If we have an existing gist, update it (if public) or recreate it
                if gist_data.get("public", False):
                    response = edit_gist(gist_token, existing_id, description="surface_tor", files={GIST_FILENAME: {"content": json.dumps(payload, indent=2, ensure_ascii=False)}})
                    existing_id = response.get("id", existing_id)
                else:
                    response = create_gist(gist_token, "surface_tor", GIST_FILENAME, json.dumps(payload, indent=2, ensure_ascii=False), public=True)
                    existing_id = response.get("id")
            else:
                response = create_gist(gist_token, "surface_tor", GIST_FILENAME, json.dumps(payload, indent=2, ensure_ascii=False), public=True)
                existing_id = response.get("id")
            payload["gist_id"] = existing_id
        except Exception as exc:
            print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Gist sync failed: {exc}. Local data saved anyway.")
            # Reset gist_id to None so we retry creating a new gist next time
            payload["gist_id"] = None
            existing_id = None

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
    process group, so :func:`os.killpg` reaches both the proot wrapper
    **and** its child processes (e.g. the real ``cloudflared`` binary).
    Without this, killing only the proot parent leaves ``cloudflared``
    orphaned and holding onto the metrics port.

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


def start_reverse_proxy(host: str, port: int, target: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", "Replica.replica.main:app", "--host", host, "--port", str(port)]
    env = build_proxy_environment(target)
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


_CLOUDFLARED_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')


def extract_tunnel_url_from_stderr(stderr_log: Path) -> str | None:
    """Fallback: parse the captured cloudflared stderr log for the quick-tunnel URL.

    The metrics API (``/quicktunnel``) is the primary way to discover the tunnel
    endpoint, but it can be unreliable — especially when cloudflared runs under
    proot or when the metrics server lags behind the tunnel creation.  Cloudflared
    always prints the hostname to stderr when a quick tunnel is created, so we
    can recover it with a simple regex over the captured log.
    """
    try:
        content = stderr_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CLOUDFLARED_URL_RE.search(content)
    if match:
        return match.group(0)
    return None


def start_tunnel_for_port(host: str, port: int, metrics_port: int, cloudflared_bin: str, proot_rootfs: Path | None = None, protocol: str | None = None) -> tuple[subprocess.Popen, str | None]:
    probe_host = resolve_probe_host(host)

    # When proot is needed (DNS broken), prefer the bundled cloudflared binary
    # which is statically linked and works inside the proot rootfs. The system
    # cloudflared is dynamically linked and its shared libraries won't be
    # found inside proot.
    actual_bin = cloudflared_bin
    if proot_rootfs is not None:
        bundled = script_dir / "cloudflared"
        if bundled.exists():
            actual_bin = str(bundled)

    cmd_tunnel = [
        actual_bin,
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        f"127.0.0.1:{metrics_port}",
    ]
    if protocol:
        cmd_tunnel += ["--protocol", protocol]
    cmd_tunnel += ["--url", f"http://{probe_host}:{port}"]

    # On systems where /etc/resolv.conf is missing (e.g., Android/Termux),
    # wrap cloudflared in proot so it can resolve DNS via a writable rootfs.
    if proot_rootfs is not None:
        proot_bin = resolve_proot_path()
        if proot_bin:
            cmd_tunnel = _build_proot_prefix(proot_bin, proot_rootfs, actual_bin) + cmd_tunnel

    # Capture cloudflared stderr to a log file for diagnostics when the tunnel fails
    stderr_log = script_dir / "workspace" / f"cloudflared_{port}.stderr"
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_fd = open(stderr_log, "w")
    try:
        proc = subprocess.Popen(cmd_tunnel, stdout=subprocess.DEVNULL, stderr=stderr_fd, start_new_session=True)
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

    Recovery (tunnel restart) is triggered only when:
    - the local proxy port is **not listening** (proxy process died), or
    - the cloudflared tunnel process has **exited** (poll() returns a code), or
    - there is no public URL at all (tunnel never came up).
    """
    local_port = record.get("local_port")
    public_url = record.get("public_url")
    proxy_proc = record.get("proxy_proc")
    tunnel_proc = record.get("tunnel_proc")

    # --- Proxy process check ------------------------------------------------
    # The reverse proxy must still be running and listening on its port.
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
    record["proxy_proc"] = start_reverse_proxy(host, record["local_port"], target)
    record["target"] = target
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def restart_tunnel_process(record: dict, host: str, cloudflared_bin: str, proot_rootfs: Path | None = None, protocol: str | None = None) -> None:
    stop_process(record.get("tunnel_proc"))
    metrics_port = record.get("metrics_port", record["local_port"] + 10000)
    tunnel_proc, public_url = start_tunnel_for_port(host, record["local_port"], metrics_port, cloudflared_bin, proot_rootfs, protocol=protocol)
    if not public_url:
        stop_process(tunnel_proc)
        raise RuntimeError(f"Tunnel did not report a URL for port {record['local_port']}")
    record["tunnel_proc"] = tunnel_proc
    record["public_url"] = public_url
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def restart_tunnel_record(record: dict, host: str, cloudflared_bin: str, proot_rootfs: Path | None = None, protocol: str | None = None) -> None:
    stop_process(record.get("tunnel_proc"))
    stop_process(record.get("proxy_proc"))
    record["proxy_proc"] = start_reverse_proxy(host, record["local_port"], record["target"])
    record["tunnel_proc"] = None
    restart_tunnel_process(record, host, cloudflared_bin, proot_rootfs, protocol=protocol)
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def recover_tunnel_record(index: int, records: list[dict], host: str, cloudflared_bin: str, proot_rootfs: Path | None = None, protocol: str | None = None) -> None:
    record = records[index]
    if index == len(records) - 1:
        restart_tunnel_record(record, host, cloudflared_bin, proot_rootfs, protocol=protocol)
        return

    restart_tunnel_process(record, host, cloudflared_bin, proot_rootfs, protocol=protocol)
    downstream_record = records[index + 1]
    restart_proxy_record(downstream_record, host, record["public_url"])


def format_routing_table(routing_table: dict) -> str:
    records = sorted(routing_table.items(), key=lambda item: item[1].get("local_port", 0))
    if not records:
        return "Routing chain is empty."

    lines = [f"{colorama.Fore.CYAN}Routing chain (origin -> final tunnel):{colorama.Style.RESET_ALL}"]
    for index, (node, record) in enumerate(records):
        source_url = record.get("origin_target") if index == 0 else records[index - 1][1].get("public_url")
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


def cleanup_stale_cloudflared_processes() -> int:
    """Kill orphaned cloudflared tunnel processes from previous Surface-Tor runs.

    When a proot-wrapped cloudflared process is killed, the child cloudflared
    can survive as an orphan (reparented to init).  These orphans hold onto
    metrics ports, causing 'address already in use' errors in subsequent runs,
    which cascades into tunnel failures and prevents state/gist persistence.

    Returns the number of stale processes killed.
    """
    killed = 0
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=5,
        )
        my_pid = os.getpid()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid = int(parts[0])
            args = parts[1]
            if pid == my_pid:
                continue
            # Match cloudflared tunnel processes started by Surface-Tor.
            # We match on the distinctive flags our code always passes.
            if (
                "cloudflared" in args
                and "tunnel" in args
                and "no-autoupdate" in args
                and "--metrics" in args
            ):
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                    print(f"{colorama.Fore.YELLOW}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} "
                          f"Killed stale cloudflared process (PID {pid}) from a previous run.")
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass
    return killed


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
    parser = argparse.ArgumentParser(description="Run the Replica script.")
    parser.add_argument('-t', '--target', type=str, default="https://example.com", help='Specify the target URL.')
    parser.add_argument('-n', '--number', type=int, default=3, help='The number of reverse proxies. more proxy = more anonymity but slower speed.')
    parser.add_argument('-g', '--github-token', type=str, required=False, help='Specify the GitHub token.')
    parser.add_argument('-N', '--name', type=str, default=None, help='Override the display name for the tunnel site. If not given, the name is fetched from the target URL (or the domain is used as fallback).')
    parser.add_argument('-d', '--desc', type=str, default=None, help='Override the description for the tunnel site. If not given, the description is fetched from the target URL (or empty if unavailable).')
    parser.add_argument('--host', '-H', type=str, default="0.0.0.0", help='Specify the host to bind the server.')
    parser.add_argument('--port', '-p', type=int, default=8000, help='Specify the starting port to bind the server. it will use the specified port to specified port + number of reverse proxies. make sure that they are available.')
    parser.add_argument('--metrics', '-m', type=int, default=10000, help='Specify the starting port for metrics. it will use the specified port to specified port + number of reverse proxies. make sure that they are available.')
    parser.add_argument('--protocol', type=str, default=None, choices=['quic', 'http2', 'h2mux', ''], help='Cloudflare Tunnel protocol to use. Default: cloudflared auto-selects (quic). On Android/Termux, --protocol h2mux forces TCP-based connections if QUIC/UDP is unreliable.')
    args = parser.parse_args()

    if args.github_token:
        os.environ["GITHUB_TOKEN"] = args.github_token

    for i in range(args.number):
        port_to_check = args.port + i
        probe_host = resolve_probe_host(args.host)
        try:
            response = requests.get(f"http://{probe_host}:{port_to_check}", timeout=2)
            if response.status_code < 400:
                raise ValueError(
                    f"Port {port_to_check} is already in use. Please specify a different starting port using the --port argument."
                )
        except requests.exceptions.RequestException:
            continue

    # Kill any orphaned cloudflared processes from previous runs.
    # Without this, stale processes hold metrics ports, causing "address
    # already in use" errors and tunnel failures that prevent state persistence.
    killed = cleanup_stale_cloudflared_processes()
    if killed:
        print(f"{colorama.Fore.YELLOW}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Cleaned up {killed} stale cloudflared process(es) from previous runs.")
        time.sleep(1)  # give the kernel a moment to release ports

    routing_table = {}
    target = args.target
    gist_token = args.github_token or os.getenv("GITHUB_TOKEN")

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
    gist_state_id = local_state.get("gist_id") if isinstance(local_state, dict) else None

    # Prepare proot rootfs if DNS resolution is broken (e.g., Android/Termux)
    proot_rootfs = prepare_proot_rootfs()
    if _dns_resolution_broken():
        if proot_rootfs is not None:
            print(f"{colorama.Fore.GREEN}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} DNS workaround active: cloudflared will run via proot with a writable rootfs.")
        else:
            proot_bin = resolve_proot_path()
            if proot_bin is None:
                print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} /etc/resolv.conf is missing and proot is not installed. cloudflared may fail to resolve DNS.")
            else:
                print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Could not prepare proot rootfs; cloudflared may fail to resolve DNS.")

    try:
        for i in range(args.number):
            port = args.port + i
            proxy_proc = start_reverse_proxy(args.host, port, target)
            print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Started reverse proxy on port {port}")

            metrics_port = port + args.metrics
            cloudflared_bin = resolve_cloudflared_path()
            if cloudflared_bin is None:
                print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} cloudflared is not installed or not in PATH.")
                print(f"{colorama.Fore.YELLOW}[INFO] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Install cloudflared or download it next to this script, then restart.")
                continue

            tunnel_proc, tunnel_url = start_tunnel_for_port(args.host, port, metrics_port, cloudflared_bin, proot_rootfs, protocol=args.protocol)

            if tunnel_url:
                print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel established on port {port} -> {tunnel_url}")
                routing_table[f"node_{port}"] = {
                    "local_port": port,
                    "metrics_port": metrics_port,
                    "public_url": tunnel_url,
                    "target": target,
                    "origin_target": args.target,
                    "host": args.host,
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
                raise RuntimeError(error_msg)

        if len(routing_table) == args.number and routing_table:
            prev_gist_id = gist_state_id
            gist_state_id, source_info = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id, name_override=args.name, desc_override=args.desc)
            _print_persist_summary(source_info, gist_state_id, prev_gist_id, gist_token)

        if routing_table:
            print(format_routing_table(routing_table))

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
                        recover_tunnel_record(index, records, args.host, cloudflared_bin, proot_rootfs, protocol=args.protocol)
                        if len(routing_table) == args.number:
                            gist_state_id, _ = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id, name_override=args.name, desc_override=args.desc)
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
        cleanup_proot_rootfs()
        cleanup_stderr_logs()


if __name__ == "__main__":
    main()
