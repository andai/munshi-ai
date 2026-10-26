# andai/deploy/compose

`docker-compose.andai.yml` is the overlay on `deploy/compose/docker-compose.yml`: andai services,
model endpoint wiring, loopback-only ports. Its environment template is the repo-root `.env.example`
(copy to `.env` at the repo root, git-ignored), which lists every variable a container reads.
