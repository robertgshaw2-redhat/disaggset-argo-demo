# DisaggregatedSet canary rollouts with Argo CD and Argo Rollouts

A GitOps release of a prefill/decode (P/D) inference deployment. One LWS
[DisaggregatedSet](https://lws.sigs.k8s.io/docs/concepts/disaggregatedset/) runs the model. Argo CD syncs it from git, and
an Argo Rollout holds each release at 33%, then 67%, then 100% of the replica groups. The holds come from the
DisaggregatedSet's proposed `spec.rolloutStrategy.canaryPercent` field
([hasB4K/lws#13](https://github.com/hasB4K/lws/pull/13)). Stock Argo CD and Argo Rollouts drive it, with no plugins.

This exact tree ran on B200s on GKE on 2026-11-06, under continuous load. [RESULTS.md](RESULTS.md) has the full log.

## How it works

```
 git commit (new engine) ──► Argo CD ──► DisaggregatedSet template ──► new revision, held at 0%
                                    └──► Rollout pod template ───────► Rollout steps start
                                                                          │
     Rollout step: set canaryPercent 33 ──► LWS controller moves 1 of 3 groups per role
                   wait for CanaryHeld, check counts, send 20 requests through the router
                   pause ◄── promote (Argo CD UI or kubectl argo rollouts promote)
                   67 ... pause ... 100 ... then back to 0 for the next release
```

**One field per owner**, so nothing fights:

| Field | Owner |
|---|---|
| DisaggregatedSet template, rollout budgets (`maxSurge`, `maxUnavailable`), `scalingPolicy` | git, through Argo CD (server-side apply and diff) |
| `spec.rolloutStrategy.canaryPercent` | the Rollout's steps (`app/rollout/steps.py`, server-side apply as `pd-demo-canary`) |
| Role replicas | an autoscaler, through each role's `DisaggregatedSetRoleScaler` `/scale` (here, `kubectl scale`) |
| Which pods run which revision | the LWS controller |

Git never writes `canaryPercent`, so Argo CD neither resets it nor reports it as drift, and no `ignoreDifferences` is
needed. Between releases it rests at 0, so a new template starts with zero new groups whatever the replica count.

**The Rollout drives the DisaggregatedSet without managing it.** Its pod is a placeholder, and a release changes that
pod's annotations in the same commit as the engine. Each step is an AnalysisRun whose only metric is a Job running
`steps.py`. The Job sets the percentage, waits for the DisaggregatedSet's own status, and smoke-tests the router. A
failed Job fails the step and aborts the Rollout.

**The steps** (`app/rollout/rollout.yaml`), with 3 groups per role:

| Step | `canaryPercent` | Groups per role (old + new) | Then |
|---|---|---|---|
| 1 | 33 | 2 + 1 | wait for `CanaryHeld`, check counts, smoke test, **pause** |
| 2 | 67 | 1 + 2 | same, **pause** |
| 3 | 100 | 0 + 3 | wait for `Available`, smoke test |
| 4 | 0 | – | rest: holds the next release at 0 |

Per role, new groups = `min(d-1, max(1, ceil(d*p/100)))` for 0 < p < 100, so at 5 groups the same steps give 2 and
then 4 new groups. `steps.py` checks every hold against that formula.

**Argo CD health.** Argo CD has no health check for DisaggregatedSet. `argo/argocd-cm-health.yaml` maps
`Progressing/CanaryHeld` and `Available` to Healthy, so the Application shows Suspended while the Rollout waits at a
pause, not Progressing.

## What's in the repo

| Path | What |
|---|---|
| `app/` | What Argo CD syncs: the DisaggregatedSet (`model-server/`), the Rollout and its step runner (`rollout/`), and the router (`router/`: llm-d-router v1.0.0's endpoint picker with an InferencePool and Envoy, always P/D) |
| `app/gen.py` | Writes `model-server/qwen.yaml` and `rollout/rollout.yaml`. A release is: change `ENGINE`, run it, commit |
| `argo/` | The Argo CD Application and the DisaggregatedSet health check |
| `load/load.yaml` | Optional: 4 workers sending requests through the router, logging failures |

The model is Qwen3-0.6B: 3 prefill and 3 decode groups, one GPU each, with vLLM and NIXL KV transfer.

## Try it

You need:

- **Kubernetes with GPUs:** 6 at rest, plus 2 of surge during a release, and up to 12 to repeat the 3 → 5 scaling tests.
- **The LWS controller and DisaggregatedSet CRD from hasB4K/lws#13.** The image built from `1bb5823` is
  `ghcr.io/robertgshaw2-redhat/lws:canary-percent-1bb5823@sha256:e5978fab0ce138cc2a979e79e9290da9505242efe9c4225d4771070c83915333`
  (amd64). The CRD comes from `kustomize build config/crd` on that branch.
- **Argo CD and Argo Rollouts.** The run used Argo CD v3.5 and Argo Rollouts v1.10.
- **The Gateway API Inference Extension CRDs** (`InferencePool`, `inference.networking.k8s.io/v1`), for the router.

Then:

1. Fork this repo. Set `repoURL` in `argo/application.yaml` to your fork, and the destination namespace too.
2. In `app/gen.py`, set `GPU`, `NODE_SELECTOR` and `TOLERATIONS` for your cluster. The defaults are GKE B200s with the
   NVIDIA DRA driver. Run `python3 gen.py` in `app/` and commit.
3. Install the health check and the Application:

   ```sh
   kubectl -n argocd patch configmap argocd-cm --type merge --patch-file argo/argocd-cm-health.yaml
   kubectl -n argocd apply -f argo/application.yaml
   ```

   The first sync creates everything. A one-off Job (`pd-demo-init-canary`) sets `canaryPercent` to 0.
4. Release: change `ENGINE` in `app/gen.py` (for example `r7` → `r8`), run `python3 gen.py`, commit, push. Promote at each
   pause from the Argo CD UI (the Rollout's resume action) or with
   `kubectl argo rollouts promote qwen-release -n <namespace>`. Don't use `promote --full`, which skips the step that
   resets the percentage to 0.
5. Roll back: set `ENGINE` back and bump `RELEASE`, then commit. Bumping `RELEASE` makes Argo Rollouts run the steps
   again; without it, Argo Rollouts treats a return to the stable template as already rolled out. The rollback goes
   through the same holds.
6. Scale like an autoscaler would: `kubectl scale disaggregatedsetrolescaler qwen-prefill --replicas=5`.

## What was tested

Two releases, a rollback, and two more releases, on B200s with ~36 requests/s through the router the whole time. Every
hold matched the formula above, every step's smoke test passed, and the router returned no 4xx or 5xx. A final release
from this exact repo passed too (19,148 requests, 0 failures).

| Case | Result |
|---|---|
| Release and rollback through 33 / 67 / 100% | 2 + 1, 1 + 2, 0 + 3. A rollback at 5 groups gave 3 + 2, 1 + 4, 0 + 5 |
| Scale 3 → 5 during a 33% hold | 3 + 2: both revisions grow, so the new share stays near 33% |
| Then scale 5 → 3, `scaleDownDuringRollout: Immediate` | 2 + 1, still held |
| Then scale 5 → 3, `scaleDownDuringRollout: Deferred` | 1 + 2, **still held**. The new groups are kept, so the share is 67% until the next step |
| Scale while a step is still moving | Lands on the same hold as scaling at a pause. Old groups still needed are kept, not drained and regrown |
| Scale one role only (prefill 3 → 5, decode 3) | prefill 3 + 2, decode unchanged at 2 + 1. Each role is computed on its own |
| `scaleUpDuringRollout: Immediate` vs `Deferred` | Same counts. Immediate asks for the extra new group at once and settled ~60 s sooner |
| Argo CD | Synced throughout. No drift from `canaryPercent` or replicas. Suspended at each pause, Healthy between releases |

Seen along the way:

- **`CanaryHeld` lags a scale by a few seconds.** For 7–8 s after a scaler changes, `CanaryHeld` can still show the
  previous counts, because a scale does not change the DisaggregatedSet's generation. `steps.py` also requires every
  role's `readyReplicas` to equal its `desiredReplicas`, which avoids acting on that.
- **Under `Deferred`, the share can sit above the requested percentage** until the next step, as hasB4K/lws#13's KEP
  says.

Not tested: a failed analysis while held, sub-roles or Hash groups, and `CanaryBlocked` (a role with one group).

The same demo ran earlier on the per-role partition design
([kubernetes-sigs/lws#1265](https://github.com/kubernetes-sigs/lws/pull/1265)).
[RESULTS.md](RESULTS.md#against-the-per-role-partition-lws1265) compares the two.
