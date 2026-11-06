# DisaggregatedSet canary rollouts with Argo CD, Kargo or Argo Rollouts

A GitOps release of a prefill/decode (P/D) inference deployment. One LWS
[DisaggregatedSet](https://lws.sigs.k8s.io/docs/concepts/disaggregatedset/) runs the model, Argo CD syncs it from git,
and each release is held at 33%, then 67%, then 100% of the replica groups. The holds come from the DisaggregatedSet's
proposed `spec.rolloutStrategy.canaryPercent` field ([hasB4K/lws#13](https://github.com/hasB4K/lws/pull/13)).

Two ways to drive the holds are included and were run on B200s on GKE on 2026-11-06 under continuous load:

- **Kargo** (`kargo/`, recommended): each hold is a Kargo Stage that writes `canaryPercent` to git and verifies the
  release before it can move on. Standard GitOps: git holds the whole desired state.
- **Argo Rollouts** (`app/rollouts/`): a placeholder Rollout whose steps set `canaryPercent` on the cluster. Works with
  stock tools, but bends Argo Rollouts to drive a workload it does not manage.

[RESULTS.md](RESULTS.md) has the full log of every run.

## The workload (`app/base/`)

- **The DisaggregatedSet** `qwen`: Qwen3-0.6B on vLLM with NIXL KV transfer, 3 prefill and 3 decode groups of one GPU
  each, `scaling.mode: External` (each role has a `DisaggregatedSetRoleScaler` an autoscaler can drive).
  `app/gen.py` writes it; a release is a commit that changes `ENGINE` there.
- **The router**: llm-d-router v1.0.0's endpoint picker (always P/D) behind Envoy, with the
  **`disaggregatedset-rollout-screener`**. It picks one DisaggregatedSet revision per request before the prefill and
  decode profiles run, so a request's prefill and decode pods always come from the same revision. A mixed old/new pair
  never happens, which is what makes a canary of a KV-incompatible release (a new vLLM, model or KV layout) safe. A
  revision gets traffic once it has a Ready pod in every role, in proportion to its Ready pods. The chosen revision is
  returned in the `x-llm-d-disagg-revision` response header, and a request that sends that header is pinned to it
  (no match is a 503, never a fallback). The screener is Alpha in v1.0.0, so the EPP runs with
  `--allow-experimental-plugins`.

## Variant 1: Kargo

```
 commit to main (app/) ──► Warehouse: new Freight
                                │ auto
                                ▼
   canary-33 ──verified──► canary-67 ──verified──► full
   each Stage's promotion:
     copy app/ from the Freight's commit onto branch env/prod, set canaryPercent (33 / 67 / 100) in
     app/kargo/canary.yaml, commit, push, sync the Argo CD Application to that commit
   then: Stage healthy (Application Healthy = CanaryHeld, or Available at 100)
   then: verification (kargo/verify.py), which must pass before the next Stage will take this Freight
```

- **Promotion.** `canary-33` promotes new Freight automatically, so a commit to `main` starts the release. `canary-67`
  and `full` are promoted by hand in the Kargo UI (or set `autoPromotionEnabled` for them in the ProjectConfig, and
  verification alone gates the release).
- **Verification**, per Stage, as an Argo Rollouts AnalysisTemplate whose Job runs `kargo/verify.py`:
  1. waits for the hold (`CanaryHeld`, or `Available`), every role Ready;
  2. checks each role's new groups against the KEP's rounding, `min(d-1, max(1, ceil(d*p/100)))`;
  3. sends 30 requests **pinned to each revision** with `x-llm-d-disagg-revision`: all must succeed and come back from
     that revision, and the new revision's median latency must be within 2x the old one's;
  4. sends 60 unpinned requests and reports the share each revision served against the screener's weights.
- **Rollback.** Promote the previous Freight to `full`. That commit restores the old template at `canaryPercent: 100`,
  which hasB4K/lws#13 rolls straight back (lowering the percentage alone would not undo progress).
- **Ownership.** Git owns the whole DisaggregatedSet, `canaryPercent` included, so there is no drift and no
  `ignoreDifferences`. Role replicas belong to the autoscaler. Only Kargo writes `env/prod`.

## Variant 2: Argo Rollouts

A placeholder Rollout (its pod is `pause`) whose pod template changes with each release. Its steps are AnalysisRuns
whose Job (`app/rollouts/rollout/steps.py`) sets `canaryPercent` by server-side apply as its own field manager, waits
for the hold and smoke-tests the router; the Rollout pauses after 33% and 67%, and a last step sets the percentage back
to 0 so the next release starts held. Git never sets `canaryPercent` here. Caveats: an aborted Rollout does not roll the
DisaggregatedSet back, `promote --full` skips the reset to 0, and a rollback needs `RELEASE` bumped in `gen.py` (Argo
Rollouts skips the steps when the template returns to the stable one).

## Repository

| Path | What |
|---|---|
| `app/gen.py` | Writes `app/base/model-server/qwen.yaml` (and the Rollout for variant 2). Set `GPU`, `NODE_SELECTOR`, `TOLERATIONS` for your cluster |
| `app/base/` | The DisaggregatedSet and the router |
| `app/kargo/` | What Argo CD syncs for variant 1: the base plus `canary.yaml` (`canaryPercent`, set by Kargo) |
| `app/rollouts/` | What Argo CD syncs for variant 2: the base plus the Rollout and its step runner |
| `kargo/` | Project, Warehouse, Stages, and the verification |
| `argo/` | The Argo CD Application for each variant, and the DisaggregatedSet health check |
| `load/load.yaml` | Optional load: 4 workers through the router, tallying which revision served each request |

## Try it

You need:

- **Kubernetes with GPUs:** 6 at rest, plus 2 of surge during a release.
- **The LWS controller and DisaggregatedSet CRD from hasB4K/lws#13.** The image built from `1bb5823` is
  `ghcr.io/robertgshaw2-redhat/lws:canary-percent-1bb5823@sha256:e5978fab0ce138cc2a979e79e9290da9505242efe9c4225d4771070c83915333`
  (amd64); the CRD comes from `kustomize build config/crd` on that branch.
- **Argo CD and Argo Rollouts** (both variants: Kargo runs its verification as Argo Rollouts AnalysisRuns, which an
  Argo Rollouts controller executes). The run used Argo CD v3.5 and Argo Rollouts v1.10.
- **The Gateway API Inference Extension CRDs** (`InferencePool`), for the router.
- For variant 1, **Kargo** (the run used v1.12.5) and a git credential Kargo can push with.

Fork this repo, set `repoURL` everywhere it appears (`argo/`, `kargo/pipeline.yaml`), and in `app/gen.py` set the GPU
settings for your cluster; run `python3 gen.py` in `app/` and commit. Then install the health check:

```sh
kubectl -n argocd patch configmap argocd-cm --type merge --patch-file argo/argocd-cm-health.yaml
```

**Variant 1 (Kargo):**

```sh
kubectl create namespace disaggset-demo
kubectl label namespace disaggset-demo kargo.akuity.io/project=true
kubectl apply -f kargo/project.yaml
kubectl -n disaggset-demo create secret generic repo --from-literal=repoURL=git@github.com:<you>/disaggset-argo-demo.git \
  --from-file=sshPrivateKey=<a deploy key with write access>
kubectl -n disaggset-demo label secret repo kargo.akuity.io/cred-type=git
kubectl apply -k kargo/
kubectl -n argocd apply -f argo/application-kargo.yaml
```

The Warehouse finds the newest commit on `main`, `canary-33` promotes it, and the first promotion creates `env/prod`.
Release: change `ENGINE` in `app/gen.py`, run it, commit to `main`. Promote `canary-67` and `full` in the Kargo UI.

**Variant 2 (Argo Rollouts):** `kubectl -n argocd apply -f argo/application.yaml`. Release the same way; promote at
each pause from the Argo CD UI or `kubectl argo rollouts promote qwen-release -n <namespace>`.

Scale like an autoscaler would: `kubectl scale disaggregatedsetrolescaler qwen-prefill --replicas=5`.

## What was tested

| Case | Result |
|---|---|
| Releases and a rollback through 33 / 67 / 100% (Argo Rollouts) | 2 + 1, 1 + 2, 0 + 3 (old + new groups per role); a rollback at 5 groups gave 3 + 2, 1 + 4, 0 + 5 |
| Scale 3 → 5 during a 33% hold | 3 + 2: both revisions grow, so the new share stays near 33% |
| Then scale 5 → 3, `Immediate` / `Deferred` scale-down | 2 + 1 / 1 + 2, **still held** either way |
| Scale while a step is still moving | Lands on the same hold as scaling at a pause |
| Scale one role only | Each role is computed on its own |
| `scaleUpDuringRollout: Immediate` vs `Deferred` | Same counts; Immediate settled ~60 s sooner |
| Kargo, release r7 → r8 with every Stage auto-promoted | 33% → 67% → 100% in 7 minutes, each Stage verified. Pinned requests: 30/30 per revision at every Stage, each served by the revision it asked for. Unpinned requests went 35% to the new revision at 33% and 77% at 67% |
| The screener under load | 22,169 requests during the Kargo run, every successful one stamped with one revision; the load's per-revision tally followed the holds (31–36% new at 33%, 65–68% at 67%) |

Failures and findings:

- **One 503 in 22,169 requests** with the screener, at the moment the old revision's last groups were deleted at
  100%. No request logs were kept, so the cause is not established; a pinned or unpinned request reaching a revision
  as it loses its last pod in a role is the likely one. The earlier runs, without the screener, had none in about
  200,000.
- **Earlier canary Stages show Unhealthy** once a later Stage promotes, because all three Stages sync the same
  Application and each checks it is at *its* commit. Cosmetic, but it is what the Kargo UI shows.
- **`CanaryHeld` lags a scale by a few seconds** (a scale does not change the DisaggregatedSet's generation); the
  verification and step scripts also require every role Ready at its desired count.
- **Under `Deferred`, the new share can sit above the requested percentage** until the next Stage, as the KEP says.

Not tested yet: a rollback from the middle of a hold with Kargo, a failed verification, sub-roles or Hash groups,
`CanaryBlocked`, and `maxSurge: 0` on a full cluster.
