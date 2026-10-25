#!/usr/bin/env bash
# vm.sh — move code to the dev VM through git, and run commands there (D-20).
#
#   andai/scripts/vm.sh sync         push the current feat/phase1-* branch to origin and fast-forward
#                                    the VM's checkout to exactly the same commit
#   andai/scripts/vm.sh run '<cmd>'  run <cmd> on the VM inside the checkout, streaming its output;
#                                    exits with the command's exit code
#
# Environment:
#   VM_HOST      ssh target of the dev VM (required), e.g. an ~/.ssh/config alias such as dev-vm (keep real hosts out of the repo)
#   VM_REPO_DIR  checkout path on the VM (default: ~/andai-meet)
#
# Code reaches the VM and evidence comes back only through git and this script, never by hand.
# `sync` WIP-commits pending changes on the feature branch first, so every VM run sees the latest
# local files. It refuses to commit .env files or andai/.ci/, and refuses to run on development/main.
set -euo pipefail

VM_REPO_DIR="${VM_REPO_DIR:-~/andai-meet}"
WIP_MESSAGE="wip(phase1): sync to dev VM"
# Untracked local state that must never ride along in a WIP commit (GSDD runtime state).
EXCLUDES=(':(exclude).work/state.json')

die() { echo "vm.sh: $*" >&2; exit 1; }
usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

need_vm_host() {
  [ -n "${VM_HOST:-}" ] || die "set VM_HOST to the dev VM's ssh target (see andai/docs/dev-vm.md)"
}

cmd_sync() {
  need_vm_host
  local root branch staged bad local_sha vm_sha
  root="$(git rev-parse --show-toplevel)"
  cd "$root"

  branch="$(git rev-parse --abbrev-ref HEAD)"
  case "$branch" in
    development|main|master|HEAD) die "refusing to sync from '$branch'; switch to a feat/phase1-* branch" ;;
  esac
  [[ "$branch" =~ ^feat/phase1-[A-Za-z0-9._-]+$ ]] || die "only feat/phase1-* branches sync (current: $branch)"
  git remote get-url origin >/dev/null 2>&1 || die "no 'origin' remote; it is added once the GitHub repo exists (andai/docs/dev-vm.md)"

  # WIP-commit pending changes so the VM sees exactly the local files.
  if [ -n "$(git status --porcelain -- . "${EXCLUDES[@]}")" ]; then
    git add -A -- . "${EXCLUDES[@]}"
    staged="$(git diff --cached --name-only)"
    bad="$(printf '%s\n' "$staged" | grep -E '(^|/)\.env$|^andai/\.ci/' || true)"
    if [ -n "$bad" ]; then
      git reset -q
      die "refusing to commit secrets or CI scratch (index reset, working tree untouched):
$bad"
    fi
    if [ -n "$staged" ]; then
      git commit -q -m "$WIP_MESSAGE"
      echo "vm.sh: committed pending changes as '$WIP_MESSAGE'"
    fi
  fi

  if git ls-files | grep -qE '(^|/)\.env$'; then
    die "a .env file is tracked in git; remove it from the index before syncing"
  fi

  git push -u origin "$branch"
  # shellcheck disable=SC2029  # VM_REPO_DIR and branch are expanded locally on purpose (branch is validated above)
  ssh "$VM_HOST" "git -C $VM_REPO_DIR fetch origin && git -C $VM_REPO_DIR checkout $branch && git -C $VM_REPO_DIR pull --ff-only origin $branch"

  local_sha="$(git rev-parse HEAD)"
  # shellcheck disable=SC2029
  vm_sha="$(ssh "$VM_HOST" "git -C $VM_REPO_DIR rev-parse HEAD")"
  [ "$local_sha" = "$vm_sha" ] || die "VM is at $vm_sha but local HEAD is $local_sha"
  echo "vm.sh: VM at $vm_sha ($branch)"
}

cmd_run() {
  need_vm_host
  [ $# -ge 1 ] || die "usage: vm.sh run '<cmd>'"
  # shellcheck disable=SC2029  # the command is meant to be expanded on the VM
  ssh "$VM_HOST" "cd $VM_REPO_DIR && $*"
}

[ $# -ge 1 ] || usage
sub="$1"; shift
case "$sub" in
  sync) cmd_sync "$@" ;;
  run)  cmd_run "$@" ;;
  -h|--help|help) usage ;;
  *) usage ;;
esac
