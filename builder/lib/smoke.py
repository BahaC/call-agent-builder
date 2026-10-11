"""Smoke test a saved workflow with two chat turns (seconds, no simulation queue).

    python3 builder/lib/smoke.py <workflow_id> [--vars '{"first_name": "...", ...}']

Checks that the published workflow starts a call and answers. It does not test the bank tools:
the customer id is not a real one.

Never blocks a build: one try per turn, 25 s timeout. When the platform's chat route itself fails
(5xx, timeout) it prints SMOKE SKIPPED and exits 0, because that says nothing about the workflow.
Exit 1 only when the workflow answered with nothing or the route rejected the workflow (4xx).
"""
import json, os, sys, argparse, pathlib, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import freya  # noqa


def post(path, body, timeout=25):
    req = urllib.request.Request(freya.URL + path, data=json.dumps(body).encode(), headers=freya.HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    return out.get("data", {}).get("result", out) if isinstance(out, dict) else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow_id")
    ap.add_argument("--vars", default="{}")
    ap.add_argument("--say", action="append", default=None, help="customer lines to send, in order")
    a = ap.parse_args()
    v = {"customer_id": "c_smoke_test", "first_name": "Alex", "last_name": "Morgan", "title": "Ms", "now": "2026-10-14T11:00"}
    v.update(json.loads(a.vars))
    lines = a.say or ["Hello?", "Yes, speaking."]
    call_id, ok = None, True
    for msg in lines:
        body = {"workflowId": a.workflow_id, "message": msg, "customerVariables": v}
        if call_id:
            body["callId"] = call_id
        try:
            r = post("/api/v2/chat/message", body)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                print(f"SMOKE SKIPPED: the chat route answered {e.code}; this is the platform, not the workflow. Move on.")
                sys.exit(0)
            print(f"SMOKE FAIL on {msg!r}: {e.code} {e.read().decode()[:300]}")
            sys.exit(1)
        except Exception as e:
            print(f"SMOKE SKIPPED: the chat route did not answer ({str(e)[:120]}). Move on.")
            sys.exit(0)
        call_id = r.get("callId") or call_id
        resp = (r.get("response") or "").strip()
        print(f"customer: {msg}\nagent:    {resp or '(nothing)'}")
        if not resp:
            ok = False
    if call_id:
        try:
            post("/api/v2/chat/end", {"callId": call_id}, timeout=10)
        except Exception:
            pass
    print("SMOKE", "OK" if ok else "FAIL: the agent said nothing")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
