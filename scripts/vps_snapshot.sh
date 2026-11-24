#!/bin/bash
# Read-only evidence snapshot of the stock agent's server, for the audit.
#
# Run this ON THE DROPLET, as root:
#
#     cd /root/polymarket-bot && bash scripts/vps_snapshot.sh
#
# What it does: collects service status, deploy logs, redacted bot logs, the
# bot's state files, server facts, and a read-only Alpaca account report into
# one archive under /root. You then download that archive and upload it to
# your Claude Code session.
#
# What it does not do: it does not restart, stop, or change anything. It does
# not place, change, or cancel orders (the Alpaca part sends GET requests
# only). It does not copy your .env file; secret values found in .env are
# replaced with REDACTED everywhere in the snapshot before it is packed.
#
# The archive contains private account data (positions, orders, balances).
# Do not commit it and do not post it anywhere public.

set -u
umask 077

BOT_DIR="${BOT_DIR:-/root/polymarket-bot}"
SERVICE="${SERVICE:-stock-agent}"
LOG_DAYS="${LOG_DAYS:-21}"
OUT_PARENT="${OUT_PARENT:-$HOME}"
ENV_FILE="${ENV_FILE:-$BOT_DIR/.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
NAME="stock-agent-snapshot-$STAMP"
OUT="$OUT_PARENT/$NAME"
ARCHIVE="$OUT_PARENT/$NAME.tar.gz"

case "$OUT_PARENT" in
  "$BOT_DIR"|"$BOT_DIR"/*)
    echo "Refusing to write the snapshot inside the repository ($BOT_DIR)." >&2
    exit 1 ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (it is used to redact secrets). Nothing was collected." >&2
  exit 1
fi

mkdir -p "$OUT"/{system,service,deploy,logs,state,alpaca,network} || exit 1

# run <output file> <command...>: capture a command's output, never fail the script.
run() {
  local target="$OUT/$1"; shift
  {
    echo "\$ $*"
    if command -v "$1" >/dev/null 2>&1; then
      "$@" 2>&1
      echo "[exit code: $?]"
    else
      echo "[command not found: $1]"
    fi
  } > "$target"
}

echo "Collecting snapshot into $OUT ..."

# ── System ────────────────────────────────────────────────────────────────
{
  echo "snapshot_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "server_local_time: $(date)"
  echo "hostname: $(hostname)"
  echo "bot_dir: $BOT_DIR"
  echo "service: $SERVICE"
  echo "log_days: $LOG_DAYS"
} > "$OUT/meta.txt"
run system/uptime.txt uptime
run system/uname.txt uname -a
[ -r /etc/os-release ] && cp /etc/os-release "$OUT/system/os-release.txt"
run system/memory.txt free -m
run system/disk.txt df -h
run system/top-memory.txt bash -c "ps aux --sort=-%mem | head -n 15"
run system/timedatectl.txt timedatectl
run system/failed-units.txt systemctl --failed --no-pager
run system/reboots.txt bash -c "last -x reboot shutdown 2>/dev/null | head -n 20"
run system/python-processes.txt bash -c "pgrep -af 'python' || echo 'no python processes'"

# ── Service ───────────────────────────────────────────────────────────────
run service/status.txt systemctl status "$SERVICE" --no-pager -l
run service/show.txt systemctl show "$SERVICE" --no-pager \
  -p ActiveState -p SubState -p UnitFileState -p NRestarts -p Result \
  -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p ExecMainStatus \
  -p MemoryCurrent -p MemoryPeak -p User -p WorkingDirectory
run service/unit-file.txt systemctl cat "$SERVICE" --no-pager
run service/start-stop-history.txt bash -c \
  "journalctl -u '$SERVICE' --since '120 days ago' --no-pager -o short-iso 2>/dev/null \
   | grep -E 'systemd\[1\]|Stock Agent starting' | tail -n 2000"

# ── Deploy state ──────────────────────────────────────────────────────────
if [ -d "$BOT_DIR/.git" ]; then
  run deploy/git-head.txt git -C "$BOT_DIR" log -1 --format='%H %cI %s'
  run deploy/git-status.txt git -C "$BOT_DIR" status --short --branch
  run deploy/git-recent.txt git -C "$BOT_DIR" log -15 --format='%h %cI %s'
  run deploy/git-remote.txt bash -c \
    "git -C '$BOT_DIR' remote -v | sed -E 's#://[^/@]+@#://REDACTED@#'"
else
  echo "No git checkout found at $BOT_DIR" > "$OUT/deploy/git-head.txt"
fi
run deploy/crontab.txt crontab -l
run deploy/cron-d.txt ls -la /etc/cron.d
if [ -r /var/log/auto_update.log ]; then
  tail -n 1000 /var/log/auto_update.log > "$OUT/deploy/auto_update.log.tail"
  wc -lc /var/log/auto_update.log > "$OUT/deploy/auto_update.log.size"
else
  echo "/var/log/auto_update.log not found" > "$OUT/deploy/auto_update.log.tail"
fi
if [ -x "$BOT_DIR/venv/bin/python" ]; then
  run deploy/python-version.txt "$BOT_DIR/venv/bin/python" --version
  run deploy/pip-freeze.txt "$BOT_DIR/venv/bin/python" -m pip freeze
else
  run deploy/python-version.txt python3 --version
fi

# ── Bot state files ───────────────────────────────────────────────────────
DATA_DIR="${STOCK_DATA_DIR:-$BOT_DIR/data/stock_agent}"
if [ -d "$DATA_DIR" ]; then
  run state/listing.txt bash -c "ls -la --time-style=long-iso -R '$DATA_DIR' | head -n 400"
  run state/sizes.txt du -ah "$DATA_DIR"
  find "$DATA_DIR" -maxdepth 1 -type f -name '*.json' -size -50M \
    -exec cp -p {} "$OUT/state/" \; 2>/dev/null
  find "$DATA_DIR" -maxdepth 1 -type f -size +50M > "$OUT/state/skipped-too-large.txt" 2>/dev/null
else
  echo "Data directory not found: $DATA_DIR" > "$OUT/state/listing.txt"
fi
python3 "$SCRIPT_DIR/snapshot_ledger.py" "${STATE_DB_PATH:-$DATA_DIR/state.db}" \
  > "$OUT/state/ledger-summary.json" 2> "$OUT/state/ledger-summary.errors"
for name in runtime-health.json deploy-status.json; do
  if [ -r "$BOT_DIR/data/$name" ]; then
    cp -p "$BOT_DIR/data/$name" "$OUT/deploy/$name"
  fi
done

# ── Logs ──────────────────────────────────────────────────────────────────
mkdir -p "$OUT/logs/files"
copied_logs=0
for log in "$DATA_DIR/logs/stock_agent.log" "$DATA_DIR/logs"/stock_agent.log.[0-9]*; do
  [ -f "$log" ] && [ ! -L "$log" ] || continue
  name="${log##*/}"
  [[ "$name" =~ ^stock_agent\.log(\.[0-9]+)?$ ]] || continue
  tail -c 1048576 -- "$log" > "$OUT/logs/files/$name.tail"
  copied_logs=$((copied_logs + 1))
  [ "$copied_logs" -lt 12 ] || break
done
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u "$SERVICE" --since "$LOG_DAYS days ago" --no-pager -o short-iso \
    2>/dev/null | tail -n 250000 > "$OUT/logs/journal.log"
  run logs/journal-disk-usage.txt journalctl --disk-usage
else
  echo "journalctl not available" > "$OUT/logs/journal.log"
fi

# ── Network and server posture ────────────────────────────────────────────
run network/listening-ports.txt ss -tlnp
run network/ufw.txt ufw status verbose
run network/nginx-status.txt systemctl status nginx --no-pager -l
run network/nginx-sites.txt bash -c "ls -la /etc/nginx/sites-enabled /etc/nginx/conf.d 2>&1"
run network/sshd-settings.txt bash -c \
  "sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|passwordauthentication|pubkeyauthentication|port) '"
run network/unattended-upgrades.txt systemctl is-enabled unattended-upgrades
run network/enabled-services.txt bash -c \
  "systemctl list-unit-files --type=service --state=enabled --no-pager | head -n 80"

# ── Alpaca (read-only) ────────────────────────────────────────────────────
REPORT="$SCRIPT_DIR/alpaca_readonly_report.py"
if [ -f "$REPORT" ] && [ -r "$ENV_FILE" ]; then
  python3 "$REPORT" --env-file "$ENV_FILE" --out "$OUT/alpaca" \
    > "$OUT/alpaca/run.log" 2>&1
  echo "[exit code: $?]" >> "$OUT/alpaca/run.log"
else
  echo "Skipped: need $REPORT and a readable $ENV_FILE" > "$OUT/alpaca/run.log"
fi

# ── Settings summary, redaction, and leak check (all in one Python pass) ──
BOT_DIR="$BOT_DIR" ENV_FILE="$ENV_FILE" OUT="$OUT" python3 - <<'PY'
import os
import re
import sys
from collections import Counter
from pathlib import Path

out = Path(os.environ["OUT"])
env_file = Path(os.environ["ENV_FILE"])
bot_dir = Path(os.environ["BOT_DIR"])

# Settings whose values are safe and useful to show.
SAFE = {
    "STOCK_AGENT_MODE", "ALPACA_BASE_URL", "OPTIONS_ENABLED", "MAX_POSITION_PCT",
    "MAX_TOTAL_EXPOSURE_PCT", "MAX_CLUSTER_PCT", "PRIORITY_SYMBOLS",
    "STOCK_LOG_LEVEL", "STOCK_DATA_DIR", "TIPRANKS_ENABLED",
    "TIPRANKS_MIN_SMART_SCORE", "TIPRANKS_UNIVERSE_LIMIT", "TRADING_MODE",
    "LOG_LEVEL",
}
# A value is scrubbed from the snapshot when its name looks like a credential,
# or when it is long enough that it could only be one. Short plain settings
# (numbers, on/off flags) are left alone so they do not mangle the state files.
SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASS|PRIVATE|CREDENTIAL|WEBHOOK|CHAT_ID|GUILD|ADDRESS|PROXY|_USER|_HOST|_CH_"
)


def is_secret(name, value):
    if name in SAFE or len(value) < 6:
        return False
    if SECRET_NAME.search(name.upper()):
        return True
    return len(value) >= 20 and " " not in value


secrets = {}
summary = ["# .env summary (names only; values shown only for non-secret settings)", ""]
if env_file.is_file():
    for raw in env_file.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in SAFE:
            summary.append(f"{key}={value}")
        elif value:
            summary.append(f"{key}=<set, {len(value)} characters>")
            if is_secret(key, value):
                secrets[value] = key
        else:
            summary.append(f"{key}=<empty>")
else:
    summary.append(f"{env_file} not found or not readable")
(out / "env-summary.txt").write_text("\n".join(summary) + "\n")

# Credential defaults written into the source code are secrets too.
default_pattern = re.compile(
    r"""environ\.get\(\s*["'](\w*(?:KEY|TOKEN|SECRET|PASS)\w*)["']\s*,\s*["']([^"']{6,})["']"""
)
for source in sorted((bot_dir / "stock_agent").glob("*.py")):
    try:
        for name, value in default_pattern.findall(source.read_text(errors="replace")):
            secrets.setdefault(value, name)
    except OSError:
        pass

# Patterns that catch credentials even if they are not in .env.
GENERIC = [
    (re.compile(r"(?i)\b(apikey|api_key|token|access_token|secret|password)=([^&\s\"'<>]{6,})"), r"\1=REDACTED"),
    (re.compile(r"/bot\d{5,}:[A-Za-z0-9_-]{20,}"), "/botREDACTED"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{20,}"), r"\1 REDACTED"),
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?bot\s+)[A-Za-z0-9._-]{20,}"), r"\1REDACTED"),
    (re.compile(r"(?i)(APCA-API-(?:KEY-ID|SECRET-KEY)['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]{8,}"), r"\1REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "sk-REDACTED"),
    (re.compile(r"\bpplx-[A-Za-z0-9]{20,}"), "pplx-REDACTED"),
]

ordered = sorted(secrets, key=len, reverse=True)
replaced = Counter()


def scrub(text):
    for value in ordered:
        if value in text:
            replaced[secrets[value]] += text.count(value)
            text = text.replace(value, f"REDACTED[{secrets[value]}]")
    for pattern, replacement in GENERIC:
        text, count = pattern.subn(replacement, text)
        if count:
            replaced["pattern match"] += count
    return text


# Work through every file one line at a time so a large journal does not need
# much memory on a small server.
files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "env-summary.txt")
for path in files:
    tmp = path.with_name(path.name + ".scrub")
    changed = False
    try:
        with path.open(errors="replace") as src, tmp.open("w") as dst:
            for line in src:
                clean = scrub(line)
                changed = changed or clean != line
                dst.write(clean)
        if changed:
            os.replace(tmp, path)
        else:
            tmp.unlink()
    except OSError as exc:
        print(f"Could not scrub {path.name}: {exc.strerror}", file=sys.stderr)
        sys.exit(4)

# Leak check: no secret value may remain anywhere in the snapshot.
leaks = []
for path in sorted(p for p in out.rglob("*") if p.is_file()):
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                for value in ordered:
                    if value in line:
                        leaks.append(f"{path.relative_to(out)}: {secrets[value]}")
    except OSError:
        leaks.append(f"{path.relative_to(out)}: unreadable")

report = ["# Redaction report", ""]
report.append(f"Secret values checked: {len(ordered)} (from .env and defaults in source code)")
if replaced:
    report.append("Replacements made:")
    report += [f"- {name}: {count}" for name, count in sorted(replaced.items())]
else:
    report.append("No secret values were found in the collected files.")
report.append("")
report.append("Leak check: " + ("FAILED" if leaks else "passed"))
report += [f"- {leak}" for leak in sorted(set(leaks))]
(out / "redaction-report.txt").write_text("\n".join(report) + "\n")

# A quick error digest so the audit does not have to scan the whole journal.
journal = out / "logs" / "journal.log"
if journal.is_file():
    levels = Counter()
    messages = Counter()
    line_pattern = re.compile(r"\b(ERROR|WARNING|CRITICAL)\b\s+(.*)")
    with journal.open(errors="replace") as handle:
        for line in handle:
            match = line_pattern.search(line)
            if match:
                levels[match.group(1)] += 1
                shape = re.sub(r"\d+(\.\d+)?", "N", match.group(2))[:140]
                messages[f"{match.group(1)} {shape}"] += 1
    digest = ["# Log digest", ""]
    digest += [f"{level}: {count}" for level, count in levels.most_common()] or ["No ERROR or WARNING lines found."]
    digest += ["", "## Most frequent messages (numbers replaced with N)", ""]
    digest += [f"{count:6d}  {message}" for message, count in messages.most_common(60)]
    (out / "logs" / "digest.txt").write_text("\n".join(digest) + "\n")

sys.exit(3 if leaks else 0)
PY
REDACT_STATUS=$?

if [ "$REDACT_STATUS" -ne 0 ]; then
  echo "" >&2
  echo "Redaction did not complete cleanly (status $REDACT_STATUS)." >&2
  echo "No archive was created. The unpacked files are in $OUT; delete them with:" >&2
  echo "    rm -rf '$OUT'" >&2
  exit 1
fi

cat > "$OUT/README.txt" <<EOF
Stock agent server snapshot, taken $(date -u +%Y-%m-%dT%H:%M:%SZ).

Private: contains account positions, orders, and balances. Do not commit or
publish. Secret values from .env were replaced with REDACTED (see
redaction-report.txt). The .env file itself is not included.

meta.txt              when and where this was taken
env-summary.txt       which settings exist; values only for non-secret ones
redaction-report.txt  what was scrubbed, and the leak check result
system/               memory, disk, uptime, processes
service/              systemd status, restart count, start/stop history
deploy/               git state on the server, cron, auto-update log, pip freeze
state/                bounded read-only ledger summary, JSON state files and sizes
logs/                 service journal (last $LOG_DAYS days), rotated file-log tails and error digest
network/              listening ports, firewall, nginx, ssh settings
alpaca/               read-only account report: summary.md plus raw JSON
EOF

tar -czf "$ARCHIVE" -C "$OUT_PARENT" "$NAME" && rm -rf "$OUT"

if [ ! -f "$ARCHIVE" ]; then
  echo "Could not create the archive. Unpacked files remain in $OUT." >&2
  exit 1
fi

echo ""
echo "Snapshot ready: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo ""
echo "Next:"
echo "  1. On your own computer, download it:"
echo "       scp root@<droplet-ip>:$ARCHIVE ."
echo "  2. Upload the file to your Claude Code session and say it is the droplet snapshot."
echo "  3. Afterwards, delete it from the server:"
echo "       rm '$ARCHIVE'"
