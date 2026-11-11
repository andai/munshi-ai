# Rotating the key-encryption key (KEK)

andai-core keeps two kinds of secret in its database, each envelope-encrypted: the TOTP secret of
every two-step enrolment (`user_totp`) and each org's API key on the meeting platform
(`platform_principals`). Every value has its own data key; the KEK wraps that data key, and the row
records the `key_id` of the KEK that wrapped it (a truncated hash, not the key).

Rotating the KEK changes only the wrapping: `kek-rewrap` unwraps each data key with the old KEK and
wraps it again with the new one. The encrypted secrets themselves are never decrypted or rewritten,
so a rotation needs no downtime and can be run again safely if it stops half way.

This runbook rotates the KEK. It does not change the org API keys or anyone's TOTP secret.

## What you need

- The current KEK and the place you keep it (your key custody). Without it nothing sealed today can
  be opened.
- A backup of the `andai` database taken before you start.
- Shell access to the stack (Compose) or `kubectl` access to its namespace (Kubernetes).
- A test account with two-step sign-in, and a test meeting link (or, on a test stack, a mock link)
  for the smoke test.

Rehearse on a staging stack with synthetic data first. Never paste a key into a chat, ticket,
commit or command line, and never `cat` or `echo` it to a terminal: every step below moves keys
file to file.

## Where the keys live

| | Active KEK (seals and opens) | Retired KEKs (open only) |
|---|---|---|
| Compose | `ANDAI_KEK` in the env file | the file named by `ANDAI_KEK_RETIRED_FILE`, mounted read-only into andai-core and andai-worker |
| Kubernetes | Secret key `active` (mounted as `ANDAI_KEK_FILE`) | the same Secret's key `retired` (mounted as `ANDAI_KEK_RETIRED_FILE`) |

A retired file holds one base64 key per line; blank lines and lines starting with `#` are ignored,
so you can note when each key was retired. andai-core and andai-worker refuse to start when a key
is malformed, listed twice, or listed both as active and as retired; the error names the line, never
the key.

## Steps

### 1. Check the starting point

    make -C andai kek-status

Every line shows a table, a key id, whether that key is `active`, `retired` or `not loaded`, and
how many values it wraps. Exit status 0 means every value is under the active key. Note the active
key id: it becomes the old key below.

On Kubernetes, run the same command in a pod:
`kubectl -n <ns> exec deploy/andai-core -- python -m andai_core kek-status`.

### 2. Generate the new key

On the host that holds your keys, into a file only you can read:

    umask 077
    openssl rand -base64 32 > /etc/munshi/andai-kek.new

### 3. Load the new key as active and the old key as retired, on every replica

**Compose.**

1. Copy the current `ANDAI_KEK` from the env file into the retired file, readable only by the
   containers' user (uid 10001):

       umask 077
       grep '^ANDAI_KEK=' .env | tail -n 1 | cut -d= -f2- > /etc/munshi/andai-kek-retired
       sudo chown 10001 /etc/munshi/andai-kek-retired && sudo chmod 0400 /etc/munshi/andai-kek-retired

   (If the file already lists keys from an unfinished earlier rotation, append instead:
   `grep '^ANDAI_KEK=' .env | tail -n 1 | cut -d= -f2- | sudo tee -a /etc/munshi/andai-kek-retired
   >/dev/null`.)
2. Put the new key into the env file without it passing through the command line:

       awk -v f=/etc/munshi/andai-kek.new 'BEGIN { getline k < f }
         /^ANDAI_KEK=/ { print "ANDAI_KEK=" k; next } { print }' .env > .env.next
       chmod 600 .env.next && mv .env.next .env

3. Mount the retired file into andai-core and andai-worker: uncomment the read-only `volumes:`
   example under `ANDAI_KEK_RETIRED_FILE` in `andai/deploy/compose/docker-compose.andai.yml` for
   both services, and set `ANDAI_KEK_RETIRED_FILE=/run/secrets/andai-kek-retired` in the env file.
4. Recreate both services: `make -C andai up`.

**Kubernetes.** Replace the Secret with both keys, then restart and wait for the rollout:

    kubectl -n <ns> create secret generic andai-kek \
      --from-file=active=/etc/munshi/andai-kek.new \
      --from-file=retired=/etc/munshi/andai-kek-retired \
      --dry-run=client -o yaml | kubectl -n <ns> apply -f -
    kubectl -n <ns> rollout restart deploy/andai-core deploy/andai-worker
    kubectl -n <ns> rollout status deploy/andai-core && kubectl -n <ns> rollout status deploy/andai-worker

(`andai-kek-retired` here holds the old key, written as in the Compose step 1; the Secret and
deployment names are the chart's.)

**Check every replica** of andai-core and andai-worker before going on: each must report the new
key as active and the old one as retired. A replica still on the old configuration cannot open
values once they move to the new key.

    for app in andai-core andai-worker; do
      for pod in $(kubectl -n <ns> get pods -l app.kubernetes.io/name=$app -o name); do
        kubectl -n <ns> exec "$pod" -- python -m andai_core kek-status | head -n 1
      done
    done

On Compose there is one replica of each and `make -C andai up` recreated both from the same env
file (it waits until they are healthy, and a service with a bad key does not start):
`make -C andai kek-status | head -n 1` shows the new active key. At this point `kek-status` exits
1: the values are still under the old key, which is expected.

### 4. Re-wrap

First a dry run, which writes nothing and reports what would move:

    make -C andai kek-rewrap DRY_RUN=1

`failed` must be 0 for every table. Then:

    make -C andai kek-rewrap

It prints, per table, how many values it re-wrapped, how many it skipped (changed by someone else
meanwhile, for example a user enrolling again, so already under the new key) and how many failed.
If it stops for any reason, run it again: it only picks up what is left. `BATCH=n` sets the rows
per transaction (default 100).

### 5. Confirm nothing is left under the old key

    make -C andai kek-status

Exit status 0, and the old key shows `0` in every table. andai-worker's `/metrics` agrees:
`munshi_sealed_values{key_id="<old key id>"}` is gone (it reads at most every 5 minutes).

### 6. Smoke test

- Sign in with the test account's TOTP code (opens a TOTP secret).
- Send the bot to the test meeting from the portal, or open a running meeting's live transcript
  (opens the org's platform key).

### 7. Take a backup

Take a fresh database backup now. It is the first one in which every value is under the new key.

### 8. Retire the old key

Remove the old key's line from the retired file (Compose) or from the Secret's `retired` key
(Kubernetes); leave the file or key in place, empty if nothing else is retired, since the services
refuse to start when `ANDAI_KEK_RETIRED_FILE` names a file that is not there. Roll out again as in
step 3, check that `kek-status` lists no retired key, and repeat the smoke test.

### 9. Keep the old key offline until old backups expire

Every backup taken before step 7 still holds values sealed under the old key. Keep the old key
offline in your key custody, labelled with its key id and the date it was retired, until the last
of those backups has aged out of your retention. Then destroy it. Delete `/etc/munshi/andai-kek.new`
once the new key is in your custody.

## When something goes wrong

- **A replica fails to start after step 3** with `KeyUnavailable`: the message names the problem
  (a malformed key, a line number, a key listed twice or as both active and retired). Fix the file
  or Secret and roll out again. Nothing has been re-wrapped yet.
- **Sign-ins or platform calls fail with "cannot open" after step 4** on some replicas: a replica
  was not on the new configuration. Finish the rollout (step 3); every value opens again once all
  replicas hold both keys.
- **`kek-rewrap` reports `failed` above 0**, and `kek-status` lists a key as `not loaded`: some
  values are sealed under a key that is neither active nor retired (an older rotation that was not
  finished). Find that key in your custody by its key id, add it to the retired file and run
  `kek-rewrap` again. If it is lost, the affected values cannot be recovered: users with a lost TOTP
  secret enrol again after an admin resets their two-step sign-in; an org with a lost platform key
  needs a new one minted (escalate: there is no command for that yet).
- **`kek-rewrap` stopped half way** (a crash, a lost connection): run it again.
- **Rolling back before step 8:** swap the keys (old as active, new as retired), roll out, and run
  `kek-rewrap`: it moves every value back to the old key.
- **Restoring a backup older than the rotation after step 8:** add the old key back to the retired
  keys, roll out, restore, and run `kek-rewrap`.
