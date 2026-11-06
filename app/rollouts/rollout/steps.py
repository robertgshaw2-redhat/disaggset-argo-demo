#!/usr/bin/env python3
"""One step of a release: set the qwen DisaggregatedSet's canaryPercent (hasB4K/lws#13) and check the result.

  steps.py hold P   set canaryPercent to P, wait until the controller reports the hold (Progressing=True, reason
                    CanaryHeld) or, for P = 100, the finished rollout (Available=True), check each role's group counts
                    against the KEP's rounding, then smoke-test the router. Exit 0 passes the Rollout step.
  steps.py rest P   set canaryPercent to P (0): the next release starts held at zero new groups, at any replica count.
  steps.py init P   as rest, but only if this script has never set it (a Job synced with the app, which can run before
                    the DisaggregatedSet exists, so it waits for it; a no-op if anything set it since).

The field is written by server-side apply as field manager pd-demo-canary, its only owner: the Argo CD Application
applies the rest of the DisaggregatedSet server-side from git, which leaves it out. No ignoreDifferences is needed.
"""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

NAME = "qwen"
MANAGER = "pd-demo-canary"
ROUTER = "http://pd-demo-proxy/v1/chat/completions"  # the router Service in this namespace
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = open(f"{SA}/namespace").read().strip()  # the Job runs in the app's namespace
DS_URL = (f"https://kubernetes.default.svc/apis/disaggregatedset.x-k8s.io/v1/namespaces/{NAMESPACE}"
          f"/disaggregatedsets/{NAME}")
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def api(method, url, body=None, content_type="application/json"):
    token = open(f"{SA}/token").read()
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": content_type})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.load(r)


def set_percent(percent):
    apply = {"apiVersion": "disaggregatedset.x-k8s.io/v1", "kind": "DisaggregatedSet",
             "metadata": {"name": NAME, "namespace": NAMESPACE},
             "spec": {"rolloutStrategy": {"canaryPercent": percent}}}
    api("PATCH", f"{DS_URL}?fieldManager={MANAGER}&force=true", apply, "application/apply-patch+yaml")
    log(f"set canaryPercent {percent}")


def expected_new(desired, percent):
    """New groups the KEP's rounding asks for: min(d - 1, max(1, ceil(d * p / 100))) for 0 < p < 100."""
    if percent >= 100:
        return desired
    if percent == 0 or desired <= 1:
        return 0
    return min(desired - 1, max(1, -(-desired * percent // 100)))


def settled(ds, percent):
    """None when the controller reports the requested state and the counts agree; otherwise what it is waiting for."""
    st, gen = ds.get("status", {}), ds["metadata"]["generation"]
    if st.get("observedGeneration") != gen:
        return "controller has not observed the change"
    conds = {c["type"]: c for c in st.get("conditions", [])
             if c.get("status") == "True" and c.get("observedGeneration") == gen}
    if percent >= 100:
        if "Available" not in conds:
            p = conds.get("Progressing", {})
            return f"not Available yet: {p.get('reason')}: {p.get('message')}"
    else:
        p = conds.get("Progressing")
        if not p or p.get("reason") != "CanaryHeld":
            what = f"{p.get('reason')}: {p.get('message')}" if p else "no current Progressing condition"
            # A rollout already at or past the share settles as Available when every group is new.
            if "Available" in conds:
                return None
            return f"not CanaryHeld yet: {what}"
    for rs in st.get("roleStatuses", []):
        desired = rs.get("desiredReplicas", 0)
        have = {k: rs.get(k, 0) for k in ("replicas", "readyReplicas", "updatedReplicas")}
        if have["readyReplicas"] < desired:
            return f"{rs['name']}: {have}, {desired} desired"
    return None


def report(ds, percent):
    for rs in ds["status"]["roleStatuses"]:
        desired, updated = rs.get("desiredReplicas", 0), rs.get("updatedReplicas", 0)
        want = expected_new(desired, percent)
        note = "" if updated == want else f"  (KEP rounding gives {want})"
        log(f"{rs['name']}: {updated} of {desired} groups on the new revision, "
            f"{rs.get('replicas', 0) - updated} old, all ready{note}")
    for c in ds["status"].get("conditions", []):
        if c["type"] in ("Progressing", "Available") and c.get("status") == "True":
            log(f"{c['type']}={c['status']} {c['reason']}: {c.get('message')}")


def smoke(n=20):
    for i in range(n):
        body = {"model": "Qwen/Qwen3-0.6B", "max_tokens": 32,
                "messages": [{"role": "user", "content": f"Smoke test {i}: name a color."}]}
        req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
        assert out["choices"][0]["message"]["content"], out
    log(f"smoke test: {n}/{n} requests through the router succeeded")


def main():
    command, percent = sys.argv[1], int(sys.argv[2])
    for _ in range(60):
        try:
            ds = api("GET", DS_URL)
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or command != "init":
                raise
            time.sleep(5)
    else:
        sys.exit(f"DisaggregatedSet {NAME} not found")
    if command == "init":
        if any(f.get("manager") == MANAGER for f in ds["metadata"].get("managedFields", [])):
            log("canaryPercent already managed by", MANAGER, "- nothing to do")
            return
        set_percent(percent)
        return
    set_percent(percent)
    if command == "rest":
        return
    deadline, steady = time.time() + 1800, 0
    while True:
        waiting = settled(api("GET", DS_URL), percent)
        steady = steady + 1 if waiting is None else 0
        if steady == 3:  # 15 s without change
            break
        if waiting:
            log("waiting:", waiting)
        if time.time() > deadline:
            sys.exit(f"rollout did not settle at canaryPercent {percent}: {waiting}")
        time.sleep(5)
    report(api("GET", DS_URL), percent)
    smoke()


if __name__ == "__main__":
    main()
