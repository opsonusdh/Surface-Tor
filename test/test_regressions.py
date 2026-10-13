"""Regression tests: one (or more) per bug fixed in the launcher.

Each test is written so that it fails against the previous behaviour.
"""
import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest
import requests

module_path = Path(__file__).parent.parent / "main.py"
spec = importlib.util.spec_from_file_location("surface_tor_main_regressions", module_path)
main_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main_module)


CHAIN = [{
    "local_port": 8000,
    "target": "https://example.com",
    "origin_target": "https://example.com",
    "public_url": "https://final.trycloudflare.com",
}]


@pytest.fixture(autouse=True)
def _no_real_metadata(monkeypatch):
    monkeypatch.setattr(main_module, "fetch_site_metadata",
                        lambda target, user_agent=None: ("Example", "A description", "metadata", "metadata"))


# ---------------------------------------------------------------------------
# Tunnel URL detection: a failed provisioning must never look like a tunnel
# ---------------------------------------------------------------------------

# Real stderr captured from cloudflared 2026.9.3 when it cannot reach Cloudflare.
REAL_FAILURE_LOG = (
    '2026-10-09T06:46:26Z INF Requesting new quick Tunnel on trycloudflare.com...\n'
    'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
    'dial tcp: lookup api.trycloudflare.com on 8.8.8.8:53: no such host\n'
)


def test_cloudflare_control_plane_hosts_are_never_reported_as_the_tunnel(tmp_path):
    log = tmp_path / "cloudflared.stderr"
    log.write_text(REAL_FAILURE_LOG, encoding="utf-8")
    assert main_module.extract_tunnel_url_from_stderr(log) is None


def test_real_tunnel_url_is_found_even_after_control_plane_mentions(tmp_path):
    log = tmp_path / "cloudflared.stderr"
    log.write_text(
        REAL_FAILURE_LOG
        + "INF |  https://quiet-river-demo.trycloudflare.com  |\n"
        + "INF auth at https://login.trycloudflare.com/authorize\n",
        encoding="utf-8",
    )
    assert main_module.extract_tunnel_url_from_stderr(log) == "https://quiet-river-demo.trycloudflare.com"


def test_start_tunnel_reports_no_url_when_cloudflared_fails_to_provision(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "script_dir", tmp_path)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    class ExitedProc:
        def poll(self):
            return 1

    def fake_popen(cmd, **kwargs):
        kwargs["stderr"].write(REAL_FAILURE_LOG)
        kwargs["stderr"].flush()
        return ExitedProc()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    proc, url = main_module.start_tunnel_for_port("127.0.0.1", 8000, 18000, "/fake/cloudflared")
    assert url is None


# ---------------------------------------------------------------------------
# Gist handling
# ---------------------------------------------------------------------------

def test_a_transient_gist_failure_keeps_the_existing_gist_id(tmp_path, monkeypatch):
    """Used to reset gist_id to None, so the next sync created a brand-new Gist
    and orphaned the one the published search page points at."""
    state = tmp_path / main_module.GIST_FILENAME
    state.write_text(json.dumps({"gist_id": "ORIGINAL123", "records": {}, "target_index": {}}), encoding="utf-8")

    calls = []
    stored = {"records": {}, "target_index": {}}
    monkeypatch.setattr(main_module, "get_gist", lambda token, gid: {
        "public": True, "files": {main_module.GIST_FILENAME: {"content": json.dumps(stored)}}})

    def flaky_edit(token, gid, description=None, files=None):
        calls.append(("edit", gid))
        if len([c for c in calls if c[0] == "edit"]) == 1:
            raise requests.ConnectionError("502 from api.github.com")
        return {"id": gid}

    monkeypatch.setattr(main_module, "edit_gist", flaky_edit)
    monkeypatch.setattr(main_module, "create_gist",
                        lambda *a, **k: calls.append(("create",)) or {"id": "SHOULD-NOT-HAPPEN"})

    gid, _ = main_module.persist_tunnel_state(CHAIN, local_path=state, gist_token="t", gist_id="ORIGINAL123")
    assert gid == "ORIGINAL123"
    assert json.loads(state.read_text())["gist_id"] == "ORIGINAL123"

    gid, _ = main_module.persist_tunnel_state(CHAIN, local_path=state, gist_token="t", gist_id=gid)
    assert gid == "ORIGINAL123"
    assert ("create",) not in calls
    assert [c for c in calls if c[0] == "edit"] == [("edit", "ORIGINAL123"), ("edit", "ORIGINAL123")]


def test_failing_to_read_the_gist_does_not_stop_the_local_save(tmp_path, monkeypatch):
    """get_gist() used to sit outside any try/except, so a network error
    crashed the save and the local file was never written."""
    state = tmp_path / main_module.GIST_FILENAME
    state.write_text(json.dumps({"gist_id": "abc123", "records": {}, "target_index": {}}), encoding="utf-8")

    def boom(token, gid):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(main_module, "get_gist", boom)
    monkeypatch.setattr(main_module, "edit_gist", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("still down")))

    gid, _ = main_module.persist_tunnel_state(CHAIN, local_path=state, gist_token="t")

    saved = json.loads(state.read_text())
    assert gid == "abc123"
    assert saved["gist_id"] == "abc123"
    assert [r["url"] for r in saved["records"].values()] == ["https://final.trycloudflare.com"]


def test_target_index_is_kept_locally_but_never_published(tmp_path, monkeypatch):
    state = tmp_path / main_module.GIST_FILENAME
    uploaded = {}

    def fake_create(token, description, file_name, content, public=True):
        uploaded.update(json.loads(content))
        return {"id": "NEWGIST1"}

    monkeypatch.setattr(main_module, "create_gist", fake_create)
    main_module.persist_tunnel_state(CHAIN, local_path=state, gist_token="t")

    assert "target_index" not in uploaded
    assert "https://example.com" not in json.dumps(uploaded)   # the origin URL appears nowhere in what is published
    local = json.loads(state.read_text())
    assert list(local["target_index"]) == ["https://example.com"]


def test_a_legacy_gist_that_still_publishes_target_index_is_cleaned_on_next_sync(tmp_path, monkeypatch):
    state = tmp_path / main_module.GIST_FILENAME
    legacy = {"records": {"k1": {"name": "n", "desc": "", "url": "https://old.example"}},
              "target_index": {"https://example.com": "k1"}}
    uploaded = {}
    monkeypatch.setattr(main_module, "get_gist", lambda t, g: {
        "public": True, "files": {main_module.GIST_FILENAME: {"content": json.dumps(legacy)}}})
    monkeypatch.setattr(main_module, "edit_gist", lambda t, g, description=None, files=None: (
        uploaded.update(json.loads(files[main_module.GIST_FILENAME]["content"])) or {"id": g}))

    main_module.persist_tunnel_state(CHAIN, local_path=state, gist_token="t", gist_id="legacy123")

    assert "target_index" not in uploaded
    assert "k1" not in uploaded["records"]                       # old record for this origin was replaced
    assert json.loads(state.read_text())["target_index"]["https://example.com"] in uploaded["records"]


# ---------------------------------------------------------------------------
# Secrets must not reach child processes
# ---------------------------------------------------------------------------

def test_github_token_never_reaches_the_proxy_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert "GITHUB_TOKEN" not in main_module.build_proxy_environment("https://example.com")
    assert "GITHUB_TOKEN" not in main_module._sanitized_child_env()


def test_github_token_is_not_passed_to_cloudflared(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(main_module, "script_dir", tmp_path)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})
    seen = {}

    class ExitedProc:
        def poll(self):
            return 0

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return ExitedProc()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    main_module.start_tunnel_for_port("127.0.0.1", 8000, 18000, "/fake/cloudflared")

    assert seen["env"] is not None            # an explicit, sanitized env, not an inherited one
    assert "GITHUB_TOKEN" not in seen["env"]


def test_proxy_user_agent_settings_come_only_from_the_cli(monkeypatch):
    monkeypatch.setenv("UPSTREAM_USER_AGENT", "STALE-FROM-SHELL")
    monkeypatch.setenv("IMPERSONATE_PROFILE", "STALE")
    plain = main_module.build_proxy_environment("https://example.com")
    assert "UPSTREAM_USER_AGENT" not in plain and "IMPERSONATE_PROFILE" not in plain

    pinned = main_module.build_proxy_environment("https://example.com", user_agent="CustomBot/1.0", impersonate="firefox")
    assert pinned["UPSTREAM_USER_AGENT"] == "CustomBot/1.0"
    assert pinned["IMPERSONATE_PROFILE"] == "firefox"


# ---------------------------------------------------------------------------
# Atomic state writes
# ---------------------------------------------------------------------------

def test_save_json_leaves_no_temp_files_behind(tmp_path):
    main_module.save_json(tmp_path / "state.json", {"a": 1})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_failed_save_keeps_the_previous_file_intact_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"gist_id": "KEEPME"}), encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(main_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        main_module.save_json(target, {"gist_id": "NEW"})

    assert json.loads(target.read_text())["gist_id"] == "KEEPME"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# ---------------------------------------------------------------------------
# Page metadata decoding
# ---------------------------------------------------------------------------

BENGALI = "বাংলা সাহিত্য — Café"


def test_utf8_pages_without_a_charset_header_decode_correctly():
    raw = f"<title>{BENGALI}</title>".encode("utf-8")
    assert BENGALI in main_module._decode_html_bytes(raw, "text/html")


def test_an_explicit_header_charset_is_honoured():
    raw = "<title>Café</title>".encode("latin-1")
    assert "Café" in main_module._decode_html_bytes(raw, "text/html; charset=ISO-8859-1")


def test_a_meta_declared_charset_is_used_for_legacy_pages():
    raw = '<meta charset="iso-8859-1"><title>Café Münchën</title>'.encode("latin-1")
    assert "Café Münchën" in main_module._decode_html_bytes(raw, "text/html")


def test_undecodable_bytes_never_raise():
    assert "ok" in main_module._decode_html_bytes(b"<title>ok \xff\xfe</title>", "text/html")


class _FakeStreamResponse:
    """Mimics a streamed requests.Response whose body has been consumed."""

    def __init__(self, body: bytes, content_type: str):
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield self._body

    @property
    def content(self):  # what real requests does after iter_content() has run
        raise RuntimeError("The content for this response was already consumed")

    @property
    def apparent_encoding(self):
        return self.content

    def close(self):
        pass


def test_fetching_metadata_never_touches_the_consumed_response_body(monkeypatch):
    """A first version of the charset fix read response.apparent_encoding after
    streaming, which raises RuntimeError. It must work from the bytes in hand."""
    monkeypatch.undo()  # drop the autouse fetch_site_metadata stub for this test
    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)
    body = f"<html><head><title>{BENGALI}</title><meta name='description' content='ঠিক আছে'></head></html>".encode("utf-8")
    monkeypatch.setattr(main_module.requests, "get", lambda *a, **k: _FakeStreamResponse(body, "text/html"))

    name, desc, name_src, desc_src = main_module.fetch_site_metadata("https://example.com")

    assert name == BENGALI
    assert desc == "ঠিক আছে"
    assert (name_src, desc_src) == ("metadata", "metadata")


def test_inline_svg_titles_do_not_leak_into_the_site_name():
    parser = main_module._SiteMetadataParser()
    parser.feed("<html><head><title>My Site</title></head><body>"
                "<svg><title>Search icon</title></svg><svg><title>Menu icon</title></svg></body></html>")
    assert " ".join(" ".join(parser.title_parts).split()) == "My Site"


# ---------------------------------------------------------------------------
# main(): argument handling and start-up checks
# ---------------------------------------------------------------------------

class _Proc:
    def poll(self):
        return None


def _run_main(monkeypatch, argv, *, ports_busy=False, patched=True, replica_present=True):
    """Run main() with processes/network replaced; stop after start-up."""
    started = {"proxies": [], "persist": []}

    def fake_proxy(host, port, target, user_agent=None, impersonate=None):
        started["proxies"].append({"host": host, "port": port, "target": target,
                                   "user_agent": user_agent, "impersonate": impersonate})
        return _Proc()

    def fake_persist(records, **kwargs):
        started["persist"].append({"records": records, **kwargs})
        return "GID123456", {}

    def stop_after_startup(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    monkeypatch.setattr(main_module, "install_shutdown_handlers", lambda: None)
    monkeypatch.setattr(main_module, "start_reverse_proxy", fake_proxy)
    monkeypatch.setattr(main_module, "resolve_cloudflared_path", lambda: "/fake/cloudflared")
    monkeypatch.setattr(main_module, "start_tunnel_for_port",
                        lambda host, port, metrics_port, binary, protocol=None: (_Proc(), f"https://node{port}.trycloudflare.com"))
    monkeypatch.setattr(main_module, "persist_tunnel_state", fake_persist)
    monkeypatch.setattr(main_module, "load_local_state", lambda path=None: {})
    monkeypatch.setattr(main_module, "cleanup_stderr_logs", lambda: None)
    monkeypatch.setattr(main_module, "ensure_replica_patched", lambda: patched)
    if replica_present:
        # these tests are about the launcher's logic, not about whether Replica was cloned here
        monkeypatch.setattr(main_module, "require_replica_dir", lambda: None)
    if not ports_busy:
        monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=3.0: False)
    monkeypatch.setattr(main_module.time, "sleep", stop_after_startup)
    return started


def test_main_rejects_a_chain_of_zero_nodes(monkeypatch):
    started = _run_main(monkeypatch, ["-n", "0"])
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2
    assert started["proxies"] == []


def test_main_binds_to_loopback_by_default(monkeypatch):
    started = _run_main(monkeypatch, ["-n", "1"])
    with pytest.raises(SystemExit):
        main_module.main()
    assert started["proxies"][0]["host"] == "127.0.0.1"


def test_main_refuses_a_port_held_by_a_non_http_listener(monkeypatch):
    """The old HTTP-based pre-check treated any non-HTTP listener as free."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy_port = listener.getsockname()[1]
    try:
        started = _run_main(monkeypatch, ["-n", "1", "-p", str(busy_port)], ports_busy=True)
        with pytest.raises(ValueError, match="already in use"):
            main_module.main()
        assert started["proxies"] == []
    finally:
        listener.close()


@pytest.mark.parametrize("bad", ["https://exa mple.com", "not a url", "https://bad<host>.com", "ftp://example.com", "https://"])
def test_canonicalize_rejects_targets_that_cannot_be_real_urls(bad):
    with pytest.raises(ValueError):
        main_module.canonicalize_site_target(bad)


@pytest.mark.parametrize("good, expected", [
    ("https://Example.com/", "https://example.com"),
    ("example.com", "https://example.com"),
    ("http://example.com:8080/path?q=1", "http://example.com:8080"),
    ("https://example.com:443", "https://example.com"),
    ("https://abc-def.trycloudflare.com", "https://abc-def.trycloudflare.com"),
])
def test_canonicalize_still_accepts_normal_targets(good, expected):
    assert main_module.canonicalize_site_target(good) == expected


def test_the_title_lookup_sends_the_pinned_user_agent(monkeypatch):
    """If -u is set because the site rejects unfamiliar agents, the launcher's own
    title/description lookup must not announce itself with a different one."""
    monkeypatch.undo()
    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)
    seen = []

    def fake_get(url, headers=None, **kwargs):
        seen.append(headers["User-Agent"])
        return _FakeStreamResponse(b"<title>Hi</title>", "text/html; charset=utf-8")

    monkeypatch.setattr(main_module.requests, "get", fake_get)
    main_module.fetch_site_metadata("https://example.com", user_agent="Mozilla/5.0 Pinned")
    main_module.fetch_site_metadata("https://example.com")
    assert seen == ["Mozilla/5.0 Pinned", "SurfaceTor/1.0"]


def test_persisting_state_hands_the_user_agent_to_the_title_lookup(tmp_path, monkeypatch):
    got = {}

    def spy(target, user_agent=None):
        got["user_agent"] = user_agent
        return ("Example", "Desc", "metadata", "metadata")

    monkeypatch.setattr(main_module, "fetch_site_metadata", spy)
    main_module.persist_tunnel_state(CHAIN, local_path=tmp_path / "s.json", user_agent="Mozilla/5.0 Pinned")
    assert got["user_agent"] == "Mozilla/5.0 Pinned"
