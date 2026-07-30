#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"

command="$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(data.get("tool_input", {}).get("command", ""))
' 2>/dev/null || true)"

if [[ -z "$command" ]]; then
  exit 0
fi

if printf '%s' "$command" | grep -Eq 'terraform[[:space:]]+(apply|destroy)' \
   && printf '%s' "$command" | grep -q 'environments/prod'; then
  echo "BLOCKED: terraform apply/destroy against infrastructure/environments/prod is not allowed via an automated tool call. This is a hard guardrail (see CLAUDE.md), not a judgment call — ask the user to run it manually after explicit human review." >&2
  exit 2
fi

if printf '%s' "$command" | grep -Eq 'git[[:space:]]+push([[:space:]]+\S+)*[[:space:]]+(--force\b|--force-with-lease\b|-f\b)'; then
  echo "BLOCKED: git push --force (or -f / --force-with-lease) is not allowed via an automated tool call. This is a hard guardrail (see CLAUDE.md), not a judgment call — ask the user to run it manually if truly needed." >&2
  exit 2
fi

exit 0
