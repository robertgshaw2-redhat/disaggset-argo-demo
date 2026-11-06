#!/usr/bin/env python3
"""Writes base/model-server/qwen.yaml and rollouts/rollout/rollout.yaml from ENGINE below. Run from this directory:
python3 gen.py

A release is a commit that changes ENGINE (and reruns this). It changes the DisaggregatedSet's template, which starts
its rolling update. How the release is then held at 33/67/100% (hasB4K/lws#13's spec.rolloutStrategy.canaryPercent)
depends on the variant Argo CD syncs:

  rollouts/  a driver Rollout, whose pod template this also changes, runs steps that set canaryPercent
             (rollouts/rollout/steps.py); git never sets it.
  kargo/     Kargo promotes the release commit through Stages that set canaryPercent in git (../kargo/).

The settings below are the ones the demo ran with (B200s on GKE, GPUs through the NVIDIA DRA driver). Change GPU,
NODE_SELECTOR and TOLERATIONS for your cluster, rerun, and commit before the first sync.
"""
import textwrap

# The engine revision to run. Each is KV-compatible with the others (only --max-num-seqs differs), so mixed old/new
# prefill-decode pairs keep working while a rollout is held.
ENGINE = "r8"
# Bump to start the steps without an engine change, e.g. to roll back to the stable engine through the steps
# (Argo Rollouts skips the steps when the pod template returns to the stable one).
RELEASE = 3

MAX_NUM_SEQS = {"r6": 64, "r7": 48, "r8": 56, "r9": 40}
IMAGE = "ghcr.io/llm-d/llm-d-cuda@sha256:094fb7bc018af54cb6a93999f72b0f38b7c89960f285f3718fbd95adc192fc27"  # v0.9.0
SIDECAR = "ghcr.io/llm-d/llm-d-router-disagg-sidecar@sha256:83cdf20199485329baca89b69d78f695735855ed93412aeaeec325d2374ced04"  # v1.0.0
MODEL = "Qwen/Qwen3-0.6B"
REPLICAS = 3
# Static: git owns each role's replicas. External: an autoscaler owns them, through the DisaggregatedSetRoleScaler
# "qwen-<role>" the controller creates (its /scale subresource); git then omits replicas.
SCALING = "External"
# Under External scaling: what a scale-down does while a rollout is held (Deferred or Immediate).
SCALE_DOWN_DURING_ROLLOUT = "Immediate"
# Under External scaling: how target growth is paced while old revisions remain (Deferred or Immediate).
SCALE_UP_DURING_ROLLOUT = "Immediate"

# How each pod gets its one GPU: "dra" (a ResourceClaimTemplate, model-server/resource-claim.yaml, device class
# gpu.nvidia.com) or "device-plugin" (an nvidia.com/gpu: 1 limit; then drop resource-claim.yaml from kustomization.yaml).
GPU = "dra"
NODE_SELECTOR = {"cloud.google.com/gke-accelerator": "nvidia-b200"}
TOLERATIONS = [{"effect": "NoSchedule", "key": "nvidia.com/gpu", "operator": "Exists"}]

SCRIPT = r"""
set -euo pipefail
python3 -c "from huggingface_hub import snapshot_download as s; s('{model}', local_dir='/models/m')" >/dev/null
exec vllm serve /models/m \
  --served-model-name {model} \
  --port {port} \
  --max-model-len 16384 \
  --max-num-seqs {seqs} \
  --gpu-memory-utilization 0.5 \
  --enable-prefix-caching \
  --kv-transfer-config '{{"kv_connector":"NixlConnector","kv_role":"kv_both"}}' \
  --disable-access-log-for-endpoints=/health,/metrics \
  --shutdown-timeout 60
""".strip()


def role(name):
    port = 8000 if name == "prefill" else 8200
    sidecar = ""
    if name == "decode":
        sidecar = f"""\
initContainers:
- name: routing-proxy
  image: {SIDECAR}
  args: [--port=8000, --model-server-port=8200, --data-parallel-size=1, --kv-connector=nixlv2, --secure-proxy=false]
  ports:
  - containerPort: 8000
    name: sidecar
    protocol: TCP
  restartPolicy: Always
"""
    script = SCRIPT.format(model=MODEL, port=port, seqs=MAX_NUM_SEQS[ENGINE])
    if GPU == "dra":
        gpu_resources, gpu_limit = "    claims:\n    - name: gpu\n", ""
        gpu_claims = "resourceClaims:\n- name: gpu\n  resourceClaimTemplateName: one-b200\n"
    else:
        gpu_resources, gpu_limit, gpu_claims = "", "      nvidia.com/gpu: 1\n", ""
    placement = ""
    if NODE_SELECTOR:
        placement += "nodeSelector:\n" + "".join(f"  {k}: {v}\n" for k, v in NODE_SELECTOR.items())
    if TOLERATIONS:
        placement += "tolerations:\n" + "".join(
            "- " + "\n  ".join(f"{k}: {v}" for k, v in t.items()) + "\n" for t in TOLERATIONS)
    spec = f"""\
{sidecar}containers:
- name: vllm
  image: {IMAGE}
  command: [/bin/bash, -c]
  args:
  - |-
{textwrap.indent(script, '    ')}
  env:
  - name: VLLM_NIXL_SIDE_CHANNEL_HOST
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
  - name: VLLM_HTTP_TIMEOUT_KEEP_ALIVE
    value: "300"
  - name: UCX_TLS
    value: tcp,cuda_copy
  - name: PYTHONHASHSEED
    value: "0"
  - name: HOME
    value: /tmp/home
  - name: HF_HOME
    value: /tmp/hf
  ports:
  - containerPort: {port}
    name: vllm
    protocol: TCP
  lifecycle:
    preStop:
      exec:
        command: [/bin/sh, -c, "touch /tmp/draining; sleep 8"]
  readinessProbe:
    exec:
      command: [/bin/sh, -c, "test ! -f /tmp/draining && python3 -c \\"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=2)\\""]
    periodSeconds: 3
    timeoutSeconds: 3
    failureThreshold: 1
  startupProbe:
    httpGet:
      path: /health
      port: {port}
    periodSeconds: 5
    failureThreshold: 240
  resources:
{gpu_resources}    requests:
      cpu: "8"
      memory: 48Gi
    limits:
{gpu_limit}      memory: 48Gi
  volumeMounts:
  - mountPath: /models
    name: models
  - mountPath: /dev/shm
    name: dshm
{gpu_claims}{placement}terminationGracePeriodSeconds: 90
volumes:
- name: models
  emptyDir: {{}}
- name: dshm
  emptyDir:
    medium: Memory
    sizeLimit: 8Gi
"""
    # No canaryPercent: the Rollout's steps own it (server-side apply, field manager pd-demo-canary).
    if SCALING == "External":
        scaling, replicas = "  scaling:\n    mode: External\n", ""
    else:
        scaling, replicas = "", f"    replicas: {REPLICAS}\n"
    return f"""\
- name: {name}
{scaling}  spec:
{replicas}    startupPolicy: LeaderCreated
    rolloutStrategy:
      type: RollingUpdate
      rollingUpdateConfiguration:
        maxSurge: 1
        maxUnavailable: 0
    leaderWorkerTemplate:
      size: 1
      restartPolicy: RecreateGroupOnPodRestart
      workerTemplate:
        metadata:
          labels:
            llm-d.ai/role: {name}
            pd-demo.llm-d.ai/pool: qwen
            pd-demo.llm-d.ai/engine: {ENGINE}
        spec:
{textwrap.indent(spec, '          ')}"""


scaling_policy = ""
if SCALING == "External":
    scaling_policy = (f"  scalingPolicy:\n    scaleDownDuringRollout: {SCALE_DOWN_DURING_ROLLOUT}\n"
                      f"    scaleUpDuringRollout: {SCALE_UP_DURING_ROLLOUT}\n")
open("base/model-server/qwen.yaml", "w").write(f"""\
# Generated by ../gen.py; edit that and rerun.
apiVersion: disaggregatedset.x-k8s.io/v1
kind: DisaggregatedSet
metadata:
  name: qwen
spec:
{scaling_policy}  roles:
{textwrap.indent(role('prefill') + role('decode'), '  ')}""")

open("rollouts/rollout/rollout.yaml", "w").write(f"""\
# Generated by ../gen.py; edit that and rerun.
# The driver Rollout. Its pod is a placeholder; its annotations name the release, so a commit that changes ENGINE
# changes this template and starts the steps. Each step is an AnalysisRun whose Job runs steps.py: set the
# DisaggregatedSet's canaryPercent, wait for CanaryHeld (or Available at 100), smoke-test through the router. Promote
# at each pause with the Argo CD UI (the Rollout's resume action) or `kubectl argo rollouts promote qwen-release`. Do
# not use promote --full: it skips the last step, which sets canaryPercent back to 0 for the next release.
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: qwen-release
spec:
  replicas: 1
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app.kubernetes.io/name: qwen-release
  template:
    metadata:
      labels:
        app.kubernetes.io/name: qwen-release
      annotations:
        pd-demo.llm-d.ai/engine: {ENGINE}
        pd-demo.llm-d.ai/release: "{RELEASE}"
    spec:
      containers:
      - name: placeholder
        image: registry.k8s.io/pause:3.10
        resources:
          requests: {{cpu: 1m, memory: 8Mi}}
          limits: {{memory: 16Mi}}
      terminationGracePeriodSeconds: 1
  strategy:
    canary:
      steps:
      - analysis:  # 1 of 3 groups per role on the new revision
          templates: [{{templateName: ds-step}}]
          args: [{{name: command, value: hold}}, {{name: percent, value: "33"}}]
      - pause: {{}}
      - analysis:  # 2 of 3
          templates: [{{templateName: ds-step}}]
          args: [{{name: command, value: hold}}, {{name: percent, value: "67"}}]
      - pause: {{}}
      - analysis:  # complete the rolling update
          templates: [{{templateName: ds-step}}]
          args: [{{name: command, value: hold}}, {{name: percent, value: "100"}}]
      - analysis:  # hold the next release at zero new groups, at any replica count, until its first step
          templates: [{{templateName: ds-step}}]
          args: [{{name: command, value: rest}}, {{name: percent, value: "0"}}]
""")
