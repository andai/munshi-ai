{{/*
Helpers for the andai umbrella chart. Every name is prefixed `andai.`: named templates are global
across the parent and its subcharts (munshi, seaweedfs, prometheus, perses), so an unprefixed name
could replace a subchart's own.

The andai workloads have fixed names (andai-core, andai-worker, andai-intake, andai-portal): the
runbooks run `kubectl exec deploy/andai-core`, Prometheus scrapes andai-core:8000 and the portal
reaches andai-core:8000 exactly as in Compose. One release per namespace.
*/}}

{{- define "andai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "andai.labels" -}}
app.kubernetes.io/name: {{ include "andai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: munshi
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "andai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "andai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* The tag of every ghcr.io/andai/munshi-* image this chart runs: global.imageTag (shared with the
munshi subchart), else the chart's appVersion (the munshi.ai release). */}}
{{- define "andai.imageTag" -}}
{{- .Values.global.imageTag | default .Chart.AppVersion -}}
{{- end -}}

{{/* include "andai.image" (list . "andai-core") → ghcr.io/andai/munshi-andai-core:<tag> */}}
{{- define "andai.image" -}}
{{- $root := index . 0 -}}
{{- printf "%s/munshi-%s:%s" $root.Values.andai.imageRegistry (index . 1) (include "andai.imageTag" $root) -}}
{{- end -}}

{{/* The munshi subchart's resource names, computed the way its own munshi.fullname does. */}}
{{- define "andai.munshiFullname" -}}
{{- $m := .Values.munshi | default dict -}}
{{- if $m.fullnameOverride -}}
{{- $m.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name ($m.nameOverride | default "munshi") | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* include "andai.munshiComponent" (list . "gateway") → <release>-munshi-gateway */}}
{{- define "andai.munshiComponent" -}}
{{- $root := index . 0 -}}
{{- printf "%s-%s" (include "andai.munshiFullname" $root) (index . 1) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The munshi Secret holding ADMIN_API_TOKEN, MUNSHI_SYSTEM_WEBHOOK_SECRET and
TRANSCRIPTION_SERVICE_TOKEN: the operator's (munshi.secrets.existingSecretName) or the one the
munshi chart renders. */}}
{{- define "andai.munshiSecretName" -}}
{{- .Values.munshi.secrets.existingSecretName | default (include "andai.munshiComponent" (list . "secrets")) -}}
{{- end -}}

{{- define "andai.platformAdminUrl" -}}
{{- printf "http://%s:%v" (include "andai.munshiComponent" (list . "admin-api")) .Values.munshi.adminApi.service.port -}}
{{- end -}}

{{- define "andai.platformGatewayUrl" -}}
{{- printf "http://%s:%v" (include "andai.munshiComponent" (list . "gateway")) .Values.munshi.gateway.service.port -}}
{{- end -}}

{{/* Valkey: andai uses database 1 of the munshi Valkey (database 0 is the platform's), as in Compose. */}}
{{- define "andai.redisUrl" -}}
{{- if .Values.andai.redisUrl -}}
{{- .Values.andai.redisUrl -}}
{{- else if .Values.munshi.redis.enabled -}}
{{- printf "redis://%s:%v/1" (include "andai.munshiComponent" (list . "redis")) .Values.munshi.redis.service.port -}}
{{- else -}}
{{- required "andai.redisUrl is required when munshi.redis.enabled=false (e.g. redis://valkey.example:6379/1)" .Values.andai.redisUrl -}}
{{- end -}}
{{- end -}}

{{/* The andai database: on the munshi Postgres unless andai.database.host names another server. */}}
{{- define "andai.dbHost" -}}
{{- if .Values.andai.database.host -}}
{{- .Values.andai.database.host -}}
{{- else if .Values.munshi.postgres.enabled -}}
{{- include "andai.munshiComponent" (list . "postgres") -}}
{{- else -}}
{{- required "andai.database.host (or munshi.database.host) is required when munshi.postgres.enabled=false" .Values.munshi.database.host -}}
{{- end -}}
{{- end -}}

{{- define "andai.dbPort" -}}
{{- .Values.andai.database.port | default .Values.munshi.database.port | default 5432 -}}
{{- end -}}

{{- define "andai.dbSslMode" -}}
{{- .Values.andai.database.sslMode | default .Values.munshi.database.sslMode | default "disable" -}}
{{- end -}}

{{/* The Secret with the Postgres superuser db-init connects as (POSTGRES_USER, POSTGRES_PASSWORD,
POSTGRES_DB): andai.database.adminSecret, else the munshi Postgres credentials Secret. */}}
{{- define "andai.dbAdminSecret" -}}
{{- .Values.andai.database.adminSecret | default .Values.munshi.postgres.credentialsSecretName | default "postgres-credentials" -}}
{{- end -}}

{{/* include "andai.dbUrl" (list . "andai_app" "ANDAI_DB_PASSWORD_APP") — a SQLAlchemy URL whose
password is the container's own env var, expanded by the kubelet ($(VAR)); no password is ever in
a rendered manifest. The passwords must be URL-safe (gen-secrets.sh mints hex). */}}
{{- define "andai.dbUrl" -}}
{{- $root := index . 0 -}}
{{- $url := printf "postgresql+psycopg://%s:$(%s)@%s:%s/%s" (index . 1) (index . 2) (include "andai.dbHost" $root) (include "andai.dbPort" $root | toString) $root.Values.andai.database.name -}}
{{- $ssl := include "andai.dbSslMode" $root -}}
{{- if ne $ssl "disable" -}}
{{- $url = printf "%s?sslmode=%s" $url $ssl -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{/* SeaweedFS names, the way its seaweedfs.fullname / componentName compute them. */}}
{{- define "andai.seaweedfsFullname" -}}
{{- $s := .Values.seaweedfs | default dict -}}
{{- if $s.fullnameOverride -}}
{{- $s.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := $s.nameOverride | default "seaweedfs" -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The S3 endpoint recordings go to when the chart runs SeaweedFS: its S3 Service (filer-embedded
S3 gateway). munshi.storage.s3.endpoint must equal it (validate.yaml): a subchart's values cannot be
computed by its parent, so the parent checks them instead. */}}
{{- define "andai.s3Endpoint" -}}
{{- printf "http://%s-s3:%v" (include "andai.seaweedfsFullname" .) .Values.seaweedfs.filer.s3.port -}}
{{- end -}}

{{/* The URL munshi's meeting-api hands bots when the ASR pool runs (the client appends
/v1/audio/transcriptions). */}}
{{- define "andai.asrUrl" -}}
{{- printf "http://andai-asr:%v" .Values.gpuPools.asr.port -}}
{{- end -}}

{{/* The receivers Alertmanager has turned on, as the state file andai-worker reads
({"receivers": N, "names": [...]}; andai_worker.alerting_config.AlertingState). */}}
{{- define "andai.alertReceivers" -}}
{{- $names := list -}}
{{- if .Values.monitoring.enabled -}}
{{- $a := .Values.monitoring.alerting -}}
{{- if and $a.email.enabled (include "andai.alertEmailTo" .) -}}{{- $names = append $names "email" -}}{{- end -}}
{{- if $a.slack.enabled -}}{{- $names = append $names "slack" -}}{{- end -}}
{{- if $a.teams.enabled -}}{{- $names = append $names "teams" -}}{{- end -}}
{{- end -}}
{{- toJson (dict "receivers" (len $names) "names" $names) -}}
{{- end -}}

{{- define "andai.alertEmailTo" -}}
{{- .Values.monitoring.alerting.email.to | default .Values.andai.alertEmail -}}
{{- end -}}

{{/* include "andai.alertmanagerMount" (list . "alerting-secrets") → that entry of
prometheus.alertmanager.extraSecretMounts as YAML ("{}" when absent). */}}
{{- define "andai.alertmanagerMount" -}}
{{- $root := index . 0 -}}
{{- $want := index . 1 -}}
{{- $found := dict -}}
{{- range ((($root.Values.prometheus | default dict).alertmanager | default dict).extraSecretMounts | default list) -}}
{{- if eq .name $want -}}{{- $found = . -}}{{- end -}}
{{- end -}}
{{- toYaml $found -}}
{{- end -}}

{{/* Pod placement shared by the andai workloads (the same globals the munshi subchart uses). */}}
{{- define "andai.placement" -}}
{{- with .Values.global.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.global.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.global.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* include "andai.podSecurity" (list . 10001) — non-root pod security for an andai image. */}}
{{- define "andai.podSecurity" -}}
{{- $uid := index . 1 -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ $uid }}
  runAsGroup: {{ $uid }}
  fsGroup: {{ $uid }}
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "andai.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
{{- end -}}

{{/* The KEK: the operator's Secret (keys `active` and `retired`, which may be empty), mounted
read-only and readable by the andai user only through fsGroup; the services read the files. */}}
{{- define "andai.kekEnv" -}}
- name: ANDAI_KEK_FILE
  value: /run/secrets/andai-kek/active
- name: ANDAI_KEK_RETIRED_FILE
  value: /run/secrets/andai-kek/retired
{{- end -}}

{{- define "andai.kekVolumeMount" -}}
- name: andai-kek
  mountPath: /run/secrets/andai-kek
  readOnly: true
{{- end -}}

{{- define "andai.kekVolume" -}}
- name: andai-kek
  secret:
    secretName: {{ .Values.andai.kek.existingSecret }}
    defaultMode: 0440
    items:
      - key: active
        path: active
      - key: retired
        path: retired
{{- end -}}

{{/* Environment shared by andai-core, andai-worker and the jobs that run andai-core's code:
the ConfigMap, the app-role database URL and the secrets, each from a Secret by reference. */}}
{{- define "andai.coreEnv" -}}
- name: ANDAI_VERSION
  value: {{ include "andai.imageTag" . | quote }}
- name: ANDAI_DB_PASSWORD_APP
  valueFrom:
    secretKeyRef:
      name: {{ .Values.andai.database.existingSecret }}
      key: {{ .Values.andai.database.appPasswordKey }}
- name: ANDAI_DB_URL_APP
  value: {{ include "andai.dbUrl" (list . "andai_app" "ANDAI_DB_PASSWORD_APP") | quote }}
{{ include "andai.kekEnv" . }}
- name: PLATFORM_ADMIN_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "andai.munshiSecretName" . }}
      key: ADMIN_API_TOKEN
- name: ANDAI_PLATFORM_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "andai.munshiSecretName" . }}
      key: MUNSHI_SYSTEM_WEBHOOK_SECRET
{{- with include "andai.smtpPasswordEnv" . }}
{{ . }}
{{- end }}
{{- if .Values.andai.llm.existingSecret }}
- name: ANDAI_LLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.andai.llm.existingSecret }}
      key: {{ .Values.andai.llm.apiKeyKey }}
{{- end }}
{{- end -}}

{{- define "andai.smtpPasswordEnv" -}}
{{- if .Values.andai.smtp.existingSecret -}}
- name: SMTP_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.andai.smtp.existingSecret }}
      key: {{ .Values.andai.smtp.passwordKey }}
{{- end }}
{{- end -}}
