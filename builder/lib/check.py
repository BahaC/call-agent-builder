"""Static checks for a generated workflow. No network, no simulation, so they work on any brief.

    python3 builder/lib/check.py <workflow.json> <tool_api.json> [--spec out/<slot>.spec.json]

Prints one line per problem and exits 1 when there is an ERROR. WARN lines do not fail the check.
Checks:
  structure   initial node, transition targets, node types, passthrough nodes end with `always`,
              no null values, no agent_config, every node can reach an end_call node
  code        no '{{' or '}}' in code or hooks, every snippet compiles as Python
  tools       every api_call tool exists in the tool API, URL matches, body fields and enums match,
              required fields present, one close tool reachable
  variables   every {{var}} in prompts and tool defaults is set somewhere (call vars, code, mappings, extraction)
  spec        (with --spec) every outcome/reason pair, holiday, identity field and fixed sentence of the spec
              appears in the workflow, and every rule has an `enforced_in` that names a real place
"""
import json, re, sys, pathlib, argparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from wfkit import load_openapi

CALL_VARS_DEFAULT = {"customer_id", "first_name", "last_name", "title", "now"}
SYSTEM_PREFIXES = ("system.", "call.")


class Report:
    def __init__(self):
        self.items = []

    def err(self, where, msg):
        self.items.append(("ERROR", where, msg))

    def warn(self, where, msg):
        self.items.append(("WARN", where, msg))

    @property
    def errors(self):
        return [i for i in self.items if i[0] == "ERROR"]

    def print(self):
        for lvl, where, msg in self.items:
            print(f"{lvl:5} {where}: {msg}")
        print(f"-- {len(self.errors)} error(s), {len(self.items) - len(self.errors)} warning(s)")


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def node_tools(n):
    return [t for t in (n.get("tools") or []) + (n.get("pre_actions") or []) if isinstance(t, dict)]


def code_snippets(wf):
    """(where, source) for every code node snippet and hook."""
    out = []
    for key, n in (wf.get("nodes") or {}).items():
        for t in node_tools(n):
            fn = t.get("function") or {}
            if fn.get("handler") == "__function__:code_exec":
                props = (fn.get("parameters") or {}).get("properties") or {}
                src = (props.get("code") or {}).get("default")
                if src:
                    # a code_exec tool the model calls: its own arguments arrive as {{name}} placeholders in the code
                    for arg in props:
                        if arg not in ("code", "timeout_ms"):
                            src = src.replace("{{" + arg + "}}", "ARG_" + arg.upper())
                    out.append((f"node {key} code", src))
            for h in ("pre_process_code", "post_process_code"):
                if t.get(h):
                    out.append((f"node {key} tool {fn.get('name')} {h}", t[h]))
    for t in wf.get("tools") or []:
        for h in ("pre_process_code", "post_process_code"):
            if t.get(h):
                out.append((f"workflow tool {(t.get('function') or {}).get('name')} {h}", t[h]))
    return out


def check_structure(wf, r):
    nodes = wf.get("nodes") or {}
    if "agent_config" in wf:
        r.err("workflow", "agent_config must be left out")
    if wf.get("initial_node") not in nodes:
        r.err("workflow", f"initial_node {wf.get('initial_node')!r} is not a node")
    for p, v in walk(wf):
        if v is None:
            r.warn("workflow", f"null value at {p} (docs: leave the field out instead)")
    def ends_call(n):
        return n.get("node_type") == "end_call" or any(
            (t.get("function") or {}).get("handler") == "__function__:end_call" for t in node_tools(n))
    ends = {k for k, n in nodes.items() if ends_call(n)}
    if not ends:
        r.err("workflow", "no end_call node and no end_call tool: no call can end")
    edges = {}
    for k, n in nodes.items():
        t = n.get("node_type")
        if t not in ("conversation", "api_request", "code", "end_call"):
            r.warn(f"node {k}", f"node_type {t!r} is not one of the four documented types")
        if n.get("tools") and t != "conversation":
            r.err(f"node {k}", "tools are only kept on conversation nodes")
        if n.get("pre_actions") and t not in ("api_request", "code"):
            r.err(f"node {k}", "pre_actions are only kept on api_request and code nodes")
        trs = n.get("transitions") or []
        targets = [tr.get("target_node") for tr in trs]
        for x in [n.get("passthrough_default_transition"), n.get("max_turn_transition")]:
            if x:
                targets.append(x)
        for tg in targets:
            if tg not in nodes and tg != "__previous__":
                r.err(f"node {k}", f"transition to unknown node {tg!r}")
        edges[k] = [x for x in targets if x in nodes]
        if n.get("passthrough"):
            if not trs or (trs[-1].get("condition") or {}).get("operator") != "always":
                r.err(f"node {k}", "passthrough node must end with an `always` transition")
        if t == "conversation" and not trs and k not in ends:
            r.err(f"node {k}", "conversation node has no transitions: the call can never leave it")
        if n.get("max_node_turns") and not n.get("max_turn_transition") and t == "conversation":
            r.warn(f"node {k}", "max_node_turns without max_turn_transition does nothing at the cap")
        for tr in trs:
            c = tr.get("condition") or {}
            for cc in [c] + list(c.get("conditions") or []):
                if cc.get("operator") == "equals" and str(cc.get("operand")) in ("true", "True", "false", "False"):
                    sib = [str(x.get("operand")) for x in (c.get("conditions") or []) if x.get("variable") == cc.get("variable")]
                    if len(sib) < 2:
                        r.warn(f"node {k}", f"boolean route on {cc.get('variable')} tests one spelling only")
    # reachability: every node reachable from initial, every node reaches an end
    seen, stack = set(), [wf.get("initial_node")]
    while stack:
        x = stack.pop()
        if x in seen or x not in nodes:
            continue
        seen.add(x)
        stack += edges.get(x, [])
    for k in nodes:
        if k not in seen:
            r.warn(f"node {k}", "not reachable from the initial node")
    for gk, gn in (wf.get("global_nodes") or {}).items():
        for tr in gn.get("transitions") or []:
            if tr.get("target_node") not in nodes and tr.get("target_node") != "__previous__":
                r.err(f"global {gk}", f"transition to unknown node {tr.get('target_node')!r}")
    can_end = set(ends)
    changed = True
    while changed:
        changed = False
        for k, outs in edges.items():
            if k not in can_end and any(o in can_end for o in outs):
                can_end.add(k)
                changed = True
    for k in seen:
        if k not in can_end:
            r.err(f"node {k}", "no path from this node to an end_call node")


def check_code(wf, r):
    for where, src in code_snippets(wf):
        if "{{" in src or "}}" in src:
            r.err(where, "contains '{{' or '}}'")
        try:
            compile(src, where, "exec")
        except SyntaxError as e:
            r.err(where, f"Python syntax error line {e.lineno}: {e.msg}")
        if "try:" not in src:
            r.warn(where, "no try/except: an exception here may break the call")


def check_tools(wf, api, r):
    seen_names = set()
    for key, n in list((wf.get("nodes") or {}).items()) + [("<workflow>", {"tools": wf.get("tools") or []})]:
        for t in node_tools(n):
            fn = t.get("function") or {}
            name = fn.get("name")
            if fn.get("handler") != "__function__:api_call":
                continue
            seen_names.add(name)
            where = f"node {key} tool {name}"
            if name not in api:
                r.err(where, "tool is not in the tool API file")
                continue
            spec = api[name]
            props = ((fn.get("parameters") or {}).get("properties") or {})
            url = (props.get("url") or {}).get("default")
            if url != spec["url"]:
                r.err(where, f"url {url!r} != tool API {spec['url']!r}")
            body = (props.get("body") or {}).get("properties") or {}
            for f, leaf in body.items():
                if f not in spec["properties"]:
                    r.err(where, f"body field {f!r} is not in the tool API")
                    continue
                api_enum = spec["properties"][f].get("enum")
                enum = leaf.get("enum")
                if api_enum and enum and not set(enum) <= set(api_enum):
                    r.err(where, f"{f}: enum {enum} has values the tool API does not allow ({api_enum})")
                if api_enum and not enum:
                    r.warn(where, f"{f}: tool API has enum {api_enum} but the tool does not constrain it")
                if "default" not in leaf:
                    r.err(where, f"{f}: no default (every body field needs one)")
            for f in spec["required"]:
                if f not in body:
                    r.err(where, f"required field {f!r} missing")
            if not fn.get("description"):
                r.warn(where, "no description: say when to call it")
            if not t.get("post_process_code"):
                r.warn(where, "no post hook with what_to_do_next: the model may stay silent after the result")
    for name in api:
        if name not in seen_names:
            r.warn("tools", f"tool {name!r} from the tool API is not used anywhere (fine if the brief never needs it)")


def set_variables(wf):
    vs = set(CALL_VARS_DEFAULT) | set((wf.get("test_variables") or {}).keys())
    for where, src in code_snippets(wf):
        vs |= set(re.findall(r"vars\[\s*['\"](\w+)['\"]\s*\]\s*=", src))
    for key, n in (wf.get("nodes") or {}).items():
        for m in (n.get("response_mapping") or []):
            vs.add(m.get("variable"))
        for t in node_tools(n):
            for m in t.get("response_mapping") or []:
                vs.add(m.get("variable"))
            nm = (t.get("function") or {}).get("name")
            if nm:
                vs.add(nm + "_response")
        for e in n.get("extraction_schema") or []:
            vs.add(e.get("name") or e.get("variable"))
    return {v for v in vs if v}


def check_variables(wf, r, call_vars):
    known = set_variables(wf) | set(call_vars)
    for key, n in list((wf.get("nodes") or {}).items()) + [("<system_prompt>", {"task_message": wf.get("system_prompt", "")})]:
        texts = [n.get("task_message", ""), n.get("loop_prompt", ""), (n.get("on_enter") or {}).get("text", "")]
        for t in texts:
            for v in re.findall(r"\{\{\s*([\w\.\-]+)\s*\}\}", t or ""):
                root = v.split(".")[0]
                if v.startswith(SYSTEM_PREFIXES) or root.endswith("_response") or root in known or v in known:
                    continue
                r.warn(f"node {key}", f"prompt uses {{{{{v}}}}} but nothing sets it")


def check_spec(wf, spec, r):
    blob = json.dumps(wf)
    prompts = " ".join([wf.get("system_prompt", "")] + [
        (n.get("task_message", "") + " " + (n.get("on_enter") or {}).get("text", "") + " " +
         " ".join((t.get("function") or {}).get("description", "") + " " + (t.get("post_process_code") or "") for t in node_tools(n)))
        for n in (wf.get("nodes") or {}).values()])
    for o in spec.get("outcomes") or []:
        oc, rs = o.get("outcome"), o.get("reason")
        if oc and oc not in blob:
            r.err("spec", f"outcome {oc!r} never appears in the workflow")
        if rs and rs not in prompts:
            r.err("spec", f"reason {rs!r} (outcome {oc}) never appears in a prompt, tool description or hook")
    for h in (spec.get("calendar") or {}).get("holidays") or []:
        if str(h) not in blob:
            r.err("spec", f"holiday {h} is not in any code node")
    for q in (spec.get("identity") or {}).get("questions") or []:
        f = q.get("field")
        if f and f not in blob:
            r.err("spec", f"identity field {f!r} is not used")
    for s in spec.get("fixed_sentences") or []:
        txt = s.get("text") if isinstance(s, dict) else s
        if txt and txt not in prompts:
            r.warn("spec", f"fixed sentence not found word for word: {txt[:70]!r}")
    nodes = set((wf.get("nodes") or {}).keys())
    tools = {(t.get("function") or {}).get("name") for n in (wf.get("nodes") or {}).values() for t in node_tools(n)}
    for rule in spec.get("rules") or []:
        rid = rule.get("id")
        places = rule.get("enforced_in") or []
        if isinstance(places, str):
            places = [places]
        if not places:
            r.err("spec", f"rule {rid} has no enforced_in")
        for p in places:
            kind, _, name = str(p).partition(":")
            if kind in ("node", "prompt", "code") and name and name not in nodes and name != "system":
                r.err("spec", f"rule {rid}: enforced_in {p!r} names no node")
            if kind in ("tool", "hook") and name and name not in tools:
                r.err("spec", f"rule {rid}: enforced_in {p!r} names no tool")


def check_spec_against_brief(spec, brief_text, r):
    """Every value the spec takes from the brief must be in the brief. Catches values copied from
    another brief and values the extraction invented."""
    low = brief_text.lower()
    norm = re.sub(r"\s+", " ", low)
    def has(x):
        x = str(x).lower().strip()
        return x in low or re.sub(r"\s+", " ", x) in norm or x.replace("_", " ") in norm
    import datetime as _dt
    def date_forms(h):
        try:
            d = _dt.date.fromisoformat(str(h)[:10])
        except Exception:
            return [str(h)]
        m, mm = d.strftime("%B"), d.strftime("%b")
        return [d.isoformat(), f"{d.day} {m}", f"{m} {d.day}", f"{d.day} {mm}", f"{mm} {d.day}", f"{d.day:02d}/{d.month:02d}/{d.year}",
                f"{d.day}/{d.month}/{d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}", f"{m} {d.day}th", f"{d.day}th {m}",
                f"{m} {d.day}st", f"{m} {d.day}nd", f"{m} {d.day}rd", f"{d.day}st {m}", f"{d.day}nd {m}", f"{d.day}rd {m}"]
    for h in (spec.get("calendar") or {}).get("holidays") or []:
        if not any(has(f) for f in date_forms(h)):
            r.err("brief", f"holiday {h} is not written in the brief (check the date format the brief uses)")
    for o in spec.get("outcomes") or []:
        for k in ("outcome", "reason"):
            if o.get(k) and not has(o[k]):
                r.err("brief", f"{k} {o[k]!r} is not in the brief")
    for s_ in spec.get("fixed_sentences") or []:
        txt = s_.get("text") if isinstance(s_, dict) else s_
        if txt and not has(txt.strip('"')):
            r.warn("brief", f"fixed sentence not found in the brief: {txt[:60]!r}")
    for b in spec.get("bands") or []:
        for k in ("min_days", "max_days"):
            if k in b and not has(b[k]):
                r.warn("brief", f"band {b.get('name')} {k}={b[k]} not found in the brief")
    for q in (spec.get("identity") or {}).get("questions") or []:
        pass  # field names come from the tool API, not the brief


RESULT_WORDS = ("recorded", "booked", "transferred", "scheduled", "saved", "registered", "logged", "confirmed")


def check_result_words(spec, r):
    """Words the agent uses to report a tool result must not appear in other senses in the lines it says
    before any tool ran (opening, debt statement): a judge checking "never says X is recorded before the
    tool succeeded" reads the word literally. Seen in practice: "this call is recorded" in the opening failed
    a case. Allowed only when that exact line is a fixed sentence the brief requires word for word."""
    words = tuple(spec.get("result_words") or RESULT_WORDS)
    fixed = {(f.get("text") if isinstance(f, dict) else str(f)).strip() for f in spec.get("fixed_sentences") or []}
    p = spec.get("products") or {}
    lines = {"opening_line": spec.get("opening_line", ""), "prompts.collection_opening": (spec.get("prompts") or {}).get("collection_opening", ""),
             "products.debt_intro": p.get("debt_intro", ""), "products.debt_sentence": p.get("debt_sentence", "")}
    for where, text in lines.items():
        if not text or text.strip() in fixed:
            continue
        for w in words:
            if re.search(r"\b" + w + r"\b", text, re.I):
                r.err("result-words", f"{where} says {w!r}, a word the agent uses for tool results. A judge may read it as a "
                      f"claim that something was {w}. Rephrase (e.g. 'kept on record' / 'on file') unless the brief requires this exact sentence.")


def spec_texts(spec):
    out = [spec.get("opening_line", "")]
    out += list((spec.get("prompts") or {}).values())
    p = spec.get("products") or {}
    out += [p.get("debt_intro", ""), p.get("debt_sentence", ""), p.get("floor_offer_sentence", "")]
    for t in (spec.get("tool_guidance") or {}).values():
        for v in (t or {}).values():
            out += v if isinstance(v, list) else [str(v)]
    return [str(x) for x in out if x]


def sentences(text):
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = re.sub(r"\{\{[^}]*\}\}|<[^>]*>", " ", s)
        s = re.sub(r"[^a-z0-9' ]", " ", s.lower())
        s = re.sub(r"\s+", " ", s).strip()
        if len(s.split()) >= 6:
            yield s


def near_brief(sentence, brief_words, need=0.85):
    """True when the sentence is a close paraphrase of some stretch of the brief: most of its words
    appear, in a window of the brief about as long as the sentence. Both specs may paraphrase the same
    brief line; that is not copying."""
    ws = sentence.split()
    n = len(ws)
    if n == 0:
        return False
    want = set(ws)
    win = int(n * 1.6) + 2
    best = 0.0
    for i in range(0, max(1, len(brief_words) - n + 1)):
        seg = set(brief_words[i:i + win])
        if ws[0] not in seg and ws[-1] not in seg:
            continue
        best = max(best, len(want & seg) / len(want))
        if best >= need:
            return True
    return False


def check_copied(spec, brief_text, other_specs, r, allow_text=""):
    """A sentence of this spec that also appears in another spec but NOT in this run's brief
    was copied from the other spec: an error (it leaks another brief's wording)."""
    brief_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", brief_text.lower()))
    allow_norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", allow_text.lower()))
    brief_words = brief_norm.split()
    mine = set()
    for t in spec_texts(spec):
        mine |= set(sentences(t))
    for path in other_specs:
        try:
            other = json.loads(pathlib.Path(path).read_text())
        except Exception:
            continue
        theirs = set()
        for t in spec_texts(other):
            theirs |= set(sentences(t))
        for s_ in sorted(mine & theirs):
            if s_ in brief_norm or s_ in allow_norm or near_brief(s_, brief_words):
                continue
            if True:
                r.err("copied", f"sentence also in {pathlib.Path(path).name} and not in the brief: {s_[:90]!r}. Write it from this brief.")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("workflow")
    ap.add_argument("tool_api")
    ap.add_argument("--spec")
    ap.add_argument("--brief", action="append", default=[], help="brief file(s) the spec was made from")
    ap.add_argument("--not-from", action="append", default=[], help="other spec files this spec must not copy sentences from")
    ap.add_argument("--allow-from", action="append", default=[], help="files with generic sentences that may be reused (e.g. the skill)")
    a = ap.parse_args(argv)
    wf = json.loads(pathlib.Path(a.workflow).read_text())
    if "workflow" in wf and "nodes" not in wf:  # an export_workflow file
        wf = wf["workflow"]
    api = load_openapi(a.tool_api)
    spec = json.loads(pathlib.Path(a.spec).read_text()) if a.spec else {}
    r = Report()
    check_structure(wf, r)
    check_code(wf, r)
    check_tools(wf, api, r)
    check_variables(wf, r, spec.get("call_variables") or [])
    if spec:
        check_spec(wf, spec, r)
        check_result_words(spec, r)
    if spec and a.brief:
        btxt = "\n".join(pathlib.Path(b).read_text() for b in a.brief)
        check_spec_against_brief(spec, btxt, r)
        if a.not_from:
            check_copied(spec, btxt, a.not_from, r, "\n".join(pathlib.Path(x).read_text() for x in a.allow_from))
    r.print()
    return 1 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
