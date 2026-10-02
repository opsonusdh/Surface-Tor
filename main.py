import argparse
import hashlib
import html
import ipaddress
import json
import os
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
        f"Replica directory not found at {replica_dir}. Please ensure the 'Replica' folder exists in the same directory as this script. Run {'setup.sh' if system == 'linux' else 'setpup.ps1'}"
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


def fetch_site_metadata(target: str) -> tuple[str, str]:
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
                return fallback_name, ""

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
            title = " ".join(" ".join(parser.title_parts).split())[:300] or fallback_name
            description = " ".join(html.unescape(parser.description).split())[:1000]
            return title, description
        return fallback_name, ""
    except (requests.RequestException, ValueError, OSError, socket.gaierror):
        return fallback_name, ""


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


def serialize_tunnel_state(records: list[dict]) -> dict:
    cleaned_records = finalize_tunnel_records(records)
    if not cleaned_records:
        return {}
    origin_target = cleaned_records[0].get("origin_target") or cleaned_records[0].get("target")
    final_url = cleaned_records[-1].get("public_url")
    if not origin_target or not final_url:
        return {}
    key = make_site_key(origin_target, final_url)
    name, description = fetch_site_metadata(origin_target)
    return {key: {"name": name, "desc": description, "url": final_url}}


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


def persist_tunnel_state(records: list[dict], local_path: str | Path | None = None, gist_token: str | None = None, gist_id: str | None = None) -> str | None:
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
    serialized = serialize_tunnel_state(cleaned_records)
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
        if existing_id:
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

    save_json(local_state, payload)
    return existing_id


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


def cleanup_started_processes() -> None:
    for proc in list(_started_processes):
        if proc.poll() is not None:
            _started_processes.remove(proc)
            continue
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
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
            proc.terminate()
            proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
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
    proc = subprocess.Popen(cmd, cwd=str(script_dir), env=env)
    register_process(proc)
    return proc


def install_shutdown_handlers() -> None:
    def _handle_signal(signum, frame):
        print("\nStopping spawned reverse proxies...")
        cleanup_started_processes()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def start_tunnel_for_port(host: str, port: int, metrics_port: int, cloudflared_bin: str) -> tuple[subprocess.Popen, str | None]:
    cmd_tunnel = [
        cloudflared_bin,
        "tunnel",
        "--no-autoupdate",
        "--metrics",
        f"127.0.0.1:{metrics_port}",
        "--url",
        f"http://{host}:{port}",
    ]
    proc = subprocess.Popen(cmd_tunnel, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    register_process(proc)

    tunnel_url = None
    retries = 10
    while retries > 0:
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
        retries -= 1

    return proc, tunnel_url


def tunnel_is_healthy(public_url: str | None) -> bool:
    if not public_url:
        return False
    try:
        response = requests.get(public_url, timeout=5)
        return response.status_code < 500
    except requests.RequestException:
        return False


def restart_proxy_record(record: dict, host: str, target: str) -> None:
    stop_process(record.get("proxy_proc"))
    record["proxy_proc"] = start_reverse_proxy(host, record["local_port"], target)
    record["target"] = target
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def restart_tunnel_process(record: dict, host: str, cloudflared_bin: str) -> None:
    stop_process(record.get("tunnel_proc"))
    metrics_port = record.get("metrics_port", record["local_port"] + 10000)
    tunnel_proc, public_url = start_tunnel_for_port(host, record["local_port"], metrics_port, cloudflared_bin)
    if not public_url:
        stop_process(tunnel_proc)
        raise RuntimeError(f"Tunnel did not report a URL for port {record['local_port']}")
    record["tunnel_proc"] = tunnel_proc
    record["public_url"] = public_url
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def restart_tunnel_record(record: dict, host: str, cloudflared_bin: str) -> None:
    stop_process(record.get("tunnel_proc"))
    stop_process(record.get("proxy_proc"))
    record["proxy_proc"] = start_reverse_proxy(host, record["local_port"], record["target"])
    record["tunnel_proc"] = None
    restart_tunnel_process(record, host, cloudflared_bin)
    record["timestamp"] = time.strftime('%Y-%m-%d %H:%M:%S')


def recover_tunnel_record(index: int, records: list[dict], host: str, cloudflared_bin: str) -> None:
    record = records[index]
    if index == len(records) - 1:
        restart_tunnel_record(record, host, cloudflared_bin)
        return

    restart_tunnel_process(record, host, cloudflared_bin)
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


def main():
    colorama.just_fix_windows_console()
    install_shutdown_handlers()
    parser = argparse.ArgumentParser(description="Run the Replica script.")
    parser.add_argument('-t', '--target', type=str, default="https://example.com", help='Specify the target URL.')
    parser.add_argument('-n', '--number', type=int, default=3, help='The number of reverse proxies. more proxy = more anonymity but slower speed.')
    parser.add_argument('--github-token', type=str, required=False, help='Specify the GitHub token.')
    parser.add_argument('--host', '-H', type=str, default="0.0.0.0", help='Specify the host to bind the server.')
    parser.add_argument('--port', '-p', type=int, default=8000, help='Specify the starting port to bind the server. it will use the specified port to specified port + number of reverse proxies. make sure that they are available.')
    parser.add_argument('--metrics', '-m', type=int, default=10000, help='Specify the starting port for metrics. it will use the specified port to specified port + number of reverse proxies. make sure that they are available.')
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

    routing_table = {}
    target = args.target
    gist_token = os.getenv("GITHUB_TOKEN") or args.github_token
    local_state = load_local_state()
    gist_state_id = local_state.get("gist_id") if isinstance(local_state, dict) else None
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

            tunnel_proc, tunnel_url = start_tunnel_for_port(args.host, port, metrics_port, cloudflared_bin)

            if tunnel_url:
                print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel established on port {port} -> {tunnel_url}")
                routing_table[f"node_{port}"] = {
                    "local_port": port,
                    "metrics_port": metrics_port,
                    "public_url": tunnel_url,
                    "target": target,
                    "origin_target": args.target,
                    "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "proxy_proc": proxy_proc,
                    "tunnel_proc": tunnel_proc,
                }
                target = tunnel_url
            else:
                stop_process(proxy_proc)
                stop_process(tunnel_proc)
                print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel failed to report an endpoint on port {port}")

        if len(routing_table) == args.number and routing_table:
            gist_state_id = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id)

        if routing_table:
            print(format_routing_table(routing_table))

        while True:
            time.sleep(10)
            records = list(routing_table.values())
            for index, (key, record) in enumerate(list(routing_table.items())):
                public_url = record.get("public_url")
                if not tunnel_is_healthy(public_url):
                    print(f"{colorama.Fore.YELLOW}[WARN] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel unhealthy for {public_url or key}; restarting.")
                    try:
                        cloudflared_bin = resolve_cloudflared_path()
                        if cloudflared_bin is None:
                            raise RuntimeError("cloudflared is not installed or not in PATH")
                        recover_tunnel_record(index, records, args.host, cloudflared_bin)
                        if len(routing_table) == args.number:
                            gist_state_id = persist_tunnel_state(list(routing_table.values()), gist_token=gist_token, gist_id=gist_state_id)
                        print(f"{colorama.Fore.GREEN}[SUCCESS] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Tunnel recovered for port {record['local_port']}: {record['public_url']}")
                    except Exception as exc:
                        print(f"{colorama.Fore.RED}[ERROR] [{time.strftime('%Y-%m-%d %H:%M:%S')}]{colorama.Style.RESET_ALL} Could not recover tunnel on port {record['local_port']}: {exc}")
    except KeyboardInterrupt:
        print("\nStopping spawned reverse proxies...")
        cleanup_started_processes()
        raise SystemExit(0)
    finally:
        cleanup_started_processes()


if __name__ == "__main__":
    main()
