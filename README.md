# Surface Tor

A reverse-proxy chain with Cloudflare Quick Tunnels and a tiny searchable directory. It gives your URL a small travel itinerary: the request passes through the proxy nodes, then heads to the site you configured. The included search page lets people look up the final public tunnel URL without reading a JSON file like it is a treasure map drawn by an accountant.

> **Reality check:** despite the name and the dark-themed search page, this is not Tor, does not create `.onion` services, and does not guarantee anonymity. Cloudflare, your hosting provider, the destination site, and network observers may still see connection metadata. More proxy layers mean more hops, latency, and things that can break, not a mathematically certified invisibility cloak. For any normal observer or standard tracking system on the surface web, tracing the source through these stacked, decoupled layers is incredibly difficult, making your architecture highly effective at breaking direct visibility. Use it only for sites and traffic you are authorized to proxy. The provided codes have features to repair the broken URLs automatically.

## What It Does

1. Starts the [Replica](https://github.com/sarperavci/Replica) reverse-proxy service for each configured node (see [Replica](#about-the-replica-reverse-proxy) below).
2. Gives each node a Cloudflare Quick Tunnel URL via `cloudflared tunnel --url`.
3. Chains the nodes: each proxy after the first targets the preceding tunnel URL.
4. Monitors local proxy ports and process liveness and attempts to recover unhealthy tunnels.
5. Saves one searchable record per original site in `surface_tor.json` and, when configured, creates or updates a public GitHub Gist named `surface_tor.json`.
6. Provides a static search template at [pages/index.html](https://opsonusdh.github.io/Surface-Tor/pages/). It reads the Gist ID from `pages/lookup.txt` and fetches current public Gist data when someone searches.

The saved state looks roughly like this:

```json
{
  "gist_id": "your-gist-id",
  "records": {
    "hash-of-origin-and-final-url": {
      "name": "Example Domain",
      "desc": "A description from the site's HTML metadata",
      "url": "https://your-final-tunnel.trycloudflare.com"
    }
  },
  "target_index": {
    "https://example.com": "hash-of-origin-and-final-url"
  }
}
```

`target_index` lets the launcher replace a site's old record when its final tunnel URL changes, while preserving records for other sites. Only the final public tunnel URL is published as a result; intermediate URLs are used internally to build the chain.

## About the Replica reverse proxy

Surface Tor chains instances of [Replica](https://github.com/sarperavci/Replica), a lightweight reverse proxy. Each node is a FastAPI app served by uvicorn, configured through the `TARGET_ORIGIN` environment variable that the launcher sets before spawning it. Replica forwards the incoming request to that origin, sanitizes headers, rewrites response headers and HTML URLs so links point back to itself, applies optional text replacements, optionally injects custom JavaScript, and caches static assets and HTML for speed. This means every node in the chain both receives traffic on its local port *and* simultaneously mirrors the upstream site it targets.

Replica is a git submodule-style dependency: the setup scripts clone it into the `Replica/` subdirectory and install its own `requirements.txt` (fastapi, httpx, uvicorn, httpx-curl-cffi, python-dotenv, websockets) plus the root `requirements.txt` (which adds flask and colorama).

## Prerequisites

Install these before starting:

- **Git**, to clone the repository and its Replica dependency.
- **Python 3.10 or newer**. Python 3.12 is a good choice.
- **PowerShell** on Windows, or a POSIX shell on Linux.
- Internet access so setup can download Cloudflared, clone Replica, install packages, and contact GitHub.
- **proot** *(Android/Termux only — see [Platform notes](#platform-notes))* if `/etc/resolv.conf` is unavailable, so Go binaries like cloudflared can resolve DNS.

The setup scripts download Cloudflared and clone/install Replica when it is missing. The root launcher dependencies are installed separately below. A virtual environment is recommended so Python packages do not become the unexpected roommates of your system Python.

## Platform notes

### Android / Termux DNS workaround

Go binaries — including `cloudflared` — resolve DNS by reading `/etc/resolv.conf`. On Android/Termux the `/etc` partition is read-only (it symlinks to `/system/etc`) and typically lacks a `resolv.conf` file. This causes cloudflared to fall back to `[::1]:53`, where no DNS server listens, so tunnels fail to establish.

Surface Tor detects this at startup (it checks whether `/etc/resolv.conf` exists) and, when the `proot` binary is available, transparently wraps every `cloudflared` invocation in proot with a writable rootfs that contains:

- a `resolv.conf` with working nameservers (copied from Termux's own resolv.conf or, as a fallback, `8.8.8.8`, `8.8.4.4`, `1.1.1.1`),
- CA certificates so cloudflared can verify HTTPS connections.

When proot is active, Surface Tor also prefers the **bundled** `cloudflared` binary (the one downloaded by `setup.sh` into the repository root) over any system-installed copy, because the bundled binary is statically linked and works inside the proot rootfs whereas dynamically-linked system copies lose their shared libraries.

If proot is not found, Surface Tor prints a warning but continues — cloudflared may still work if it uses its own DNS resolver or if `/etc/resolv.conf` exists on your system.

To install proot on Termux:

```sh
pkg install proot
```

### Stale process cleanup

When a proot-wrapped `cloudflared` process is killed, the child `cloudflared` can survive as an orphan (reparented to init) and hold onto its metrics port. On the next launch this causes `address already in use` errors that cascade into tunnel failures. At startup the launcher scans running processes for orphaned `cloudflared tunnel` instances (matching the distinctive flags Surface Tor passes) and kills them, then waits briefly for the kernel to release the ports.

## Step-by-Step Setup

### 1. Clone the repository

Replace `<repository-url>` with the Git URL for this repository:

```sh
git clone <repository-url>
cd <repository-folder>
```

### 2. Create and activate a virtual environment

**Windows PowerShell:**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, allow scripts only for the current PowerShell process, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

**Linux:**

```sh
python3 -m venv .venv
. .venv/bin/activate
```

### 3. Run the platform setup script

**Windows PowerShell:**

```powershell
$repo = Get-Location
.\setup.ps1
Set-Location $repo
```

This downloads `cloudflared.exe`, clones Replica if needed, and installs Replica's requirements. (`setpup.ps1` is not provided — use `setup.ps1`.)

**Linux:**

```sh
chmod +x setup.sh
./setup.sh
```

This downloads `cloudflared`, clones Replica if needed, and installs Replica's requirements.

### 4. Install the launcher requirements

Run this from the repository root after setup:

**Windows:**

```powershell
py -3.12 -m pip install -r requirements.txt
```

**Linux:**

```sh
python -m pip install -r requirements.txt
```

### 5. Start the website you want to proxy

Create or deploy your own Flask, Django, or other web application and make sure it is reachable at a public HTTP or HTTPS URL. Surface Tor forwards traffic to that origin; it does not create or host the application for you. Localhost and private-network targets are intentionally not supported by the metadata lookup.

For example, if your app is available at `https://my-site.example`, use that as `--target` below. Use a site you own or have permission to proxy. The internet has enough surprise paperwork already.

### 6. Start the proxy chain

**Windows:**

```powershell
py -3.12 main.py --target "https://my-site.example" --number 3
```

**Linux:**

```sh
python3 main.py --target "https://my-site.example" --number 3
```

The launcher prints the nodes and their routes. The final node's public URL is the URL visitors use. Cloudflare Quick Tunnel URLs are temporary and may change after recovery or restart; the launcher updates the saved final URL when it recovers a chain.

## Command-Line Options

| Option | Short | Default | Meaning |
| --- | --- | --- | --- |
| `-t`, `--target` | | `https://example.com` | Public HTTP(S) origin site to proxy. |
| `-n`, `--number` | | `3` | Number of proxy/tunnel nodes in the chain. |
| `-g`, `--github-token` | | none | GitHub token used to create or update the public Gist. |
| `-N`, `--name` | | none (auto) | Override the display name saved to the Gist. If omitted, the name is fetched from the target site's HTML `<title>` (or the domain is used as fallback). |
| `-d`, `--desc` | | none (auto) | Override the description saved to the Gist. If omitted, the description is fetched from the site's `<meta name="description">` (or empty if unavailable). |
| `--host`, `-H` | | `0.0.0.0` | Local bind host for the proxy servers (uvicorn). Health checks always probe `127.0.0.1`, so `0.0.0.0` is safe. |
| `--port`, `-p` | | `8000` | First local proxy port; later nodes use following ports. |
| `--metrics`, `-m` | | `10000` | Starting offset for Cloudflared metrics ports (actual metrics port = `local_port + metrics`). |
| `--protocol` | | none (auto) | Cloudflare Tunnel protocol: `quic`, `http2`, or `h2mux`. Default lets cloudflared auto-select (usually `quic`). On Android/Termux, use `h2mux` to force TCP-based connections if QUIC/UDP is unreliable. |

Example with explicit ports, four nodes, name/description overrides, and a protocol:

```sh
python3 main.py --target "https://my-site.example" --number 4 --host 0.0.0.0 --port 8000 --metrics 10000 --protocol h2mux --name "My Site" --desc "A proxied site"
```

On Windows, replace `python3` with `py -3.12` if that is how Python is installed.

### How many nodes should I use?

`--number 1` creates one proxy node. `--number 3` creates three chained proxy nodes. More nodes add routing hops, but also add latency and more failure points. They do **not** guarantee anonymity or hide you from Cloudflare, your server host, or the origin site. Start small, check that the chain works, and add layers only if you understand the trade-offs. A six-hop chain is still not a privacy policy.

### Health checks and recovery

Each node in the chain has two components:

- A **Replica reverse-proxy process** (uvicorn) listening on its local port.
- A **cloudflared tunnel process** exposing a public URL.

The launcher checks health every 10 seconds using **socket-level port checks and process liveness** (`subprocess.Popen.poll()`), **not** HTTP status codes:

- It opens a TCP connection to the node's local port — if nothing is listening, the proxy process died.
- It checks whether each spawned process (proxy and tunnel) has exited.
- It verifies a public URL exists for the node.

Crucially, the health check **ignores HTTP response codes**. When the upstream target server is unreachable or returns a 502, that is a proxy-level issue, not a tunnel-level issue. Restarting the tunnel would not fix an unreachable origin, so a 502 / 503 / 504 from the upstream is treated as **healthy** — the tunnel infrastructure is fine, the destination is just down.

Recovery is triggered only when:

- The local proxy port is **not listening** (proxy process died), or
- The cloudflared tunnel process has **exited**, or
- There is **no public URL** (tunnel never came up).

When recovery triggers, the launcher restarts only the affected components and rebinds downstream nodes that depend on the recovered URL. The final URL in saved state is updated automatically after a successful recovery.

## GitHub Gist Sync

The launcher can save locally without a GitHub token. To create/update the public Gist, provide a token with permission to create and edit Gists. Treat it like a house key, except the house is your GitHub account.

Before contacting the Gist API, the launcher **validates the token** by calling `https://api.github.com/user`. If the token is missing, invalid, or the network check fails, it prints a warning and falls back to local-only mode — the JSON state file is still written, but the Gist is not updated. Get a token with the `gist` scope at <https://github.com/settings/tokens>.

**Recommended: set it in the environment.**

PowerShell, for the current terminal session:

```powershell
$env:GITHUB_TOKEN = "YOUR_GITHUB_TOKEN"
py -3.12 main.py --target "https://my-site.example" --number 3
Remove-Item Env:GITHUB_TOKEN
```

Linux:

```sh
export GITHUB_TOKEN="YOUR_GITHUB_TOKEN"
python3 main.py --target "https://my-site.example" --number 3
unset GITHUB_TOKEN
```

You can also pass it as an argument:

```sh
python3 main.py --target "https://my-site.example" --number 3 --github-token "YOUR_GITHUB_TOKEN"
```

Command-line arguments can be saved in shell history or visible in process listings, so the environment-variable method is preferable. Never commit the token, paste it into `lookup.txt`, or publish it in the Gist. If a token is exposed, revoke it and create a replacement.

On the first run with a valid token, the launcher creates a **public** Gist called `surface_tor.json` and stores its ID in the local state file. Later runs fetch the current Gist, merge the updated site record, preserve other sites, and update the Gist. GitHub does not let an existing private Gist be made public in place; if the saved Gist is private, the launcher creates a new public Gist and saves the new ID locally. The old private Gist is not deleted automatically. If a Gist API call fails for any reason (rate limit, network error, revoked token), the launcher logs a warning and saves the state locally so no data is lost.

## Publish the search page

1. Run the launcher with `GITHUB_TOKEN` configured so it creates or updates the public `surface_tor.json` Gist.
2. Open the Gist on GitHub. Find the Gist ID in its URL, or copy the full Gist page URL.
3. Replace the single line in `pages/lookup.txt` with that URL. Both of these formats are supported:

   **Gist page URL (recommended):**

   ```text
   https://gist.github.com/YOUR_GITHUB_NAME/YOUR_GIST_ID
   ```

   **Raw/Gist API URL:**

   ```text
   https://gist.githubusercontent.com/YOUR_GITHUB_NAME/YOUR_GIST_ID/raw/HASH/surface_tor.json
   ```

   The template extracts the Gist ID by looking for a 20+ character hex segment in the path, or — for raw URLs — the segment immediately before `raw`. Do not paste a token here. The Gist must be public so visitors can search it.

4. Push the repository to GitHub and enable GitHub Pages for the repository from the repository root.
5. Open the published template at `https://YOUR_GITHUB_NAME.github.io/YOUR_REPOSITORY/pages/`.

The search template is intentionally static: it uses the browser to read `pages/lookup.txt`, calls GitHub's public Gist API, and ranks records by matching site name, description, and URL. The strongest matches appear first. It has dark and light themes; result URLs are blue, and the rest of the palette stays monochrome.

You can also copy `pages/index.html` and `pages/lookup.txt` into your own static website. Keep them together so the page can find `lookup.txt` next door. The lookup file is a pointer to the Gist, not a storage location for credentials.

## Troubleshooting

- **`Replica` directory missing:** run `setup.ps1` (Windows) or `setup.sh` (Linux) from the repository checkout.
- **`cloudflared` not found:** rerun setup, or set `CLOUDFLARED_PATH` to the installed executable. The launcher checks (in order): the `CLOUDFLARED_PATH` env var, the system `PATH`, and finally the bundled binary in the repository root.
- **`DNS workaround active` warning / tunnels fail on Android:** install proot (`pkg install proot`) and ensure your resolv.conf nameservers are reachable. The launcher creates a proot rootfs with DNS config automatically when proot is available. On Termux you can also try `--protocol h2mux` if QUIC tunnels fail.
- **Port already in use:** choose a different `--port`; the following ports are used for the remaining nodes. If you see `address already in use` for a metrics port, a stale cloudflared from a previous run may be holding it — the launcher cleans these up automatically at startup, but check for stray processes with `pgrep cloudflared`.
- **No Gist appears:** verify `GITHUB_TOKEN` is set and has Gist permissions. The launcher validates the token at startup and will fall back to local-only mode with a warning if it is invalid. Without a token, state is saved locally only.
- **Gist sync fails after a successful run:** check GitHub's API rate limits, or verify the token wasn't revoked. Local state is always saved even when Gist sync fails.
- **Search says the Gist is unavailable:** check that the Gist is public, `pages/lookup.txt` contains its correct URL (or raw URL), and GitHub's unauthenticated API has not rate-limited requests.
- **A tunnel URL changed:** Quick Tunnel addresses are temporary. Use the latest final URL printed by the launcher or shown in the updated Gist.
- **Upstream returns 502 but the tunnel is marked healthy:** this is expected. A 502 from a proxy node means the target site is unreachable, not that the tunnel is broken. The health check uses socket + process liveness, not HTTP status, so no recovery is triggered.
- **Ctrl+C:** the launcher attempts to stop the proxy and Cloudflared processes it started, including any proot wrappers and orphaned children.

## MIT License

Do anything, just don't be evil.
