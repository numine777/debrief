#!/usr/bin/env bash
# Imports this flake into a Home Manager flake, as the README shows, and
# activates it for the current user: over a manual `debrief install`, again
# with nothing to change, then switched to a configuration without Claude Code.
#
# For disposable CI machines only: it rewrites the real home directory.
# NIXPKGS and HOME_MANAGER pick the consumer's inputs (default: 26.05).
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
system=$(nix eval --impure --raw --expr builtins.currentSystem)
sed -e "s|@DEBRIEF@|git+file://$root|" \
  -e "s|@NIXPKGS@|${NIXPKGS:-github:NixOS/nixpkgs/nixos-26.05}|" \
  -e "s|@HOME_MANAGER@|${HOME_MANAGER:-github:nix-community/home-manager/release-26.05}|" \
  -e "s|@SYSTEM@|$system|" -e "s|@USER@|$(id -un)|" -e "s|@HOME@|$HOME|" \
  "$root/nix/e2e/flake.nix.in" >"$work/flake.nix"
flake="path:$work"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}
has() { grep -qF -- "$2" "$1" || fail "$1 lacks: $2"; }
lacks() { if grep -qF -- "$2" "$1"; then fail "$1 still has: $2"; fi; }
in_store() { [[ $(readlink -f "$1") == /nix/store/* ]] || fail "$1 is not linked into the Nix store"; }
# What one activation step printed, from a captured activation.
step_output() { awk -v step="$1" '/^Activating /{p = ($2 == step); next} p' <<<"$2"; }
records() { cat .claude/CLAUDE.md .codex/AGENTS.md .claude/settings.json; }

cd "$HOME"

echo "::group::Install Debrief by hand, as before the module"
nix build "$flake#homeConfigurations.full.config.programs.debrief.package" -o "$work/debrief"
mkdir -p .claude
printf '# My rules\n' >.claude/CLAUDE.md
"$work/debrief/bin/debrief" install --harness claude,codex --claude-settings
echo "::endgroup::"

echo "::group::Activate the configuration with Claude Code and Codex"
nix build "$flake#homeConfigurations.full.activationPackage" -o "$work/full"
"$work/full/activate"
echo "::endgroup::"
[[ ! -e .local/share/debrief/debrief.pyz && ! -e .local/bin/debrief ]] || fail "the manual install was left behind"
for f in .local/bin/debrief-session .agents/skills/ai-session .agents/skills/ai-session-closeout \
  .claude/skills/ai-session .claude/skills/ai-session-closeout; do
  in_store "$f"
done
[[ $(head -n1 .claude/CLAUDE.md) == "# My rules" ]] || fail "CLAUDE.md lost the user's text"
has .claude/CLAUDE.md '`bin/session` below means `~/.local/bin/debrief-session`'
has .codex/AGENTS.md "<!-- debrief:begin v"
has .claude/settings.json "$HOME/.local/bin/debrief-session context"

echo "::group::The command on PATH and an agent's first step"
export PATH="$HOME/.nix-profile/bin:${XDG_STATE_HOME:-$HOME/.local/state}/nix/profile/bin:$PATH"
debrief --version
repo="$work/repo"
git init -q -b main "$repo"
git -C "$repo" -c user.name=CI -c user.email=ci@example.com commit -q --allow-empty -m "Start"
debrief init "$repo"
out=$(cd "$repo" && "$HOME/.local/bin/debrief-session" start)
echo "$out"
grep -qF "leg-01 (new)" <<<"$out" || fail "debrief-session start did not open a leg"
grep -qF "$HOME/.agents/skills/ai-session/SKILL.md" <<<"$out" || fail "start did not point at the skill"
head -n3 .agents/skills/ai-session/SKILL.md
echo "::endgroup::"

echo "::group::The viewer service (informational)"
if curl -sf --retry 10 --retry-delay 2 --retry-all-errors -o /dev/null http://127.0.0.1:7319/; then
  echo "the service answers on 127.0.0.1:7319"
else
  echo "the service did not answer (no user service manager on this runner?)"
  if [[ $(uname) == Darwin ]]; then
    launchctl print "gui/$(id -u)/org.nix-community.home.debrief" 2>&1 | head -n 20 || true
    tail -n 20 "$HOME/Library/Logs/debrief.log" 2>/dev/null || true
  else
    systemctl --user status debrief 2>&1 | head -n 20 || true
  fi
fi
echo "::endgroup::"

echo "::group::Activate again: Debrief has nothing to change"
records >"$work/before"
out=$("$work/full/activate" 2>&1)
echo "$out"
echo "::endgroup::"
for step in debriefMigrate debrief; do
  [[ -z $(step_output "$step" "$out") ]] || fail "the second activation's $step step printed: $(step_output "$step" "$out")"
done
records | cmp -s - "$work/before" || fail "the second activation changed the instruction files"

echo "::group::Switch to a configuration with only Codex"
nix build "$flake#homeConfigurations.codex.activationPackage" -o "$work/codex"
"$work/codex/activate"
echo "::endgroup::"
[[ $(cat .claude/CLAUDE.md) == "# My rules" ]] || fail "CLAUDE.md keeps more than the user's text"
lacks .claude/settings.json debrief-session
[[ ! -e .claude/skills/ai-session ]] || fail "Claude Code's skill links remain"
has .codex/AGENTS.md "<!-- debrief:begin v"
in_store .agents/skills/ai-session

echo "Home Manager end-to-end: ok"
