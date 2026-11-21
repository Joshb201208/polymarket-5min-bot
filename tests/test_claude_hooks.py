"""The Claude Code guard hooks do what CLAUDE.md says they do.

These run the hook scripts exactly as Claude Code does: JSON on stdin, exit
code 2 means "blocked". They use throwaway git repositories, so they never
touch this checkout's branches.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / ".claude" / "hooks" / "guard_bash.py"
PHASE_LOCK = REPO_ROOT / ".claude" / "hooks" / "phase_lock.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    """Two throwaway repos: one on a feature branch, one on master."""
    made = {}
    for name, branch in (("feature", "audit/full-dig"), ("master", "master")):
        path = tmp_path_factory.mktemp(name)
        _git(path, "init", "-q", "-b", "master")
        _git(path, "commit", "-q", "--allow-empty", "-m", "init")
        if branch != "master":
            _git(path, "switch", "-q", "-c", branch)
        made[name] = path
    return made


def guard_blocks(command: str, cwd: Path) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    result = subprocess.run(
        [sys.executable, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert result.returncode in (0, 2), result.stderr
    if result.returncode == 2:
        assert "Blocked by" in result.stderr
    return result.returncode == 2


BLOCKED_ANYWHERE = [
    "git push origin master",
    "git push origin main",
    "git push origin HEAD:master",
    "git push origin feature:refs/heads/master",
    "git -C . push origin main",
    "git -c push.default=current push origin master",
    "cd /tmp && git push origin master",
    'echo "$(git push origin master)"',
    "git push --force origin feat/x",
    "git push --force-with-lease origin feat/x",
    "git push -f",
    "git push origin +feat/x",
    "git push --delete origin feat/x",
    "gh pr merge 3 --squash",
    "python -m stock_agent",
    "python3 -m stock_agent",
    "venv/bin/python -m stock_agent",
    "STOCK_AGENT_MODE=LIVE python3 -m stock_agent",
    "nohup python3 -m stock_agent &",
    "bash -c 'python3 -m stock_agent'",
    "python3 stock_agent/__main__.py",
    "cat .env",
    "cat ./.env",
    "cat /root/polymarket-bot/.env",
    "cp .env.example .env",
    "git add .env",
    "echo FOO=1 >> .env",
    "source .env",
    "ssh root@203.0.113.5 uptime",
    "scp a b:c",
    "curl -X POST https://paper-api.alpaca.markets/v2/orders",
    "wget https://api.alpaca.markets/v2/account",
]

ALLOWED_ON_A_FEATURE_BRANCH = [
    "git status",
    "git push",
    "git push -u origin audit/full-dig",
    "git push origin HEAD",
    "git commit -m 'docs: explain that master deploys'",
    "git commit -m 'docs: mention .env handling'",
    "git log --oneline master",
    "git diff origin/master...HEAD",
    "git fetch origin master",
    "git switch -c audit/full-dig",
    "git add -A",
    "gh pr create --draft --title x --body 'never commit .env'",
    "gh pr view 3",
    "python3 -m pytest -q",
    "python3 -m compileall -q stock_agent",
    "python3 -c 'import stock_agent.config'",
    "cat .env.example",
    "echo 'never read .env'",
    "rg -n 'os.environ' stock_agent",
    "rg -n alpaca.markets stock_agent",
    "curl https://pypi.org/simple/",
    "ls -la && wc -l stock_agent/*.py",
]


@pytest.mark.parametrize("command", BLOCKED_ANYWHERE)
def test_guard_blocks(command, repos):
    assert guard_blocks(command, repos["feature"])


@pytest.mark.parametrize("command", ALLOWED_ON_A_FEATURE_BRANCH)
def test_guard_allows(command, repos):
    assert not guard_blocks(command, repos["feature"])


@pytest.mark.parametrize("command", ["git push", "git push origin HEAD", "git commit -m x"])
def test_guard_blocks_pushes_and_commits_while_on_master(command, repos):
    assert guard_blocks(command, repos["master"])


def test_guard_allows_reading_while_on_master(repos):
    assert not guard_blocks("git status", repos["master"])


def test_guard_fails_open_on_garbage_input():
    result = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True)
    assert result.returncode == 0


def phase_lock_decision(tmp_path: Path, phase: str, file_path: str) -> str:
    """Return 'deny', 'ask', or 'pass' for an edit to file_path in the given phase."""
    (tmp_path / "docs" / "revamp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "revamp" / "PHASE").write_text(phase)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": file_path}, "cwd": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(PHASE_LOCK)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode in (0, 2), result.stderr
    if result.returncode == 2:
        return "deny"
    if result.stdout.strip():
        return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
    return "pass"


@pytest.mark.parametrize(
    "phase, file_path, expected",
    [
        ("audit\n", "stock_agent/config.py", "deny"),
        ("audit\n", "stock_agent/risk_manager.py", "deny"),
        ("audit\n", "deploy/auto_update.sh", "deny"),
        ("audit\n", "requirements.txt", "deny"),
        ("plan\n", "stock_agent/risk_manager.py", "deny"),
        ("", "stock_agent/config.py", "deny"),                # empty file: safest phase
        ("nonsense\n", "stock_agent/config.py", "deny"),      # unknown word: still locked
        ("audit\n", "docs/revamp/audit/findings/01-x.md", "pass"),
        ("audit\n", "tests/test_x.py", "pass"),
        ("audit\n", "requirements-dev.txt", "pass"),
        ("audit\n", ".env.example", "pass"),
        ("build\n", "stock_agent/risk_manager.py", "pass"),
        ("build\n", "deploy/setup.sh", "pass"),
        ("audit\n", ".env", "deny"),
        ("build\n", ".env", "deny"),
        ("audit\n", "docs/revamp/PHASE", "ask"),
        ("build\n", ".claude/settings.json", "ask"),
        ("build\n", ".claude/hooks/guard_bash.py", "ask"),
    ],
)
def test_phase_lock(tmp_path, phase, file_path, expected):
    assert phase_lock_decision(tmp_path, phase, file_path) == expected


def test_phase_lock_ignores_files_outside_the_repo(tmp_path):
    assert phase_lock_decision(tmp_path, "audit\n", "/tmp/somewhere-else.txt") == "pass"


def test_phase_file_holds_a_valid_phase():
    phase = (REPO_ROOT / "docs" / "revamp" / "PHASE").read_text().split("#")[0].strip().lower()
    assert phase in {"audit", "plan", "build"}
