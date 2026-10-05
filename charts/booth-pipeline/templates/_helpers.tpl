{{/*
Standard name/label helpers, the same shape `helm create` scaffolds and every other booth-* chart
uses — nothing booth-pipeline-specific here.
*/}}

{{- define "booth-pipeline.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "booth-pipeline.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "booth-pipeline.labels" -}}
app.kubernetes.io/name: {{ include "booth-pipeline.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "booth-pipeline.selectorLabels" -}}
app.kubernetes.io/name: {{ include "booth-pipeline.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Job template for one task attempt (ADR 0096), rendered into the task-job ConfigMap as JSON.
*/}}
{{- define "booth-pipeline.taskJob" -}}
{{- $sidecar := and .Values.boothDatabase.url .Values.core.url }}
{{- if $sidecar }}{{- if not (regexMatch `^[^ ]+@sha256:[0-9a-f]{64}$` .Values.credentialSidecar.image) }}
{{- fail "credentialSidecar.image must be pinned by digest (ghcr.io/projectbooth/credential-sidecar@sha256:...), never a tag (ADR 0095)" }}
{{- end }}{{- end }}
apiVersion: batch/v1
kind: Job
metadata:
  labels:
    {{- include "booth-pipeline.labels" . | nindent 4 }}
    app.kubernetes.io/component: task
spec:
  # The API/scheduler pod deletes each Job when its task ends; this collects one it never got to.
  ttlSecondsAfterFinished: {{ .Values.runner.ttlSecondsAfterFinished }}
  template:
    metadata:
      labels:
        {{- include "booth-pipeline.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: task
    spec:
      serviceAccountName: {{ include "booth-pipeline.fullname" . }}-task
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      {{- if $sidecar }}
      initContainers:
        # booth-core's credential sidecar, postgres mode (ADR 0095). A NATIVE sidecar (restartPolicy:
        # Always): it starts before the task and is stopped when the task's container exits, so it
        # never keeps the Job alive. It authenticates as THIS task — its --token-file is the task's
        # own platform token, kept current by the runner — and KubernetesJobRunner adds the task's
        # --workspace/--scope/--access, or removes it from a task with no platform access.
        - name: credential-sidecar-postgres
          image: {{ .Values.credentialSidecar.image }}
          restartPolicy: Always
          args:
            - --kind=postgres
            - --listen=127.0.0.1:5432
            - --token-file=/var/run/booth-sidecar/token
            - --core-url={{ .Values.core.url }}
          {{- if or .Values.credentialSidecar.renewMarginSeconds .Values.credentialSidecar.renewIntervalSeconds }}
          env:
            {{- with .Values.credentialSidecar.renewMarginSeconds }}
            - {name: RENEW_MARGIN_SECONDS, value: {{ . | quote }}}
            {{- end }}
            {{- with .Values.credentialSidecar.renewIntervalSeconds }}
            - {name: RENEW_INTERVAL_SECONDS, value: {{ . | quote }}}
            {{- end }}
          {{- end }}
          securityContext:
            {{- toYaml .Values.securityContext | nindent 12 }}
          resources:
            {{- toYaml .Values.credentialSidecar.resources | nindent 12 }}
          volumeMounts:
            - name: booth-sidecar
              mountPath: /var/run/booth-sidecar
              readOnly: true
      {{- end }}
      containers:
        - name: task
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["booth-pipeline-runner"]
          securityContext:
            {{- toYaml .Values.securityContext | nindent 12 }}
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: BOOTH_RUNNER_AUTH_TOKEN_FILE
              value: /etc/booth/runner-auth/token
            # One task per pod: it exits when its task's stream ends (or if no task ever arrives).
            - name: BOOTH_RUNNER_MAX_CONCURRENT
              value: "1"
            - name: BOOTH_RUNNER_ONE_SHOT
              value: "1"
            {{- if .Values.execution.runnerEnvPassthrough }}
            - name: BOOTH_PIPELINE_RUNNER_ENV_PASSTHROUGH
              value: {{ join "," .Values.execution.runnerEnvPassthrough | quote }}
            {{- end }}
            {{- if $sidecar }}
            # The runner keeps the task's token here for the sidecar, and waits for its lease before
            # starting a task (DATABASE_URL itself is added per task, with the workspace's database).
            - name: BOOTH_RUNNER_SIDECAR_TOKEN_FILE
              value: /var/run/booth-sidecar/token
            - name: BOOTH_RUNNER_SIDECAR_HEALTHZ
              value: http://127.0.0.1:5432/healthz
            - name: BOOTH_RUNNER_SIDECAR_WAIT_SECONDS
              value: {{ .Values.credentialSidecar.waitSeconds | quote }}
            {{- end }}
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            periodSeconds: 1
          resources:
            {{- toYaml .Values.runner.resources | nindent 12 }}
          volumeMounts:
            - name: runner-auth
              mountPath: /etc/booth/runner-auth
              readOnly: true
            - name: tmp
              mountPath: /tmp
            {{- if $sidecar }}
            - name: booth-sidecar
              mountPath: /var/run/booth-sidecar
            {{- end }}
      volumes:
        - name: runner-auth
          secret:
            secretName: per-task # replaced per task by KubernetesJobRunner
        - name: tmp
          emptyDir:
            sizeLimit: {{ .Values.execution.scratchSize | quote }}
        {{- if $sidecar }}
        # Memory-backed and private to this one task's pod: only the runner and the sidecar mount it.
        - name: booth-sidecar
          emptyDir:
            medium: Memory
            sizeLimit: 1Mi
        {{- end }}
{{- end -}}

{{- define "booth-pipeline.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "booth-pipeline.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
