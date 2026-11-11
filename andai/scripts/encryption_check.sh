#!/usr/bin/env bash
# encryption_check.sh PROJECT — do a Compose project's Postgres and object-storage volumes sit on
# encrypted storage? (andai/docs/encryption-at-rest.md; `make -C andai encryption-check PROJECT=…`)
#
# Read-only: it inspects the volumes' metadata with `docker volume ls/inspect` and asks the host
# which block device holds them (`findmnt`, `lsblk` on Linux; `df`, `fdesetup`, `diskutil` on
# macOS).
# It never reads volume contents, changes a disk or volume, or needs sudo. It prints paths and
# verdicts only.
#
#   Linux, native Docker: the volume's mountpoint → its filesystem source → that device and every
#     device under it; a `crypt` (dm-crypt/LUKS) layer anywhere → encrypted.
#   macOS, Docker Desktop: volumes live inside the Linux VM's disk image (Docker.raw), which is not
#     dm-crypt; the check reports whether the host volume holding that image is FileVault-encrypted
#     ("host-encrypted (FileVault)", exit 0) and says so.
#   Anything else (another OS, Docker Desktop on Linux, Colima/OrbStack, a remote Docker host, a
#   non-local volume driver, a filesystem that is not a block device): unknown — check by hand.
#
# Exit status: 0 every volume is on encrypted storage; 1 at least one is NOT; 2 otherwise (unknown,
# volume not found, usage).
set -u

project="${1:-}"
if [ -z "$project" ]; then
  echo "usage: encryption_check.sh PROJECT   (e.g. andai-e2e)" >&2
  exit 2
fi

# Compose volume keys of the stack's data: Postgres (andai and the platform) and versitygw's store.
VOLUMES="postgres-data storage-data"
DOC="andai/docs/encryption-at-rest.md"
DESKTOP_IMAGE="$HOME/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"

worst=0
host_only=""
say() { # volume verdict detail
  echo "encryption-check: $project $1: $2 — $3"
  case "$2" in
    "NOT encrypted") worst=1 ;;
    unknown) [ "$worst" -eq 0 ] && worst=2 ;;
    host-encrypted*) host_only=1 ;;
  esac
}

endpoint="${DOCKER_HOST:-}"
if [ -z "$endpoint" ]; then
  endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
fi
host_os="$(uname -s 2>/dev/null || echo unknown)"
engine_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || true)"

# Sets $path to the host path holding a volume's data, or leaves it empty with a reason in $why.
path=""
why=""
host_path_for() { # mountpoint
  path=""
  why=""
  case "$engine_os" in
    *"Docker Desktop"*)
      if [ "$host_os" = Darwin ] && [ -e "$DESKTOP_IMAGE" ]; then
        path="$DESKTOP_IMAGE"
      elif [ "$host_os" = Darwin ]; then
        why="Docker Desktop's disk image is not at its default path; check the disk holding it"
      else
        why="Docker Desktop keeps volumes in a VM disk image; check the filesystem holding it"
      fi
      ;;
    *)
      if [ "$host_os" = Linux ]; then
        path="$1"
      else
        why="Docker on $host_os (${engine_os:-unknown engine}) is not a host this check knows"
      fi
      ;;
  esac
}

check_linux() { # volume path
  local probe="$2" source="" types
  # Walk up until findmnt can resolve the path (Docker's volume directories are root-only).
  while [ -n "$probe" ]; do
    source="$(findmnt -no SOURCE -T "$probe" 2>/dev/null | head -n 1)"
    [ -n "$source" ] && break
    [ "$probe" = / ] && break
    probe="$(dirname "$probe")"
  done
  source="${source%%\[*}"
  if [ -z "$source" ]; then
    say "$1" unknown "cannot resolve the filesystem holding $2"
    return
  fi
  case "$source" in
    /dev/*) ;;
    *)
      say "$1" unknown "$2 (resolved at $probe) is on $source, not a block device; check by hand"
      return
      ;;
  esac
  if ! types="$(lsblk -lnso TYPE "$source" 2>/dev/null)" || [ -z "$types" ]; then
    say "$1" unknown "lsblk cannot describe $source (holding $2, resolved at $probe)"
    return
  fi
  if printf '%s\n' "$types" | grep -qx '[[:space:]]*crypt[[:space:]]*'; then
    say "$1" encrypted "$2 (resolved at $probe) is on $source, over a dm-crypt device"
  else
    say "$1" "NOT encrypted" "$2 (resolved at $probe) is on $source, with no dm-crypt layer"
  fi
}

check_macos() { # volume image mountpoint-in-vm
  local mount status
  mount="$(df -P "$2" 2>/dev/null | awk 'NR == 2 { print $NF }')"
  local where="the Docker Desktop disk image $2 (volume $3 in the VM; the image is not dm-crypt)"
  case "$mount" in
    / | /System/Volumes/Data)
      status="$(fdesetup status 2>/dev/null || true)"
      case "$status" in
        *"FileVault is On."*)
          say "$1" "host-encrypted (FileVault)" "$where is on the startup disk, FileVault on" ;;
        *"FileVault is Off."*)
          say "$1" "NOT encrypted" "$where is on the startup disk, FileVault off" ;;
        *) say "$1" unknown "$where; FileVault state not reported (${status:-no answer})" ;;
      esac
      ;;
    "")
      say "$1" unknown "cannot find the disk holding $where"
      ;;
    *)
      status="$(diskutil info "$mount" 2>/dev/null \
        | awk -F: '/^ *FileVault:/ { gsub(/ /, "", $2); print $2 }')"
      case "$status" in
        Yes) say "$1" "host-encrypted (FileVault)" "$where is on $mount, FileVault on" ;;
        No) say "$1" "NOT encrypted" "$where is on $mount, FileVault off" ;;
        *) say "$1" unknown "$where is on $mount; its FileVault state is not reported" ;;
      esac
      ;;
  esac
}

for key in $VOLUMES; do
  case "$endpoint" in
    "" | unix://*) ;;
    *)
      say "$key" unknown "the Docker host is remote (${endpoint%%://*}); run this check there"
      continue
      ;;
  esac
  name="$(docker volume ls -q --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.volume=$key" 2>/dev/null | head -n 1)"
  if [ -z "$name" ]; then
    say "$key" unknown "no such volume in project $project (is the stack up?)"
    continue
  fi
  driver="$(docker volume inspect -f '{{.Driver}}' "$name" 2>/dev/null || true)"
  options="$(docker volume inspect -f '{{if .Options}}set{{end}}' "$name" 2>/dev/null || true)"
  mountpoint="$(docker volume inspect -f '{{.Mountpoint}}' "$name" 2>/dev/null || true)"
  if [ "$driver" != local ] || [ -n "$options" ] || [ -z "$mountpoint" ]; then
    say "$key" unknown \
      "volume $name uses driver ${driver:-?}${options:+ with options}; check by hand"
    continue
  fi
  host_path_for "$mountpoint"
  if [ -z "$path" ]; then
    say "$key" unknown "$why"
  elif [ "$host_os" = Darwin ]; then
    check_macos "$key" "$path" "$mountpoint"
  else
    check_linux "$key" "$path"
  fi
done

case "$worst" in
  0)
    if [ -n "$host_only" ]; then
      echo "encryption-check: $project: every data volume is on a FileVault-encrypted host disk;" \
        "Docker Desktop's VM disk image itself is not encrypted"
    else
      echo "encryption-check: $project: every data volume is on encrypted storage"
    fi
    ;;
  1) echo "encryption-check: $project: data is NOT encrypted at rest ($DOC)" ;;
  *) echo "encryption-check: $project: unknown; check the volumes above by hand ($DOC)" ;;
esac
exit "$worst"
