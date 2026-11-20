#!/usr/bin/env python3
"""PreToolUse phase lock for Edit / Write / NotebookEdit.

The revamp runs in phases recorded in docs/revamp/PHASE (audit, plan, build).
Until the phase is `build`, production code and deploy files are read-only, so
an audit session cannot quietly start "fixing" a live trading bot.

Decisions:
  - editing the real .env                         -> blocked, always
  - editing docs/revamp/PHASE or these guardrails -> ask Josh to confirm
  - editing locked paths while phase != build     -> blocked
  - everything else                               -> normal permission flow

Exit code 2 blocks the call and shows stderr to Claude. A bug in this script
fails open.
"""

import json
import os
import sys
from pathlib import Path

PHASE_FILE = "docs/revamp/PHASE"
LOCKED_PREFIXES = ("stock_agent/", "deploy/")
LOCKED_FILES = ("requirements.txt",)
ASK_PREFIXES = (".claude/hooks/",)
ASK_FILES = (PHASE_FILE, ".claude/settings.json")
ENV_FILES = (".env", ".env.local", ".env.production")


def read_phase(root: Path) -> str:
    try:
        for line in (root / PHASE_FILE).read_text().splitlines():
            word = line.split("#", 1)[0].strip().lower()
            if word:
                return word
    except OSError:
        pass
    return "audit"  # missing or empty file means the safest phase


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        sys.exit(0)

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = Path(payload.get("cwd") or root) / target
    try:
        rel = target.resolve().relative_to(root).as_posix()
    except ValueError:
        sys.exit(0)  # outside the repo: not our concern

    if rel in ENV_FILES:
        print(
            "Blocked by .claude/hooks/phase_lock.py: the real .env file is off "
            "limits. Put new variable names in .env.example and tell Josh.",
            file=sys.stderr,
        )
        sys.exit(2)

    if rel in ASK_FILES or rel.startswith(ASK_PREFIXES):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": (
                            f"{rel} controls the revamp phase or its guardrails. "
                            "Only approve if you (Josh) asked for this change."
                        ),
                    }
                }
            )
        )
        sys.exit(0)

    phase = read_phase(root)
    if phase != "build" and (rel in LOCKED_FILES or rel.startswith(LOCKED_PREFIXES)):
        print(
            f"Blocked by .claude/hooks/phase_lock.py: the revamp is in the "
            f"`{phase}` phase, so `{rel}` is read-only. Record what you found "
            "under docs/revamp/ instead. Code changes start only after Josh "
            "moves docs/revamp/PHASE to `build`.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
