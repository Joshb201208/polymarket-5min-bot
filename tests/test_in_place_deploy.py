"""Offline fixtures for the cron entrypoint and candidate activation."""

import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from deploy import update_release as mod

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

    def clock_closed(self):
        self.events.append("clock")

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


def test_unhealthy_candidate_rolls_back_unit_and_checkout(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.healthy_candidate = False
    assert updater.locked_update() == 1
    assert updater.current == OLD
    assert updater.unit_path.read_text() == "working unit"
    assert ("health", OLD, 101) in updater.events
    assert mod.read_json(updater.state)["failed_sha"] == NEW


def test_existing_stopped_and_hold_do_not_fetch(tmp_path):
    updater = FakeUpdater(tmp_path)
    updater.service_state = "inactive"
    assert updater.locked_update() == 0
    updater.service_state = "active"
    updater.hold.touch()
    assert updater.locked_update() == 0
    assert updater.events == []


def test_operator_stop_while_installing_does_not_get_undone(tmp_path, monkeypatch):
    updater = FakeUpdater(tmp_path)
    monkeypatch.setattr(updater, "dependencies", lambda: setattr(updater, "service_state", "inactive"))
    assert updater.locked_update() == 0
    assert updater.current == OLD
    assert not any("try-restart" in event for event in updater.events)


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
    assert "service not active; not starting it" in proc.stdout
