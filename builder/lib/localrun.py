"""Run a workflow's code nodes locally, without Freya, to test the date and account facts.

    python3 builder/lib/localrun.py <workflow.json> --now 2026-10-14T11:00 [--products products.json] [--show]

Runs `setup`, then (with --products) `account_prep` on that product list, the way the platform
would: a shared `vars` dict, the snippet executed as-is. Prints the facts the model will see.
Use it to check every date rule of a brief by hand: deadlines per band, holidays, attempt dates,
the callback window. A wrong date here is a wrong date in every call.

As a library: run_code_nodes(wf, vars) -> vars after the code nodes ran.
"""
import json, sys, argparse, pathlib


def code_of(node):
    for t in node.get("pre_actions") or []:
        fn = t.get("function") or {}
        if fn.get("handler") == "__function__:code_exec":
            return ((fn.get("parameters") or {}).get("properties") or {}).get("code", {}).get("default")
    return None


def run_node(wf, key, vars):
    src = code_of(wf["nodes"][key])
    if not src:
        raise KeyError(f"node {key} has no code")
    env = {"vars": vars}
    exec(compile(src, key, "exec"), env)
    return vars


def run_code_nodes(wf, vars, products=None):
    for key, n in wf["nodes"].items():
        if n.get("node_type") == "code" and key != "account_prep":
            run_node(wf, key, vars)
    if products is not None and "account_prep" in wf["nodes"]:
        vars["products"] = products
        run_node(wf, "account_prep", vars)
    return vars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow")
    ap.add_argument("--now", required=True)
    ap.add_argument("--products", help="JSON file with a list of products as get_account returns them")
    ap.add_argument("--show", action="store_true", help="print the full date calendar too")
    a = ap.parse_args()
    wf = json.loads(pathlib.Path(a.workflow).read_text())
    wf = wf.get("workflow", wf) if "nodes" not in wf else wf
    prods = json.loads(pathlib.Path(a.products).read_text()) if a.products else None
    v = run_code_nodes(wf, {"now": a.now, "customer_id": "c_local"}, prods)
    for k in sorted(v):
        if k in ("products",):
            continue
        val = str(v[k])
        if k == "date_facts" and not a.show:
            val = "\n".join(val.split("\n")[:10]) + "\n  ..."
        print(f"== {k}\n{val}")
    if v.get("code_error"):
        print("CODE ERROR:", v["code_error"])
        sys.exit(1)


if __name__ == "__main__":
    main()
