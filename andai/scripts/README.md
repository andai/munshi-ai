# andai/scripts

Operator scripts: `gen-secrets.sh` (fills stack secrets in an env file, never rotates existing ones),
`health.sh` (HTTP and container health of the stack), `vm.sh` (sync and run commands on the dev
VM through git) and `images.py` (builds, checks and pushes the image catalogue `andai/images.json`;
run through `make -C andai images | images-check`).
