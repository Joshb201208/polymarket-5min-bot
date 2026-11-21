#!/usr/bin/env python3
"""PreToolUse guard for Bash commands.

Blocks the handful of shell commands that could hurt a live trading bot:
pushing to the deploy branch, force-pushing, merging pull requests, running
the live agent, touching the real .env, reaching the production server, and
calling the broker from the command line.

Claude Code sends the tool call as JSON on stdin. Exit code 2 blocks the call
and shows stderr to Claude. Any other outcome lets the normal permission flow
continue, so a bug in this script fails open rather than freezing the session.

This is a seatbelt, not a security boundary: it catches the commands Claude
would normally write, not every possible way of spelling them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

PROTECTED_BRANCHES = {"master", "main"}

SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|&]")
GIT_WORD = re.compile(r"(?:^|[\s(`$])git\b")
DOTENV = re.compile(r"(?<![\w.\-])\.env(?![\w.\-])")
DOTENV_REDIRECT = re.compile(r">\s*[\"']?[^\s\"']*(?<![\w.\-])\.env(?![\w.\-])")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"^python[\d.]*$")
LIVE_AGENT = re.compile(r"-m\s+stock_agent(?![\w.])|stock_agent/__main__\.py")

WRAPPERS = {"sudo", "nohup", "time", "exec", "command", "env", "nice", "timeout"}
SHELLS = {"bash", "sh", "zsh", "dash", "eval"}
# Commands that may mention ".env" in free text without touching the file.
TEXT_CARRIERS = {"echo", "printf", "gh"}


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/guard_bash.py: {reason}", file=sys.stderr)
    sys.exit(2)


def current_branch(cwd: str | None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def program(segment: str) -> tuple[str, str]:
    """Return (program name, rest of segment), skipping VAR=x and wrappers."""
    words = segment.strip().lstrip("(`").split()
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word) or word in WRAPPERS or (
            i > 0 and words[i - 1] == "timeout" and word[:1].isdigit()
        ):
            i += 1
            continue
        break
    if i >= len(words):
        return "", ""
    name = words[i].strip("\"'").rsplit("/", 1)[-1]
    return name, " ".join(words[i + 1:])


def git_args(segment: str) -> list[str]:
    """Return the words after `git` in a command segment, or [] if not git."""
    match = GIT_WORD.search(segment)
    if not match:
        return []
    return re.sub(r"[\"'()`]", " ", segment[match.end():]).split()


def git_verb(args: list[str]) -> tuple[str, list[str]]:
    """Skip git's global options (-C dir, -c k=v, ...) to find the verb."""
    i = 0
    while i < len(args):
        word = args[i]
        if word in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        return word, args[i + 1:]
    return "", []


def check_push(rest: list[str], cwd: str | None) -> None:
    flags = [w for w in rest if w.startswith("-")]
    targets = [w for w in rest if not w.startswith("-")]

    for flag in flags:
        if flag in ("-f", "--mirror", "--delete", "-d", "--all") or flag.startswith("--force"):
            block(
                f"`git push {flag}` is not allowed in this repo. Push your own "
                "feature branch normally and open a pull request."
            )

    refspecs = targets[1:]  # targets[0] is the remote name
    if not refspecs:
        branch = current_branch(cwd)
        if branch in PROTECTED_BRANCHES:
            block(
                f"you are on `{branch}`, which auto-deploys to the live trading "
                "bot within 10 minutes. Create a branch first "
                "(`git switch -c <type>/<short-name>`) and push that."
            )
        return

    for spec in refspecs:
        if spec.startswith("+"):
            block("a leading `+` in a refspec is a force-push. Not allowed.")
        dest = spec.split(":")[-1]
        if dest.startswith("refs/heads/"):
            dest = dest[len("refs/heads/"):]
        if dest == "HEAD":
            dest = current_branch(cwd)
        if dest in PROTECTED_BRANCHES:
            block(
                f"pushing to `{dest}` deploys straight to the live trading bot. "
                "Push a feature branch and open a pull request; Josh merges."
            )


def check_segment(segment: str, cwd: str | None) -> None:
    name, rest = program(segment)

    # Shell wrappers hide the real command, so inspect their whole text.
    scan = segment if name in SHELLS else None

    if (PYTHON.match(name) and LIVE_AGENT.search(rest)) or (
        scan and re.search(r"\bpython[\d.]*\s[^|;&\n]*" + LIVE_AGENT.pattern, scan)
    ):
        block(
            "running the live agent talks to real APIs and can place orders. "
            "Use `python3 -m pytest -q` and fakes instead."
        )

    if name in ("ssh", "scp", "sftp") or (scan and re.search(r"\b(ssh|scp|sftp)\s", scan)):
        block(
            "connecting to servers is out of scope. If you need facts from the "
            "droplet, ask Josh to run `scripts/vps_snapshot.sh` and upload the result."
        )

    if (name in ("curl", "wget") or scan) and "alpaca.markets" in segment:
        block("do not call the Alpaca API from the command line in development sessions.")

    if (name == "gh" or scan) and re.search(r"\bpr\s+merge\b", segment):
        block("only Josh merges pull requests. Leave the PR open and report back.")

    args = git_args(segment)
    verb, verb_rest = git_verb(args) if args else ("", [])
    if verb == "push":
        check_push(verb_rest, cwd)
    elif verb == "commit" and current_branch(cwd) in PROTECTED_BRANCHES:
        block(
            "do not commit on the deploy branch. Create a branch first "
            "(`git switch -c <type>/<short-name>`), then commit."
        )

    if DOTENV.search(segment):
        mentions_only = (
            name in TEXT_CARRIERS or (name == "git" and verb in ("commit", "log", "grep"))
        ) and not DOTENV_REDIRECT.search(segment)
        if not mentions_only:
            block(
                "commands must not read, write, copy, or stage the real `.env` "
                "file (`.env.example` is fine). If you were only writing text "
                "that mentions .env, use the Write or Edit tool instead of the shell."
            )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        sys.exit(0)

    for segment in SEGMENT_SPLIT.split(command):
        if segment.strip():
            check_segment(segment, payload.get("cwd"))

    sys.exit(0)


if __name__ == "__main__":
    main()
