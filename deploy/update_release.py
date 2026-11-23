"""In-place pull deployment for the existing root cron; no broker mutations."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = "Joshb201208/polymarket-5min-bot"
UNIT = "stock-agent.service"
REMOTE_REF = "refs/remotes/origin/master"
SHA = re.compile(r"[0-9a-f]{40}\Z")
PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class Deferred(Exception):
    """A guard declined to deploy; the current release is unchanged."""


def say(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} stock-agent deploy: {message}", flush=True)


def read_json(path: Path) -> dict:
    try:
        record = json.loads(path.read_text())
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as out:
        json.dump(data, out)
    tmp.replace(path)


def stamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("market timestamp is not offset-aware")
    return result


def request_json(url: str, headers: dict) -> dict:
    with urlopen(Request(url, headers=headers), timeout=8) as response:
        return json.load(response)


def broker_clock() -> dict:
    """Runs in a transient systemd unit with EnvironmentFile; never shell-sources it."""
    mode = os.environ.get("STOCK_AGENT_MODE", "PAPER").upper()
    default_url = PAPER_URL if mode == "PAPER" else LIVE_URL
    url = os.environ.get("ALPACA_BASE_URL", PAPER_URL).rstrip("/")
    if mode not in {"PAPER", "LIVE"} or url != default_url:
        raise ValueError("broker mode and host disagree")
    result = request_json(
        f"{url}/v2/clock",
        {
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        },
    )
    return {field: result[field] for field in ("timestamp", "is_open", "next_open")}


class Updater:
    def __init__(self, root: Path, unit_path: Path = Path("/etc/systemd/system/stock-agent.service")):
        self.root = root.resolve()
        self.data = self.root / "data"
        self.hold = self.data / "deploy-hold"
        self.state = self.data / "deploy-status.json"
        self.heartbeat = self.data / "runtime-health.json"
        self.unit_path = unit_path

    def run(self, *args: str, timeout: int = 20, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=self.root, timeout=timeout, check=check, capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    def git(self, *args: str) -> str:
        return self.run("git", *args).stdout

    def systemctl(self, *args: str, timeout: int = 20, check: bool = True) -> str:
        return self.run("systemctl", *args, timeout=timeout, check=check).stdout.strip()

    def active_state(self) -> str:
        return self.systemctl("is-active", UNIT, check=False)

    def pid(self) -> int:
        return int(self.systemctl("show", UNIT, "--property=MainPID", "--value") or "0")

    def unit_needs_reload(self) -> bool:
        return self.systemctl("show", UNIT, "--property=NeedDaemonReload", "--value") == "yes"

    def eligible(self, expected_state: str | None = None) -> str:
        if self.hold.exists():
            raise Deferred("deploy hold file present")
        state = self.active_state()
        if state not in {"active", "failed"}:
            raise Deferred("service not active or failed; not starting it")
        if expected_state is not None and state != expected_state:
            raise Deferred("service state changed during preparation; not starting it")
        return state

    def commit(self, ref: str) -> str:
        value = self.git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        if not SHA.fullmatch(value):
            raise Deferred("invalid commit identity")
        return value

    def clock_closed(self, *, use_heartbeat: bool = True) -> None:
        if use_heartbeat:
            record = read_json(self.heartbeat)
            if record.get("pid") == self.pid() and record.get("market_source") == "live":
                clock = {
                    "timestamp": record.get("market_observed_at"),
                    "is_open": record.get("market_open"),
                    "next_open": record.get("market_next_open"),
                }
                try:
                    self.verify_closed(clock)
                    return
                except (KeyError, ValueError, TypeError):
                    pass
        try:
            reply = self.run(
                "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                "--property", f"EnvironmentFile={self.root / '.env'}",
                "/usr/bin/python3", str(self.root / "deploy/update_release.py"), "--clock",
                timeout=16,
            )
            self.verify_closed(json.loads(reply.stdout))
        except (KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            raise Deferred("broker session could not be confirmed closed") from exc

    @staticmethod
    def verify_closed(clock: dict) -> None:
        observed, opening = stamp(clock["timestamp"]), stamp(clock["next_open"])
        now = datetime.now(timezone.utc)
        if abs(now - observed) > timedelta(seconds=45) or opening <= now:
            raise ValueError("invalid or stale clock")
        if clock["is_open"] is not False:
            raise Deferred("market is not confirmed closed")
        if opening - now < timedelta(minutes=30):
            raise Deferred("next market open is less than 30 minutes away")

    def ci(self, sha: str, state: dict) -> None:
        now = time.time()
        retry_after = state.get("github_retry_after", 0)
        if isinstance(retry_after, (int, float)) and retry_after > now:
            raise Deferred("GitHub API backoff active")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        prefix = f"https://api.github.com/repos/{REPO}/actions"
        params = urlencode({"head_sha": sha, "event": "push", "branch": "master", "per_page": 30})
        try:
            runs = request_json(f"{prefix}/workflows/ci.yml/runs?{params}", headers)["workflow_runs"]
            runs = [
                run for run in runs if (
                    run.get("head_sha") == sha and run.get("head_branch") == "master"
                    and run.get("event") == "push"
                    and run.get("head_repository", {}).get("full_name", "").casefold() == REPO.casefold()
                )
            ]
            if not runs:
                raise Deferred("no matching CI run")
            latest = max(runs, key=lambda run: (run["run_number"], run["run_attempt"]))
            if latest.get("status") != "completed" or latest.get("conclusion") != "success":
                raise Deferred("latest matching CI run is not successful")
            run_id, attempt = int(latest["id"]), int(latest["run_attempt"])
            jobs = request_json(f"{prefix}/runs/{run_id}/attempts/{attempt}/jobs?per_page=30", headers)
            test = [job for job in jobs["jobs"] if job.get("name") == "test"]
            if len(test) != 1 or test[0].get("status") != "completed" or test[0].get("conclusion") != "success":
                raise Deferred("required test job not successful")
        except HTTPError as exc:
            if exc.code in {403, 429}:
                until = [now + 600]
                for name, base in (("Retry-After", now), ("X-RateLimit-Reset", 0)):
                    try:
                        until.append(float(exc.headers[name]) + base)
                    except (KeyError, TypeError, ValueError):
                        pass
                state["github_retry_after"] = max(until)
                save_json(self.state, state)
            raise Deferred(f"GitHub API status {exc.code}") from exc
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
            raise Deferred("GitHub CI response unavailable or invalid") from exc

    @staticmethod
    def docs_only(paths: list[str]) -> bool:
        passive = {"README.md", "CLAUDE.md", "LICENSE", "CHANGELOG.md"}
        return bool(paths) and all(path in passive or path.startswith("docs/") for path in paths)

    @staticmethod
    def check_paths(paths: list[str]) -> None:
        for path in paths:
            if path in {".env", "data", "venv"} or path.startswith(("data/", "venv/")):
                raise Deferred("incoming change collides with private runtime files")

    def ensure_clean(self, paths: list[str]) -> None:
        status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=no")
        if status:
            raise Deferred("tracked checkout changes; refusing destructive update")
        tracked = set(self.git("ls-files", "-z").split("\0"))
        for path in paths:
            file = self.root / path
            if path not in tracked and (file.exists() or file.is_symlink()):
                raise Deferred("incoming path collides with a local file")
            for parent in file.parents:
                if parent == self.root:
                    break
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise Deferred("incoming path collides with a local directory boundary")

    def healthy(self, sha: str, previous_pid: int) -> bool:
        record = read_json(self.heartbeat)
        try:
            age = datetime.now(timezone.utc) - stamp(record["updated_at"])
            pid = self.pid()
            return (
                self.active_state() == "active"
                and pid > 0 and pid != previous_pid and record.get("pid") == pid
                and record.get("release_sha") == sha and record.get("ready") is True
                and record.get("reconciled") is True
                and timedelta() <= age < timedelta(seconds=60)
            )
        except (KeyError, TypeError, ValueError):
            return False

    def wait_healthy(self, sha: str, previous_pid: int) -> bool:
        for _ in range(20):
            if self.healthy(sha, previous_pid):
                return True
            if self.active_state() in {"inactive", "failed"}:
                return False
            time.sleep(2)
        return False

    def install_unit(self, payload: bytes) -> None:
        tmp = self.unit_path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.chmod(0o644)
        tmp.replace(self.unit_path)
        self.systemctl("daemon-reload")

    def dependencies(self) -> None:
        self.run(str(self.root / "venv/bin/pip"), "install", "-r", "requirements.txt", "--quiet", timeout=480)

    def update(self) -> None:
        initial_state = self.eligible()
        recovering = initial_state == "failed"
        old = self.commit("HEAD")
        self.git("fetch", "--no-tags", "origin", f"refs/heads/master:{REMOTE_REF}")
        new = self.commit(REMOTE_REF)
        checkout_changed = old != new
        if recovering and not checkout_changed:
            raise Deferred("failed service requires a newer release")
        candidate_path = self.root / "deploy/stock-agent.service"
        paths = []
        if checkout_changed:
            self.run("git", "merge-base", "--is-ancestor", old, new)
            changed = self.git("diff", "--no-renames", "--name-only", "-z", old, new)
            paths = [path for path in changed.split("\0") if path]
        self.check_paths(paths)
        self.ensure_clean(paths)
        old_unit = None
        needs_reload = False
        if not checkout_changed:
            # The old cron can advance HEAD on the first cutover without
            # replacing its systemd unit. Unit drift is an activation, not a
            # documentation update, even though the checkout needs no reset.
            old_unit = self.unit_path.read_bytes()
            needs_reload = self.unit_needs_reload()
            if old_unit == candidate_path.read_bytes() and not needs_reload:
                raise Deferred("already current")
        state = read_json(self.state)
        if state.get("failed_sha") == new:
            raise Deferred("candidate quarantined following earlier failure")
        self.clock_closed(use_heartbeat=not recovering)
        self.ci(new, state)
        self.eligible(initial_state)
        self.clock_closed(use_heartbeat=not recovering)
        if not recovering and checkout_changed and (not paths or self.docs_only(paths)):
            self.git("reset", "--hard", new)
            say(f"advanced to {new[:12]} (documentation-only; no restart)")
            return

        old_pid = self.pid()
        if old_unit is None:
            old_unit = self.unit_path.read_bytes()
        changed_deps = bool({"requirements.txt", "requirements.lock"} & set(paths))
        changed_unit = False
        activation_attempted = False
        try:
            if checkout_changed:
                self.git("reset", "--hard", new)
            self.dependencies()
            self.eligible(initial_state)
            self.clock_closed(use_heartbeat=not recovering)
            candidate_unit = candidate_path.read_bytes()
            if candidate_unit != old_unit:
                changed_unit = True
                self.install_unit(candidate_unit)
            elif needs_reload:
                self.systemctl("daemon-reload")
            self.eligible(initial_state)
            if recovering:
                self.systemctl("reset-failed", UNIT)
                activation_attempted = True
                # Refuse a conflicting stop already queued by an operator.
                self.systemctl("start", "--job-mode=fail", UNIT, timeout=210)
            else:
                activation_attempted = True
                self.systemctl("try-restart", UNIT, timeout=210)
            if not self.wait_healthy(new, old_pid):
                raise RuntimeError("candidate did not reconcile and report healthy")
        except (Deferred, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if recovering and activation_attempted:
                state["failed_sha"] = new
                state["last_event"] = {
                    "event": "recovery_failed", "active_sha": new, "candidate_sha": new,
                    "at": datetime.now(timezone.utc).isoformat(), "reason": error_kind(exc),
                }
                save_json(self.state, state)
                # Do not restart, stop or restore the previously failed release.
                raise RuntimeError("failed-service recovery did not become healthy; candidate quarantined") from exc
            say(f"candidate {new[:12]} did not activate ({error_kind(exc)}); restoring {old[:12]}")
            try:
                if checkout_changed:
                    self.git("reset", "--hard", old)
                if changed_unit:
                    self.install_unit(old_unit)
                if changed_deps:
                    self.dependencies()
                # If stopped concurrently, restore disk but do not resurrect the unit.
                unit_state = self.active_state()
                failing_pid = self.pid()
                can_recover_prior = not recovering and (checkout_changed or changed_unit)
                if can_recover_prior and unit_state == "active" and failing_pid != old_pid:
                    self.systemctl("try-restart", UNIT, timeout=210)
                    if not self.wait_healthy(old, failing_pid):
                        raise RuntimeError("previous release did not become healthy")
                elif can_recover_prior and activation_attempted and unit_state == "activating":
                    # Restart=on-failure may already be launching the restored code.
                    if not self.wait_healthy(old, failing_pid):
                        raise RuntimeError("previous release did not recover")
                elif (can_recover_prior and activation_attempted and unit_state == "failed"
                      and self.active_state() == "failed"):
                    # Only recover a failed candidate, never an inactive or stopping unit.
                    self.systemctl("restart", "--job-mode=fail", UNIT, timeout=210)
                    if not self.wait_healthy(old, failing_pid):
                        raise RuntimeError("previous release did not recover")
                if not isinstance(exc, Deferred):
                    state["last_event"] = {
                        "event": "rollback", "active_sha": old, "candidate_sha": new,
                        "at": datetime.now(timezone.utc).isoformat(), "reason": error_kind(exc),
                    }
            finally:
                if not isinstance(exc, Deferred):
                    state["failed_sha"] = new
                    save_json(self.state, state)
            if isinstance(exc, Deferred):
                raise
            raise RuntimeError("activation failed; checkout and unit on disk restored") from exc
        state["last_event"] = {
            "event": "deployed", "active_sha": new, "candidate_sha": new,
            "at": datetime.now(timezone.utc).isoformat(),
            "reason": "failed service recovered" if recovering else "startup health passed",
        }
        try:
            save_json(self.state, state)
        except OSError:
            say("deployment succeeded, but status event could not be written")
        if checkout_changed:
            say(f"update successful: {old[:12]} -> {new[:12]}")
        else:
            say(f"repaired installed unit for {new[:12]}")

    def locked_update(self) -> int:
        self.data.mkdir(parents=True, exist_ok=True)
        with (self.data / "deploy.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                say("another updater holds the lock")
                return 0
            try:
                self.update()
            except Deferred as exc:
                say(str(exc))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                say(f"failed: {error_kind(exc)}")
                return 1
        return 0


def error_kind(exc: Exception) -> str:
    if isinstance(exc, Deferred):
        return str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        return f"{Path(exc.cmd[0]).name} exited {exc.returncode}"
    return type(exc).__name__


def main() -> int:
    os.umask(0o077)
    if sys.argv[1:] == ["--clock"]:
        try:
            print(json.dumps(broker_clock()))
        except Exception:
            say("read-only broker clock failed")
            return 1
        return 0
    return Updater(Path(sys.argv[1])).locked_update()


if __name__ == "__main__":
    raise SystemExit(main())
