import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

module_path = Path(__file__).parent.parent / "main.py"
spec = importlib.util.spec_from_file_location("surface_tor_main", module_path)
main_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main_module)


def test_resolve_probe_host_uses_loopback_for_bind_all():
    assert main_module.resolve_probe_host("0.0.0.0") == "127.0.0.1"
    assert main_module.resolve_probe_host("::") == "127.0.0.1"
    assert main_module.resolve_probe_host("127.0.0.1") == "127.0.0.1"


def test_build_proxy_environment_keeps_target_origin():
    env = main_module.build_proxy_environment("https://example.com")
    assert env["TARGET_ORIGIN"] == "https://example.com"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_format_routing_table_shows_colored_origin_to_final_chain():
    output = main_module.format_routing_table({
        "node_8001": {
            "local_port": 8001,
            "public_url": "https://dialog-apartment.trycloudflare.com",
        },
        "node_8000": {
            "local_port": 8000,
            "origin_target": "https://example.com",
            "public_url": "https://treasures-reaching.trycloudflare.com",
            "proxy_proc": object(),
        },
    })

    assert "example.com" in output
    assert "treasures-reaching.trycloudflare.com" in output
    assert "dialog-apartment.trycloudflare.com" in output
    assert "Final URL: https://dialog-apartment.trycloudflare.com" in output
    assert "Popen" not in output
    assert output.index("node_8000") < output.index("node_8001")


def test_cleanup_started_processes_stops_every_child():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    main_module._started_processes[:] = [proc]

    main_module.cleanup_started_processes()

    assert proc.poll() is not None
    assert main_module._started_processes == []


def test_site_key_hashes_target_and_final_url(monkeypatch):
    key0 = main_module.make_seeded_key(0, 0)
    key1 = main_module.make_seeded_key(0, 1)

    assert key0 != key1
    assert len(key0) == 64
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description", "metadata", "metadata"))
    chain = [{"public_url": "https://a.example", "local_port": 8000, "target": "https://target.example", "origin_target": "https://target.example", "timestamp": "now"}]
    record, _ = main_module.serialize_tunnel_state(chain)
    site_key = main_module.make_site_key("https://target.example", "https://a.example")
    assert list(record) == [site_key]
    assert record[site_key] == {"name": "Example title", "desc": "Example description", "url": "https://a.example"}
    assert site_key != main_module.make_site_key("https://other.example", "https://a.example")
    assert site_key != main_module.make_site_key("https://target.example", "https://new-a.example")


def test_fetch_site_metadata_extracts_title_and_meta_description(monkeypatch):
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"<html><head><title>Example &amp; Co</title><meta name='description' content=' A useful site. '></head></html>"

        def close(self):
            return None

    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)
    monkeypatch.setattr(main_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    name, desc, name_src, desc_src = main_module.fetch_site_metadata("https://example.com")
    assert name == "Example & Co"
    assert desc == "A useful site."
    assert name_src == "metadata"
    assert desc_src == "metadata"


def test_surface_state_reuses_existing_gist_id_and_filename(tmp_path, monkeypatch):
    state_path = tmp_path / main_module.GIST_FILENAME
    state_path.write_text(json.dumps({"gist_id": "existing-123", "records": {}, "target_index": {}}), encoding="utf-8")

    calls = []
    remote_state = {
        "gist_id": "existing-123",
        "records": {
            "old-target-key": {"name": "Old title", "desc": "old", "url": "https://old-final.example"},
            "other-site-key": {"name": "Other site", "desc": "keep me", "url": "https://other.example"},
        },
        "target_index": {
            "https://target.example": "old-target-key",
            "https://other.example": "other-site-key",
        },
    }
    uploaded = {}

    def fake_get(*args, **kwargs):
        return {"public": True, "files": {main_module.GIST_FILENAME: {"content": json.dumps(remote_state)}}}

    def fake_edit(*args, **kwargs):
        calls.append("edit")
        uploaded.update(json.loads(kwargs["files"][main_module.GIST_FILENAME]["content"]))
        return {"id": "existing-123"}

    monkeypatch.setattr(main_module, "get_gist", fake_get)
    monkeypatch.setattr(main_module, "edit_gist", fake_edit)
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Target title", "Target description", "metadata", "metadata"))

    loaded = main_module.load_local_state(state_path)
    chain = [{"local_port": 8000, "public_url": "https://b.example", "target": "https://target.example", "origin_target": "https://target.example", "timestamp": "now"}]
    reused, _ = main_module.persist_tunnel_state(chain, local_path=state_path, gist_token="token123", gist_id=loaded.get("gist_id"))

    assert reused == "existing-123"
    assert calls == ["edit"]
    assert state_path.name == main_module.GIST_FILENAME
    new_key = main_module.make_site_key("https://target.example", "https://b.example")
    assert "old-target-key" not in uploaded["records"]
    assert uploaded["records"][new_key] == {"name": "Target title", "desc": "Target description", "url": "https://b.example"}
    assert uploaded["records"]["other-site-key"] == remote_state["records"]["other-site-key"]
    assert uploaded["target_index"]["https://target.example"] == new_key


def test_persist_tunnel_state_keeps_only_valid_public_urls(tmp_path, monkeypatch):
    path = tmp_path / main_module.GIST_FILENAME
    records = [
        {"local_port": 8000, "public_url": "https://good.example", "target": "https://target.example", "timestamp": "now"},
        {"local_port": 8001, "public_url": "", "target": "https://target.example", "timestamp": "old"},
        {"local_port": 8002, "public_url": "https://good.example", "target": "https://target.example", "timestamp": "duplicate"},
        {"local_port": 8003, "public_url": "https://stale.example", "target": "https://target.example", "timestamp": "stale", "healthy": False},
    ]

    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Target title", "Target description", "metadata", "metadata"))
    main_module.persist_tunnel_state(records, local_path=path, gist_token=None)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert len(saved["records"]) == 1
    assert all("url" in record and record["url"] for record in saved["records"].values())
    assert "https://stale.example" not in [r["url"] for r in saved["records"].values()]
    assert [r["url"] for r in saved["records"].values()] == ["https://good.example"]
    assert saved["records"][next(iter(saved["records"]))]["name"] == "Target title"


def test_persist_removes_legacy_chain_entries_but_keeps_other_sites(tmp_path, monkeypatch):
    path = tmp_path / main_module.GIST_FILENAME
    legacy_records = {
        "layer1": {"name": "example.com", "desc": "reverse proxy tunnel", "url": "https://old-one.example"},
        "layer2": {"name": "old-one.example", "desc": "reverse proxy tunnel", "url": "https://old-two.example"},
        "layer3": {"name": "old-two.example", "desc": "reverse proxy tunnel", "url": "https://old-final.example"},
        "other": {"name": "Other website", "desc": "keep this", "url": "https://other.example"},
    }
    path.write_text(json.dumps({"records": legacy_records}), encoding="utf-8")
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description", "metadata", "metadata"))
    chain = [
        {"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://new-one.example"},
        {"local_port": 8001, "target": "https://new-one.example", "origin_target": "https://example.com", "public_url": "https://new-final.example"},
    ]

    main_module.persist_tunnel_state(chain, local_path=path)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert set(saved["records"]) == {"other", main_module.make_site_key("https://example.com", "https://new-final.example")}
    assert saved["records"]["other"] == legacy_records["other"]


def test_private_gist_is_replaced_with_public_gist(tmp_path, monkeypatch):
    path = tmp_path / main_module.GIST_FILENAME
    path.write_text(json.dumps({"gist_id": "private-id", "records": {}, "target_index": {}}), encoding="utf-8")
    existing_state = {"records": {"other": {"name": "Other", "desc": "keep", "url": "https://other.example"}}, "target_index": {}}
    created = []

    monkeypatch.setattr(main_module, "get_gist", lambda token, gist_id: {"public": False, "files": {main_module.GIST_FILENAME: {"content": json.dumps(existing_state)}}})
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description", "metadata", "metadata"))

    def fake_create(token, description, file_name, content, public=False):
        created.append({"public": public, "content": json.loads(content)})
        return {"id": "public-id"}

    monkeypatch.setattr(main_module, "create_gist", fake_create)
    chain = [{"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://final.example"}]

    gist_id, _ = main_module.persist_tunnel_state(chain, local_path=path, gist_token="test-token")
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert gist_id == "public-id"
    assert saved["gist_id"] == "public-id"
    assert created[0]["public"] is True
    assert "other" in created[0]["content"]["records"]


def test_persist_tunnel_state_saves_local_copy_even_when_gist_api_fails(tmp_path, monkeypatch):
    """When the GitHub API returns an error (e.g. 401 invalid token), the local
    JSON file must still be saved and the function must not raise."""
    path = tmp_path / main_module.GIST_FILENAME

    # Mock create_gist to raise an HTTP error (like an invalid token would)
    import requests as req_lib

    def fake_create(token, description, file_name, content, public=False):
        raise req_lib.HTTPError("401 Client Error: Unauthorized for url: https://api.github.com/gists")

    monkeypatch.setattr(main_module, "create_gist", fake_create)
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Title", "Desc", "metadata", "metadata"))

    chain = [{"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://final.example"}]

    # Should NOT raise — local file must still be saved
    result, _ = main_module.persist_tunnel_state(chain, local_path=path, gist_token="invalid-token")
    assert result is None  # gist_id should be None after failure

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["gist_id"] is None  # reset to None on failure
    assert len(saved["records"]) == 1  # local records still saved


def test_intermediate_recovery_rebinds_next_proxy_without_restarting_its_tunnel(monkeypatch):
    stopped = []
    replacement_proxy = object()
    replacement_tunnel = object()
    records = [
        {"local_port": 8000, "public_url": "https://layer1.example", "proxy_proc": object(), "tunnel_proc": object()},
        {"local_port": 8001, "public_url": "https://layer2.example", "proxy_proc": object(), "tunnel_proc": object(), "metrics_port": 18001},
        {"local_port": 8002, "public_url": "https://layer3.example", "target": "https://layer2.example", "proxy_proc": object(), "tunnel_proc": object()},
        {"local_port": 8003, "public_url": "https://layer4.example", "target": "https://layer3.example", "proxy_proc": object(), "tunnel_proc": object()},
    ]
    old_failing_tunnel = records[1]["tunnel_proc"]
    old_next_proxy = records[2]["proxy_proc"]
    old_next_tunnel = records[2]["tunnel_proc"]
    old_last_proxy = records[3]["proxy_proc"]
    old_last_tunnel = records[3]["tunnel_proc"]

    monkeypatch.setattr(main_module, "stop_process", lambda proc: stopped.append(proc))
    monkeypatch.setattr(main_module, "start_tunnel_for_port", lambda host, port, metrics_port, binary, proot_rootfs=None, **kwargs: (replacement_tunnel, "https://replacement-layer2.example"))
    monkeypatch.setattr(main_module, "start_reverse_proxy", lambda host, port, target: replacement_proxy)

    main_module.recover_tunnel_record(1, records, "127.0.0.1", "cloudflared")

    assert records[1]["public_url"] == "https://replacement-layer2.example"
    assert records[2]["target"] == "https://replacement-layer2.example"
    assert records[2]["tunnel_proc"] is old_next_tunnel
    assert records[3]["proxy_proc"] is old_last_proxy
    assert records[3]["tunnel_proc"] is old_last_tunnel
    assert stopped == [old_failing_tunnel, old_next_proxy]


def test_final_layer_recovery_restarts_proxy_and_tunnel(monkeypatch):
    stopped = []
    replacement_proxy = object()
    replacement_tunnel = object()
    records = [
        {"local_port": 8000, "public_url": "https://layer1.example", "proxy_proc": object(), "tunnel_proc": object()},
        {"local_port": 8001, "public_url": "https://layer2.example", "target": "https://layer1.example", "proxy_proc": object(), "tunnel_proc": object(), "metrics_port": 18001},
    ]
    final_record = records[-1]
    old_proxy = final_record["proxy_proc"]
    old_tunnel = final_record["tunnel_proc"]
    monkeypatch.setattr(main_module, "stop_process", lambda proc: stopped.append(proc))
    monkeypatch.setattr(main_module, "start_reverse_proxy", lambda host, port, target: replacement_proxy)
    monkeypatch.setattr(main_module, "start_tunnel_for_port", lambda host, port, metrics_port, binary, proot_rootfs=None, **kwargs: (replacement_tunnel, "https://replacement-final.example"))

    main_module.recover_tunnel_record(1, records, "127.0.0.1", "cloudflared")

    assert final_record["proxy_proc"] is replacement_proxy
    assert final_record["tunnel_proc"] is replacement_tunnel
    assert final_record["public_url"] == "https://replacement-final.example"
    assert stopped[:2] == [old_tunnel, old_proxy]


def test_save_json_writes_json_file(tmp_path):
    file_path = tmp_path / "config.json"

    saved = main_module.save_json(file_path, {"name": "demo", "count": 2})

    assert saved == file_path
    assert json.loads(file_path.read_text(encoding="utf-8")) == {"name": "demo", "count": 2}


def test_create_and_edit_gist_calls_github_api(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
        return FakeResponse({"html_url": "https://gist.github.com/abc123"})

    def fake_patch(url, headers=None, json=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
        return FakeResponse({"html_url": "https://gist.github.com/abc123"})

    monkeypatch.setattr(main_module.requests, "post", fake_post)
    monkeypatch.setattr(main_module.requests, "patch", fake_patch)

    created = main_module.create_gist("token123", "demo", "payload.json", "{\"ok\": true}")
    updated = main_module.edit_gist("token123", "abc123", "demo", {"payload.json": {"content": "{\"ok\": false}"}})

    assert created["html_url"] == "https://gist.github.com/abc123"
    assert updated["html_url"] == "https://gist.github.com/abc123"
    assert calls[0]["headers"]["Authorization"] == "token token123"
    assert calls[1]["headers"]["Authorization"] == "token token123"
    assert calls[0]["json"]["public"] is True


class _FakeProc:
    """Minimal stand-in for subprocess.Popen used in health-check tests."""
    def __init__(self, alive=True):
        self._alive = alive
    def poll(self):
        return None if self._alive else 0


def test_tunnel_is_healthy_proxy_port_open_and_procs_alive(monkeypatch):
    """When both proxy and tunnel processes are alive and the local port is
    open, the tunnel is healthy — regardless of upstream HTTP status."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: True)
    record = {
        "local_port": 9999,
        "public_url": "https://tunnel.example",
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=True),
        "tunnel_proc": _FakeProc(alive=True),
    }
    assert main_module.tunnel_is_healthy(record) is True


def test_tunnel_is_healthy_proxy_port_closed_is_unhealthy(monkeypatch):
    """When the local proxy port is not open (process died or crashed),
    the tunnel is unhealthy — even if a public_url exists."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: False)
    record = {
        "local_port": 9999,
        "public_url": "https://tunnel.example",
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=True),
        "tunnel_proc": _FakeProc(alive=True),
    }
    assert main_module.tunnel_is_healthy(record) is False


def test_tunnel_is_healthy_proxy_proc_exited_is_unhealthy(monkeypatch):
    """When the proxy process has exited, the tunnel is unhealthy."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: True)
    record = {
        "local_port": 9999,
        "public_url": "https://tunnel.example",
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=False),
        "tunnel_proc": _FakeProc(alive=True),
    }
    assert main_module.tunnel_is_healthy(record) is False


def test_tunnel_is_healthy_tunnel_proc_exited_is_unhealthy(monkeypatch):
    """When the cloudflared tunnel process has exited, the tunnel is unhealthy."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: True)
    record = {
        "local_port": 9999,
        "public_url": "https://tunnel.example",
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=True),
        "tunnel_proc": _FakeProc(alive=False),
    }
    assert main_module.tunnel_is_healthy(record) is False


def test_tunnel_is_healthy_no_public_url_is_unhealthy(monkeypatch):
    """If there is no public_url the tunnel is unhealthy (never came up)."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: True)
    record = {
        "local_port": 9999,
        "public_url": None,
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=True),
        "tunnel_proc": _FakeProc(alive=True),
    }
    assert main_module.tunnel_is_healthy(record) is False


def test_tunnel_is_healthy_502_from_upstream_is_still_healthy(monkeypatch):
    """Core regression: upstream returning 502 (target unreachable) must NOT
    trigger a tunnel restart.  The health check uses socket + process state,
    not HTTP status codes, so a 502 is irrelevant."""
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=None: True)
    # Even if the local proxy responds with 502, the health check never
    # looks at HTTP status — it only checks socket + process liveness.
    record = {
        "local_port": 9999,
        "public_url": "https://tunnel.example",
        "host": "127.0.0.1",
        "proxy_proc": _FakeProc(alive=True),
        "tunnel_proc": _FakeProc(alive=True),
    }
    assert main_module.tunnel_is_healthy(record) is True


# ---------------------------------------------------------------------------
# Tests for DNS/proot workaround
# ---------------------------------------------------------------------------

def test_start_tunnel_uses_resolve_probe_host_for_url(monkeypatch):
    """The --url passed to cloudflared must use a connectable address
    (127.0.0.1), not the raw bind host (0.0.0.0)."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class FakeProc:
            def poll(self):
                return 0  # exits immediately so we don't wait

        return FakeProc()

    # Redirect script_dir to a temp workspace so stderr logs go there
    tmpdir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(main_module, "script_dir", tmpdir)
    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    try:
        main_module.start_tunnel_for_port("0.0.0.0", 8000, 18000, "/fake/cloudflared", proot_rootfs=None)
        # The --url should use 127.0.0.1, not 0.0.0.0
        assert "http://127.0.0.1:8000" in captured["cmd"]
        assert "http://0.0.0.0:8000" not in " ".join(captured["cmd"])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_dns_resolution_broken_detects_missing_resolv_conf():
    """_dns_resolution_broken should return True when /etc/resolv.conf is missing."""
    result = main_module._dns_resolution_broken()
    # On this system (Termux), /etc/resolv.conf is indeed missing
    assert result is True


def test_prepare_proot_rootfs_creates_resolv_conf(monkeypatch):
    """prepare_proot_rootfs should create a rootfs with working DNS config."""
    monkeypatch.setattr(main_module, "_proot_rootfs", None)
    monkeypatch.setattr(main_module, "_dns_resolution_broken", lambda: True)
    monkeypatch.setattr(main_module, "resolve_proot_path", lambda: "/fake/proot")

    tmpdir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(main_module, "script_dir", tmpdir)

    try:
        rootfs = main_module.prepare_proot_rootfs()
        assert rootfs is not None
        resolv_conf = rootfs / "etc" / "resolv.conf"
        assert resolv_conf.exists()
        assert "nameserver" in resolv_conf.read_text()

        # Should be cached (returns same rootfs)
        rootfs2 = main_module.prepare_proot_rootfs()
        assert rootfs2 == rootfs
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        main_module._proot_rootfs = None


def test_prepare_proot_rootfs_returns_none_when_dns_works(monkeypatch):
    """When /etc/resolv.conf exists, no proot rootfs is needed."""
    monkeypatch.setattr(main_module, "_proot_rootfs", None)
    monkeypatch.setattr(main_module, "_dns_resolution_broken", lambda: False)

    result = main_module.prepare_proot_rootfs()
    assert result is None


def test_build_proot_prefix_includes_rootfs_and_bindings():
    """_build_proot_prefix should include -r rootfs and necessary -b bindings."""
    tmpdir = Path(tempfile.mkdtemp())
    rootfs = tmpdir / "rootfs"
    rootfs.mkdir()
    script_dir_path = tmpdir / "project"
    script_dir_path.mkdir()

    monkeypatch_script_dir = script_dir_path

    try:
        prefix = main_module._build_proot_prefix(
            "/usr/bin/proot", rootfs, str(script_dir_path / "cloudflared")
        )

        assert "/usr/bin/proot" in prefix[0]
        assert "-r" in prefix
        assert str(rootfs) in prefix
        assert "-b" in prefix
        assert "/dev" in prefix
        assert "/proc" in prefix
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests for process-group cleanup and stale-process handling
# ---------------------------------------------------------------------------

def test_start_tunnel_passes_protocol_to_command(monkeypatch):
    """When protocol is specified, --protocol should appear in the cloudflared command."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class FakeProc:
            def poll(self):
                return 0  # exits immediately

        return FakeProc()

    tmpdir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(main_module, "script_dir", tmpdir)
    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    try:
        main_module.start_tunnel_for_port("0.0.0.0", 8000, 18000, "/fake/cloudflared",
                                          proot_rootfs=None, protocol="h2mux")
        assert "--protocol" in captured["cmd"]
        assert "h2mux" in captured["cmd"]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_start_tunnel_no_protocol_by_default(monkeypatch):
    """Without protocol, no --protocol flag should be in the command."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("FakeProc", (), {"poll": lambda self: 0})()

    tmpdir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(main_module, "script_dir", tmpdir)
    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    try:
        main_module.start_tunnel_for_port("0.0.0.0", 8000, 18000, "/fake/cloudflared",
                                          proot_rootfs=None)
        assert "--protocol" not in captured["cmd"]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_start_tunnel_uses_start_new_session(monkeypatch):
    """Processes should be started with start_new_session=True so process groups can be killed."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        class FakeProc:
            def poll(self):
                return 0
        return FakeProc()

    tmpdir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(main_module, "script_dir", tmpdir)
    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    try:
        main_module.start_tunnel_for_port("0.0.0.0", 8000, 18000, "/fake/cloudflared",
                                          proot_rootfs=None)
        assert captured["kwargs"].get("start_new_session") is True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_stop_process_kills_process_group(monkeypatch):
    """stop_process should kill the entire process group (proot + children)."""
    killpg_calls = []

    class FakeProc:
        def __init__(self):
            self._pid = 12345
        @property
        def pid(self):
            return self._pid
        def poll(self):
            return None  # still alive
        def wait(self, timeout=None):
            return 0

    proc = FakeProc()

    # Mock os.getpgid and os.killpg to verify process group kill
    monkeypatch.setattr(main_module.os, "getpgid", lambda pid: 12345)
    monkeypatch.setattr(main_module.os, "killpg",
                        lambda pgid, sig: killpg_calls.append((pgid, sig)))

    main_module.stop_process(proc)

    assert len(killpg_calls) > 0
    # First signal should be SIGTERM
    assert killpg_calls[0][1] == main_module.signal.SIGTERM


def test_cleanup_stale_cloudflared_processes_kills_orphans(monkeypatch):
    """cleanup_stale_cloudflared_processes should kill orphaned cloudflared tunnel processes."""
    killed = []

    def fake_run(*args, **kwargs):
        class FakeResult:
            stdout = "  99999 /data/data/com.termux/files/home/Surface-Tor/cloudflared tunnel --no-autoupdate --metrics 127.0.0.1:8100 --url http://127.0.0.1:8000\n"
        return FakeResult()

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)
    monkeypatch.setattr(main_module.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))

    result = main_module.cleanup_stale_cloudflared_processes()
    assert result == 1
    assert killed[0][0] == 99999
    assert killed[0][1] == main_module.signal.SIGKILL


def test_cleanup_stale_cloudflared_processes_ignores_unrelated(monkeypatch):
    """cleanup should NOT kill processes that don't match the cloudflared tunnel pattern."""
    killed = []

    def fake_run(*args, **kwargs):
        class FakeResult:
            stdout = (
                "  32252 runsv cloudflared\n"
                "  99998 python3 some_other_script.py\n"
            )
        return FakeResult()

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)
    monkeypatch.setattr(main_module.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))

    result = main_module.cleanup_stale_cloudflared_processes()
    assert result == 0
    assert len(killed) == 0


# ---------------------------------------------------------------------------
# Tests for stderr URL extraction fallback
# ---------------------------------------------------------------------------

def test_extract_tunnel_url_from_stderr_finds_banner_format(tmp_path):
    """The regex must find the URL inside cloudflared's banner output
    (the multi-line box format with | borders), as seen in error.log."""
    stderr_log = tmp_path / "cloudflared_8002.stderr"
    stderr_log.write_text(
        "2026-10-05T05:53:10Z INF +------+\n"
        "2026-10-05T05:53:10Z INF |  https://cases-daisy-manga-defendant.trycloudflare.com                                     |\n"
        "2026-10-05T05:53:10Z INF +------+\n",
        encoding="utf-8",
    )
    assert main_module.extract_tunnel_url_from_stderr(stderr_log) == \
        "https://cases-daisy-manga-defendant.trycloudflare.com"


def test_extract_tunnel_url_from_stderr_finds_inline_format(tmp_path):
    """The regex must also find the URL in the inline single-line format
    that some cloudflared versions print."""
    stderr_log = tmp_path / "cloudflared.stderr"
    stderr_log.write_text(
        "2024-01-01T00:00:00Z INF Your quick Tunnel has been created! "
        "Visit it at (it may take some time to be reachable): "
        "https://alpha-beta-gamma-delta.trycloudflare.com\n",
        encoding="utf-8",
    )
    assert main_module.extract_tunnel_url_from_stderr(stderr_log) == \
        "https://alpha-beta-gamma-delta.trycloudflare.com"


def test_extract_tunnel_url_from_stderr_returns_none_when_no_url(tmp_path):
    """When the stderr log has no trycloudflare URL, return None."""
    stderr_log = tmp_path / "cloudflared.stderr"
    stderr_log.write_text(
        "2024-01-01T00:00:00Z INF Requesting new quick Tunnel...\n"
        "2024-01-01T00:00:00Z INF Version 2024.1.1\n",
        encoding="utf-8",
    )
    assert main_module.extract_tunnel_url_from_stderr(stderr_log) is None


def test_extract_tunnel_url_from_stderr_returns_none_when_file_missing(tmp_path):
    """When the stderr log file does not exist, return None gracefully."""
    stderr_log = tmp_path / "nonexistent.stderr"
    assert main_module.extract_tunnel_url_from_stderr(stderr_log) is None


def test_extract_tunnel_url_from_stderr_finds_first_url(tmp_path):
    """When multiple URLs appear, return the first (the actual tunnel URL)."""
    stderr_log = tmp_path / "cloudflared.stderr"
    stderr_log.write_text(
        "2024-01-01T00:00:00Z INF https://first-trycloudflare.trycloudflare.com\n"
        "2024-01-01T00:00:01Z INF https://second-trycloudflare.trycloudflare.com\n",
        encoding="utf-8",
    )
    result = main_module.extract_tunnel_url_from_stderr(stderr_log)
    assert result == "https://first-trycloudflare.trycloudflare.com"


def test_start_tunnel_fallback_to_stderr_when_metrics_api_fails(monkeypatch, tmp_path):
    """When the metrics API never returns a URL, the function should still
    recover the tunnel URL from the captured stderr log via regex."""
    monkeypatch.setattr(main_module, "script_dir", tmp_path)
    monkeypatch.setattr(main_module, "_tunnel_stderr_logs", {})

    # cloudflared output that contains the URL in banner format
    stderr_content = (
        "2026-10-05T05:53:10Z INF |  https://cases-daisy-manga-defendant.trycloudflare.com                                     |\n"
    )

    class FakeProc:
        def __init__(self):
            self._poll_count = 0
        def poll(self):
            self._poll_count += 1
            return None  # always alive

    class FakeResponse:
        status_code = 503  # metrics API never returns 200

    # Stub out requests.get to simulate metrics API never responding
    monkeypatch.setattr(main_module.requests, "get", lambda *a, **kw: FakeResponse())

    # Stub subprocess.Popen to return our fake proc and write stderr
    def fake_popen(cmd, **kwargs):
        stderr_fd = kwargs["stderr"]
        stderr_fd.write(stderr_content)
        stderr_fd.flush()
        return FakeProc()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(main_module, "register_process", lambda p: None)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)  # skip sleeps

    proc, url = main_module.start_tunnel_for_port(
        "0.0.0.0", 8002, 18002, "/fake/cloudflared", proot_rootfs=None
    )

    assert url is not None
    assert url == "https://cases-daisy-manga-defendant.trycloudflare.com"
    assert proc is not None


# ---------------------------------------------------------------------------
# Tests for --name / --desc override and --github-token priority
# ---------------------------------------------------------------------------

def test_serialize_tunnel_state_uses_fetched_name_and_desc_by_default(monkeypatch):
    """Without overrides, serialize_tunnel_state should use fetch_site_metadata."""
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched Title", "Fetched Description", "metadata", "metadata"))
    records = [{"local_port": 8000, "public_url": "https://final.example", "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    result, _ = main_module.serialize_tunnel_state(records)
    key = next(iter(result))
    assert result[key]["name"] == "Fetched Title"
    assert result[key]["desc"] == "Fetched Description"


def test_serialize_tunnel_state_name_override_takes_priority(monkeypatch):
    """When name_override is provided, it replaces the fetched name."""
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched Title", "Fetched Description", "metadata", "metadata"))
    records = [{"local_port": 8000, "public_url": "https://final.example", "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    result, _ = main_module.serialize_tunnel_state(records, name_override="My Custom Name")
    key = next(iter(result))
    assert result[key]["name"] == "My Custom Name"
    assert result[key]["desc"] == "Fetched Description"  # desc still fetched


def test_serialize_tunnel_state_desc_override_takes_priority(monkeypatch):
    """When desc_override is provided, it replaces the fetched description."""
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched Title", "Fetched Description", "metadata", "metadata"))
    records = [{"local_port": 8000, "public_url": "https://final.example", "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    result, _ = main_module.serialize_tunnel_state(records, desc_override="My Custom Desc")
    key = next(iter(result))
    assert result[key]["name"] == "Fetched Title"  # name still fetched
    assert result[key]["desc"] == "My Custom Desc"


def test_serialize_tunnel_state_both_overrides(monkeypatch):
    """Both name and desc overrides should be used together."""
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched Title", "Fetched Description", "metadata", "metadata"))
    records = [{"local_port": 8000, "public_url": "https://final.example", "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    result, _ = main_module.serialize_tunnel_state(records, name_override="Custom", desc_override="Desc")
    key = next(iter(result))
    assert result[key]["name"] == "Custom"
    assert result[key]["desc"] == "Desc"


def test_persist_tunnel_state_passes_overrides_to_serialize(monkeypatch, tmp_path):
    """persist_tunnel_state should pass name_override/desc_override through to serialize_tunnel_state."""
    path = tmp_path / main_module.GIST_FILENAME
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched", "Desc", "metadata", "metadata"))

    serialize_calls = []
    original_serialize = main_module.serialize_tunnel_state

    def tracking_serialize(records, name_override=None, desc_override=None):
        serialize_calls.append({"name_override": name_override, "desc_override": desc_override})
        return original_serialize(records, name_override=name_override, desc_override=desc_override)

    monkeypatch.setattr(main_module, "serialize_tunnel_state", tracking_serialize)
    chain = [{"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://final.example"}]
    main_module.persist_tunnel_state(chain, local_path=path, name_override="Custom Name", desc_override="Custom Desc")

    assert len(serialize_calls) == 1
    assert serialize_calls[0]["name_override"] == "Custom Name"
    assert serialize_calls[0]["desc_override"] == "Custom Desc"


def test_persist_tunnel_state_no_overrides_passes_none(monkeypatch, tmp_path):
    """Without overrides, persist_tunnel_state should pass None (meaning: fetch from URL)."""
    path = tmp_path / main_module.GIST_FILENAME
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Fetched", "Desc", "metadata", "metadata"))

    serialize_calls = []
    original_serialize = main_module.serialize_tunnel_state

    def tracking_serialize(records, name_override=None, desc_override=None):
        serialize_calls.append({"name_override": name_override, "desc_override": desc_override})
        return original_serialize(records, name_override=name_override, desc_override=desc_override)

    monkeypatch.setattr(main_module, "serialize_tunnel_state", tracking_serialize)
    chain = [{"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://final.example"}]
    main_module.persist_tunnel_state(chain, local_path=path)

    assert len(serialize_calls) == 1
    assert serialize_calls[0]["name_override"] is None
    assert serialize_calls[0]["desc_override"] is None


# ---------------------------------------------------------------------------
# Tests for metadata source tracking and persist summary
# ---------------------------------------------------------------------------

def test_fetch_site_metadata_returns_domain_fallback_when_no_title(monkeypatch):
    """When there is no <title> tag, the name should fall back to the domain
    and name_source should be 'domain'."""
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"<html><head><meta name='description' content='A site without a title'></head></html>"

        def close(self):
            return None

    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)
    monkeypatch.setattr(main_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    name, desc, name_src, desc_src = main_module.fetch_site_metadata("https://example.com")
    assert name == "example.com"
    assert name_src == "domain"
    assert desc == "A site without a title"
    assert desc_src == "metadata"


def test_fetch_site_metadata_returns_unavailable_when_no_meta_description(monkeypatch):
    """When there is no <meta name='description'> tag, desc_source should be
    'unavailable' and the description should be empty."""
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"<html><head><title>A Site</title></head></html>"

        def close(self):
            return None

    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)
    monkeypatch.setattr(main_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    name, desc, name_src, desc_src = main_module.fetch_site_metadata("https://example.com")
    assert name == "A Site"
    assert name_src == "metadata"
    assert desc == ""
    assert desc_src == "unavailable"


def test_fetch_site_metadata_returns_domain_and_unavailable_on_network_error(monkeypatch):
    """On network failure, both sources should be 'domain' and 'unavailable'."""
    import requests as req_lib

    monkeypatch.setattr(main_module, "_validate_public_http_url", lambda url: None)

    def fake_get(*args, **kwargs):
        raise req_lib.RequestException("network down")

    monkeypatch.setattr(main_module.requests, "get", fake_get)

    name, desc, name_src, desc_src = main_module.fetch_site_metadata("https://example.com")
    assert name == "example.com"
    assert name_src == "domain"
    assert desc == ""
    assert desc_src == "unavailable"


def test_serialize_tunnel_state_reports_user_source_for_name_override(monkeypatch):
    """When name_override is provided, name_source in source_info should be 'user'."""
    monkeypatch.setattr(main_module, "fetch_site_metadata",
                        lambda target: ("Fetched", "", "metadata", "unavailable"))
    records = [{"local_port": 8000, "public_url": "https://final.example",
                "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    record, source_info = main_module.serialize_tunnel_state(records, name_override="Custom Name")
    assert source_info["name_source"] == "user"
    assert source_info["name"] == "Custom Name"
    # desc should still come from fetch (unavailable in this mock)
    assert source_info["desc_source"] == "unavailable"
    # the persisted record dict should also reflect the override
    key = next(iter(record))
    assert record[key]["name"] == "Custom Name"


def test_serialize_tunnel_state_reports_user_source_for_desc_override(monkeypatch):
    """When desc_override is provided, desc_source in source_info should be 'user'."""
    monkeypatch.setattr(main_module, "fetch_site_metadata",
                        lambda target: ("Fetched", "", "metadata", "unavailable"))
    records = [{"local_port": 8000, "public_url": "https://final.example",
                "target": "https://example.com", "origin_target": "https://example.com", "timestamp": "now"}]
    record, source_info = main_module.serialize_tunnel_state(records, desc_override="Custom Desc")
    assert source_info["desc_source"] == "user"
    assert source_info["desc"] == "Custom Desc"
    # name should still come from metadata
    assert source_info["name_source"] == "metadata"


def test_persist_tunnel_state_returns_source_info(tmp_path, monkeypatch):
    """persist_tunnel_state should return (gist_id, source_info) where source_info
    carries the resolved name, description, and their provenance."""
    path = tmp_path / main_module.GIST_FILENAME
    monkeypatch.setattr(main_module, "fetch_site_metadata",
                        lambda target: ("My Site", "My description", "metadata", "metadata"))
    chain = [{"local_port": 8000, "target": "https://example.com",
              "origin_target": "https://example.com", "public_url": "https://final.example"}]

    gist_id, source_info = main_module.persist_tunnel_state(chain, local_path=path)
    assert gist_id is None  # no token provided
    assert source_info["name"] == "My Site"
    assert source_info["desc"] == "My description"
    assert source_info["name_source"] == "metadata"
    assert source_info["desc_source"] == "metadata"


def test_print_persist_summary_shows_metadata_sources_and_gist_created(capsys):
    """_print_persist_summary should report the name/desc, their source, and
    indicate that a new gist was created."""
    source_info = {"name": "Example Site", "desc": "A useful site",
                   "name_source": "metadata", "desc_source": "metadata"}
    main_module._print_persist_summary(source_info, "gist-abc123", None, "token")
    captured = capsys.readouterr()
    assert "Example Site" in captured.out
    assert "A useful site" in captured.out
    assert "metadata" in captured.out
    assert "Gist created" in captured.out
    assert "gist-abc123" in captured.out


def test_print_persist_summary_shows_domain_fallback_and_no_token(capsys):
    """When name came from domain fallback and no token was provided."""
    source_info = {"name": "example.com", "desc": "",
                   "name_source": "domain", "desc_source": "unavailable"}
    main_module._print_persist_summary(source_info, None, None, None)
    captured = capsys.readouterr()
    assert "domain name fallback" in captured.out
    assert "not found on webpage" in captured.out
    assert "No GitHub token" in captured.out
    assert "gist not synced" in captured.out


def test_print_persist_summary_shows_user_override_and_gist_updated(capsys):
    """When overrides were used and an existing gist was updated."""
    source_info = {"name": "Custom", "desc": "Desc",
                   "name_source": "user", "desc_source": "user"}
    main_module._print_persist_summary(source_info, "gist-xyz", "gist-xyz", "token")
    captured = capsys.readouterr()
    assert "user-specified" in captured.out
    assert "Gist updated" in captured.out
    assert "gist-xyz" in captured.out


def test_print_persist_summary_shows_gist_recreated(capsys):
    """When the previous gist was private and a new public one was created."""
    source_info = {"name": "My Site", "desc": "Desc",
                   "name_source": "metadata", "desc_source": "metadata"}
    main_module._print_persist_summary(source_info, "new-id", "old-id", "token")
    captured = capsys.readouterr()
    assert "Gist recreated" in captured.out


def test_print_persist_summary_no_source_info_still_prints_gist_status(capsys):
    """Even with empty source_info (no valid records), gist status should print."""
    main_module._print_persist_summary({}, None, None, None)
    captured = capsys.readouterr()
    assert "No GitHub token" in captured.out
