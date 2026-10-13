#!/usr/bin/env python3
"""Apply this project's small local patch to a freshly-cloned Replica checkout.

Replica is fetched as a plain `git clone` (see setup.sh/setup.ps1) rather than
vendored into this repo, so a change made directly inside Replica/ would be
lost whenever that directory is deleted and re-cloned. This script makes the
change reproducible for anyone running setup from a clean checkout, without
forking Replica or committing its source here. It is idempotent: running it
again on an already-patched checkout does nothing.

What it adds
------------
Two optional environment variables, both read by Replica at startup and both
set by this project's launcher (main.py) from its CLI flags:

  UPSTREAM_USER_AGENT   A literal User-Agent string. When set, every request
                        Replica makes upstream carries exactly this
                        User-Agent header, replacing whatever the visitor's
                        own browser sent. (Stock Replica forwards the
                        visitor's User-Agent unchanged, so an origin that
                        rejects unusual or bot-like UA strings would reject
                        some visitors and not others.) The TLS/HTTP2
                        fingerprint profile is derived from this string with
                        the same chrome/firefox rule Replica already uses, so
                        the header and the fingerprint stay consistent.

  IMPERSONATE_PROFILE   Optional advanced override: force a specific curl_cffi
                        impersonation profile (chrome, firefox, safari, ...)
                        instead of deriving it.

With neither set, Replica behaves exactly as upstream.
"""
import sys
from pathlib import Path

REPLICA_DIR = Path(__file__).resolve().parent.parent / "Replica" / "replica"

# Presence of this exact string in proxy.py is how main.py (and this script)
# know the patch is already applied. Bump the version if the patch changes.
MARKER = "# --- Surface Tor patch v2: upstream user-agent ---"

CONFIG_ADDITION = f'''
{MARKER}
# Optional upstream User-Agent / TLS-profile overrides. Set by the Surface Tor
# launcher from -u/--user-agent and --impersonate; empty means "unchanged
# upstream behavior".
settings.UPSTREAM_USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", "").strip()
settings.IMPERSONATE_PROFILE = os.getenv("IMPERSONATE_PROFILE", "").strip()
'''

PROXY_OLD = (
    '    ua = request.headers.get("user-agent", "")\n'
    '    impersonate = "firefox" if "firefox" in ua.lower() else "chrome"'
)
PROXY_NEW = (
    f'    {MARKER}\n'
    '    forced_ua = getattr(settings, "UPSTREAM_USER_AGENT", "")\n'
    '    if forced_ua:\n'
    '        for _name in [h for h in request_headers if h.lower() == "user-agent"]:\n'
    '            request_headers.pop(_name)\n'
    '        request_headers["user-agent"] = forced_ua\n'
    '    ua = forced_ua or request.headers.get("user-agent", "")\n'
    '    forced_profile = getattr(settings, "IMPERSONATE_PROFILE", "")\n'
    '    impersonate = forced_profile or ("firefox" if "firefox" in ua.lower() else "chrome")'
)


def patch_config() -> bool:
    path = REPLICA_DIR / "config.py"
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    path.write_text(text.rstrip("\n") + "\n" + CONFIG_ADDITION, encoding="utf-8")
    return True


def patch_proxy() -> bool:
    path = REPLICA_DIR / "proxy.py"
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    if PROXY_OLD not in text:
        print(
            f"[patch_replica] WARNING: expected code not found in {path}. Replica may have "
            "changed upstream, so the -u/--user-agent patch was NOT applied. This script "
            "needs updating to match the new Replica source.",
            file=sys.stderr,
        )
        return False
    path.write_text(text.replace(PROXY_OLD, PROXY_NEW, 1), encoding="utf-8")
    return True


def main() -> int:
    if not REPLICA_DIR.exists():
        print(f"[patch_replica] {REPLICA_DIR} not found — clone Replica first (see setup.sh / setup.ps1).", file=sys.stderr)
        return 1
    # Patch proxy.py first: if its expected code has moved we bail out before
    # touching config.py, so we never leave Replica half-patched.
    proxy_changed = patch_proxy()
    proxy_text = (REPLICA_DIR / "proxy.py").read_text(encoding="utf-8")
    if MARKER not in proxy_text:
        return 2
    config_changed = patch_config()
    if proxy_changed or config_changed:
        print("[patch_replica] Applied upstream user-agent patch to Replica.")
    else:
        print("[patch_replica] Already applied, nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
