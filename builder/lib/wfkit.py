"""wfkit: building blocks for Freya phone-agent workflows.

Everything in this file is generic platform engineering. It holds NO rule, value or name
from any bank brief. Brief-specific values come from the spec the builder extracts from the
brief, and are passed in as arguments.

Typical use inside a generated script (out/<slot>_gen.py):

    import sys, json; sys.path.insert(0, "builder/lib")
    from wfkit import *
    api = load_openapi(TOOL_API_PATH)
    verify = api_tool(api, "verify_identity", "Check ONE security answer ...",
                      fields={"field": {"description": "..."}, "answer": {"description": "..."}},
                      post=hook("..."))
    ...
    wf = workflow(initial="setup", system_prompt=SYSTEM, nodes={...}, test_variables={...})
    write_workflow(wf, "out/<slot>.workflow.json")
"""
import json, re, pathlib

# --------------------------------------------------------------------------- openapi

def load_openapi(path):
    """Read a tool API file. Returns {tool_name: {"url", "properties", "required", "summary", "response"}}.

    The tool name is the path without its leading slash. Field names, types, enums and
    descriptions come from the file, so a changed tool API changes the generated tools."""
    doc = json.loads(pathlib.Path(path).read_text())
    base = (doc.get("servers") or [{"url": ""}])[0]["url"].rstrip("/")
    out = {}
    for p, ops in doc.get("paths", {}).items():
        op = ops.get("post") or next(iter(ops.values()))
        schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
        resp = ((((op.get("responses") or {}).get("200") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
        name = op.get("operationId") or re.sub(r"\W+", "_", p.strip("/"))
        out[name] = {
            "url": base + p,
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
            "summary": op.get("summary", ""),
            "response": resp.get("properties", {}),
        }
    return out


def enum_of(api, tool, field):
    """The allowed values of one request field, or None when the tool API gives none."""
    return (api[tool]["properties"].get(field) or {}).get("enum")


# --------------------------------------------------------------------------- tools

def api_tool(api, name, description, fields=None, post=None, pre=None, retries=0,
             mapping=None, omit=(), timeout_ms=12000, call_vars=("customer_id",)):
    """An api_call tool built from the tool API file.

    fields: per-field overrides {field: {"default": ..., "description": ..., "enum": [...]}}.
      - Every field of the tool API is included unless listed in `omit` (required fields cannot be omitted).
      - A field named in `call_vars` gets the default "{{field}}" and is filled from the call variable.
      - Any other field gets "{{field}}" unless overridden; the model fills it.
      - An enum from the tool API cannot be replaced, only narrowed (a subset).
      - A field the tool API does not have raises an error: the bank would reject it.
    post / pre: python hook source (see hook()).
    retries: config.maxRetries. Keep 0 on tools that write data.
    """
    if name not in api:
        raise KeyError(f"tool {name!r} is not in the tool API file: {sorted(api)}")
    spec = api[name]
    fields = dict(fields or {})
    unknown = set(fields) - set(spec["properties"])
    if unknown:
        raise KeyError(f"{name}: fields {sorted(unknown)} are not in the tool API file")
    for f in omit:
        if f in spec["required"]:
            raise ValueError(f"{name}: required field {f!r} cannot be omitted")
    props = {}
    for f, s in spec["properties"].items():
        if f in omit:
            continue
        ov = fields.get(f, {})
        leaf = {"type": s.get("type", "string")}
        api_enum = s.get("enum")
        enum = ov.get("enum", api_enum)
        if api_enum and enum and not set(enum) <= set(api_enum):
            raise ValueError(f"{name}.{f}: enum {enum} is not a subset of the tool API enum {api_enum}")
        if enum:
            leaf["enum"] = list(enum)
        if f in call_vars:
            leaf["default"] = "{{" + f + "}}"
        else:
            leaf["default"] = ov.get("default", "{{" + f + "}}")
        desc = ov.get("description") or s.get("description")
        if desc:
            leaf["description"] = desc
        props[f] = leaf
    req = [f for f in spec["required"] if f in props]
    t = {
        "type": "function",
        "config": {"timeoutMs": timeout_ms, "maxRetries": retries},
        "function": {
            "name": name,
            "handler": "__function__:api_call",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "default": spec["url"]},
                    "method": {"type": "string", "default": "POST"},
                    "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                    "body": {"type": "object", "properties": props, "required": req},
                },
            },
        },
    }
    if post:
        t["post_process_code"] = post
    if pre:
        t["pre_process_code"] = pre
    if mapping:
        t["response_mapping"] = mapping
    return t


def hook(body):
    """Wrap a post-hook body in the safe envelope used by every tool.

    Inside `body` these names exist:
      result  the tool result envelope (dict); the bank's JSON is result['data']
      d       result['data'] as a dict (created if missing)
      ok      True when the bank answered ok=true with HTTP 200
      vars    the workflow variable table (read and write)
    Set d['what_to_do_next'] to one plain instruction: the model reads it with the result.
    The hook never raises and never contains double braces."""
    lines = [
        "try:",
        "    if isinstance(result, dict):",
        "        result.pop('header', None)",
        "        d = result.get('data')",
        "        if not isinstance(d, dict):",
        "            d = dict()",
        "            result['data'] = d",
        "        sc = result.get('status_code')",
        "        ok = (d.get('ok') is True or str(d.get('ok')).lower() == 'true') and str(sc) in ('200', 'None')",
    ]
    lines += ["        " + l for l in body.strip("\n").split("\n")]
    lines += ["except Exception:", "    pass", ""]
    return _no_braces("\n".join(lines))


def ok_fail_hook(ok_text, fail_texts, counter_var=None, ok_vars=None, fail_vars=None):
    """A post-hook that tells the model what to do after success or failure.

    fail_texts: one text, or a list indexed by how many times this tool already failed
      (fail_texts[0] for the first failure, fail_texts[1] for the second, the last one after that).
    counter_var: workflow variable that counts failures (needed when fail_texts is a list).
    ok_vars / fail_vars: {variable: string value} written on success / failure, for routing.
      Use string values such as 'yes' or 'recorded', never booleans."""
    if isinstance(fail_texts, str):
        fail_texts = [fail_texts]
    body = []
    body.append("if ok:")
    body.append("    d['what_to_do_next'] = " + repr(ok_text))
    for k, v in (ok_vars or {}).items():
        body.append(f"    vars[{k!r}] = {str(v)!r}")
    body.append("else:")
    if counter_var:
        body.append("    try:")
        body.append(f"        n = int(vars.get({counter_var!r}) or 0)")
        body.append("    except Exception:")
        body.append("        n = 0")
        body.append(f"    vars[{counter_var!r}] = str(n + 1)")
        body.append("    texts = " + repr(list(fail_texts)))
        body.append("    d['what_to_do_next'] = texts[min(n, len(texts) - 1)]")
    else:
        body.append("    d['what_to_do_next'] = " + repr(fail_texts[0]))
    for k, v in (fail_vars or {}).items():
        body.append(f"    vars[{k!r}] = {str(v)!r}")
    return hook("\n".join(body))


DROP_EMPTY_FIELDS_PRE = """try:
    b = config.get('body')
    if isinstance(b, dict):
        for k in list(b.keys()):
            v = b.get(k)
            if v is None or (isinstance(v, str) and (v.strip() == '' or v.strip().lower() in ('none', 'null') or v.startswith('{' + '{'))):
                b.pop(k, None)
except Exception:
    pass
"""
"""Pre-hook: remove body fields the model left empty, 'none', or as an unresolved placeholder.
Use it on tools with optional fields, for example a reason that some outcomes must not send."""


def drop_fields_when_pre(field, when_field, values):
    """Pre-hook: remove `field` from the body when body[when_field] is one of `values`.
    Example: drop 'reason' when 'outcome' is one that takes no reason."""
    src = (
        "try:\n"
        "    b = config.get('body')\n"
        "    if isinstance(b, dict):\n"
        "        if b.get(" + repr(when_field) + ") in " + repr(tuple(values)) + ":\n"
        "            b.pop(" + repr(field) + ", None)\n"
        "        v = b.get(" + repr(field) + ")\n"
        "        if v is None or (isinstance(v, str) and (v.strip() == '' or v.strip().lower() in ('none', 'null') or v.startswith('{' + '{'))):\n"
        "            b.pop(" + repr(field) + ", None)\n"
        "except Exception:\n"
        "    pass\n"
    )
    return _no_braces(src)


# --------------------------------------------------------------------------- code nodes

def code_node(name, code, target, extra_transitions=None, timeout_ms=10000):
    """A passthrough code node: runs `code` on entry, then routes. The last transition is `always`."""
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": target,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
                         "parameters": {"type": "object", "properties": {
                             "code": {"type": "string", "default": _no_braces(code)},
                             "timeout_ms": {"type": "number", "default": timeout_ms}}}}}],
        "transitions": (extra_transitions or []) + [always(target)],
    }


def api_node(name, tool, target, mapping=None, extra_transitions=None):
    """A passthrough api_request node that runs one api_call tool on entry (for example get_account)."""
    return {
        "name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": target,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [tool],
        "response_mapping": mapping or [],
        "transitions": (extra_transitions or []) + [always(target)],
    }


def conversation_node(name, task_message, tools, transitions, on_enter=None, first_entry_only=True,
                      max_turns=60, max_turn_target=None, routing_prompt=None):
    """A dialogue step. on_enter: a fixed sentence (str, spoken as is), "llm" (the model speaks first) or None (silent)."""
    n = {"name": name, "node_type": "conversation", "task_message": task_message, "tools": tools,
         "transitions": transitions, "max_node_turns": max_turns, "on_enter_first_entry_only": first_entry_only}
    if on_enter is None:
        n["on_enter"] = {"type": "silent"}
    elif on_enter == "llm":
        n["on_enter"] = {"type": "llm"}
    else:
        n["on_enter"] = {"type": "tts", "text": on_enter}
    if max_turn_target:
        n["max_turn_transition"] = max_turn_target
    if routing_prompt:
        n["routing_prompt"] = routing_prompt
    return n


def end_node(name="end"):
    """Ends the call silently. The goodbye is said in the node before it."""
    return {
        "name": name, "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True, "transitions": [],
        "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                     "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}},
    }


# --------------------------------------------------------------------------- transitions

def always(target, description="always"):
    return {"target_node": target, "description": description, "condition": {"operator": "always"}}


def var_equals(target, variable, value, description=None):
    """Route when a variable equals a string value. Booleans are tested in both spellings."""
    vals = [str(value)]
    if str(value).lower() in ("true", "false"):
        vals = [str(value).lower(), str(value).capitalize()]
    return {"target_node": target, "description": description or f"{variable} == {value}",
            "condition": {"logic": "or", "conditions": [
                {"variable": variable, "operator": "equals", "operand": v} for v in vals]}}


def any_of(target, conditions, description):
    """Route when any of the condition dicts matches. Build them with cond_*()."""
    return {"target_node": target, "description": description, "condition": {"logic": "or", "conditions": conditions}}


def cond_equals(variable, value):
    return {"variable": variable, "operator": "equals", "operand": str(value)}


def cond_true(variable):
    """Both spellings of a boolean true, as a list to splice into any_of()."""
    return [cond_equals(variable, "true"), cond_equals(variable, "True")]


def cond_gt(variable, n):
    return {"variable": variable, "operator": "greater_than", "operand": n}


def intent(target, description, tool_call=False):
    """Route on what the user said (routing model). tool_call=True lets the dialogue model trigger it
    by calling transition_to_<target>."""
    t = {"target_node": target, "description": description,
         "condition": {"operator": "matches_intent", "description": description}}
    if tool_call:
        t["is_tool_call"] = True
    return t


def tool_called(target, tool, flag_var=None, description=None):
    """Route once `tool` has returned (success or failure). Optionally also on a flag variable set by its hook."""
    conds = [cond_gt(f"{tool}_success_count", 0), cond_gt(f"{tool}_failure_count", 0)]
    if flag_var:
        conds.insert(0, cond_equals(flag_var, "yes"))
    return any_of(target, conds, description or f"{tool} has been called")


# --------------------------------------------------------------------------- code snippets

def date_lib(holidays, weekend_days=(5, 6)):
    """Python source with calendar helpers, for the start of a code node.

    Defines: today, now (datetime), isbd(d), nextbd(d), prevbd(d), nth_bd(n, start=today),
    add_bd(d, n), last_bd_of_week(monday), monday_of(d), say(d), full(d), iso(d), parse_iso(s).
    holidays: list of 'YYYY-MM-DD' strings from the brief. weekend_days: Python weekday numbers.
    Reads the call variable `now`; never the real clock."""
    hol = sorted(set(str(h) for h in holidays))
    return _no_braces(f"""import datetime
HOL = set({hol!r})
WEEKEND = set({list(weekend_days)!r})
raw_now = str(vars.get('now') or '')
try:
    now = datetime.datetime.fromisoformat(raw_now.strip()[:16].replace(' ', 'T'))
except Exception:
    now = datetime.datetime.fromisoformat(raw_now.strip()[:10] + 'T00:00')
today = now.date()
def iso(d):
    return d.isoformat()
def parse_iso(s):
    return datetime.date.fromisoformat(str(s)[:10])
def isbd(d):
    return d.weekday() not in WEEKEND and d.isoformat() not in HOL
def nextbd(d):
    d = d + datetime.timedelta(days=1)
    while not isbd(d):
        d = d + datetime.timedelta(days=1)
    return d
def prevbd(d):
    d = d - datetime.timedelta(days=1)
    while not isbd(d):
        d = d - datetime.timedelta(days=1)
    return d
def add_bd(d, n):
    k = 0
    while k < n:
        d = d + datetime.timedelta(days=1)
        if isbd(d):
            k += 1
    return d
def nth_bd(n, start=None):
    return add_bd(start or today, n)
def monday_of(d):
    return d - datetime.timedelta(days=d.weekday())
def last_bd_of_week(monday):
    best = None
    for i in range(7):
        x = monday + datetime.timedelta(days=i)
        if isbd(x):
            best = x
    return best
def say(d):
    return d.strftime('%A') + ' ' + str(d.day) + ' ' + d.strftime('%B %Y')
def full(d):
    return say(d) + ' (' + d.isoformat() + ')'
""")


def calendar_facts_code(days_ahead=45):
    """Python source (needs date_lib first) that writes vars['date_facts']: today, a this/next weekday
    table and a day-by-day calendar marking weekends and holidays with the next business day."""
    return f"""
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
mon = monday_of(today)
wk = []
for i in range(7):
    t = mon + datetime.timedelta(days=i)
    n = t + datetime.timedelta(days=7)
    wk.append('- this ' + DAYS[i] + ' = ' + full(t) + ('  (already past)' if t < today else '') + '; next ' + DAYS[i] + ' = ' + full(n))
cal = []
for i in range(0, {int(days_ahead)}):
    x = today + datetime.timedelta(days=i)
    if isbd(x):
        tag = 'business day'
    elif x.weekday() in WEEKEND:
        tag = 'WEEKEND, not a business day; next business day is ' + full(nextbd(x))
    else:
        tag = 'BANK HOLIDAY, not a business day; next business day is ' + full(nextbd(x))
    cal.append('- ' + full(x) + ': ' + tag + ('  <- TODAY' if i == 0 else ''))
vars['today_iso'] = today.isoformat()
vars['date_facts'] = '\\n'.join(['Today is ' + full(today) + ', time now ' + now.strftime('%H:%M') + '. Use this, never the real clock.',
    '"this <weekday>" is in the current week (Monday ' + iso(mon) + ' to Sunday ' + iso(mon + datetime.timedelta(days=6)) + '); "next <weekday>" is in the following calendar week:']
    + wk + ['Calendar:'] + cal)
"""


PRODUCTS_LOADER = """import json, re
prods = vars.get('products')
if isinstance(prods, str):
    try:
        prods = json.loads(prods)
    except Exception:
        prods = None
if isinstance(prods, dict):
    prods = [prods]
if not isinstance(prods, list) or not prods:
    found = dict()
    for k in list(vars.keys()):
        m = re.search(r'products\\.(\\d+)\\.(\\w+)$', str(k))
        if m:
            found.setdefault(int(m.group(1)), dict())[m.group(2)] = vars.get(k)
    prods = [found[i] for i in sorted(found)]
def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0
def money(x):
    x = float(x)
    return ('%d' % x) if x == int(x) else ('%.2f' % x)
"""
"""Python source that loads the product list mapped from get_account into `prods` (list of dicts).
Map the result with api_node(..., mapping=[{"path": "data.products", "variable": "products"}]).
Also defines num() and money()."""


def guarded(code, fallback_vars):
    """Wrap code in try/except. On error, write every fallback variable so prompts never show a raw placeholder."""
    body = "\n".join("    " + l for l in code.strip("\n").split("\n"))
    fb = "\n".join(f"    vars[{k!r}] = {v!r}" for k, v in fallback_vars.items())
    return _no_braces("try:\n" + body + "\n    vars['code_state'] = 'ok'\nexcept Exception as e:\n" + fb + "\n    vars['code_error'] = str(e)[:200]\n")


# --------------------------------------------------------------------------- assembly

def workflow(initial, nodes, system_prompt="", test_variables=None, tools=None, global_nodes=None):
    wf = {"initial_node": initial, "system_prompt": system_prompt, "nodes": nodes,
          "global_nodes": global_nodes or {}, "test_variables": test_variables or {}}
    if tools:
        wf["tools"] = tools
    for k, n in nodes.items():
        n.setdefault("name", k)
    return wf


def write_workflow(wf, path):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(wf, indent=1))
    return str(p)


def _no_braces(code):
    if "{{" in code or "}}" in code:
        raise ValueError("code contains '{{' or '}}', which the platform may treat as a placeholder. "
                         "Use dict() instead of nested dict literals and build strings without double braces.")
    return code


# --------------------------------------------------------------------------- confirmation gate, refusals

def say_end(name, text, fn=None):
    """An end node that speaks `text` (fixed, in the call language) and then ends the call."""
    n = end_node(name)
    n["terminal"]["function"]["name"] = fn or (name + "_closed")
    n["terminal"]["function"]["parameters"]["properties"] = {
        "message_mode": {"type": "string", "default": "custom"},
        "spoken_message": {"type": "string", "default": text}}
    return n


def confirm_gate(name, question, reask, yes_target, no_target, *, yes_examples, hedge_examples, no_examples,
                 facts="", giveup_target=None, max_turns=6):
    """Two conversation nodes that ask for an explicit yes BEFORE a writing action. They have NO tools, so a
    hedged answer can never trigger the action: only the routing model's 'clear yes' transition leads on.

    question: the read-back + confirmation question, spoken verbatim on entry ({{var}} allowed).
    reask:    the short question spoken again after a hedged/unclear answer (e.g. "Onaylıyor musunuz?").
    yes_target: a node that performs the action (see action_step). no_target: where a no goes.
    *_examples: short lists of real phrasings in the call language, taken from the documents.
    Returns a dict {name: node, name + "_again": node} to merge into the nodes dict.
    Route into `name` with a variable transition set by the hook of the preview tool (same turn)."""
    again = name + "_again"
    hedge = ("The caller's last message is NOT a plain yes: it hesitates, guesses, adds a condition or a 'but', "
             "asks a question, or is unclear (for example: " + "; ".join(hedge_examples) + "). "
             "This applies even when the message also contains a yes word.")
    yes = ("The caller's last message is a plain, unconditional yes with no hesitation, guess, condition or question "
           "(for example: " + "; ".join(yes_examples) + ").")
    no = "The caller declines or cancels (for example: " + "; ".join(no_examples) + ")."
    routing = ("Judge ONLY the caller's last message. 1) Any hesitation, guess, condition, 'but' or question, even next to "
               "a yes word, is the hesitant/unclear transition. 2) Only a plain unconditional yes is the yes transition. "
               "3) A refusal or cancel is the no transition. 4) If the caller talks about something else, choose none.")
    task = ("You are only waiting for the caller's confirmation. The action has NOT been done yet: never say it is done. "
            "You have no tool here. If the caller asks a short question, answer it in one sentence from these facts, then "
            "ask again exactly: \"" + reask + "\". If the caller presses you to just do it, say that you need a clear yes "
            "first and ask again exactly: \"" + reask + "\"." + (("\nFacts: " + facts) if facts else ""))
    def trs(hedge_target):
        return [intent(hedge_target, hedge), intent(yes_target, yes), intent(no_target, no)]
    n1 = conversation_node(name, task, [], trs(again), on_enter=question, first_entry_only=False,
                           max_turns=max_turns, max_turn_target=giveup_target or no_target, routing_prompt=routing)
    n2 = conversation_node(again, task, [], trs(again), on_enter=reask, first_entry_only=False,
                           max_turns=max_turns, max_turn_target=giveup_target or no_target, routing_prompt=routing)
    return {name: n1, again: n2}


def action_step(name, tool, how, transitions, max_turns=20, max_turn_target=None):
    """The node a confirm_gate's yes leads to: the model calls `tool` once, right away, then follows the tool
    hook's what_to_do_next. `how` says which values to send (e.g. "week = {{week_new}}")."""
    tname = tool["function"]["name"] if "function" in tool else tool.get("name", "the tool")
    task = ("The caller has just given a clear yes. Call " + tname + " now, exactly once, with " + how + ". "
            "Say nothing before the call. After the result, follow what_to_do_next word for word. "
            "Never call " + tname + " a second time in this call.")
    return conversation_node(name, task, [tool], transitions, on_enter="llm", first_entry_only=True,
                             max_turns=max_turns, max_turn_target=max_turn_target)


def refusal_global(name, when, text, excluded_nodes):
    """A global interrupt for a request the agent must refuse anywhere in the call (e.g. information or actions on
    someone else's account). Speaks the fixed `text` (refusal + where the caller goes next) and ends the call.
    Returns (global_nodes_entry, extra_nodes): put the first in workflow(global_nodes=...), merge the second
    into nodes, and add name + '_end' to excluded_nodes of other globals."""
    end_name = name + "_end"
    g = {"name": name, "node_type": "conversation", "description": when,
         "condition": {"operator": "matches_intent", "description": when},
         "task_message": "Say nothing.", "tools": [], "on_enter": {"type": "silent"},
         "passthrough": True, "passthrough_default_transition": end_name,
         "excluded_nodes": list(excluded_nodes) + [end_name],
         "transitions": [always(end_name)]}
    return {name: g}, {end_name: say_end(end_name, text)}


def list_reader_code():
    """Python source (no braces) defining read_list(vars, mapped_var, path): returns a list of dicts from a tool
    result. On Freya a mapped list often does NOT arrive as a list: it can be missing, a JSON string, or flattened
    into keys like '<node>_response.data.data.cards.0.cardNumber'. read_list tries all three. Paste this into
    every code node that reads a list: code_node(..., list_reader_code() + your_code, ...)."""
    return '''
import json as _json, re as _re
def read_list(vars, mapped_var, path):
    v = vars.get(mapped_var)
    if isinstance(v, str):
        try:
            v = _json.loads(v)
        except Exception:
            v = None
    if isinstance(v, list) and v:
        return [x for x in v if isinstance(x, dict)]
    found = dict()
    pat = _re.compile(_re.escape(path) + r'\\.(\\d+)\\.(\\w+)$')
    for k in list(vars.keys()):
        m = pat.search(str(k))
        if m:
            found.setdefault(int(m.group(1)), dict())[m.group(2)] = vars.get(k)
    return [found[i] for i in sorted(found)]
'''
