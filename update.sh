#!/usr/bin/env bash
# FortiGate OT Lab - controller bijwerken naar de nieuwste versie uit git.
#
# Draait IN de controller-container, vanuit de web UI (Updates) of vanaf de
# Proxmox host via de installer-oneliner (kies "Bijwerken").
#
#   update.sh            alleen bijwerken als er nieuwe commits zijn
#   update.sh --always   ook zonder nieuwe commits dependencies + herstart
#
# Instellingen en uploads staan in data/ (niet in git) en blijven behouden.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALWAYS=0
[[ ${1:-} == --always ]] && ALWAYS=1
cd "$ROOT"

say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
OLD="$(git rev-parse HEAD)"
say "Huidige versie: $(git log -1 --format='%h %s' HEAD) (branch $BRANCH)"

say "Nieuwe versie ophalen..."
git fetch --quiet origin "$BRANCH"
NEW="$(git rev-parse FETCH_HEAD)"

if [[ $OLD == "$NEW" ]]; then
  say "Al up-to-date."
  (( ALWAYS )) || exit 0
else
  DIRTY="$(git status --porcelain --untracked-files=no)"
  if [[ -n $DIRTY ]]; then
    say "FOUT: er zijn lokale wijzigingen in getrackte bestanden:"
    echo "$DIRTY"
    say "Zet ze veilig en draai: git -C $ROOT checkout -- <bestand>   (of: git stash)"
    exit 1
  fi
  say "Wijzigingen:"
  git log --format='  %h %s' "$OLD..$NEW"
  git merge --quiet --ff-only "$NEW"
fi

say "Python-dependencies bijwerken..."
.venv/bin/pip install -q -r controller/requirements.txt

if [[ $OLD != "$NEW" ]] && ! git diff --quiet "$OLD" "$NEW" -- otsim; then
  say "Let op: de OT-simulators zijn gewijzigd. Klik in de web UI op 'Simulators bijwerken'."
fi

say "Controller herstarten..."
systemctl restart otlab.service
say "Klaar: $(git log -1 --format='%h %s')"
