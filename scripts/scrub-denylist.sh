#!/usr/bin/env bash
# Fail if any term from the private denylist appears in the tree or in any commit message.
#
# The terms come from $SCRUB_DENYLIST (newline-separated literal strings, matched case-insensitively),
# set from a repository secret in CI. Neither the terms nor the matching text are ever printed: a hit
# reports only file:line for the tree and the commit hash for messages.
set -euo pipefail
set +x

if [[ -z "${SCRUB_DENYLIST:-}" ]]; then
  echo "::warning::SCRUB_DENYLIST not set; personal-identifier check skipped"
  exit 0
fi

umask 077
f="$(mktemp)"
trap 'rm -f "$f"' EXIT
# Drop blank lines and CRs; mask every term before anything else can print it.
printf '%s\n' "$SCRUB_DENYLIST" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' > "$f" || true
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  while IFS= read -r term; do echo "::add-mask::$term"; done < "$f"
fi
if [[ ! -s "$f" ]]; then
  echo "::warning::SCRUB_DENYLIST has no terms; personal-identifier check skipped"
  exit 0
fi
echo "Checking the tree and all commit messages against $(wc -l < "$f" | tr -d ' ') denylist term(s)."

hits=0

# Tree: list matching files, then only the line numbers within each. The matching text is never printed.
tree_hits=0
while IFS= read -r -d '' file; do
  if (( !tree_hits )); then echo "Denylisted term found in the tree at:"; tree_hits=1; fi
  grep -n -i -F -f "$f" -- "$file" | cut -d: -f1 | while IFS= read -r line; do
    printf '  %s:%s\n' "$file" "$line"
  done
done < <(git grep -l -z -i -F -I -f "$f" -- . || true)
(( tree_hits )) && hits=1

# Commit messages on all refs: print the hash only.
msg_hits=0
while IFS= read -r sha; do
  if git log -1 --format='%B' "$sha" | grep -q -i -F -f "$f"; then
    if (( !msg_hits )); then echo "Denylisted term found in the message of commit(s):"; msg_hits=1; fi
    printf '  %s\n' "$sha"
  fi
done < <(git rev-list --all)
(( msg_hits )) && hits=1

if (( hits )); then
  echo "::error::Personal-identifier check failed (locations above; terms and matches are not shown)"
  exit 1
fi
echo "No denylisted terms found."
