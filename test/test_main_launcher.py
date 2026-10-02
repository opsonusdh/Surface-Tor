import importlib.util
import json
from pathlib import Path

module_path = Path(__file__).with_name("main.py")
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

    assert "FROM example.com" in output
    assert "TO treasures-reaching.trycloudflare.com" in output
    assert "FROM treasures-reaching.trycloudflare.com" in output
    assert "TO dialog-apartment.trycloudflare.com" in output
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
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description"))
    chain = [{"public_url": "https://a.example", "local_port": 8000, "target": "https://target.example", "origin_target": "https://target.example", "timestamp": "now"}]
    record = main_module.serialize_tunnel_state(chain)
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

    assert main_module.fetch_site_metadata("https://example.com") == ("Example & Co", "A useful site.")


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
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Target title", "Target description"))

    loaded = main_module.load_local_state(state_path)
    chain = [{"local_port": 8000, "public_url": "https://b.example", "target": "https://target.example", "origin_target": "https://target.example", "timestamp": "now"}]
    reused = main_module.persist_tunnel_state(chain, local_path=state_path, gist_token="token123", gist_id=loaded.get("gist_id"))

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

    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Target title", "Target description"))
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
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description"))
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
    monkeypatch.setattr(main_module, "fetch_site_metadata", lambda target: ("Example title", "Example description"))

    def fake_create(token, description, file_name, content, public=False):
        created.append({"public": public, "content": json.loads(content)})
        return {"id": "public-id"}

    monkeypatch.setattr(main_module, "create_gist", fake_create)
    chain = [{"local_port": 8000, "target": "https://example.com", "origin_target": "https://example.com", "public_url": "https://final.example"}]

    gist_id = main_module.persist_tunnel_state(chain, local_path=path, gist_token="test-token")
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert gist_id == "public-id"
    assert saved["gist_id"] == "public-id"
    assert created[0]["public"] is True
    assert "other" in created[0]["content"]["records"]


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
    monkeypatch.setattr(main_module, "start_tunnel_for_port", lambda host, port, metrics_port, binary: (replacement_tunnel, "https://replacement-layer2.example"))
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
    monkeypatch.setattr(main_module, "start_tunnel_for_port", lambda host, port, metrics_port, binary: (replacement_tunnel, "https://replacement-final.example"))

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
