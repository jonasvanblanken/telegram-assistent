#!/usr/bin/env bash
# Apply the local patches to a checkout of
#   * delta endpoint (GET /api/messages/since) — the whole delta in ONE call
#   * OpenAI transcription backend (upstream ships only a stub)
#   * transcription allow-list (upstream defaults to a deny-list)
# against
# adelaidasofia/whatsapp-mcp.
#
#   ./apply.sh /opt/whatsapp-mcp/app
#
# Verified against tag v0.4.1. If upstream has since touched the two anchor
# regions, `git apply` refuses and this script stops — that is the point.
# Silently half-applying would produce a bridge that compiles and never
# transcribes, which is the failure mode this patch exists to remove.
set -euo pipefail

REPO="${1:?usage: apply.sh <path-to-whatsapp-mcp-checkout>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -d "$REPO/whatsapp-bridge" ]] || { echo "ERROR: $REPO is not a whatsapp-mcp checkout" >&2; exit 1; }

# The added .go files are copied unconditionally — copying is idempotent, and
# guarding it was a real bug: a guard keyed on ONE artifact short-circuited the
# whole function when a LATER patch added more files, so the new ones silently
# never landed. Only the diff needs a guard, because git apply is not idempotent.
cp "$HERE/whisper_openai.go"         "$REPO/whatsapp-bridge/whisper_openai.go"
cp "$HERE/whisper_openai_test.go"    "$REPO/whatsapp-bridge/whisper_openai_test.go"
cp "$HERE/whisper_allowlist.go"      "$REPO/whatsapp-bridge/whisper_allowlist.go"
cp "$HERE/whisper_allowlist_test.go" "$REPO/whatsapp-bridge/whisper_allowlist_test.go"
cp "$HERE/delta.go"                  "$REPO/whatsapp-bridge/delta.go"
cp "$HERE/delta_test.go"             "$REPO/whatsapp-bridge/delta_test.go"

# Idempotency, done the only way that actually holds as the patch set grows:
# reset every file the diff touches back to the pristine tag, then apply the
# whole diff. A "have I applied this already?" marker cannot work here — the
# diff is cumulative, so after the first patch lands a second one is always
# "partly applied", and git apply refuses all-or-nothing. Restoring first makes
# the operation a pure function of the diff.
TARGETS="$(awk '/^\+\+\+ b\//{sub(/^\+\+\+ b\//,""); print}' "$HERE/openai-backend.diff")"
[[ -n "$TARGETS" ]] || { echo "ERROR: no targets found in openai-backend.diff" >&2; exit 1; }
# shellcheck disable=SC2086
git -C "$REPO" checkout -- $TARGETS

if ! git -C "$REPO" apply --check "$HERE/openai-backend.diff" 2>/dev/null; then
  cat >&2 <<MSG
ERROR: openai-backend.diff does not apply cleanly to this checkout.

Upstream has changed one of the two patched regions:
  - whatsapp-bridge/whisper.go   (the transcribeOpenAI stub + the backend switch)
  - whatsapp-bridge/config.go    (the Whisper config block)

Do NOT force this. Re-derive the patch against the new upstream, confirm
'go test ./...' still passes, then re-run. A half-applied patch yields a
bridge that starts fine and transcribes nothing.
MSG
  rm -f "$REPO/whatsapp-bridge/whisper_openai.go" "$REPO/whatsapp-bridge/whisper_openai_test.go" \
        "$REPO/whatsapp-bridge/whisper_allowlist.go" "$REPO/whatsapp-bridge/whisper_allowlist_test.go" \
        "$REPO/whatsapp-bridge/delta.go" "$REPO/whatsapp-bridge/delta_test.go"
  exit 1
fi

git -C "$REPO" apply "$HERE/openai-backend.diff"
echo "    Patches angewendet (Delta-Endpunkt + OpenAI-Backend + Transkriptions-Allow-Liste)"
