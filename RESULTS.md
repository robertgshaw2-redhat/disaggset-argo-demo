# Results

All runs: 2026-11-06, GKE, NVIDIA B200 (one per group), LWS controller built from hasB4K/lws#13 at `1bb5823`, Argo CD
v3.5 and Argo Rollouts v1.10 installed namespace-scoped. Qwen3-0.6B, 3 prefill + 3 decode groups at rest,
`scaling.mode: External`, `maxSurge: 1`, `maxUnavailable: 0`. Load: `load/load.yaml` (4 workers, ~36 requests/s).

Times are UTC. Groups are old + new per role; prefill and decode were equal unless noted. Releases are named by
`ENGINE` in `app/gen.py`.

## Run 1: releases, scaling during a hold, rollback

`scaleUpDuringRollout` was the default (`Deferred`) for this run.

Load: 4 workers through the router (~36 requests/s) for the whole run. The load pod logged 62,630 requests and 0
failures by 19:39; its final log line was lost when the pod was deleted. The router's Envoy counters since its start
(which also cover two earlier runs of the partition version of this demo) show 171,877 requests, 0 4xx, 0 5xx, and 15 without a response
code. Every step's smoke test passed (20/20), and every settled hold matched the KEP's rounding.

Times UTC; groups are old + new per role (prefill and decode were always equal).

| Time | Action | Result |
|---|---|---|
| before 19:11 | switch the cluster from the lws#1265 build: remove the role partitions (old controller, at rest), apply #13's CRD, point the Application here, init Job sets 0%, swap the controller image | no rollout: same revision hash (`b9856edc`), Available throughout |
| 19:12 | release r6 → r7 (`scaleDownDuringRollout: Immediate`) | 19:14 `CanaryHeld` at 33%: 2 + 1, pause |
| 19:14 | scale both scalers 3 → 5 | 19:16 `CanaryHeld`: **3 + 2** (old grew from 2 to 3) |
| 19:16 | scale 5 → 3 | 19:17 `CanaryHeld`: **2 + 1** (the excess came from both revisions) |
| 19:28 | promote | 19:30 67%: 1 + 2, pause |
| 19:30 | promote | 19:32 100%: 0 + 3, r6 deleted; 19:32 rest at 0% |
| 19:33 | release r7 → r8, `Deferred` | 19:33 new revision created at 0/0, `CanaryHeld` at 0%; 19:34 33%: 2 + 1, pause |
| 19:35 | scale 3 → 5 | 19:37 `CanaryHeld`: **3 + 2** |
| 19:37 | scale 5 → 3 | 19:38 `CanaryHeld`: **1 + 2**: Deferred kept both new groups and the old revision absorbed the cut, but **the hold survived** |
| 19:39 | promote, promote | 67%: already 1 + 2, no change; 19:42 100%: 0 + 3 |
| 19:43 | at rest, scale 3 → 5 | 0 + 5 on r8 |
| 19:45 | roll back to r7 (`ENGINE = "r7"`, `RELEASE = 3`), at 5 groups | 19:48 33%: **3 + 2**; 19:52 67%: **1 + 4**; 19:55 100%: 0 + 5, r8 deleted |
| 19:56 | scale 5 → 3 | 0 + 3, Available |

## Run 2: scaling while a step is moving, one role at a time, Immediate scale-up

The three cases the first run left out. `scaleDownDuringRollout: Immediate` throughout (so each scale-down returns to
the share), `scaleUpDuringRollout` as noted. Same load: 62,375 requests from the load pod, 0 failures. All 6 hold steps
passed; no count differed from the KEP's rounding.

| Time | `scaleUp` | Action | Result |
|---|---|---|---|
| 20:33 | Deferred | release r7 → r9; new revision created at 0/0 (0% hold), step sets 33% | 1 new group loading, old still 3 |
| 20:33:59 | Deferred | **scale both 3 → 5 while the 33% step is still moving** | old kept at 3 (no group drained); new 1 → 2 requested only at 20:35:07, after the first new group was Ready; 20:36:41 `CanaryHeld` **3 + 2** |
| 20:37 | Deferred | scale both 5 → 3 | 20:37:51 2 + 1 |
| 20:38:03 | Deferred | **scale prefill only 3 → 5** | 20:39:48 prefill **3 + 2**, decode untouched at **2 + 1**; `CanaryHeld` "prefill 2/5, decode 1/3" |
| 20:40:11 | Deferred | promote to 67%; **scale decode only 3 → 5 while that step is moving** | decode's old revision stayed at 2 until its new groups were Ready, then drained to 1; 20:45:13 `CanaryHeld` prefill **1 + 4**, decode **1 + 4** |
| 20:45 | Deferred | promote to 100%, scale both back to 3 | 0 + 3 |
| 20:48:56 | **Immediate** | release r9 → r6, step sets 33%; **scale both 3 → 5 while moving** (20:49:24) | new 1 → 2 requested **within 6 s** (20:49:30), not after the first was Ready; 20:51:02 `CanaryHeld` **3 + 2**, 98 s after the scale against 162 s under Deferred |
| 20:51 | Immediate | scale both 5 → 3, then **decode only 3 → 5** at the settled hold (20:52:19) | old decode 2 → 3 and new 1 → 2 requested together; 20:54:01 prefill 2 + 1, decode **3 + 2** |
| 20:54 | Immediate | promote to 67% with prefill at 3 and decode at 5 | 20:57:52 prefill **1 + 2**, decode **1 + 4** |
| 20:58 | Immediate | promote to 100%, scale decode back to 3 | 0 + 3 |

What it shows:

- **Scaling while a step is moving lands on the same hold as scaling at a pause.** The controller recomputed the share
  for the new total mid-step: it kept the old groups it still needed instead of draining and re-growing them, and the
  step's Job simply waited for the new `CanaryHeld`.
- **Each role is computed on its own.** With prefill at 3 and decode at 5, the same percentage gave each role its own
  rounding (33%: 2 + 1 and 3 + 2; 67%: 1 + 2 and 1 + 4), and scaling one role never moved the other.
- **`scaleUpDuringRollout` changes only the pacing.** `Immediate` requested the extra new group at once; `Deferred`
  waited for the loading new group to become Ready first. Both settled at the same counts, and the old revision was topped
  up under both, as the KEP says.

## Run 3: this repository, as published

The live Application was pointed at this exact tree (namespace-agnostic `app/`, short Service names in Envoy, the
router's pool namespace from the downward API), which synced with no change to the DisaggregatedSet or Rollout. Then one
release, r6 → r7, at 3 groups per role, with the load pod running: 19,148 requests, 0 failures.

| Time | Step | Result |
|---|---|---|
| 21:10 | 33% | 21:12 `CanaryHeld`: 2 + 1, smoke test 20/20, pause |
| 21:12 | promote → 67% | 21:14 `CanaryHeld`: 1 + 2, smoke test 20/20, pause |
| 21:15 | promote → 100% | 21:17 0 + 3, `Available`, smoke test 20/20; rest at 0% |

## Against the per-role partition (lws#1265)

The same demo on lws#1265's per-role `partition`, run on 2026-11-02:

| | #1265 partition | #13 canaryPercent |
|---|---|---|
| Scale-up 3 → 5 during a 1-of-3 hold | 2 + 3: all growth on the new revision (60% new) | 3 + 2: both revisions grow (40% new) |
| Then 5 → 3, `Deferred` | 0 + 3: **the release completed** with the Rollout still paused at step 1 | 1 + 2: still held (`CanaryHeld`), 67% new until the next step |
| Then 5 → 3, `Immediate` | 2 + 1, held | 2 + 1, held |
| Rest value between releases | the replica count (3): only holds a new release at zero groups while replicas ≤ 3 | 0: holds at any replica count |
| Steps at 5 groups | the steps' absolute partitions (2, 1) would need recomputing | same steps; 33% → 2 new, 67% → 4 new |
| Argo CD | health check on `RolloutPartitioned` | health check on `CanaryHeld`; otherwise unchanged |

## Observations

- **Everything held where the KEP says it would.** No step needed a retry, and no count differed from the rounding.
- **`Deferred` scale-down leaves the new share above the requested percentage** (1 + 2 at 33%) until the next step, as
  the KEP documents. The hold is not lost, which was the #1265 failure. `CanaryHeld`'s message reports the real share
  ("prefill 2/3"), so a step driver can see it.
- **`CanaryHeld` stays briefly after the scaler changes.** For 7–8 s after a scale (19:14:53, 19:16:46, 19:35:37), the
  condition still said `CanaryHeld` with the old counts while the LWS replicas were already moving, because a scaler
  change does not bump the DisaggregatedSet's generation. A driver that checks `observedGeneration` alone could read a
  stale hold right after an autoscaler acts; checking `readyReplicas == desiredReplicas` per role as well (as
  `steps.py` does) avoids it.
- **The 67% step at 3 groups was a no-op after the Deferred scale-down** (already 2 new): it settled at once and
  reported `CanaryHeld` at 67%.
- **Rolling back to a deleted revision** re-created its LWS under the same hash (`0c74d8d1`) and held it like a release.
- **Not tested:** a failed analysis while held, sub-roles or Hash groups, and `CanaryBlocked` (every role has at least
  two groups here).
