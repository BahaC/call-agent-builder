---
name: build-collections-agent
description: Build a Freya phone agent for a bank collections brief (clean spec or raw bank material) by extracting a spec, generating the workflow with builder/lib, checking it statically and testing its code locally. Use when asked to build a phone agent from a brief and a tool API file.
---

# Build a collections phone agent from a brief

You are the builder. You read a bank's brief and its tool API file and produce a phone agent (a Freya
workflow) that follows the brief exactly. Nobody edits your output by hand. It is scored on customers
you never see, each run three times: a case counts only if all three runs pass. **Reliability beats
cleverness: put rules into code and routing, and give the model finished facts, not calculations.**

Work from the kit root (the folder with `freya.py`). The run prompt gives you: the brief file(s), the
tool API file, the slot name, the time you must finish by, and whether practice cases exist.

## Hard rules

- Every value, rule, name and sentence comes from the brief files and the tool API file of THIS run.
  Never copy a value, rule, outcome name, holiday or sentence from another brief, from
  `builder/examples/`, from `examples/` or from memory. Examples show shapes only.
- Never open `builder/devtests/`, `out/` files of other slots, or `submission/`: they belong to other
  briefs. Read only the brief files and the tool API file named in the run prompt.
- Tool names, field names and enums come from the given tool API file (`load_openapi`). Never type a
  URL, a field or an enum value by hand.
- Never edit a workflow JSON by hand. Change the spec or the generator script and regenerate.
- Never write `{{` or `}}` inside Python code or spec expressions.
- Stay inside the time you were given. If you are close to it, stop improving and finish (step 9).

## Steps

### 1. Read (do not skip anything)
Read every brief file completely, then the tool API file, then `builder/SPEC.md`.
Read `builder/examples/example.spec.json` to see the JSON shape. It is a made-up bank: copy its
structure, never its words.

If the brief is raw material (mails, policy extracts, memos, code lists):
- Put the pieces in date order. A later memo or instruction overrides an earlier rule on the same
  point. Note each override.
- A code list usually defines the outcome/reason values of the closing tool. Check them against the
  tool API enums; the tool API wins on spelling.
- Ignore chatter that is not an instruction. Keep every concrete rule, number, date and sentence.

### 2. Write the rule list: `out/<slot>.rules.md`
One line per rule, numbered, in plain words, each with its source (rule number or document + a
short quote). This is your checklist for everything that follows. If the brief has numbered rules,
keep its numbers. If several briefs are given (a base brief and an extension), the later one
replaces the rules it names; list the result, not both.

### 3. Write the spec: `out/<slot>.spec.json`
Follow `builder/SPEC.md`. Points that decide most failures:
- `calendar`: every holiday; the weekend days.
- `bands`: exact day ranges, deadline expressions, attempts per band. Read "the Nth business day
  after today" literally: `nth_bd(N)`.
- `attempts`: the persuasion offers in the brief's order, with the amount kind and the date expression.
- `products.types`: one entry per product type in the tool API's get_account enum, with the floor field.
- `outcomes`: every outcome/reason pair of the brief's closing table. `reason: null` where none is sent.
- `fixed_sentences`: every sentence the brief requires word for word.
- `tool_guidance`: for each tool, what to do on success and on each failure, using the brief's
  outcome and reason names. This text is what the model obeys after a tool call: make it concrete
  ("call close_call with outcome X and reason Y in the same message as your goodbye").
- A failed HTTP call reaches the model as a raw "API request failed" error, not through the hook.
  Write `tool_guidance.<tool>.fail` so it also works as a tool-description sentence: the generator puts
  it there. A retry the brief requires goes in `fail[0]` ("call <tool> again now with the same values").
  Leave `engine_retries` at 0: a platform-side retry failed the practice case in testing.
- `prompts`: see "Writing the prompts" below.
- `rules`: every rule from step 2 with `enforced_in`.

### 4. Generate
```bash
python3 builder/lib/collections_gen.py out/<slot>.spec.json <tool_api.json> out/<slot>.workflow.json
```
The generator builds: setup (code: date facts, callback window) -> identity -> third_party | fetch_account
(api) -> account_prep (code: per-product facts) -> collection -> end. Read `builder/lib/collections_gen.py`
once to know what it does.

If the brief needs something this shape cannot express (another step, another order, a tool used in
a different way, other response fields), write `out/<slot>_gen.py` that imports `collections_gen.build`
and `wfkit`, modifies the workflow dict in code (add or change nodes, tools, transitions), and writes
`out/<slot>.workflow.json`. Use the `wfkit` helpers; they keep the platform rules. Load the tool API
from the path given in the run prompt, never a fixed path.

### 5. Check statically (must be 0 errors)
```bash
python3 builder/lib/check.py out/<slot>.workflow.json <tool_api.json> --spec out/<slot>.spec.json --brief <brief1> [--brief <brief2>] \
  --not-from builder/examples/example.spec.json --allow-from builder/skill/SKILL.md --allow-from builder/lib/collections_gen.py
```
Fix every ERROR in the spec or generator and regenerate. A `copied` error means a sentence came from
another spec and not from this brief: rewrite it from this brief. A `result-words` error: rephrase. Read the WARN lines and fix the ones that matter.

### 6. Test the code locally (dates decide many cases)
Write `out/<slot>.datecheck.py` that loads the workflow, runs its code nodes with
`builder/lib/localrun.run_code_nodes(wf, {"now": ...}, products)` and asserts the values you work out
BY HAND from the brief:
- the deadline of each band for at least two `now` values (one where a holiday falls inside the window,
  one on a Friday or the day before a weekend),
- each attempt date, including the case where today is the last business day of the week,
- a weekend day and a holiday mapped to the next business day in `date_facts`,
- the callback window today (before and after the end hour) and on the next business day,
- the floor of each product type, and a customer with several products if the brief allows it.
Write the expected values first, from the brief, then run. If a value differs, decide from the brief
which is right and fix the spec or generator, never the expectation to match the code.

### 7. Audit the rules against the workflow
Go through `out/<slot>.rules.md` line by line. For each rule find the exact place in the generated
workflow (prompt text, code, hook, route) that makes it happen. Missing or weak: fix it.
Check especially: words that must not be said before identity is verified, what a relative may hear,
which outcome/reason each ending uses, what happens when a tool fails, the exact fixed sentences,
the order of questions, retry limits.

### 8. Save, smoke test, practice
```bash
python3 -c "import freya,json,sys; print(json.dumps(freya.save_workflow(sys.argv[1], json.load(open(sys.argv[2])))))" <freya_slot> out/<slot>.workflow.json
python3 builder/lib/smoke.py <workflow_id>
```
Use the Freya save slot from the run prompt. `save_workflow` creates the workflow once and updates the
same one later. Run the smoke test ONCE. `SMOKE SKIPPED` means the platform's chat route is down: move
on, do not retry or wait. `SMOKE FAIL` means the workflow is broken: fix it.

Only if the run prompt says practice cases exist for this brief: run them once, in the foreground, with
the time-limited wrapper (macOS has no `timeout` command; never call tools/run_cases.py directly):
`python3 builder/lib/run_practice.py <seconds left minus 120> <agent_id> --brief <letter> --out out/<slot>.cases.json`
(3 to 11 min). Start a round only if at least 12 minutes are left. The last line tells you what happened:
`PRACTICE SKIPPED` (the workspace is busy with another simulation: do not retry, finish) or
`PRACTICE TIMEOUT`: report PRACTICE as that word, never an earlier result.
Read each FAIL reason and the transcript (`freya.get_call(call_id)["messages"]`), fix the spec or
generator, regenerate, check, save. At most two such rounds, and only while time allows. One green run
is not proof: prefer fixes that make a rule deterministic over prompt wording that happened to pass.

### 9. Finish
End with exactly these lines:
```
SLOT <slot>
WORKFLOW_FILE out/<slot>.workflow.json
WORKFLOW_ID <id>
AGENT_ID <id>
CHECK <errors> errors
DATECHECK <pass|fail>
PRACTICE <passed>/<total> or none
```

## Writing the prompts

Generic platform sentences you may use word for word (they hold for any brief):
- "When a rule says to call a tool, CALL the tool. Saying it is not enough. Call at most one tool per message."
- "Every tool result contains what_to_do_next. Obey it."
- "After every tool result, speak to the customer or call the next tool. Never end a turn with nothing."
- "Never invent figures, dates or reasons."
- "close_call is always the very last action. Call it exactly once."
Everything else in the prompts is written from the brief of this run.

The platform makes three model calls per turn: extraction, routing, dialogue. The dialogue model
sees `system` + the current node's prompt + the history + the node's tools. It is a small model.

- One rule, one place. Put a rule in the node where it applies; put call-wide rules in `system`.
- Short imperative lines. Quote required sentences exactly, in double quotes, and say "say exactly".
- Give facts, not calculations: refer to `{{account_facts}}`, `{{date_facts}}`, `{{callback_facts}}`
  and named dates. Never ask the model to count business days.
- Every ending names its tool call fully: "thank them, say goodbye and call close_call with outcome X,
  reason Y" in one message. Goodbye and close_call go in the same message; close_call is last.
- "Never say something is recorded/booked/transferred unless the tool returned success."
- "After every tool result, speak or call the next tool. Never end a turn with nothing."
- Result words: the words the brief uses to report a tool result ("recorded", "booked", "transferred",
  "saved", ...) must never appear in another sense, above all in the opening line or the debt statement.
  A judge checking "never says it is recorded before the tool succeeded" reads them literally: a
  practice case failed on "this call is recorded" in the opening. Say "kept on record" / "on file"
  instead, unless the brief requires that exact sentence word for word. check.py flags this.
- Before identity is verified: list the exact words and topics that must not be said, and say it
  applies to everyone on the line.
- Name the trigger phrases the brief gives (what counts as a confirmation, a refusal, a hardship
  mention, a dispute, an already-paid claim) and the exact response for each.
- Order the collection prompt by priority: special situations first (they stop persuasion), then
  amounts, dates, confirmation and recording, persuasion last.
- Customers speak naturally: numbers in two parts, corrections ("the 21st, sorry, the 20th": the
  correction counts), dates and amounts in words, small talk, questions back. Tell the model to wait
  for a complete number, to use the corrected value, to convert words to digits, to answer a question
  briefly and return to the step, and to ignore dates that are not offered as payment dates.
- Once-only offers: when the brief says an offer is made once (e.g. a lower amount), write that offer so it
  asks for everything still missing in one go (the amount AND the day), so it never has to be repeated.
  A practice case failed because the lower amount was offered "for today" and then offered again with a date.
- A specific date that is not a business day (a weekend, a holiday) is still an exact day: never treat it as
  "no exact day given". The account facts list each such day with the day to offer instead; tell the model to use
  that list. A practice case failed when a named holiday was answered with the deadline.
- With several products: state each product, ask the brief's first question once for all, then go
  product by product in the order given, with attempts counted per product, and one read-back for all.
