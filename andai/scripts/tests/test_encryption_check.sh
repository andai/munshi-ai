#!/usr/bin/env bash
# Tests for andai/scripts/encryption_check.sh with stub `docker`, `uname`, `findmnt`, `lsblk`, `df`,
# `fdesetup` and `diskutil` first on PATH; no Docker, no real disks. Each stub answers from
# STUB_* variables. Run: bash andai/scripts/tests/test_encryption_check.sh
set -u

here="$(cd "$(dirname "$0")" && pwd)"
check="$here/../encryption_check.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stubs="$work/bin"
mkdir -p "$stubs"

# docker: STUB_VOLUMES lists the compose volume keys that exist (named "<project>_<key>"), with
# driver STUB_DRIVER (default local) and mountpoints under STUB_VOLROOT.
cat > "$stubs/docker" <<'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "context inspect") echo "unix:///var/run/docker.sock" ;;
  "info --format") echo "${STUB_ENGINE_OS:-Ubuntu 24.04 LTS}" ;;
  "volume ls")
    project="" key=""
    for arg in "$@"; do
      case "$arg" in
        label=com.docker.compose.project=*) project="${arg#*=}"; project="${project#*=}" ;;
        label=com.docker.compose.volume=*) key="${arg#*=}"; key="${key#*=}" ;;
      esac
    done
    for v in ${STUB_VOLUMES-postgres-data storage-data}; do
      [ "$v" = "$key" ] && echo "${project}_$key"
    done
    ;;
  "volume inspect")
    format="$4" name="$5"
    case "$format" in
      *Driver*) echo "${STUB_DRIVER:-local}" ;;
      *Options*) echo "" ;;
      *Mountpoint*) echo "${STUB_VOLROOT:-/var/lib/docker/volumes}/$name/_data" ;;
    esac
    ;;
  *) exit 1 ;;
esac
STUB
cat > "$stubs/uname" <<'STUB'
#!/usr/bin/env bash
echo "${STUB_OS:-Linux}"
STUB
# findmnt: paths under /var/lib/docker/volumes are root-only (fails), like the real host.
cat > "$stubs/findmnt" <<'STUB'
#!/usr/bin/env bash
path="${!#}"
case "$path" in /var/lib/docker/volumes*) exit 1 ;; esac
echo "${STUB_SOURCE:-/dev/mapper/data}"
STUB
cat > "$stubs/lsblk" <<'STUB'
#!/usr/bin/env bash
[ -n "${STUB_LSBLK_FAIL:-}" ] && exit 32
printf '%b\n' "${STUB_TYPES:-lvm\ncrypt\npart\ndisk}"
STUB
cat > "$stubs/df" <<'STUB'
#!/usr/bin/env bash
echo "Filesystem 512-blocks Used Available Capacity Mounted on"
echo "/dev/disk3s5 100 50 50 50% ${STUB_DF_MOUNT:-/System/Volumes/Data}"
STUB
cat > "$stubs/fdesetup" <<'STUB'
#!/usr/bin/env bash
echo "${STUB_FDE:-FileVault is On.}"
STUB
cat > "$stubs/diskutil" <<'STUB'
#!/usr/bin/env bash
echo "   FileVault:                 ${STUB_DISKUTIL_FV:-Yes}"
STUB
chmod +x "$stubs"/*

# A Docker Desktop disk image and a "volume" holding a plaintext marker the check must never print.
home="$work/home"
mkdir -p "$home/Library/Containers/com.docker.docker/Data/vms/0/data"
: > "$home/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"
volroot="$work/volumes"
mkdir -p "$volroot/andai-e2e_postgres-data/_data"
echo "PLAINTEXT-MARKER-7f3a" > "$volroot/andai-e2e_postgres-data/_data/secret.txt"

failures=0
out=""
status=0
run() { # env assignments...
  out="$(env -i PATH="$stubs:/usr/bin:/bin" HOME="$home" DOCKER_HOST=unix:///stub.sock "$@" \
    bash "$check" andai-e2e 2>&1)"
  status=$?
}
expect() { # name expected-status needle...
  local name="$1" want="$2"
  shift 2
  local ok=1 needle
  [ "$status" -eq "$want" ] || ok=0
  for needle in "$@"; do
    printf '%s' "$out" | grep -qF -- "$needle" || ok=0
  done
  if printf '%s' "$out" | grep -qF "PLAINTEXT-MARKER"; then ok=0; fi
  if [ "$ok" -eq 1 ]; then
    echo "ok   $name"
  else
    echo "FAIL $name (exit $status, want $want)"
    printf '%s\n' "$out" | sed 's/^/     /'
    failures=$((failures + 1))
  fi
}

run
expect "linux dm-crypt → encrypted" 0 \
  "postgres-data: encrypted" "storage-data: encrypted" "resolved at /var/lib/docker" \
  "every data volume is on encrypted storage"

run STUB_TYPES='part\ndisk' STUB_SOURCE=/dev/sda2
expect "linux plain disk → NOT encrypted" 1 "postgres-data: NOT encrypted" "/dev/sda2" \
  "NOT encrypted at rest"

run STUB_SOURCE='/dev/mapper/data[/@docker]'
expect "btrfs subvolume source is reduced to its device" 0 "on /dev/mapper/data, over a dm-crypt"

run STUB_SOURCE=tank/docker
expect "zfs dataset → unknown" 2 "postgres-data: unknown" "not a block device"

run STUB_LSBLK_FAIL=1
expect "lsblk cannot answer → unknown" 2 "lsblk cannot describe"

run STUB_OS=FreeBSD STUB_ENGINE_OS=FreeBSD
expect "unknown host → unknown" 2 "postgres-data: unknown" "not a host this check knows"

run STUB_OS=Darwin STUB_ENGINE_OS="Docker Desktop"
expect "macOS Docker Desktop, FileVault on → host-encrypted" 0 \
  "postgres-data: host-encrypted (FileVault)" "FileVault on" "the image is not dm-crypt" \
  "Docker.raw" "VM disk image itself is not encrypted"

run STUB_OS=Darwin STUB_ENGINE_OS="Docker Desktop" STUB_FDE="FileVault is Off."
expect "macOS Docker Desktop, FileVault off → NOT encrypted" 1 "NOT encrypted" "FileVault off"

run STUB_OS=Darwin STUB_ENGINE_OS="Docker Desktop" STUB_FDE="Encryption in progress"
expect "macOS FileVault state unclear → unknown" 2 "FileVault state not reported"

run STUB_OS=Darwin STUB_ENGINE_OS="Docker Desktop" STUB_DF_MOUNT=/Volumes/External
expect "macOS image on a FileVault external disk → host-encrypted" 0 "/Volumes/External" \
  "host-encrypted (FileVault)"

run STUB_OS=Darwin STUB_ENGINE_OS="Docker Desktop" STUB_DF_MOUNT=/Volumes/External \
  STUB_DISKUTIL_FV=No
expect "macOS image on an unencrypted external disk → NOT encrypted" 1 "/Volumes/External" \
  "FileVault off"

run STUB_OS=Darwin STUB_ENGINE_OS="Colima"
expect "macOS without Docker Desktop → unknown" 2 "not a host this check knows"

run STUB_OS=Linux STUB_ENGINE_OS="Docker Desktop"
expect "Docker Desktop on Linux → unknown" 2 "VM disk image"

run STUB_VOLUMES=postgres-data
expect "missing volume → unknown" 2 "storage-data: unknown" "is the stack up?"

run STUB_DRIVER=nfs
expect "non-local volume driver → unknown" 2 "uses driver nfs"

run STUB_VOLROOT="$volroot" STUB_TYPES='disk'
expect "volume contents are never printed" 1 "andai-e2e_postgres-data/_data"

out="$(env -i PATH="$stubs:/usr/bin:/bin" HOME="$home" DOCKER_HOST=tcp://10.0.0.5:2376 \
  bash "$check" andai-e2e 2>&1)"
status=$?
expect "remote Docker host → unknown" 2 "the Docker host is remote (tcp)"

out="$(env -i PATH="$stubs:/usr/bin:/bin" bash "$check" 2>&1)"
status=$?
expect "no project → usage" 2 "usage:"

if [ "$failures" -ne 0 ]; then
  echo "$failures failed"
  exit 1
fi
echo "all passed"
