# Surface Tor

A reverse-proxy chain with Cloudflare Quick Tunnels and a tiny searchable directory. Each node is a [Replica](https://github.com/sarperavci/Replica) mirror of the site you configure, exposed through its own throwaway `trycloudflare.com` address, and the included search page lets people look up the current public URL without reading a JSON file like it is a treasure map drawn by an accountant.

> **What this is, and what it is not.** Despite the name and the dark-themed search page, this is not Tor, does not create `.onion` services, and does **not** make you anonymous. Every node in a chain started by one launcher runs on the same device, behind the same network connection and the same Cloudflare, so extra nodes add latency and failure points, not isolation. The destination site sees *your* device's IP address, Cloudflare sees every tunnel, and anyone running a node can read the traffic passing through it (see [Security and trust model](#security-and-trust-model)). What the tool genuinely gives you is an easy, account-free, temporary public mirror of a site that you can find again through a static directory page. Use it only for sites and traffic you are authorized to proxy.

## What It Does

1. Starts a [Replica](https://github.com/sarperavci/Replica) reverse-proxy node for each configured position in the chain (see [About Replica](#about-the-replica-reverse-proxy)).
2. Gives each node a Cloudflare Quick Tunnel URL via `cloudflared tunnel --url`.
3. Chains the nodes: each proxy after the first targets the preceding tunnel URL.
4. Monitors local proxy ports and process liveness, and repairs whichever half of a node failed (see [Health checks and recovery](#health-checks-and-recovery)).
5. Saves one searchable record per original site in `surface_tor.json` and, when configured, creates or updates a public GitHub Gist named `surface_tor.json`.
6. Provides a static search template at [pages/index.html](https://opsonusdh.github.io/Surface-Tor/pages/). It reads the Gist ID from `pages/lookup.txt` and fetches current public Gist data when someone searches.
7. Lets a chain continue on another device, anywhere, by using this device's final URL as that device's `--target` (see [Extending a chain from another device](#extending-a-chain-from-another-device)).

### What is saved, and what is published

The **local** state file (`surface_tor.json`, never committed) looks roughly like this:

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

`target_index` maps each original site to its current record so the launcher can replace a site's old record when its final tunnel URL changes, while preserving records for other sites. **It is kept only in the local file.** The public Gist receives `gist_id` and `records` and nothing else, so the original site's address is not written to the Gist by the launcher. If an older version of this project already published `target_index`, the next sync removes it.

Intermediate tunnel URLs are never published either; only each chain's final URL is.

> The record *key* is a SHA-256 hash of the original site's address and the final URL. Someone who already suspects which site a record belongs to can confirm it by hashing their guess. And of course the record's `name` and `desc` normally come from the site's own title and description. If the original site must not be linkable to its mirror, set `--name` and `--desc` yourself.

## Security and trust model

Read this before you point the tool at anything with accounts, logins, or personal data.

- **Whoever runs a node can read the traffic.** Cloudflare terminates TLS for every Quick Tunnel and the node behind it handles plain HTTP. Anything a visitor types into a mirrored site, including passwords and session cookies, is visible to that node's operator and to Cloudflare. **Do not log in to a site through a mirror run by someone you do not trust, and do not run mirrors of login-protected sites for other people.** A chain extended across several devices (see below) means every one of those devices can see everything.
- **Mirrors rewrite the site.** Replica rewrites URLs in responses and strips the origin's `Content-Security-Policy` headers so the mirrored pages work on a different address. That is also what makes a mirror a convenient place to tamper with a page, so mirror sites you control.
- **Replica's cache is shared between visitors.** Replica caches HTML responses for five minutes keyed by the request URL only. If the origin serves *personalised* pages without cache headers that say so (`Cache-Control: private`, `no-store`, `no-cache`), one visitor can be served another visitor's page. This was reproduced against a test origin that returned a different page per cookie and sent no cache headers. Mirror public, non-personalised content, or make sure the origin marks personalised responses as private.
- **Redirects are followed on the server.** Replica follows the origin's redirects itself. If the origin has an open redirect, a visitor could use the mirror to make *your device* fetch an address on your own network. Again: mirror sites you control, and fix open redirects on them.
- **The proxy ports are loopback-only by default** (`--host 127.0.0.1`); cloudflared connects to them locally. Widen `--host` only if you really want the nodes reachable on your network.
- **Your GitHub token stays in the launcher.** It is stripped from the environment of every Replica and cloudflared process the launcher starts. Prefer the `GITHUB_TOKEN` environment variable over `--github-token`, which can be recorded in shell history and process listings.
- **Quick Tunnels are for testing.** Cloudflare documents them as having no uptime guarantee and a cap on concurrent requests, so expect URLs to change and heavy traffic to be refused.

## About the Replica reverse proxy

Surface Tor chains instances of [Replica](https://github.com/sarperavci/Replica), a lightweight reverse proxy. Each node is a FastAPI app served by uvicorn and configured through the `TARGET_ORIGIN` environment variable the launcher sets before starting it. Replica forwards the incoming request to that origin with a browser-like TLS fingerprint, sanitizes headers (it removes Cloudflare's `cf-*` and `x-forwarded-*` headers, so visitor IP addresses are not passed upstream), rewrites response headers and HTML URLs so links point back to itself, applies optional text replacements, optionally injects custom JavaScript, and caches static assets and HTML.

Replica is a separate project and is **not** stored in this repository. The setup scripts:

- clone it into the git-ignored `Replica/` directory, **pinned to the commit this project was tested with**;
- apply a small, idempotent patch (`scripts/patch_replica.py`) that adds the `-u/--user-agent` and `--impersonate` features. The patch is re-applied automatically the first time you use either flag if your `Replica/` was cloned earlier.

## Prerequisites

- **Git**, to clone this repository and its Replica dependency.
- **Python 3.10 or newer**. Python 3.12 is a good choice.
- **PowerShell** on Windows, or a POSIX shell on Linux/Android (Termux).
- **curl** on Linux/Termux (`pkg install curl` on Termux).
- Internet access so setup can download cloudflared, clone Replica, install packages, and contact GitHub.

A virtual environment is recommended so Python packages do not become the unexpected roommates of your system Python.

## Platform notes

### Android / Termux

Go programs such as `cloudflared` read `/etc/resolv.conf` to find a DNS server. If a device does not have that file, tunnel creation fails with a message like `lookup api.trycloudflare.com on [::1]:53`, and the launcher prints cloudflared's output so you can see it. This was not needed on the devices this project was tested on, so the launcher no longer contains a workaround: earlier versions wrapped cloudflared in `proot` to supply a `resolv.conf`, but that code is gone (see the repository history if you need it back).

If QUIC/UDP is unreliable on your connection, `--protocol h2mux` forces a TCP-based tunnel.

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
.\setup.ps1
```

**Linux / Termux:**

```sh
chmod +x setup.sh
./setup.sh
```

Setup clones Replica (pinned) and patches it, downloads cloudflared, and installs `requirements.txt`. cloudflared is downloaded at a **pinned version and checked against the SHA-256 Cloudflare publishes for it**; setup refuses to install a file that does not match. To try another version, set `CLOUDFLARED_VERSION` (for example `CLOUDFLARED_VERSION=latest ./setup.sh`); anything other than the pinned version is installed unverified, with a warning. Re-running setup is safe and skips the download when the right binary is already there.

> The PowerShell script mirrors the shell script's logic, but the shell script is the one that has been run end to end.

### 4. Start the website you want to proxy

Create or deploy your own Flask, Django, or other web application and make sure it is reachable at a public HTTP or HTTPS URL. Surface Tor forwards traffic to that origin; it does not create or host the application for you. Localhost and private-network targets are intentionally not supported by the metadata lookup.

For example, if your app is available at `https://my-site.example`, use that as `--target` below. Use a site you own or have permission to proxy. The internet has enough surprise paperwork already.

### 5. Start the proxy chain

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
| `--target` | `-t` | `https://example.com` | The URL this device's first node proxies: a real site, or another device's final tunnel URL (see [Extending a chain](#extending-a-chain-from-another-device)). |
| `--number` | `-n` | `3` | Number of proxy/tunnel nodes on this device. Minimum 1. |
| `--github-token` | `-g` | none | GitHub token used to create or update the public Gist. Prefer the `GITHUB_TOKEN` environment variable. |
| `--gist-id` | | from local state | Existing Gist ID to publish into. Lets a fresh device with no local `surface_tor.json` attach to a Gist another device created. Pass the same token (account). |
| `--origin` | | same as `--target` | The real site this chain represents, when `--target` is another device's tunnel URL. Used for the record's identity and for fetching its name/description. |
| `--no-publish` | | off | Relay mode: run this device's part of the chain but write nothing to `surface_tor.json` or any Gist, and ignore any token. |
| `--name` | `-N` | auto | Override the display name saved to the Gist. If omitted, it comes from the site's HTML `<title>` (or the domain as a fallback). |
| `--desc` | `-d` | auto | Override the description saved to the Gist. If omitted, it comes from the site's `<meta name="description">` (or is empty). |
| `--user-agent` | `-u` | none (forward the visitor's) | Send exactly this `User-Agent` header to the target on every upstream request, and use it for the launcher's own title/description lookup too. See [Pinning the User-Agent](#pinning-the-user-agent). |
| `--impersonate` | | none (derived from `-u`) | Advanced: force a specific TLS/HTTP2 fingerprint profile (for example `chrome`, `firefox`, `safari`). An unsupported name makes every proxied request fail with 502. |
| `--host` | `-H` | `127.0.0.1` | Local bind address for the proxy servers. Loopback is all cloudflared needs. |
| `--port` | `-p` | `8000` | First local proxy port; node *i* uses `port + i`. |
| `--metrics` | `-m` | `10000` | **Offset** added to each node's port to get its cloudflared metrics port (the node on 8000 uses 18000). Every resulting port must be at most 65535. |
| `--protocol` | | none (auto) | Cloudflare Tunnel protocol: `quic`, `http2`, or `h2mux`. Default lets cloudflared auto-select (usually `quic`). |

Example with explicit ports, four nodes, name/description overrides, and a protocol:

```sh
python3 main.py --target "https://my-site.example" --number 4 --port 8000 --metrics 10000 --protocol h2mux --name "My Site" --desc "A proxied site"
```

On Windows, replace `python3` with `py -3.12` if that is how Python is installed.

### Pinning the User-Agent

By default Replica forwards each visitor's own `User-Agent` upstream, so an origin that rejects unusual or bot-like user agents will reject some visitors and not others. `-u` makes every upstream request carry one fixed string instead:

```sh
python3 main.py --target "https://my-site.example" -u "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
```

The TLS/HTTP2 fingerprint is matched to the string you give (Firefox if it says Firefox, otherwise Chrome), so the header and the fingerprint stay consistent. The launcher's own lookup of the site's title and description uses the same string, so a site that rejects unfamiliar agents does not silently reduce your directory entry to a bare domain name. Use `--impersonate` only if you need a specific profile. The string must be a single non-empty line. In a chain, the user agent only matters on the device whose first node talks to the real site.

### How many nodes should I use?

`--number 1` creates one proxy node, and for most uses that is enough. Nodes in one launcher all run on the same device behind the same Cloudflare, so more of them do **not** hide you from Cloudflare, your network, or the origin, and they do not make the mirror harder to block (a blocker that blocks `trycloudflare.com` blocks all of them). Each extra node adds latency, passes every response over your own connection again, and adds a failure point. Start with one. A six-hop chain is still not a privacy policy.

## Extending a chain from another device

A chain does not have to live on one machine. Any device can continue a chain by using the previous device's final public URL as its own `--target`, and the last device publishes. The request then travels: visitor → last device's tunnel → its node(s) → previous device's tunnel → its node(s) → the real site.

**Device A (relay): the start of the chain.**

```sh
python3 main.py --target "https://example.com" --number 2 --no-publish
```

`--no-publish` keeps A's address out of `surface_tor.json` and the Gist, so it is never listed as a public entry. A prints its final URL, for example `https://abc-words.trycloudflare.com`; hand that to Device B.

**Device B (publisher): anywhere with internet access, even on another continent.**

```sh
export GITHUB_TOKEN="YOUR_GITHUB_TOKEN"
python3 main.py \
  --target "https://abc-words.trycloudflare.com" \
  --origin "https://example.com" \
  --gist-id YOUR_GIST_ID \
  --number 1
```

- `--target` is what B's first node proxies: Device A's URL.
- `--origin` is the real site. It decides the record's identity and where the name and description are fetched from, so the directory entry reads "Example Domain" rather than a tunnel hostname.
- `--gist-id` publishes into the existing Gist even though B has no local state file. The token must belong to the account that owns the Gist.

B publishes its own final URL, for example `https://def-words.trycloudflare.com`. If the Gist already contains a record pointing at A's URL (because A once ran without `--no-publish`), that record is replaced rather than listed alongside.

Things to know:

- **Quick Tunnel URLs are temporary.** If A's tunnel is recreated, A's final URL changes. A prints a warning when that happens in relay mode, but B does not find out by itself: restart B with the new `--target`.
- **Every device in the chain, and Cloudflare, can read the traffic.** Only chain devices you control or fully trust, and see the [trust model](#security-and-trust-model).
- **Each extra device adds latency and a failure point.** Chaining across devices spreads *where* nodes run; it does not add privacy.
- `--origin` is only needed when `--target` is not the real site. Without it, `--target` is treated as the real site.
- This has been tested with the launcher and Replica on one machine, with a second node standing in for the second device's tunnel URL. It has not been exercised across two physical devices over live Quick Tunnels.

### Health checks and recovery

Each node in the chain has two components:

- A **Replica reverse-proxy process** (uvicorn) listening on its local port.
- A **cloudflared tunnel process** exposing a public URL.

The launcher checks health every 10 seconds using **socket-level port checks and process liveness**, **not** HTTP status codes:

- It opens a TCP connection to the node's local port. If nothing is listening, the proxy is down.
- It checks whether each spawned process (proxy and tunnel) has exited.
- It verifies a public URL exists for the node.

The health check **ignores HTTP response codes**. When the upstream target is unreachable or returns a 502, that is a proxy-level issue, not a tunnel-level one, and restarting the tunnel would not fix an unreachable origin. A 502 / 503 / 504 from upstream is therefore treated as **healthy**.

Recovery is symptom-based and repairs only what failed:

- **The proxy died or its port stopped answering:** only the proxy is restarted, on the same port and with the same target. The tunnel and its public URL are untouched, so nothing downstream changes.
- **The tunnel process exited or never reported a URL:** the tunnel is recreated, which gives it a **new public URL**, and the next node's proxy is rebound to that new URL.
- **Both:** the proxy is restarted first, then the tunnel.

Published state is updated after a recovery that changes the final URL.

## GitHub Gist Sync

The launcher can save locally without a GitHub token. To create/update the public Gist, provide a token with permission to create and edit Gists. Treat it like a house key, except the house is your GitHub account.

Before contacting the Gist API, the launcher **validates the token** by calling `https://api.github.com/user`. If the token is missing, invalid, or the network check fails, it prints a warning and falls back to local-only mode: the JSON state file is still written, but the Gist is not updated. Get a token with the `gist` scope at <https://github.com/settings/tokens>.

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

You can also pass it as an argument (`--github-token "YOUR_GITHUB_TOKEN"`), but command-line arguments can be saved in shell history or visible in process listings, so the environment variable is preferable. Never commit the token, paste it into `lookup.txt`, or publish it in the Gist. If a token is exposed, revoke it and create a replacement.

On the first run with a valid token, the launcher creates a **public** Gist called `surface_tor.json` and stores its ID in the local state file. Later runs fetch the current Gist, merge the updated site record, preserve other sites, and update the Gist. A few details worth knowing:

- **A failed sync never loses your Gist.** If a Gist call fails (rate limit, network error, revoked token), the launcher logs a warning, saves the state locally, and **keeps the Gist ID it already had**, so the next successful run updates the same Gist instead of creating a new one that your search page does not point at. A failure to *read* the existing Gist also falls back to local state instead of aborting.
- GitHub does not let an existing private Gist be made public in place; if the saved Gist is private, the launcher creates a new public Gist and saves the new ID locally. The old private Gist is not deleted automatically.
- The state file is written atomically (to a temporary file, then renamed), so an interrupted run cannot leave a half-written file behind.

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

   The template extracts the Gist ID by looking for a 20+ character hex segment in the path, or, for raw URLs, the segment immediately before `raw`. Do not paste a token here. The Gist must be public so visitors can search it.

4. Push the repository to GitHub and enable GitHub Pages for the repository from the repository root.
5. Open the published template at `https://YOUR_GITHUB_NAME.github.io/YOUR_REPOSITORY/pages/`.

The search template is intentionally static: it uses the browser to read `pages/lookup.txt`, calls GitHub's public Gist API, and ranks records by matching site name, description, and URL. The strongest matches appear first. It has dark and light themes; result URLs are blue, and the rest of the palette stays monochrome. It only reads `records`, so it is unaffected by `target_index` no longer being published.

You can also copy `pages/index.html` and `pages/lookup.txt` into your own static website. Keep them together so the page can find `lookup.txt` next door. The lookup file is a pointer to the Gist, not a storage location for credentials.

## Troubleshooting

- **`Replica` directory missing:** run `setup.ps1` (Windows) or `setup.sh` (Linux) from the repository checkout.
- **`cloudflared` not found:** rerun setup, or set `CLOUDFLARED_PATH` to the installed executable. The launcher checks, in order: the `CLOUDFLARED_PATH` environment variable, the system `PATH`, and finally the binary in the repository root.
- **`Tunnel failed to report an endpoint` and nothing is published:** cloudflared could not create a Quick Tunnel; its own error is printed right below. A line like `lookup api.trycloudflare.com ... no such host` means this device cannot resolve or reach Cloudflare (network, DNS, firewall). The launcher exits with code 1 and writes nothing.
- **`-u` / `--impersonate` refuses to run:** the Replica patch could not be applied automatically. Run `python scripts/patch_replica.py` and read its output; if it says Replica's source has changed, the patch needs updating for that Replica version (setup pins the tested one).
- **Port already in use / ports out of range:** choose a different `--port`; the following ports are used for the remaining nodes, and each node's metrics port is `port + --metrics`. The launcher checks that the ports are free (by attempting to connect, so non-HTTP programs are detected too) and that everything stays at or below 65535. It never kills other processes to free a port.
- **No Gist appears:** verify `GITHUB_TOKEN` is set and has Gist permissions. The launcher validates the token at startup and falls back to local-only mode with a warning if it is invalid. Without a token, state is saved locally only.
- **Search says the Gist is unavailable:** check that the Gist is public, `pages/lookup.txt` contains its correct URL (or raw URL), and that GitHub's unauthenticated API has not rate-limited requests from that visitor's network.
- **A tunnel URL changed:** Quick Tunnel addresses are temporary. Use the latest final URL printed by the launcher or shown in the updated Gist. If a chain spans several devices, restart the downstream device with the upstream device's new final URL.
- **Upstream returns 502 but the tunnel is marked healthy:** this is expected. A 502 from a proxy node means the target site is unreachable, not that the tunnel is broken, so no recovery is triggered.
- **Ctrl+C:** the launcher stops the proxy and cloudflared processes it started, including their children.

## Tests

```sh
python -m pip install pytest
python -m pytest test
```

The tests mock processes and the network, so they need neither cloudflared nor an internet connection.

## MIT License

Do anything, just don't be evil.
