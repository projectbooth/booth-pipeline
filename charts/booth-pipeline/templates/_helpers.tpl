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
      volumes:
        - name: runner-auth
          secret:
            secretName: per-task # replaced per task by KubernetesJobRunner
        - name: tmp
          emptyDir:
            sizeLimit: {{ .Values.execution.scratchSize | quote }}
{{- end -}}

{{- define "booth-pipeline.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "booth-pipeline.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
