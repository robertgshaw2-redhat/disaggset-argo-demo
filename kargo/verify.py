#!/usr/bin/env python3
"""Kargo verification of one canary Stage: is the qwen DisaggregatedSet held where it should be, and does each revision
serve on its own?

  verify.py P    P is the Stage's canaryPercent (33, 67 or 100). Exit 0 passes verification; anything else fails it,
                 which keeps this Freight from the next Stage.

1. Wait until the controller reports the requested state for the current generation: Progressing=True with reason
   CanaryHeld (or Available=True once every group is on the target revision), every role Ready at its desired count.
2. Find the revisions from the pods' disaggregatedset.x-k8s.io/revision label. The target revision is the one whose
   pods carry the engine label of the DisaggregatedSet's current template.
3. Check the target's Ready groups per role against hasB4K/lws#13's rounding, min(d-1, max(1, ceil(d*p/100))).
4. Send requests through the router pinned to each revision with the x-llm-d-disagg-revision header (the EPP's
   DisaggregatedSet rollout screener keeps a pinned request's prefill and decode on that revision and fails closed
   otherwise). Every request must succeed and come back stamped with the revision it asked for, and the target's
   median latency must be within 2x the old revision's (plus 0.5 s).
5. Send unpinned requests and report the share each revision served, against the screener's expected share (Ready
   pods per revision). Reported only: a sample this small is noisy.
"""
import json
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = open(f"{SA}/namespace").read().strip()
NAME = "qwen"
ROUTER = "http://pd-demo-proxy/v1/chat/completions"
MODEL = "Qwen/Qwen3-0.6B"
REVISION_HEADER = "x-llm-d-disagg-revision"
REVISION_LABEL = "disaggregatedset.x-k8s.io/revision"
ROLE_LABEL = "disaggregatedset.x-k8s.io/role"
ENGINE_LABEL = "pd-demo.llm-d.ai/engine"
API = "https://kubernetes.default.svc"
DS_URL = f"{API}/apis/disaggregatedset.x-k8s.io/v1/namespaces/{NAMESPACE}/disaggregatedsets/{NAME}"
PODS_URL = f"{API}/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=disaggregatedset.x-k8s.io/name%3D{NAME}"
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
PINNED, UNPINNED = 30, 60


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def get(url):
    token = open(f"{SA}/token").read()
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.load(r)


def expected_new(desired, percent):
    if percent >= 100:
        return desired
    if percent == 0 or desired <= 1:
        return 0
    return min(desired - 1, max(1, -(-desired * percent // 100)))


def settled(ds, percent):
    st, gen = ds.get("status", {}), ds["metadata"]["generation"]
    have = (ds["spec"].get("rolloutStrategy") or {}).get("canaryPercent", 100)
    if have != percent:
        return f"canaryPercent is {have}, waiting for {percent} (Argo CD sync)"
    if st.get("observedGeneration") != gen:
        return "controller has not observed the change"
    conds = {c["type"]: c for c in st.get("conditions", [])
             if c.get("status") == "True" and c.get("observedGeneration") == gen}
    if "Available" not in conds:
        p = conds.get("Progressing")
        if percent >= 100 or not p or p.get("reason") != "CanaryHeld":
            return f"not settled: {p.get('reason') if p else None}: {p.get('message') if p else ''}"
    for rs in st.get("roleStatuses", []):
        if rs.get("readyReplicas", 0) != rs.get("desiredReplicas", 0) or rs.get("replicas", 0) != rs.get("desiredReplicas", 0):
            return f"{rs['name']}: {rs.get('readyReplicas', 0)} Ready of {rs.get('desiredReplicas', 0)} desired"
    return None


def ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))


def request(i, revision=None):
    body = {"model": MODEL, "max_tokens": 32,
            "messages": [{"role": "user", "content": f"Verification request {i}: name a color."}]}
    headers = {"Content-Type": "application/json"}
    if revision:
        headers[REVISION_HEADER] = revision
    req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(), headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
            served = r.headers.get(REVISION_HEADER)
        ok = bool(out["choices"][0]["message"]["content"])
        return ok, served, time.monotonic() - start, None
    except Exception as e:
        return False, None, time.monotonic() - start, repr(e)[:200]


def main():
    percent = int(sys.argv[1])
    deadline, steady = time.time() + 1800, 0
    while True:
        ds = get(DS_URL)
        waiting = settled(ds, percent)
        steady = steady + 1 if waiting is None else 0
        if steady == 3:
            break
        if waiting:
            log("waiting:", waiting)
        if time.time() > deadline:
            sys.exit(f"FAIL: not settled at canaryPercent {percent}: {waiting}")
        time.sleep(5)

    engine = ds["spec"]["roles"][0]["spec"]["leaderWorkerTemplate"]["workerTemplate"]["metadata"]["labels"][ENGINE_LABEL]
    revisions = {}  # revision -> {"engine": ..., "ready": {role: n}}
    for pod in get(PODS_URL)["items"]:
        labels = pod["metadata"]["labels"]
        rev = revisions.setdefault(labels[REVISION_LABEL], {"engine": labels.get(ENGINE_LABEL), "ready": {}})
        if ready(pod):
            rev["ready"][labels[ROLE_LABEL]] = rev["ready"].get(labels[ROLE_LABEL], 0) + 1
    targets = [r for r, v in revisions.items() if v["engine"] == engine]
    if len(targets) != 1:
        sys.exit(f"FAIL: expected one revision with engine {engine}, found {targets} in {revisions}")
    target = targets[0]
    failures = []

    for rs in ds["status"]["roleStatuses"]:
        if len(revisions) == 1:  # nothing to roll: this Freight's template is already everywhere
            log(f"{rs['name']}: only revision {target} ({engine}) is running, no rollout to hold")
            continue
        want = expected_new(rs["desiredReplicas"], percent)
        have = revisions[target]["ready"].get(rs["name"], 0)
        log(f"{rs['name']}: {have} of {rs['desiredReplicas']} Ready groups on target revision {target} ({engine}); "
            f"rounding gives {want}")
        if have != want:
            failures.append(f"{rs['name']}: {have} new groups, rounding gives {want}")

    latency = {}
    for rev in revisions:
        role = "target" if rev == target else "old"
        if not all(revisions[rev]["ready"].get(r, 0) for r in ("prefill", "decode")):
            log(f"revision {rev} ({role}) is not serving (no Ready pod in some role); not pinned")
            continue
        results = [request(i, rev) for i in range(PINNED)]
        bad = [r for r in results if not r[0] or r[1] != rev]
        latency[rev] = statistics.median(r[2] for r in results)
        log(f"pinned to {rev} ({role}, {revisions[rev]['engine']}): {PINNED - len(bad)}/{PINNED} succeeded and were "
            f"served by {rev}; median {latency[rev]:.2f} s")
        if bad:
            failures.append(f"{len(bad)} of {PINNED} requests pinned to {rev} failed or crossed revisions: {bad[:3]}")
    if target in latency:
        for rev, med in latency.items():
            if rev != target and latency[target] > 2 * med + 0.5:
                failures.append(f"target median {latency[target]:.2f} s is over 2x old {med:.2f} s + 0.5 s")
    else:
        failures.append(f"target revision {target} is not serving")

    weights = {r: sum(v["ready"].values()) for r, v in revisions.items()
               if all(v["ready"].get(x, 0) for x in ("prefill", "decode"))}
    results = [request(i) for i in range(UNPINNED)]
    served = {}
    for ok, rev, _, err in results:
        if not ok:
            failures.append(f"unpinned request failed: {err}")
        served[rev] = served.get(rev, 0) + 1
    total = sum(weights.values())
    for rev in sorted(weights):
        log(f"unpinned: {rev} served {served.get(rev, 0)}/{UNPINNED} "
            f"({100 * served.get(rev, 0) / UNPINNED:.0f}%), screener weight {100 * weights[rev] / total:.0f}%")

    if failures:
        for f in failures:
            log("FAIL:", f)
        sys.exit(1)
    log(f"PASS: canaryPercent {percent} verified")


if __name__ == "__main__":
    main()
