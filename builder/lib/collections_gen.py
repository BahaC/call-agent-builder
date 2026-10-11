"""Spec-driven generator for a collections phone agent.

    python3 builder/lib/collections_gen.py <spec.json> <tool_api.json> <out.workflow.json>

The spec (written by the builder from the brief, see builder/SPEC.md) holds every brief-specific
value: calendar, bands and deadlines, product types and floors, identity questions, outcomes,
callback window, prompts. This file holds none of them. It turns the spec into a workflow:

    setup (code)  ->  identity  ->  fetch_account (api)  ->  account_prep (code)  ->  collection  -> end
                         |                                                \\-> account_error -> end
                         \\-> third_party -> end

Mechanics done in code, not left to the model:
  - all dates: today, business days, holidays, per-band deadlines, attempt dates, callback window
  - per product: product words, floor, band, deadline, number of persuasion attempts, attempt offers
  - every tool result carries a what_to_do_next line (post hooks)
  - retry counters for writing tools, close_call reason clean-up, routing to the end node
"""
import json, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from wfkit import *  # noqa


def _p(spec, *keys, default=None):
    cur = spec
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _expr(e, what):
    """A spec expression is Python over the date_lib helpers. Check it compiles and has no braces."""
    e = str(e).strip()
    if "{{" in e or "}}" in e:
        raise ValueError(f"{what}: expression contains double braces: {e}")
    compile(e, what, "eval")
    return e


# --------------------------------------------------------------------------- code

def setup_code(spec):
    cal = spec.get("calendar") or {}
    lines = [date_lib(cal.get("holidays") or [], cal.get("weekend_days") or (5, 6)),
             calendar_facts_code(int(cal.get("days_ahead") or 45))]
    # named dates the brief needs everywhere (attempt dates, generic deadlines)
    for name, e in (spec.get("named_dates") or {}).items():
        lines.append(f"_d = {_expr(e, 'named_dates.' + name)}")
        lines.append(f"vars['{name}'] = iso(_d)")
        lines.append(f"vars['{name}_say'] = full(_d)")
    # callback window
    cb = _p(spec, "third_party", "callback") or {}
    if cb:
        frm, to = cb.get("from", "00:00"), cb.get("to", "23:59")
        cbl = ["cb_lines = []", "cb_days = []", "nowhm = now.strftime('%H:%M')", "cb_list = []"]
        for k, v in (cb.get("days") or {}).items():
            cbl.append("try:")
            cbl.append(f"    cb_list.append(({k!r}, {_expr(v, 'callback.days.' + k)}))")
            cbl.append("except Exception:")
            cbl.append("    pass")
        cbl.append(f"""for label, dd in cb_list:
    start = {frm!r}
    if dd == today:
        if nowhm >= {to!r}:
            cb_lines.append('- ' + label + ' ' + full(dd) + ': no longer possible (it is ' + nowhm + ').')
            continue
        if nowhm > start:
            start = nowhm
        cb_lines.append('- ' + label + ' ' + full(dd) + ': any time LATER than ' + start + ' and no later than ' + {to!r} + '.')
    else:
        cb_lines.append('- ' + label + ' ' + full(dd) + ': any time from ' + {frm!r} + ' to ' + {to!r} + ', both included.')
    cb_days.append(iso(dd))
cb_lines.append('Any other day, or any time outside these windows, is NOT allowed.')
tm = today + datetime.timedelta(days=1)
cb_lines.append('"Tomorrow" is ' + full(tm) + (', which is allowed.' if iso(tm) in cb_days else ', which is NOT allowed.'))
vars['callback_facts'] = '\\n'.join(cb_lines)
""")
        lines.append("\n".join(cbl))
    code = "\n".join(lines)
    return guarded(code, {"date_facts": "Date facts unavailable.", "callback_facts": "Callback window unavailable.", "setup_state": "error"}) + \
        "if vars.get('code_state') == 'ok':\n    vars['setup_state'] = 'ok'\n"


def account_code(spec):
    prod = spec.get("products") or {}
    types = prod.get("types") or {}
    bands = spec.get("bands") or []
    attempts = spec.get("attempts") or []
    if not bands:
        raise ValueError("spec.bands is empty")
    # band table and attempt table as tuples (no nested dict literals: they would produce double braces)
    band_rows = [(b["name"], int(b["min_days"]), int(b["max_days"]), int(b.get("attempts", 1))) for b in bands]
    att_rows = [(int(a["n"]), a.get("amount", "full"), list(a.get("bands") or [])) for a in attempts]
    type_rows = [(k, v.get("words", k.replace("_", " ")), v.get("floor_field", ""), v.get("floor_name", "minimum amount"))
                 for k, v in types.items()]
    # deadline and attempt-date expressions become plain Python functions (no eval at call time)
    dl_fn = ["def band_deadline(name):"]
    for b in bands:
        dl_fn.append(f"    if name == {b['name']!r}:")
        dl_fn.append(f"        return {_expr(b['deadline'], 'bands.' + b['name'] + '.deadline')}")
    dl_fn.append("    return today")
    at_fn = ["def attempt_date(n, deadline):"]
    for a in attempts:
        at_fn.append(f"    if n == {int(a['n'])}:")
        at_fn.append(f"        return {_expr(a['date'], 'attempts.' + str(a['n']) + '.date')}")
    at_fn.append("    return deadline")
    fl_fn = ["def floor_expr_for(ptype, p, due):", "    import math"]
    for k, v in types.items():
        if v.get("floor_expr"):
            fl_fn.append(f"    if ptype == {k!r}:")
            fl_fn.append(f"        return float({_expr(v['floor_expr'], 'products.types.' + k + '.floor_expr')})")
    fl_fn.append("    return None")
    debt_t = prod.get("debt_sentence") or "Your <words> has <amount> past due, <days> days late."
    debt_intro = prod.get("debt_intro") or ""
    floor_sentence = prod.get("floor_offer_sentence", "")
    code = PRODUCTS_LOADER + date_lib(_p(spec, "calendar", "holidays") or [], _p(spec, "calendar", "weekend_days") or (5, 6)) + \
        "\n".join(dl_fn) + "\n" + "\n".join(at_fn) + "\n" + "\n".join(fl_fn) + "\n" + f"""
TYPES = {type_rows!r}
BANDS = {band_rows!r}
ATTEMPTS = {att_rows!r}
FLOOR_SENTENCE = {floor_sentence!r}
DEBT_TEMPLATE = {debt_t!r}
DEBT_INTRO = {debt_intro!r}
def type_info(p):
    for k, w, ff, fn in TYPES:
        if k == str(p.get('type')):
            return w, ff, fn
    return str(p.get('type')).replace('_', ' '), '', 'minimum amount'
def fill(w, p):
    for k, v in p.items():
        w = w.replace('<' + str(k) + '>', str(v))
    return w
facts = []
debt = []
ids = []
dl_pairs = []
for i, p in enumerate(prods):
    w, ff, fname = type_info(p)
    w = fill(w, p)
    due = num(p.get('past_due'))
    dl = int(num(p.get('days_late')))
    fx = floor_expr_for(str(p.get('type')), p, due)
    floor = fx if fx is not None else (num(p.get(ff)) if ff and p.get(ff) is not None else due)
    band = None
    for name, lo, hi, natt in BANDS:
        if lo <= dl <= hi:
            band = (name, natt)
            break
    if band is None:
        band = (BANDS[-1][0], BANDS[-1][3])
    deadline = band_deadline(band[0])
    pid = str(p.get('product_id'))
    ids.append(pid)
    dl_pairs.append(pid + '=' + iso(deadline) + '=' + w)
    debt.append(DEBT_TEMPLATE.replace('<words>', w).replace('<amount>', money(due)).replace('<days>', str(dl)))
    lines = ['PRODUCT ' + str(i + 1) + ': ' + w,
             '  product_id: ' + pid + '  (copy exactly into tools)',
             '  full past-due amount: ' + money(due) + ', days late: ' + str(dl) + ', band: ' + band[0].upper(),
             '  floor (' + fname + '): ' + money(floor) + '. Never record less than this.',
             '  DEADLINE (latest promise date): ' + full(deadline),
             '  valid promise dates: business days from ' + iso(today) + ' to ' + iso(deadline) + ' included; no weekend, no holiday, nothing after the deadline.',
             '  persuasion attempts allowed for this product: ' + str(band[1]) + '.']
    for n, amount, only in ATTEMPTS:
        if n > band[1] or (only and band[0] not in only):
            continue
        try:
            ad = attempt_date(n, deadline)
        except Exception:
            ad = deadline
        if ad is None or ad > deadline:
            ad = deadline
        if amount == 'floor':
            amt = 'the floor ' + money(floor)
        elif amount == 'full_or_floor':
            amt = 'the full amount ' + money(due) + ' (or the floor ' + money(floor) + ' if the customer already said the full amount is too much)'
        else:
            amt = 'the full amount ' + money(due)
        lines.append('  attempt ' + str(n) + ' offer: ' + amt + ' on ' + full(ad) + '.')
    if band[1] < len(ATTEMPTS):
        lines.append('  there is NO attempt ' + str(band[1] + 1) + ' for this product.')
    if FLOOR_SENTENCE:
        lines.append('  floor offer sentence: "' + FLOOR_SENTENCE.replace('<floor_name>', fname).replace('<floor>', money(floor)) + '"')
    # the model must never work out weekdays itself: list every valid promise date
    valid = [today + datetime.timedelta(days=k) for k in range(0, (deadline - today).days + 1) if isbd(today + datetime.timedelta(days=k))]
    lines.append('  EVERY valid promise date for this product (checked by code; any date not listed is invalid): ' + '; '.join(full(x) for x in valid))
    # a named non-business day is an exact day: map each one before the deadline to the day to offer instead
    swaps = []
    for k in range(0, (deadline - today).days + 1):
        x = today + datetime.timedelta(days=k)
        if not isbd(x):
            nb = nextbd(x)
            if nb <= deadline:
                swaps.append(say(x) + (' (holiday)' if x.weekday() not in WEEKEND else '') + ' -> offer ' + full(nb))
    if swaps:
        lines.append('  If the customer names one of these non-business days, it IS an exact day: offer the day after the arrow, never the deadline instead: ' + '; '.join(swaps))
    facts.append('\\n'.join(lines))
vars['account_facts'] = '\\n\\n'.join(facts)
vars['deadlines'] = ';'.join(x for x in dl_pairs)
vars['date_check'] = 'No date checked yet.'
vars['date_check_ready'] = 'no'
vars['product_count'] = str(len(prods))
vars['product_ids'] = ', '.join(ids)
vars['debt_lines'] = ((DEBT_INTRO + ' ') if DEBT_INTRO else '') + ' '.join(debt)
vars['account_state'] = 'ok' if prods else 'error'
"""
    return guarded(code, {"account_facts": "Account details unavailable.", "debt_lines": "", "account_state": "error"})


# --------------------------------------------------------------------------- tools

def tool_text(spec, tool, key, default):
    return _p(spec, "tool_guidance", tool, key, default=default)


def make_tools(spec, api):
    g = spec.get("tool_guidance") or {}
    raw = spec.get("hooks") or {}
    tools = {}
    fdesc = spec.get("field_descriptions") or {}

    def fields(name):
        return {f: {"description": d} for f, d in (fdesc.get(name) or {}).items() if f in api[name]["properties"]}

    def desc(name, default):
        return _p(spec, "tool_descriptions", name, default=default)

    if "verify_identity" in api:
        v = g.get("verify_identity") or {}
        maxw = int(_p(spec, "identity", "max_wrong_total", default=0) or 0)
        nq = len(_p(spec, "identity", "questions", default=[]) or [])
        body = raw.get("verify_identity") or f"""
left = d.get('wrong_answers_left')
err = str(d.get('error') or '').lower()
# count wrong answers ourselves: the limit holds even when the bank answers the last one with an error
wrong = int(vars.get('id_wrong') or 0)
if ok and str(d.get('verified')).lower() == 'false' and not d.get('hint') and str(d.get('all_verified')).lower() != 'true':
    wrong = wrong + 1
    vars['id_wrong'] = str(wrong)
# count correct answers ourselves too: after a platform error the bank may answer the last one without all_verified
good = int(vars.get('id_correct') or 0)
if ok and str(d.get('verified')).lower() == 'true':
    good = good + 1
    vars['id_correct'] = str(good)
if {nq} > 0 and good >= {nq}:
    d['all_verified'] = True
over = (str(left) == '0') or ({maxw} > 0 and wrong >= {maxw}) or any(w in err for w in ('too many', 'no more', 'locked', 'exhausted', 'check over'))
if str(d.get('all_verified')).lower() == 'true':
    d['what_to_do_next'] = {v.get('all_verified', 'All security answers passed.')!r}
    vars['id_state'] = 'verified'
elif d.get('hint'):
    d['what_to_do_next'] = {v.get('hint', 'The answer needs to be given again in another form. This does not count as a wrong answer. Ask for it again.')!r} + ' (hint: ' + str(d.get('hint')) + ')'
elif over:
    d['what_to_do_next'] = {v.get('exhausted', 'The identity check has failed. Follow the brief for a failed identity check.')!r}
    vars['id_state'] = 'failed'
elif not ok:
    d['what_to_do_next'] = {v.get('unreadable', 'The answer could not be read. This is not a wrong answer. Ask for the same answer again.')!r}
elif str(d.get('verified')).lower() == 'true':
    d['what_to_do_next'] = {v.get('correct', 'That answer is correct. Ask the next security question.')!r}
else:
    d['what_to_do_next'] = {v.get('wrong', 'That answer is WRONG. Ask the same question again. Do not reveal the right answer.')!r} + ' Wrong answers left: ' + str(left) + '.'
"""
        tools["verify_identity"] = api_tool(api, "verify_identity",
            desc("verify_identity", "Check ONE security answer. CALL it every time the customer answers a security question. Never judge an answer yourself."),
            fields=fields("verify_identity"), post=hook(body))

    if "get_account" in api:
        tools["get_account"] = api_tool(api, "get_account", "Get the late products.", retries=2)

    def simple(name, ok_key_default, fail_default, counter=None, ok_vars=None):
        """A writing tool. Two layers for failures, because a failed HTTP call (e.g. 500) reaches the model
        as a raw `API request failed: ...` error and the post hook's what_to_do_next does NOT reach it:
          1. engine_retries (spec): the platform repeats the call itself. Only for calls the tool API says
             are safe to repeat (e.g. "Nothing was recorded. The call may be repeated.").
          2. the failure instructions are also written into the tool description, which the model always sees."""
        if name not in api:
            return
        gg = g.get(name) or {}
        fails = gg.get("fail", fail_default)
        if isinstance(fails, str):
            fails = [fails]
        retries = int(gg.get("engine_retries", 0))
        post = hook(raw[name]) if name in raw else ok_fail_hook(gg.get("ok", ok_key_default), fails, counter_var=counter, ok_vars=ok_vars)
        base = desc(name, api[name]["summary"])
        if retries > 0:
            # the engine already repeated the call: an error the model sees is the final failure
            on_err = " IF THE RESULT IS AN ERROR (for example 'API request failed'): the system has already retried. " + fails[-1]
        elif len(fails) > 1:
            on_err = (" IF THE RESULT IS AN ERROR (for example 'API request failed') the FIRST time: " + fails[0] +
                      " IF IT FAILS A SECOND TIME: " + fails[-1])
        else:
            on_err = " IF THE RESULT IS AN ERROR (for example 'API request failed'): " + fails[0]
        tools[name] = api_tool(api, name, base + on_err, fields=fields(name), post=post, retries=retries)

    # the failure counter is reset on success, so each product gets its own retry
    simple("create_promise", "The promise IS recorded. Tell the customer, then continue as the brief says.",
           ["The promise was NOT recorded. Do NOT say it is recorded. Call create_promise again now with exactly the same values.",
            "The promise was NOT recorded again. Tell the customer you could not record it and follow the brief for a service error."],
           counter="create_promise_fails", ok_vars={"create_promise_fails": "0", "promise_state": "recorded"})
    simple("schedule_callback", "The callback is booked. Follow the brief to close the call.",
           ["The callback was NOT booked. Do not say it is booked. Try once more with the same date and time.",
            "The callback could not be booked. Follow the brief for this case."], counter="schedule_callback_fails")
    simple("transfer_to_agent", "The transfer is done. Follow the brief to close the call.",
           ["The transfer did NOT work. Try once more with the same queue.",
            "The transfer failed again. Apologise and follow the brief."], counter="transfer_fails")
    simple("add_note", "The note is recorded. Continue.",
           ["The note was NOT recorded. Call add_note once more with the same values.",
            "The note could not be recorded. Continue."], counter="add_note_fails")

    if "close_call" in api:
        no_reason = [o["outcome"] for o in spec.get("outcomes") or [] if not o.get("reason")]
        no_reason = sorted(set(no_reason) - {o["outcome"] for o in spec.get("outcomes") or [] if o.get("reason")})
        reasons = sorted({o["reason"] for o in spec.get("outcomes") or [] if o.get("reason")})
        cf = fields("close_call")
        if "reason" in api["close_call"]["properties"] and not enum_of(api, "close_call", "reason") and reasons:
            cf.setdefault("reason", {})["enum"] = ["none"] + reasons
            cf["reason"].setdefault("description", "Use none when the outcome takes no reason.")
        pairs = "; ".join(f"{o['outcome']}" + (f" + {o['reason']}" if o.get("reason") else " (no reason)") for o in spec.get("outcomes") or [])
        tools["close_call"] = api_tool(api, "close_call",
            desc("close_call", "Record how the call ended. CALL it exactly once, as the very last action, in the same message as your goodbye. Allowed: " + pairs + "."),
            fields=cf, pre=drop_fields_when_pre("reason", "outcome", no_reason) if "reason" in api["close_call"]["properties"] else None,
            post=hook("d['what_to_do_next'] = 'The call is closed. Say nothing more.'\nvars['closed'] = 'yes'"))
    return tools


# --------------------------------------------------------------------------- date check tool

DATE_TOOL_DESC = ("Checks a payment date the customer named, in code. CALL it before you accept, offer back, read back or "
                  "record any date the customer names (also 'today', a weekday or a holiday), with the date as YYYY-MM-DD "
                  "and the product_id. In that same reply say nothing else. The answer appears in your instructions "
                  "under 'Last date check'. Never decide yourself whether a date is a business day or inside the deadline.")


def date_check_code(spec):
    """Python for the check_date tool. The engine replaces the two placeholders with the model's arguments
    before the code runs (verified on the platform, 11 Oct): they are the only double braces allowed here."""
    cal = spec.get("calendar") or {}
    body = date_lib(cal.get("holidays") or [], cal.get("weekend_days") or (5, 6)) + """
RAW_DATE = '''{{date}}'''
RAW_PID = '''{{product_id}}'''
def clean(x):
    x = str(x).strip()
    return '' if x.startswith('{' + '{') else x
d_txt = clean(RAW_DATE)
pid = clean(RAW_PID)
rules = []
for part in str(vars.get('deadlines') or '').split(';'):
    bits = part.split('=')
    if len(bits) >= 2:
        rules.append((bits[0].strip(), parse_iso(bits[1]), '='.join(bits[2:]) or bits[0].strip()))
day = None
try:
    day = datetime.date.fromisoformat(d_txt[:10])
except Exception:
    day = None
if day is not None and day.year != today.year:
    # the model often gets the year wrong: a payment date is always the next such day from today
    try:
        day = day.replace(year=today.year)
        if day < today:
            day = day.replace(year=today.year + 1)
    except Exception:
        pass
if day is None:
    res = 'Date check: ' + repr(d_txt) + ' is not a date in YYYY-MM-DD form. Work out the exact date from the date facts (ask the customer only if it is unclear) and call check_date again with YYYY-MM-DD.'
else:
    use = [r for r in rules if r[0] == pid] or rules
    head = 'Date check for ' + full(day) + ': '
    if day.weekday() in WEEKEND:
        kind = 'a WEEKEND day'
    elif not isbd(day):
        kind = 'a BANK HOLIDAY'
    else:
        kind = ''
    out = []
    for rid, dl, words in use:
        tag = ('[' + words + ', ' + rid + '] ') if len(use) > 1 else ''
        if day < today:
            out.append(tag + 'this date is in the PAST (today is ' + full(today) + '). It cannot be used.')
        elif day > dl:
            out.append(tag + 'AFTER the deadline. The latest allowed date is ' + full(dl) + '. It cannot be used as it is.')
        elif kind:
            nb = nextbd(day)
            pb = prevbd(day)
            msg = tag + 'NOT a business day (' + kind + '). It cannot be used as it is. Next business day: ' + full(nb)
            msg += (' (inside the deadline).' if nb <= dl else ' (AFTER the deadline ' + full(dl) + ').')
            if pb >= today:
                msg += ' Previous business day: ' + full(pb) + '.'
            out.append(msg)
        else:
            out.append(tag + 'VALID: a business day, not before today and not after the deadline ' + full(dl) + '.')
    res = head + ' '.join(out) + ' Apply the brief rules to this result now.'
vars['date_check'] = res
vars['date_check_ready'] = 'yes'
"""
    return guarded(body.replace("{{date}}", "@@DATE@@").replace("{{product_id}}", "@@PID@@"),
                   {"date_check": "Date check failed. Use the date facts instead.", "date_check_ready": "yes"}
                   ).replace("@@DATE@@", "{{date}}").replace("@@PID@@", "{{product_id}}")


def date_check_tool(spec):
    return {"type": "function", "function": {
        "name": "check_date", "handler": "__function__:code_exec", "description": DATE_TOOL_DESC,
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "default": date_check_code(spec)},
            "timeout_ms": {"type": "number", "default": 10000},
            "date": {"type": "string", "description": "The date the customer named, as YYYY-MM-DD. Use the date facts for the year."},
            "product_id": {"type": "string", "description": "The product_id the date is for, copied from the account facts."},
        }, "required": ["date", "product_id"]}}}


DATE_CHECK_PROMPT = """

DATE CHECK (code does the calendar work, not you):
- Whenever the customer names a payment date, call check_date with it (YYYY-MM-DD) and the product_id before you accept, read back, offer or record it. Say nothing else in that reply.
- Then use the result below with the brief rules (what to offer for a non-business day, for a date after the deadline, and so on). Do not check the same date twice.
Last date check: {{date_check}}
If that result is new (you have not answered it yet), answer it now."""


# --------------------------------------------------------------------------- workflow

SPEECH_RULES = """How callers speak (applies to every step):
- A number or a date may come in pieces over several turns (for example the day and month first, the year in the next turn). When what you have is incomplete, ask only for the missing part and wait. Combine the pieces before you call a tool or decide anything. A partial answer is never a wrong answer, a refusal or a reason to end the call.
- A date is complete when it has a day, a month and a year; a year said in words ("nineteen ninety") is a full four-digit year (1990). A message that trails off after a complete answer is still a complete answer: use it.
- When the caller corrects themselves ("the 21st, sorry, the 20th"), only the corrected value counts.
- Amounts and dates said in words: convert them to digits yourself.
- A weekday alone ("Monday") is not yet a date: work out which date from the date facts and say the full date back before you use it. If they named a day number in another turn, put the two together.
- A message that does not answer your question (small talk, a sentence that stops halfway) is NOT a refusal and NOT a new answer: briefly ask the same question again. Only a clear no counts as a refusal. Never move to the next offer, and never end the call, because the caller digresses.
- When one message holds an answer AND something else (a complaint, a question, small talk), act on the answer first: if it answers what you asked, call the tool for it now, exactly as if it came alone. Only then reply to the rest. Never ask again for something the caller just gave you.
- Small talk or a question back: answer in one short sentence, then return to the step you were on.
- Dates mentioned for other reasons (a wedding, a birthday) are not payment dates.
- Never repeat an offer you already made in this call unless a rule above tells you to."""


def build(spec, api):
    t = make_tools(spec, api)
    pr = spec.get("prompts") or {}
    used = set(spec.get("tools_used") or t.keys())
    use = lambda *names: [t[n] for n in names if n in t and n in used]  # noqa
    closed = tool_called("end", "close_call", "closed", "close_call has been called")

    id_tools = use("verify_identity", "transfer_to_agent", "close_call")
    third_tools = use("schedule_callback", "close_call")
    coll_tools = use("create_promise", "add_note", "transfer_to_agent", "close_call")
    date_tool = spec.get("date_tool", True) and "create_promise" in used
    coll_prompt = pr["collection"] + (DATE_CHECK_PROMPT if date_tool else "")
    coll_trans = [closed]
    if date_tool:
        coll_tools = coll_tools + [date_check_tool(spec)]
        # the tool result is only in vars: route through a code node back into a copy of the step,
        # so the step is entered again and the model answers with the fresh prompt in the same turn
        coll_trans = [closed, var_equals("date_reply", "date_check_ready", "yes", "a date check result is ready")]

    nodes = {
        "setup": code_node("setup", setup_code(spec), "identity"),
        "identity": conversation_node(
            "identity", pr["identity"], id_tools,
            [closed,
             any_of("fetch_account", [cond_equals("id_state", "verified")] + cond_true("verify_identity_response.data.all_verified"),
                    "all security answers verified"),
             intent("third_party", pr.get("third_party_intent",
                    "The person on the line says they are not the customer but someone else, or the customer is not available"),
                    tool_call=True)],
            on_enter=spec["opening_line"], max_turns=60),
        "third_party": conversation_node("third_party", pr["third_party"], third_tools, [closed], on_enter="llm", max_turns=40),
        "fetch_account": api_node("fetch_account", t["get_account"], "account_prep",
                                  mapping=[{"path": "data.products", "variable": "products"}]),
        "account_prep": code_node("account_prep", account_code(spec), "collection",
                                  [var_equals("account_error", "account_state", "error", "account could not be read")]),
        "collection": conversation_node("collection", coll_prompt, coll_tools, coll_trans,
                                        on_enter=pr.get("collection_opening", "llm"), max_turns=80),
        "account_error": conversation_node("account_error", pr.get("account_error",
                                           "You could not load the account details because of a technical problem. Apologise, say goodbye and close the call as the brief says for a service error."),
                                           use("close_call"), [closed], on_enter="llm", max_turns=10),
        "end": end_node("end"),
    }
    if date_tool:
        nodes["date_reply"] = code_node("date_reply", "vars['date_check_ready'] = 'no'\n", "collection_more")
        nodes["collection_more"] = conversation_node("collection_more", coll_prompt, coll_tools, coll_trans,
                                                     on_enter="llm", first_entry_only=False, max_turns=80)
    if not third_tools:
        nodes.pop("third_party")
        nodes["identity"]["transitions"] = [x for x in nodes["identity"]["transitions"] if x["target_node"] != "third_party"]
    tv = dict(spec.get("test_variables") or {})
    system = (pr.get("system", "") + "\n\n" + SPEECH_RULES).strip()
    return workflow("setup", nodes, system_prompt=system, test_variables=tv)


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) != 3:
        sys.exit(__doc__)
    spec = json.loads(pathlib.Path(argv[0]).read_text())
    api = load_openapi(argv[1])
    wf = build(spec, api)
    print(write_workflow(wf, argv[2]))


if __name__ == "__main__":
    main()
