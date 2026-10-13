"""Tests for extending a chain from another device, relay mode, the new CLI
flags (--user-agent, --impersonate, --origin, --gist-id, --no-publish) and the
Replica patch that makes --user-agent work.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

spec = importlib.util.spec_from_file_location("surface_tor_main_attach", ROOT / "main.py")
main_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main_module)

patch_spec = importlib.util.spec_from_file_location("patch_replica_under_test", ROOT / "scripts" / "patch_replica.py")
patch_module = importlib.util.module_from_spec(patch_spec)
patch_spec.loader.exec_module(patch_module)


@pytest.fixture(autouse=True)
def _no_real_metadata(monkeypatch):
    monkeypatch.setattr(main_module, "fetch_site_metadata",
                        lambda target, user_agent=None: ("Example Domain", "An example", "metadata", "metadata"))


class _FakeGistStore:
    """An in-memory stand-in for one GitHub Gist, shared by several 'devices'."""

    def __init__(self):
        self.gist_id = None
        self.content = None
        self.calls = []

    def install(self, monkeypatch):
        def create(token, description, file_name, content, public=True):
            self.calls.append("create")
            self.gist_id, self.content = "GIST0000000001", content
            return {"id": self.gist_id}

        def edit(token, gist_id, description=None, files=None):
            self.calls.append("edit")
            assert gist_id == self.gist_id
            self.content = files[main_module.GIST_FILENAME]["content"]
            return {"id": gist_id}

        def get(token, gist_id):
            assert gist_id == self.gist_id
            return {"public": True, "files": {main_module.GIST_FILENAME: {"content": self.content}}}

        monkeypatch.setattr(main_module, "create_gist", create)
        monkeypatch.setattr(main_module, "edit_gist", edit)
        monkeypatch.setattr(main_module, "get_gist", get)

    @property
    def published(self):
        return json.loads(self.content)


def _chain(origin, target, final_url):
    return [{"local_port": 8000, "target": target, "origin_target": origin, "public_url": final_url}]


# ---------------------------------------------------------------------------
# Two devices, one chain
# ---------------------------------------------------------------------------

def test_a_second_device_replaces_the_first_devices_published_url(tmp_path, monkeypatch):
    """Device A (home) publishes example.com -> abc. Device B (elsewhere) attaches
    to abc, extends it to def, and def must become the single public entry."""
    store = _FakeGistStore()
    store.install(monkeypatch)

    # Device A: ordinary run, no relay mode.
    gist_id, _ = main_module.persist_tunnel_state(
        _chain("https://example.com", "https://example.com", "https://abc.trycloudflare.com"),
        local_path=tmp_path / "a" / "surface_tor.json", gist_token="tok")
    assert [r["url"] for r in store.published["records"].values()] == ["https://abc.trycloudflare.com"]

    # Device B: brand-new machine with no local state, only the Gist ID and token.
    b_state = tmp_path / "b" / "surface_tor.json"
    main_module.persist_tunnel_state(
        _chain("https://example.com", "https://abc.trycloudflare.com", "https://def.trycloudflare.com"),
        local_path=b_state, gist_token="tok", gist_id=gist_id,
        attached_url="https://abc.trycloudflare.com")

    urls = [r["url"] for r in store.published["records"].values()]
    assert urls == ["https://def.trycloudflare.com"]          # abc is now an internal hop
    assert "target_index" not in store.published              # still never published
    assert "https://example.com" in json.loads(b_state.read_text())["target_index"]   # but kept locally


def test_attaching_never_touches_other_sites_in_the_same_gist(tmp_path, monkeypatch):
    store = _FakeGistStore()
    store.install(monkeypatch)
    gist_id, _ = main_module.persist_tunnel_state(
        _chain("https://other.example", "https://other.example", "https://other-tunnel.trycloudflare.com"),
        local_path=tmp_path / "a.json", gist_token="tok")

    main_module.persist_tunnel_state(
        _chain("https://example.com", "https://abc.trycloudflare.com", "https://def.trycloudflare.com"),
        local_path=tmp_path / "b.json", gist_token="tok", gist_id=gist_id,
        attached_url="https://abc.trycloudflare.com")

    assert sorted(r["url"] for r in store.published["records"].values()) == [
        "https://def.trycloudflare.com", "https://other-tunnel.trycloudflare.com"]


def test_a_trailing_slash_on_the_attached_url_still_matches(tmp_path, monkeypatch):
    store = _FakeGistStore()
    store.install(monkeypatch)
    gist_id, _ = main_module.persist_tunnel_state(
        _chain("https://example.com", "https://example.com", "https://abc.trycloudflare.com"),
        local_path=tmp_path / "a.json", gist_token="tok")
    main_module.persist_tunnel_state(
        _chain("https://example.com", "https://abc.trycloudflare.com/", "https://def.trycloudflare.com"),
        local_path=tmp_path / "b.json", gist_token="tok", gist_id=gist_id,
        attached_url="https://abc.trycloudflare.com/")
    assert [r["url"] for r in store.published["records"].values()] == ["https://def.trycloudflare.com"]


def test_without_attaching_the_earlier_record_is_left_alone(tmp_path, monkeypatch):
    """No --origin means no attach semantics: nothing extra is removed."""
    store = _FakeGistStore()
    store.install(monkeypatch)
    gist_id, _ = main_module.persist_tunnel_state(
        _chain("https://one.example", "https://one.example", "https://abc.trycloudflare.com"),
        local_path=tmp_path / "a.json", gist_token="tok")
    main_module.persist_tunnel_state(
        _chain("https://two.example", "https://abc.trycloudflare.com", "https://def.trycloudflare.com"),
        local_path=tmp_path / "b.json", gist_token="tok", gist_id=gist_id)
    assert len(store.published["records"]) == 2


# ---------------------------------------------------------------------------
# main(): the new flags are wired through
# ---------------------------------------------------------------------------

class _Proc:
    def poll(self):
        return None


def _run_main(monkeypatch, argv, *, patched=True, replica_present=True):
    seen = {"proxies": [], "persist": [], "github_calls": 0}

    def fake_proxy(host, port, target, user_agent=None, impersonate=None):
        seen["proxies"].append({"port": port, "target": target, "user_agent": user_agent, "impersonate": impersonate})
        return _Proc()

    def fake_persist(records, **kwargs):
        seen["persist"].append({"records": records, **kwargs})
        return "GISTID123456", {}

    def fake_requests_get(*args, **kwargs):
        seen["github_calls"] += 1
        return type("R", (), {"status_code": 200})()

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
        monkeypatch.setattr(main_module, "require_replica_dir", lambda: None)
    monkeypatch.setattr(main_module, "_is_port_open", lambda host, port, timeout=3.0: False)
    monkeypatch.setattr(main_module.requests, "get", fake_requests_get)
    monkeypatch.setattr(main_module.time, "sleep", stop_after_startup)
    return seen


def test_attach_flags_reach_the_state_layer(monkeypatch):
    seen = _run_main(monkeypatch, [
        "-t", "https://abc.trycloudflare.com", "--origin", "https://example.com",
        "--gist-id", "abcdef123456", "-g", "tok", "-n", "2", "-u", "CustomBot/1.0"])
    with pytest.raises(SystemExit):
        main_module.main()

    assert len(seen["persist"]) == 1
    call = seen["persist"][0]
    assert call["gist_id"] == "abcdef123456"
    assert call["attached_url"] == "https://abc.trycloudflare.com"
    assert call["records"][0]["origin_target"] == "https://example.com"   # the real site, for keys and metadata
    assert call["records"][0]["target"] == "https://abc.trycloudflare.com"  # what node 1 actually proxies
    # node 1 proxies the other device's URL; node 2 proxies node 1's tunnel
    assert [p["target"] for p in seen["proxies"]] == ["https://abc.trycloudflare.com", "https://node8000.trycloudflare.com"]
    assert all(p["user_agent"] == "CustomBot/1.0" for p in seen["proxies"])


def test_without_origin_nothing_is_treated_as_an_attach(monkeypatch):
    seen = _run_main(monkeypatch, ["-t", "https://example.com", "-n", "1", "-g", "tok"])
    with pytest.raises(SystemExit):
        main_module.main()
    assert seen["persist"][0]["attached_url"] is None
    assert seen["persist"][0]["records"][0]["origin_target"] == "https://example.com"


def test_relay_mode_publishes_nothing_and_never_uses_the_token(monkeypatch, capsys):
    seen = _run_main(monkeypatch, ["--no-publish", "-g", "tok", "-n", "2"])
    with pytest.raises(SystemExit):
        main_module.main()
    assert seen["persist"] == []
    assert seen["github_calls"] == 0                     # token not even validated against GitHub
    out = capsys.readouterr().out
    assert "Relay mode" in out
    assert "https://node8001.trycloudflare.com" in out   # the URL to hand to the next device


@pytest.mark.parametrize("argv", [
    ["-u", "line1\nline2"],
    ["-u", "   "],
    ["--origin", "ftp://example.com"],
    ["--origin", "not a url"],
    ["--gist-id", "https://gist.github.com/someone/abcdef123456"],
    ["--gist-id", "../users"],
])
def test_bad_flag_values_are_rejected_before_anything_starts(monkeypatch, argv):
    seen = _run_main(monkeypatch, argv)
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2
    assert seen["proxies"] == []


def test_user_agent_flag_refuses_to_run_if_replica_cannot_be_patched(monkeypatch):
    seen = _run_main(monkeypatch, ["-u", "CustomBot/1.0"], patched=False)
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2
    assert seen["proxies"] == []        # never starts a node that would silently ignore -u


def test_the_patch_is_not_required_when_no_user_agent_flag_is_used(monkeypatch):
    seen = _run_main(monkeypatch, ["-n", "1"], patched=False)
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 0
    assert len(seen["proxies"]) == 1


def test_routing_table_shows_what_the_first_node_really_proxies():
    table = main_module.format_routing_table({"node_8000": {
        "local_port": 8000, "public_url": "https://def.trycloudflare.com",
        "target": "https://abc.trycloudflare.com", "origin_target": "https://example.com"}})
    assert "abc.trycloudflare.com" in table
    assert "example.com" not in table


# ---------------------------------------------------------------------------
# The Replica patch
# ---------------------------------------------------------------------------

PRISTINE_CONFIG = "import os\n\nclass Settings:\n    pass\n\n\n# Single settings instance\nsettings = Settings()\n"
PRISTINE_PROXY = (
    "async def proxy_request(request, path):\n"
    "    request_headers = dict(request.headers)\n"
    "    ua = request.headers.get(\"user-agent\", \"\")\n"
    "    impersonate = \"firefox\" if \"firefox\" in ua.lower() else \"chrome\"\n"
    "    return request_headers.get(\"user-agent\"), impersonate\n"
)


@pytest.fixture
def fake_replica(tmp_path, monkeypatch):
    package = tmp_path / "Replica" / "replica"
    package.mkdir(parents=True)
    (package / "config.py").write_text(PRISTINE_CONFIG, encoding="utf-8")
    (package / "proxy.py").write_text(PRISTINE_PROXY, encoding="utf-8")
    monkeypatch.setattr(patch_module, "REPLICA_DIR", package)
    return package


def test_patch_applies_to_both_files_and_is_idempotent(fake_replica, capsys):
    assert patch_module.main() == 0
    assert patch_module.MARKER in (fake_replica / "config.py").read_text()
    assert patch_module.MARKER in (fake_replica / "proxy.py").read_text()
    first = ((fake_replica / "config.py").read_text(), (fake_replica / "proxy.py").read_text())

    assert patch_module.main() == 0
    assert "Already applied" in capsys.readouterr().out
    assert ((fake_replica / "config.py").read_text(), (fake_replica / "proxy.py").read_text()) == first


def _run_patched_proxy(source, settings_attrs, visitor_ua):
    """Execute the patched function for real; returns (UA header sent upstream, TLS profile)."""
    namespace = {"settings": type("S", (), settings_attrs)()}
    exec(source, namespace)
    request = type("Req", (), {"headers": {"user-agent": visitor_ua}})()
    return asyncio.run(namespace["proxy_request"](request, "/"))


FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


@pytest.mark.parametrize("forced_ua, forced_profile, visitor_ua, expected_ua, expected_profile", [
    # nothing set: stock Replica behaviour, visitor's UA passes through
    ("", "", FIREFOX_UA, FIREFOX_UA, "firefox"),
    ("", "", CHROME_UA, CHROME_UA, "chrome"),
    # forced UA replaces the visitor's; TLS profile follows the forced string
    ("CustomBot/1.0", "", FIREFOX_UA, "CustomBot/1.0", "chrome"),
    (FIREFOX_UA, "", CHROME_UA, FIREFOX_UA, "firefox"),
    # explicit profile wins over the derived one, UA still forced
    ("CustomBot/1.0", "safari", CHROME_UA, "CustomBot/1.0", "safari"),
    # profile alone changes the fingerprint but not the header text
    ("", "safari", CHROME_UA, CHROME_UA, "safari"),
])
def test_patched_replica_sets_the_upstream_user_agent_and_profile(
        fake_replica, forced_ua, forced_profile, visitor_ua, expected_ua, expected_profile):
    patch_module.main()
    source = (fake_replica / "proxy.py").read_text()
    sent_ua, profile = _run_patched_proxy(
        source, {"UPSTREAM_USER_AGENT": forced_ua, "IMPERSONATE_PROFILE": forced_profile}, visitor_ua)
    assert sent_ua == expected_ua
    assert profile == expected_profile


def test_patch_refuses_to_half_apply_when_replica_source_has_changed(fake_replica, capsys):
    (fake_replica / "proxy.py").write_text("async def proxy_request(request, path):\n    return None\n", encoding="utf-8")
    assert patch_module.main() == 2
    assert "NOT applied" in capsys.readouterr().err
    assert (fake_replica / "config.py").read_text() == PRISTINE_CONFIG       # config left untouched


def test_patch_reports_a_missing_replica_checkout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(patch_module, "REPLICA_DIR", tmp_path / "nope")
    assert patch_module.main() == 1


def test_ensure_replica_patched_applies_the_patch_when_it_is_missing(tmp_path, monkeypatch):
    package = tmp_path / "Replica" / "replica"
    package.mkdir(parents=True)
    (package / "proxy.py").write_text("# stock replica\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "replica_dir", tmp_path / "Replica")
    assert main_module.replica_is_patched() is False

    ran = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        (package / "proxy.py").write_text("# Surface Tor patch v2\n", encoding="utf-8")

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)
    assert main_module.ensure_replica_patched() is True
    assert ran and ran[0][1].endswith("patch_replica.py")


def test_ensure_replica_patched_reports_failure_if_the_patch_does_not_take(tmp_path, monkeypatch):
    package = tmp_path / "Replica" / "replica"
    package.mkdir(parents=True)
    (package / "proxy.py").write_text("# stock replica\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "replica_dir", tmp_path / "Replica")
    monkeypatch.setattr(main_module.subprocess, "run", lambda *a, **k: None)
    assert main_module.ensure_replica_patched() is False


def test_main_exits_cleanly_and_publishes_nothing_when_a_tunnel_cannot_be_created(monkeypatch, capsys):
    seen = _run_main(monkeypatch, ["-n", "2", "-g", "tok"])
    stopped = []
    monkeypatch.setattr(main_module, "stop_process", lambda proc: stopped.append(proc))
    monkeypatch.setattr(main_module, "start_tunnel_for_port",
                        lambda host, port, metrics_port, binary, protocol=None: (_Proc(), None))
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 1                      # a clean exit, not a traceback
    assert seen["persist"] == []
    assert len(stopped) == 2                        # the proxy and the tunnel process it had started
    assert "Nothing was published" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["-p", "60000"],                      # default metrics offset would push 60000 + 10000 past 65535
    ["-p", "65000", "-n", "10"],          # node ports themselves would run off the end
    ["-p", "0"],
    ["-m", "0"],
])
def test_port_and_metrics_values_that_overflow_are_rejected_up_front(monkeypatch, argv):
    seen = _run_main(monkeypatch, argv)
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2
    assert seen["proxies"] == []


def test_default_ports_are_accepted(monkeypatch):
    seen = _run_main(monkeypatch, ["-n", "3"])
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 0
    assert [p["port"] for p in seen["proxies"]] == [8000, 8001, 8002]


def test_relay_mode_warns_when_its_final_url_changes_after_a_recovery(monkeypatch, capsys):
    seen = _run_main(monkeypatch, ["--no-publish", "-n", "2"])
    sleeps = {"n": 0}

    def sleep_once(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise KeyboardInterrupt

    def fake_recover(index, records, host, binary, protocol=None):
        records[index]["public_url"] = "https://changed.trycloudflare.com"

    monkeypatch.setattr(main_module.time, "sleep", sleep_once)
    monkeypatch.setattr(main_module, "tunnel_is_healthy", lambda record: record["local_port"] != 8001)
    monkeypatch.setattr(main_module, "recover_tunnel_record", fake_recover)
    with pytest.raises(SystemExit):
        main_module.main()

    out = capsys.readouterr().out
    assert "final URL changed to https://changed.trycloudflare.com" in out
    assert seen["persist"] == []


def test_publishing_device_republishes_with_the_attach_url_after_a_recovery(monkeypatch):
    seen = _run_main(monkeypatch, ["-t", "https://abc.trycloudflare.com", "--origin", "https://example.com", "-n", "1"])
    sleeps = {"n": 0}

    def sleep_once(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise KeyboardInterrupt

    def fake_recover(index, records, host, binary, protocol=None):
        records[index]["public_url"] = "https://changed.trycloudflare.com"

    monkeypatch.setattr(main_module.time, "sleep", sleep_once)
    monkeypatch.setattr(main_module, "tunnel_is_healthy", lambda record: False)
    monkeypatch.setattr(main_module, "recover_tunnel_record", fake_recover)
    with pytest.raises(SystemExit):
        main_module.main()

    assert len(seen["persist"]) == 2                                     # at start-up, then after recovery
    assert seen["persist"][1]["attached_url"] == "https://abc.trycloudflare.com"
    assert seen["persist"][1]["records"][0]["public_url"] == "https://changed.trycloudflare.com"


def test_importing_main_does_not_require_a_replica_checkout(tmp_path):
    """Used to raise FileNotFoundError on import, so tests could not even load before setup."""
    import shutil
    copy = tmp_path / "main.py"
    shutil.copy(ROOT / "main.py", copy)
    spec2 = importlib.util.spec_from_file_location("main_without_replica", copy)
    module = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(module)            # must not raise
    assert not (tmp_path / "Replica").exists()


def test_main_exits_with_a_clear_message_when_replica_is_missing(monkeypatch, tmp_path, capsys):
    seen = _run_main(monkeypatch, ["-n", "1"], replica_present=False)
    monkeypatch.setattr(main_module, "replica_dir", tmp_path / "does-not-exist")
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 1
    assert seen["proxies"] == []
    assert "Run setup" in capsys.readouterr().out


def test_help_works_without_a_replica_checkout(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, ["--help"], replica_present=False)
    monkeypatch.setattr(main_module, "replica_dir", tmp_path / "does-not-exist")
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 0
    assert "--no-publish" in capsys.readouterr().out


def test_main_gives_the_user_agent_to_the_publishing_step(monkeypatch):
    seen = _run_main(monkeypatch, ["-n", "1", "-u", "Mozilla/5.0 Pinned"])
    with pytest.raises(SystemExit):
        main_module.main()
    assert seen["persist"][0]["user_agent"] == "Mozilla/5.0 Pinned"
