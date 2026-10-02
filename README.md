# Surface Tor

A reverse-proxy chain with Cloudflare Quick Tunnels and a tiny searchable directory. It gives your URL a small travel itinerary: the request passes through the proxy nodes, then heads to the site you configured. The included search page lets people look up the final public tunnel URL without reading a JSON file like it is a treasure map drawn by an accountant.

> **Reality check:** despite the name and the dark-themed search page, this is not Tor, does not create `.onion` services, and does not guarantee anonymity. Cloudflare, your hosting provider, the destination site, and network observers may still see connection metadata. More proxy layers mean more hops, latency, and things that can break, not a mathematically certified invisibility cloak. For any normal observer or standard tracking system on the surface web, tracing the source through these stacked, decoupled layers is incredibly difficult, making your architecture highly effective at breaking direct visibility. Use it only for sites and traffic you are authorized to proxy. The provided codes have features to repair the broken URLs automatically. 

## What It Does

1. Starts the [Replica](https://github.com/sarperavci/Replica) reverse-proxy service for each configured node.
2. Gives each node a Cloudflare Quick Tunnel URL.
3. Chains the nodes: each proxy after the first targets the preceding tunnel URL.
4. Monitors the public URLs and attempts to recover unhealthy tunnels.
5. Saves one searchable record per original site in `surface_tor.json` and, when configured, creates or updates a public GitHub Gist named `surface_tor.json`.
6. Provides a static search template at `pages/index.html`. It reads the Gist ID from `pages/lookup.txt` and fetches current public Gist data when someone searches.

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

`target_index` lets the launcher replace a site's old record when its final tunnel URL changes, while preserving records for other sites. Only the final tunnel URL is published as a result; intermediate URLs are used internally to build the chain.

## Prerequisites

Install these before starting:

- **Git**, to clone the repository and its Replica dependency.
- **Python 3.10 or newer**. Python 3.12 is a good choice.
- **PowerShell** on Windows, or a POSIX shell on Linux.
- Internet access so setup can download Cloudflared, clone Replica, install packages, and contact GitHub.

The setup scripts download Cloudflared and clone/install Replica when it is missing. The root launcher dependencies are installed separately below. A virtual environment is recommended so Python packages do not become the unexpected roommates of your system Python.

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

This downloads `cloudflared.exe`, clones Replica if needed, and installs Replica's requirements. If your checkout uses the legacy spelling, `setpup.ps1` is retained as a compatibility wrapper; `setup.ps1` is the preferred name.

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

| Option | Default | Meaning |
| --- | --- | --- |
| `-t`, `--target` | `https://example.com` | Public HTTP(S) origin site to proxy. |
| `-n`, `--number` | `3` | Number of proxy/tunnel nodes in the chain. |
| `--host`, `-H` | `0.0.0.0` | Local bind host for the proxy servers. |
| `--port`, `-p` | `8000` | First local proxy port; later nodes use following ports. |
| `--metrics`, `-m` | `10000` | Metrics-port offset used by Cloudflared. |
| `--github-token` | none | GitHub token used to create or update the public Gist. |

Example with explicit ports and four nodes:

```sh
python3 main.py --target "https://my-site.example" --number 4 --host 0.0.0.0 --port 8000 --metrics 10000
```

On Windows, replace `python3` with `py -3.12` if that is how Python is installed.

### How many nodes should I use?

`--number 1` creates one proxy node. `--number 3` creates three chained proxy nodes. More nodes add routing hops, but also add latency and more failure points. They do **not** guarantee anonymity or hide you from Cloudflare, your server host, or the origin site. Start small, check that the chain works, and add layers only if you understand the trade-offs. A six-hop chain is still not a privacy policy.

## GitHub Gist Sync

The launcher can save locally without a GitHub token. To create/update the public Gist, provide a token with permission to create and edit Gists. Treat it like a house key, except the house is your GitHub account.

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

On the first run with a token, the launcher creates a **public** Gist called `surface_tor.json` and stores its ID in the local state file. Later runs fetch the current Gist, merge the updated site record, preserve other sites, and update the Gist. GitHub does not let an existing private Gist be made public in place; if the saved Gist is private, the launcher creates a new public Gist and saves the new ID locally. The old private Gist is not deleted automatically.

## Publish the Search Page

1. Run the launcher with `GITHUB_TOKEN` configured so it creates or updates the public `surface_tor.json` Gist.
2. Open the Gist on GitHub. Find the Gist ID in its URL, or copy the full Gist page URL.
3. Replace the single line in `pages/lookup.txt` with that URL. A regular Gist page URL is recommended, for example:

   ```text
   https://gist.github.com/YOUR_GITHUB_NAME/YOUR_GIST_ID
   ```

   The template extracts the ID and requests the latest public Gist contents on every search. Do not paste a token here. The Gist must be public so visitors can search it.
4. Push the repository to GitHub and enable GitHub Pages for the repository from the repository root.
5. Open the published template at `https://YOUR_GITHUB_NAME.github.io/YOUR_REPOSITORY/pages/`.

The search template is intentionally static: it uses the browser to read `pages/lookup.txt`, calls GitHub's public Gist API, and ranks records by matching site name, description, and URL. The strongest matches appear first. It has dark and light themes; result URLs are blue, and the rest of the palette stays monochrome.

You can also copy `pages/index.html` and `pages/lookup.txt` into your own static website. Keep them together so the page can find `lookup.txt` next door. The lookup file is a pointer to the Gist, not a storage location for credentials.

## Troubleshooting

- **`Replica` directory missing:** run `setup.ps1` or `setup.sh` from the repository checkout.
- **`cloudflared` not found:** rerun setup, or set `CLOUDFLARED_PATH` to the installed executable.
- **Port already in use:** choose a different `--port`; the following ports are used for the remaining nodes.
- **No Gist appears:** verify `GITHUB_TOKEN` is set and has Gist permissions. Without a token, state is saved locally only.
- **Search says the Gist is unavailable:** check that the Gist is public, `pages/lookup.txt` contains its correct URL, and GitHub's unauthenticated API has not rate-limited requests.
- **A tunnel URL changed:** Quick Tunnel addresses are temporary. Use the latest final URL printed by the launcher or shown in the updated Gist.
- **Ctrl+C:** the launcher attempts to stop the proxy and Cloudflared processes it started.

## MIT License
Do anything, just don't be evil. 