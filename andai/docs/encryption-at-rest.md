# Encryption at rest

munshi.ai keeps meeting data on infrastructure the operator runs. Part of protecting it at rest is
done by munshi; the rest is the operator's duty, and `make -C andai encryption-check` lets a Compose
operator check their part.

## What munshi encrypts

- **Secrets in the andai database.** TOTP secrets and each org's platform API key are
  envelope-encrypted with AES-256-GCM under a key-encryption key (KEK) the operator provides
  (`ANDAI_KEK` / `ANDAI_KEK_FILE`). The KEK never reaches the database. It can be rotated without
  downtime: `runbooks/key-rotation.md`. On Kubernetes the KEK is a Secret the operator creates,
  mounted read-only into andai-core and andai-worker as files (`ANDAI_KEK_FILE`,
  `ANDAI_KEK_RETIRED_FILE`); it is never an environment value.
- **Recordings on Kubernetes with the chart's SeaweedFS.** The andai Helm chart sets the recordings
  bucket's default encryption to SSE-S3 (AES256), keyed by a SeaweedFS KEK from an operator Secret
  (`WEED_S3_SSE_KEK`), so every object meeting-api writes is encrypted by the object store
  (`kubernetes.md`). With your own bucket (`seaweedfs.enabled=false`) its own encryption applies.

Nothing else is encrypted by munshi: the rest of the Postgres database (meetings, transcripts,
minutes, users), the object store on Compose (versitygw writes plain files), SeaweedFS's filer
metadata and indexes, Valkey and the backups are as safe as the disks and media they sit on.
Postgres has no built-in encryption at rest.

## What the operator must provide

- **Encrypted disks for Postgres and object storage.** On Linux, put Docker's data root (or at
  least its volumes directory) on a LUKS/dm-crypt device; on a cloud VM, use the provider's volume
  encryption; on Kubernetes, use an encrypted StorageClass (per-volume LUKS, or encrypted cloud
  volumes) or place the node's local storage path on dm-crypt. Docker Desktop on a Mac keeps
  volumes inside its Linux VM's disk image, which is not encrypted; the most that host gives is
  FileVault on the disk holding that image. Use it for development only.
- **Custody of the KEK.** Keep it outside the database and its backups, readable only by the
  andai-core and andai-worker containers (uid 10001). On Kubernetes, start k3s with
  `--secrets-encryption` (or use your platform's equivalent), so the KEK Secrets (andai's and
  SeaweedFS's SSE-S3 key) are not stored in the cluster datastore in clear. SeaweedFS documents no
  rotation for its SSE-S3 key: keep it, backed up apart from the data, for as long as the data.
- **Encrypted backups.** A database dump holds every meeting in clear (only the sealed secrets stay
  sealed). Encrypt backups and keep their keys apart from the backups. Keep a retired KEK until
  every backup sealed under it has expired (`runbooks/key-rotation.md`, step 9).

## How to check (Compose)

    make -C andai encryption-check PROJECT=<compose project>

It finds the project's `postgres-data` and `storage-data` volumes and reports, for each, whether
it sits on encrypted storage. It only reads: it inspects the volumes' metadata and asks the host
which device holds them. It never reads volume contents, changes a disk, or needs sudo.

| Host | What it checks | Verdicts |
|---|---|---|
| Linux, native Docker | the volume's filesystem source (`findmnt`) and every device under it (`lsblk`): a `crypt` layer means dm-crypt/LUKS | `encrypted` (exit 0), `NOT encrypted` (exit 1) |
| macOS, Docker Desktop | the disk holding Docker Desktop's disk image (`df`), and its FileVault state (`fdesetup`, or `diskutil` for another disk) | `host-encrypted (FileVault)` (exit 0), `NOT encrypted` (exit 1) |
| anything else | nothing it can tell: another OS, Docker Desktop on Linux, Colima or OrbStack, a remote Docker host, a non-local volume driver, a filesystem that is not a block device (ZFS, NFS) | `unknown` (exit 2) |

A volume that does not exist (the stack is not up) is also `unknown`. When any volume is `NOT
encrypted` the exit status is 1, otherwise 2 if any is unknown. `make -C andai up` runs the same
check at the end and prints a warning when the volumes are not proved encrypted; it never fails the
bring-up.

What the check does not prove: that the device's key is held safely, that backups are encrypted,
or anything inside Docker Desktop's VM. On Linux, Docker's volume directories are readable by root
only, so the check resolves the nearest directory it can see (it prints which); if
`/var/lib/docker/volumes` is a separate mount from `/var/lib/docker`, check that mount by hand
(`sudo findmnt -T /var/lib/docker/volumes`, `lsblk -s <device>`).

On Kubernetes, check each node by hand: `findmnt -no SOURCE -T <persistent volume path>` should
be a `/dev/mapper/…` device, and `lsblk -o NAME,TYPE` should show `crypt` under it.
