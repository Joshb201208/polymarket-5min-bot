#!/bin/bash
# SessionStart hook. Whatever this prints is added to Claude's context.
# In cloud sessions it also installs the Python dependencies so tests can run.
# Always exits 0: a failed install must not stop the session from starting.

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
  python3 -m pip install -q -r requirements.txt -r requirements-dev.txt >/dev/null 2>&1 \
    || python3 -m pip install -q --break-system-packages -r requirements.txt -r requirements-dev.txt >/dev/null 2>&1 \
    || echo "Note: dependency install failed. Run: python3 -m pip install -r requirements.txt -r requirements-dev.txt"
fi

phase=$(sed -e 's/#.*//' -e 's/[[:space:]]//g' docs/revamp/PHASE 2>/dev/null | grep -m1 . | tr '[:upper:]' '[:lower:]')
phase=${phase:-audit}
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)

echo "Revamp phase: ${phase} (from docs/revamp/PHASE). Current branch: ${branch:-unknown}."
case "$phase" in
  audit) echo "Audit phase: production code is read-only. Follow docs/revamp/audit/PLAYBOOK.md and write findings under docs/revamp/audit/." ;;
  plan)  echo "Plan phase: production code is read-only. Turn the approved audit into docs/revamp/PLAN.md using docs/revamp/PLAN_TEMPLATE.md." ;;
  build) echo "Build phase: implement one approved item from docs/revamp/PLAN.md per pull request, with tests." ;;
  *)     echo "Unknown phase value. Treat it as audit and ask Josh." ;;
esac
echo "Pushing to master deploys to the live trading bot. Work on a branch and open a pull request."
exit 0
