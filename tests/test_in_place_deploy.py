"""Offline fixtures for the cron entrypoint and candidate activation."""

import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from deploy import update_release as mod
from stock_agent.runtime_status import RuntimeStatus

OLD = "a" * 40
NEW = "b" * 40


class FakeUpdater(mod.Updater):
    def __init__(self, root):
        unit = root / "installed.service"
        super().__init__(root, unit)
        self.data.mkdir()
        (root / "deploy").mkdir()
        unit.write_text("working unit")
        (root / "deploy/stock-agent.service").write_text("candidate unit")
        self.current = OLD
        self.paths = "stock_agent/scheduler.py\0"
        self.service_pid = 100
        self.service_state = "active"
        self.healthy_candidate = True
        self.events = []

    def git(self, *args):
        self.events.append(args)
        if args[0] == "rev-parse":
            return (NEW if args[-1].startswith(mod.REMOTE_REF) else self.current) + "\n"
        if args[0] == "diff":
            return self.paths
        if args[0] == "reset":
            self.current = args[-1]
        return ""

    def run(self, *args, **kwargs):
        self.events.append(args)

    def clock_closed(self, *, use_heartbeat=True):
        self.events.append("clock" if use_heartbeat else "broker-clock")

    def ci(self, sha, state):
        self.events.append(("ci", sha))

    def active_state(self):
        return self.service_state

    def pid(self):
        return self.service_pid

    def wait_healthy(self, sha, previous_pid):
        self.events.append(("health", sha, previous_pid))
        return sha == OLD or self.healthy_candidate

    def systemctl(self, *args, **kwargs):
        self.events.append(args)
        if args[0] == "try-restart":
            self.service_pid += 1
        if args[0] == "reset-failed":
            self.service_state = "inactive"
        if args[0] == "start":
            self.service_state = "active" if self.healthy_candidate else "failed"
            self.service_pid = 200 if self.healthy_candidate else 0


def test_docs_only_updated_without_install_or_restart(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.paths = "README.md\0docs/other.md\0"
    assert updater.locked_update() == 0
    assert updater.current == NEW
    assert ("ci", NEW) in updater.events
    assert not any("try-restart" in event or "install" in event for event in updater.events)


def test_update_checks_new_pid_and_exact_sha(tmp_path):
    updater = FakeUpdater(tmp_path)
    assert updater.locked_update() == 0
    assert updater.events.index(("ci", NEW)) < updater.events.index(("reset", "--hard", NEW))
    assert ("try-restart", mod.UNIT) in updater.events
    assert ("health", NEW, 100) in updater.events
    assert updater.unit_path.read_text() == "candidate unit"
    event = RuntimeStatus(updater.heartbeat, sha=NEW).deployment_event()
    assert event["event"] == "deployed"
    assert RuntimeStatus(updater.heartbeat, sha=OLD).deployment_event() is None


def test_already_current_repairs_legacy_unit_through_activation_guards(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.current = NEW
    assert updater.locked_update() == 0
    restart = updater.events.index(("try-restart", mod.UNIT))
    reload = updater.events.index(("daemon-reload",))
    assert updater.events.index(("ci", NEW)) < reload < restart
    assert updater.events[:reload].count("clock") == 3
    assert ("health", NEW, 100) in updater.events
    assert updater.unit_path.read_text() == "candidate unit"
    assert not any(event[0] == "reset" for event in updater.events)

    updater.events.clear()
    assert updater.locked_update() == 0
    assert ("ci", NEW) not in updater.events
    assert ("try-restart", mod.UNIT) not in updater.events


def test_matching_files_but_stale_loaded_unit_is_reloaded(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    updater.current = NEW
    updater.unit_path.write_text("candidate unit")
    monkeypatch.setattr(updater, "unit_needs_reload", lambda: True)
    assert updater.locked_update() == 0
    assert updater.events.index(("ci", NEW)) < updater.events.index(("daemon-reload",))
    assert updater.events.index(("daemon-reload",)) < updater.events.index(("try-restart", mod.UNIT))
    assert ("health", NEW, 100) in updater.events
    assert not any(event[0] == "reset" for event in updater.events)


@pytest.mark.parametrize("gate", ["ci", "clock_closed", "operator_stop"])
def test_current_sha_drift_defers_without_approval(tmp_path, monkeypatch, gate):
    updater = FakeUpdater(tmp_path)
    updater.current = NEW

    def reject(*args, **kwargs):
        raise mod.Deferred("fixture gate")

    if gate == "operator_stop":
        install_unit = updater.install_unit

        def stop_after_install(payload):
            install_unit(payload)
            updater.service_state = "inactive"

        monkeypatch.setattr(updater, "install_unit", stop_after_install)
    else:
        monkeypatch.setattr(updater, gate, reject)
    assert updater.locked_update() == 0
    assert updater.unit_path.read_text() == "working unit"
    assert not any(event[0] in {"reset", "restart", "try-restart"} for event in updater.events)


def test_current_sha_unhealthy_repair_restores_legacy_unit(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    updater.current = NEW

    def wait_healthy(sha, previous_pid):
        updater.events.append(("health", sha, previous_pid))
        return updater.unit_path.read_text() == "working unit"

    monkeypatch.setattr(updater, "wait_healthy", wait_healthy)
    assert updater.locked_update() == 1
    assert updater.current == NEW
    assert updater.unit_path.read_text() == "working unit"
    assert [event for event in updater.events if event[0] == "health"] == [
        ("health", NEW, 100), ("health", NEW, 101),
    ]
    assert not any(event[0] == "reset" for event in updater.events)
    assert mod.read_json(updater.state)["failed_sha"] == NEW


def test_systemd_runtime_status_matches_updater_even_with_separate_ledger_path(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    monkeypatch.setenv("STOCK_AGENT_RUNTIME_STATUS", str(updater.heartbeat))
    assert RuntimeStatus.for_data_dir(tmp_path / "data/stock_agent").path == updater.heartbeat


def test_health_requires_fresh_matching_pid_sha_and_reconcile(tmp_path):
    updater = FakeUpdater(tmp_path)
    now = datetime.now(timezone.utc)
    base = {
        "updated_at": now.isoformat(), "pid": updater.service_pid, "release_sha": NEW,
        "ready": True, "reconciled": True,
    }
    mod.save_json(updater.heartbeat, base)
    assert updater.healthy(NEW, 99)
    assert not updater.healthy(NEW, updater.service_pid)
    for change in (
        {"release_sha": OLD}, {"pid": 0}, {"ready": False}, {"reconciled": False},
        {"updated_at": (now - timedelta(seconds=61)).isoformat()},
    ):
        mod.save_json(updater.heartbeat, {**base, **change})
        assert not updater.healthy(NEW, 99)


def test_unhealthy_candidate_rolls_back_unit_and_checkout(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.healthy_candidate = False
    assert updater.locked_update() == 1
    assert updater.current == OLD
    assert updater.unit_path.read_text() == "working unit"
    assert ("health", OLD, 101) in updater.events
    assert mod.read_json(updater.state)["failed_sha"] == NEW
    event = RuntimeStatus(updater.heartbeat, sha=OLD).deployment_event()
    assert event["event"] == "rollback" and event["candidate_sha"] == NEW


def test_existing_stopped_and_hold_do_not_fetch(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "inactive"
    assert updater.locked_update() == 0
    updater.service_state = "active"
    updater.hold.touch()
    assert updater.locked_update() == 0
    assert updater.events == []


def test_new_green_sha_recovers_failed_unit(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "failed"
    updater.service_pid = 0
    assert updater.locked_update() == 0
    assert updater.current == NEW and updater.service_state == "active"
    assert updater.events.count("broker-clock") == 3 and "clock" not in updater.events
    reset = updater.events.index(("reset-failed", mod.UNIT))
    assert updater.events.index(("ci", NEW)) < reset
    assert updater.events[reset + 1] == ("start", "--job-mode=fail", mod.UNIT)
    assert ("health", NEW, 0) in updater.events
    assert mod.read_json(updater.state)["last_event"]["reason"] == "failed service recovered"


def test_failed_service_same_sha_cannot_restart(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.current = NEW
    updater.service_state = "failed"
    assert updater.locked_update() == 0
    assert updater.service_state == "failed"
    assert updater.events and all(event[0] in {"rev-parse", "fetch"} for event in updater.events)


def test_failed_service_does_not_try_quarantined_new_sha(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "failed"
    mod.save_json(updater.state, {"failed_sha": NEW})
    assert updater.locked_update() == 0
    assert updater.current == OLD and updater.service_state == "failed"
    assert not any(event[0] in {"reset-failed", "start"} for event in updater.events)


def test_failed_service_defers_while_market_open(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "failed"

    def market_open(*, use_heartbeat):
        assert not use_heartbeat
        raise mod.Deferred("market is not confirmed closed")

    monkeypatch.setattr(updater, "clock_closed", market_open)
    assert updater.locked_update() == 0
    assert updater.current == OLD and updater.service_state == "failed"
    assert not any(event[0] in {"ci", "reset", "reset-failed", "start"} for event in updater.events)


def test_failed_service_clock_ignores_old_heartbeat_and_uses_read_only_probe(tmp_path, monkeypatch):
    updater = mod.Updater(tmp_path)
    updater.data.mkdir()
    now = datetime.now(timezone.utc)
    mod.save_json(updater.heartbeat, {
        "pid": 0, "market_source": "live", "market_open": False,
        "market_observed_at": now.isoformat(), "market_next_open": (now + timedelta(hours=2)).isoformat(),
    })

    def probe(*args, **kwargs):
        assert args[0] == "systemd-run" and args[-1] == "--clock"
        return subprocess.CompletedProcess(args, 0, mod.json.dumps({
            "timestamp": now.isoformat(), "is_open": True,
            "next_open": (now + timedelta(hours=2)).isoformat(),
        }))

    def unexpected_pid():
        pytest.fail("failed-service recovery must not validate a runtime heartbeat")

    monkeypatch.setattr(updater, "run", probe)
    monkeypatch.setattr(updater, "pid", unexpected_pid)
    with pytest.raises(mod.Deferred, match="market is not confirmed closed"):
        updater.clock_closed(use_heartbeat=False)


def test_failed_service_unhealthy_recovery_is_quarantined_without_retry(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "failed"
    updater.service_pid = 0
    updater.healthy_candidate = False
    assert updater.locked_update() == 1
    assert updater.current == NEW and updater.service_state == "failed"
    state = mod.read_json(updater.state)
    assert state["failed_sha"] == NEW and state["last_event"]["event"] == "recovery_failed"
    assert updater.events.count(("start", "--job-mode=fail", mod.UNIT)) == 1
    assert not any(event[0] in {"restart", "try-restart", "stop"} for event in updater.events)
    updater.events.clear()
    assert updater.locked_update() == 0
    assert all(event[0] in {"rev-parse", "fetch"} for event in updater.events)


def test_operator_stop_while_installing_does_not_get_undone(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    monkeypatch.setattr(updater, "dependencies", lambda: setattr(updater, "service_state", "inactive"))
    assert updater.locked_update() == 0
    assert updater.current == OLD
    assert not any("try-restart" in event for event in updater.events)


@pytest.mark.parametrize("candidate_state", ["failed", "inactive"])
def test_rollback_distinguishes_crash_from_operator_stop(tmp_path, monkeypatch, candidate_state):
    updater = FakeUpdater(tmp_path)

    def wait_healthy(sha, old_pid):
        updater.events.append(("health", sha, old_pid))
        if sha == NEW:
            updater.service_state = candidate_state
            updater.service_pid = 0
            return False
        return True

    monkeypatch.setattr(updater, "wait_healthy", wait_healthy)
    assert updater.locked_update() == 1
    assert updater.current == OLD
    assert updater.unit_path.read_text() == "working unit"
    recovered = ("restart", "--job-mode=fail", mod.UNIT) in updater.events
    assert recovered is (candidate_state == "failed")


def test_private_paths_and_untracked_collisions_preserved(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.paths = "data/portfolio.json\0"
    assert updater.locked_update() == 0
    assert updater.current == OLD
    (updater.root / "operator-helper.py").touch()
    updater.paths = "operator-helper.py\0"
    assert updater.locked_update() == 0
    assert updater.current == OLD


def test_broker_open_near_open_and_malformed_defer():
    now = datetime.now(timezone.utc)
    payload = {"timestamp": now.isoformat(), "is_open": False}
    mod.Updater.verify_closed({**payload, "next_open": (now + timedelta(hours=3)).isoformat()})
    with pytest.raises(mod.Deferred):
        mod.Updater.verify_closed({**payload, "next_open": (now + timedelta(minutes=10)).isoformat()})
    with pytest.raises(mod.Deferred):
        mod.Updater.verify_closed({
            **payload, "is_open": "false", "next_open": (now + timedelta(hours=3)).isoformat(),
        })
    with pytest.raises(ValueError):
        mod.Updater.verify_closed({**payload, "timestamp": "2026-11-23", "next_open": payload["timestamp"]})


def test_ci_requires_matching_sha_and_test_job(tmp_path, monkeypatch):
    updater = mod.Updater(tmp_path)
    run = {
        "head_sha": NEW, "head_branch": "master", "event": "push", "head_repository": {"full_name": mod.REPO},
        "run_number": 32, "run_attempt": 2, "id": 9999, "status": "completed", "conclusion": "success",
    }
    test = {"name": "test", "status": "completed", "conclusion": "success"}

    def request(url, headers):
        assert "Authorization" not in headers
        if "workflows/" in url:
            assert f"head_sha={NEW}" in url and "event=push" in url and "branch=master" in url
            return {"workflow_runs": [run]}
        assert "/runs/9999/attempts/2/jobs?" in url
        return {"jobs": [test]}

    monkeypatch.setattr(mod, "request_json", request)
    updater.ci(NEW, {})
    test["conclusion"] = "skipped"
    with pytest.raises(mod.Deferred):
        updater.ci(NEW, {})
    run["head_sha"] = OLD
    with pytest.raises(mod.Deferred):
        updater.ci(NEW, {})


def test_legacy_cron_entry_shell_refuses_stopped_service(tmp_path, monkeypatch):
    source = mod.Path(mod.__file__).parent
    target = tmp_path / "deploy"
    target.mkdir()
    for name in ("auto_update.sh", "update_release.py"):
        shutil.copy(source / name, target / name)
    mock_bin = tmp_path / "fake-bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\n[ \"$1\" = is-active ] || exit 2\nprintf 'inactive\\n'\nexit 3\n")
    systemctl.chmod(0o755)
    monkeypatch.setenv("PATH", str(mock_bin) + ":/usr/bin:/bin")
    proc = subprocess.run([target / "auto_update.sh"], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert "not starting it" in proc.stdout
