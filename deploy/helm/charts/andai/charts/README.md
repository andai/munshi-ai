# deploy/helm/charts/andai/charts

The andai chart's dependencies, vendored by `helm dependency build` (`make -C deploy/helm andai-deps`)
from `Chart.yaml` and `Chart.lock`: the munshi chart (`file://../munshi`), seaweedfs 4.53.0,
prometheus-community/prometheus 29.45.1 and perses 0.24.0. The `.tgz` archives are git-ignored;
`Chart.lock` is committed.
