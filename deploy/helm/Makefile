# deploy/helm — the v0.12 control-plane Helm chart (lite/compose/helm trio: this is `helm`), and the
# munshi.ai umbrella chart `andai` over it (charts/andai; andai/docs/kubernetes.md).
# Static gates need no cluster; `smoke` stands the munshi chart up on a local k3s node.

CHART        ?= charts/munshi
RELEASE      ?= munshi
NAMESPACE    ?= munshi
VALUES       ?= $(CHART)/values-test.yaml
ANDAI_CHART  ?= charts/andai
ANDAI_RELEASE ?= andai
# k3s writes a root-only kubeconfig; smoke targets read it via sudo. Override for other clusters.
KUBECONFIG   ?= /etc/rancher/k3s/k3s.yaml
COMPOSE      ?= ../compose/docker-compose.yml
IMAGES        = ghcr.io/andai/munshi-gateway ghcr.io/andai/munshi-admin-api ghcr.io/andai/munshi-meeting-api \
                ghcr.io/andai/munshi-runtime ghcr.io/andai/munshi-agent-api ghcr.io/andai/munshi-terminal

## lint — helm lint (default + values-test)
lint:
	helm lint $(CHART)
	helm lint $(CHART) -f $(VALUES)

## template — render to stdout with values-test
template:
	helm template $(RELEASE) $(CHART) -n $(NAMESPACE) -f $(VALUES)

## test — the static gate:helm proof (structure + lint + render assertions, no cluster)
test:
	bash tests/test_helm_lint.sh
	bash tests/test_template.sh

## andai-deps — vendor the andai chart's dependencies into charts/andai/charts (git-ignored):
## `helm dependency build` (network: seaweedfs, prometheus-community and perses chart repositories,
## the versions pinned in Chart.lock) when a pinned subchart archive is missing, otherwise only
## repackage the local munshi chart so the umbrella renders the current charts/munshi.
andai-deps:
	@if helm dependency list $(ANDAI_CHART) | awk 'NR > 1 && NF && $$3 !~ /^file:/ && $$4 != "ok" { bad = 1 } END { exit !bad }'; then \
	  echo "andai-deps: fetching the pinned subcharts (helm dependency build)"; \
	  helm dependency build $(ANDAI_CHART); \
	else \
	  helm package $(CHART) -d $(ANDAI_CHART)/charts >/dev/null; \
	  echo "andai-deps: pinned subcharts present; munshi repackaged from $(CHART)"; \
	fi

## andai-lint — helm lint the andai chart (defaults: the KEK refusal; values-test, -staging, -loadtest)
andai-lint: andai-deps
	bash tests/test_andai_lint.sh

## andai-template — render the andai chart to stdout with values-test
andai-template: andai-deps
	helm template $(ANDAI_RELEASE) $(ANDAI_CHART) -n $(NAMESPACE) -f $(ANDAI_CHART)/values-test.yaml

## andai-sync-monitoring — copy andai/deploy/monitoring into charts/andai/files/monitoring
andai-sync-monitoring:
	bash tests/test_andai_monitoring_sync.sh --write

## andai-test — the andai chart's static proof: monitoring files in sync, lint, render assertions
andai-test: andai-deps
	bash tests/test_andai_monitoring_sync.sh
	bash tests/test_andai_lint.sh
	bash tests/test_andai_template.sh

## build — build the 5 control-plane images (:dev) via the compose build
build:
	IMAGE_TAG=dev docker compose -f $(COMPOSE) build gateway admin-api meeting-api runtime agent-api

## k3s-import — load locally-built :dev images into the k3s node's containerd (no registry push)
k3s-import:
	@for img in $(IMAGES); do \
	  echo "importing $$img:dev"; \
	  docker save "$$img:dev" | sudo k3s ctr images import - ; \
	done

## s3-fixture — start the TEST-ONLY S3 that values-test.yaml points storage.s3 at (the chart runs no
## object store): a random-credential Secret, then tests/s3-fixture.yaml, then wait for its bucket
s3-fixture:
	sudo KUBECONFIG=$(KUBECONFIG) kubectl get namespace $(NAMESPACE) >/dev/null 2>&1 || \
	  sudo KUBECONFIG=$(KUBECONFIG) kubectl create namespace $(NAMESPACE)
	sudo KUBECONFIG=$(KUBECONFIG) kubectl -n $(NAMESPACE) get secret s3-fixture-credentials >/dev/null 2>&1 || \
	  sudo KUBECONFIG=$(KUBECONFIG) kubectl -n $(NAMESPACE) create secret generic s3-fixture-credentials \
	    --from-literal=AWS_ACCESS_KEY_ID="$$(openssl rand -hex 10)" \
	    --from-literal=AWS_SECRET_ACCESS_KEY="$$(openssl rand -hex 20)"
	sudo KUBECONFIG=$(KUBECONFIG) kubectl -n $(NAMESPACE) apply -f tests/s3-fixture.yaml
	sudo KUBECONFIG=$(KUBECONFIG) kubectl -n $(NAMESPACE) wait --for=condition=complete job/s3-fixture-bucket --timeout=180s

## install — helm upgrade --install onto the cluster (KUBECONFIG)
install:
	sudo KUBECONFIG=$(KUBECONFIG) helm upgrade --install $(RELEASE) $(CHART) \
	  -n $(NAMESPACE) --create-namespace -f $(VALUES) --wait --timeout 5m

## status — pods + rollout
status:
	sudo KUBECONFIG=$(KUBECONFIG) kubectl -n $(NAMESPACE) get pods,svc

## smoke — full local proof: build → import → S3 fixture → install → wait → /health → (teardown via `make down`)
smoke: build k3s-import s3-fixture install status

## down — uninstall the release + namespace
down:
	-sudo KUBECONFIG=$(KUBECONFIG) helm uninstall $(RELEASE) -n $(NAMESPACE)
	-sudo KUBECONFIG=$(KUBECONFIG) kubectl delete namespace $(NAMESPACE) --wait=false

.PHONY: lint template test andai-deps andai-lint andai-template andai-sync-monitoring andai-test build k3s-import s3-fixture install status smoke down
