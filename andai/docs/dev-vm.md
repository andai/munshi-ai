# Dev VM

The dev environment is one AWS VM running the whole stack (Munshi + the andai overlay) with Docker
Compose (D-20). Code reaches the VM only through the public GitHub repository (`andai/munshi-ai`, D-24) and
`andai/scripts/vm.sh`; evidence comes back the same way. Nothing on the VM is exposed publicly in
Phase 1: every service binds to `127.0.0.1` (D-15), there is no TLS, DNS or public hostname, and
operators reach UIs through SSH port forwarding.

Until the backends behind the OLLM STT and chat routes are verified AND AI-hosted (D-21), use this
VM only with test meetings and synthetic content.

## 1. GitHub repository (once)

The repository is **public** (munshi.ai is open source under Apache-2.0, D-24). Because of that,
nothing committed may name internal infrastructure: no OLLM URL or host, VM hostname or IP, AWS
account or instance id, keys or tokens. Evidence files redact them (see section 7), and the `.env`
file is git-ignored. No self-hosted GitHub runner is ever attached to this repository.

1. Create an empty **public** repository (no README, no license, no .gitignore).
2. Before anything is pushed, turn GitHub Actions off for it: **Settings → Actions → General →
   Actions permissions → "Disable actions"** → Save. Munshi's own workflows arrive with the fork and
   expect Munshi's secrets and registries; they must not run here (andai CI is enabled deliberately
   later, in its own plan).
3. After the first push, make `development` the default branch:
   `gh repo edit <owner>/<name> --default-branch development`.

Check:

```bash
gh repo view <owner>/<name> --json defaultBranchRef,visibility -q '.defaultBranchRef.name + " " + .visibility'
# development PUBLIC
gh api repos/<owner>/<name>/actions/permissions -q .enabled
# false
```

## 2. VM

| Item | Value |
|---|---|
| OS | Ubuntu 24.04 LTS (x86_64) |
| Size | ~4 vCPU / 16 GB RAM (t3.xlarge class). Meeting bots run Chromium; scale up if several bots run at once. |
| Disk | 100 GB gp3 (images are large: the bot image alone is ~3.6 GB) |
| Network | Security group inbound: **SSH (22) from the office IP only**. Nothing else inbound. Outbound: HTTPS to GitHub, Docker Hub, the package mirrors below, and the OLLM endpoint. |
| Public exposure | None. No load balancer, no DNS, no TLS in Phase 1. |

## 3. Toolchain on the VM

Required: Docker Engine **≥ 26** with the Compose plugin **≥ 2.24** (the overlay uses
`ports: !override`, which older Compose rejects), git, make, curl, openssl, python3, uv,
Node 22 (≥ 22.13) with pnpm via corepack, and espeak-ng (generates the audio fixtures for the OLLM
probe).

```bash
# base packages
sudo apt-get update
sudo apt-get install -y ca-certificates curl git make openssl python3 espeak-ng

# Docker Engine + Compose plugin (Docker's apt repository)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"     # log out and back in afterwards

# Node 22 + pnpm (the repo pins pnpm in package.json; corepack fetches that version)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo corepack enable

# uv (runs the OLLM probe without touching Ubuntu's externally managed Python)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Check:

```bash
lsb_release -rs                 # 24.04
docker version --format '{{.Server.Version}}'   # >= 26
docker compose version --short  # >= 2.24
pnpm --version; uv --version; espeak-ng --version
```

## 4. Clone

The repository is public, so the VM clones it over HTTPS with no credentials:
`git clone https://github.com/andai/munshi-ai.git ~/andai-meet`. The VM never needs write
access: `vm.sh sync` pushes from the developer machine and the VM only pulls.

```bash
git clone git@github.com:<owner>/<name>.git ~/andai-meet
cd ~/andai-meet
git checkout feat/phase1-compose     # or the Phase 1 branch being worked on
```

The checkout must live at `~/andai-meet`; `vm.sh` assumes that path (override with `VM_REPO_DIR`).
Never edit files in the VM checkout by hand: `vm.sh sync` fast-forwards it and fails on local edits.

## 5. Environment file

```bash
cp andai/deploy/compose/.env.example andai/deploy/compose/.env
chmod 600 andai/deploy/compose/.env
```

Then edit `andai/deploy/compose/.env` **on the VM** and replace every `__OLLM_*__` placeholder:

| Placeholder | Meaning |
|---|---|
| `__OLLM_BASE_URL__` | OLLM origin (+ path prefix if any), no trailing slash, **no `/v1`**. Used as-is for STT and for the agent harness (the claude CLI appends `/v1/messages`); the completion port uses `__OLLM_BASE_URL__/v1`. |
| `__OLLM_API_KEY__` | OLLM API key (STT, chat and agent). Typed only on the VM; never committed, never pasted into chat or evidence. |
| `__OLLM_STT_MODEL__` | Whisper model id served on `/v1/audio/transcriptions`. |
| `__OLLM_CHAT_MODEL__` | Chat model id served on `/v1/chat/completions` (also the model allowlist). |
| `__OLLM_AGENT_MODEL__` | Model id the agent harness requests on OLLM's Anthropic-compatible `/v1/messages` (must handle tool calls; may equal the chat model). |

`grep -c __OLLM_ andai/deploy/compose/.env` must print `0`. Leave the secrets (`ADMIN_TOKEN`,
`INTERNAL_API_SECRET`, …) empty: `make -C andai up` mints them. The file is git-ignored; never
commit it.

## 6. Bring-up

The single bring-up command, from the repository root on the VM:

```bash
make -C andai up
```

It mints missing secrets (`andai/scripts/gen-secrets.sh`), builds any image the stack needs that is
not on the machine (the catalogue in `andai/images.json`: every Munshi service, the bot, the hardened
agent worker and the andai services, all `ghcr.io/sunydepalpur/munshi-*:<VERSION>`), and starts
everything with `--wait` (fails if any service does not become healthy). `make -C andai up PULL=1`
pulls the published images from ghcr.io instead of building them. Then:

```bash
make -C andai health    # HTTP 200 from every service on 127.0.0.1 + no unhealthy/exited containers
make -C andai ps        # container table
make -C andai logs S=meeting-api
make -C andai down      # stop (volumes kept)
```

Operator UIs (terminal `13000`, dashboard `13001`, andai portal `13100`) are reachable only through
an SSH tunnel, e.g. `ssh -L 13000:127.0.0.1:13000 -L 13001:127.0.0.1:13001 $VM_HOST`.

## 7. Working from a laptop: `andai/scripts/vm.sh`

On the laptop, in the local checkout:

```bash
export VM_HOST=ubuntu@<vm-ip>        # or an ~/.ssh/config alias
andai/scripts/vm.sh sync             # push the current feat/phase1-* branch, fast-forward the VM to it
andai/scripts/vm.sh run 'make -C andai up && make -C andai health'
```

- `sync` works only on `feat/phase1-*` branches (never `development`/`main`). If the working tree
  has changes it commits them as `wip(phase1): sync to dev VM` first, refusing any `.env` file or
  `andai/.ci/`, then pushes to `origin`, checks the branch out on the VM with `pull --ff-only`, and
  fails unless the VM's `HEAD` equals the local `HEAD`.
- `run '<cmd>'` runs `<cmd>` in `~/andai-meet` on the VM, streams its output and returns its exit
  code. Evidence files are written locally from captured `run` output and committed, then synced.
