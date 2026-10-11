# The spec: everything the generator needs from one brief

`out/<slot>.spec.json` is written by the builder from the brief and the tool API file.
`builder/lib/collections_gen.py` turns it into a workflow. **Every value comes from the brief or the
tool API file given in this run.** Nothing is copied from another brief or from an example.

`builder/examples/example.spec.json` shows the shape with a made-up bank. It is not a real brief: do not
reuse any of its values, sentences or rules. The brief you are given decides all of them.
Never open `builder/devtests/`: it holds test material for the developers, not for the builder.

## Keys

| Key | Type | What |
|---|---|---|
| `slot` | str | Short name for this agent, e.g. the brief's file stem. |
| `sources` | [str] | The brief files read. |
| `tools_used` | [str] | Tool names this brief needs. Tools left out are not given to the model. A brief that says a tool is never used: leave it out. |
| `call_variables` | [str] | Call variables the brief lists (`customer_id`, names, `now`, ...). |
| `opening_line` | str | The exact first sentence(s) the agent says, ending with the "am I speaking with" question. May use `{{call_variable}}`. |
| `identity.questions` | [{field, ask, format}] | Security questions in the brief's order. `field` must be a value of the verify tool's enum in the tool API. |
| `identity.max_wrong_total` | int | Wrong answers that end the check. |
| `calendar.weekend_days` | [int] | Python weekday numbers that are not business days (Mon=0 ... Sun=6). |
| `calendar.holidays` | [str] | Every bank holiday as `YYYY-MM-DD`. |
| `named_dates` | {name: expr} | Dates computed once per call, written to `vars[name]` and `vars[name + "_say"]`. Use them in prompts as `{{name_say}}` and in other expressions as `parse_iso(vars.get('name'))`. |
| `third_party.callback.days` | {label: expr} | The days a callback may be booked, e.g. today and the next business day. |
| `third_party.callback.from` / `.to` | "HH:MM" | Callback hours, both included. A callback today must also be later than `now`. |
| `products.types` | {type: {words, floor_field, floor_name}} | One entry per product `type` value of get_account. `words` is how the agent names the product; `<field>` is replaced by that product field, e.g. `credit card ending <card_last4>`. `floor_field` is the get_account field that holds the lowest acceptable amount. |
| `products.types.<type>.floor_expr` | expr | Optional, when the floor is computed instead of read from a field, e.g. a percentage of the amount due. Python with `due` (the past-due amount), `p` (the product dict) and `math`: `math.ceil(due * 0.2)`. Wins over `floor_field`. |
| `products.debt_intro` | str | Optional sentence said once before the products (e.g. who is calling and why). |
| `products.debt_sentence` | str | One sentence per product with `<words>`, `<amount>`, `<days>`. |
| `products.floor_offer_sentence` | str | Optional sentence used when offering the floor, with `<floor_name>` and `<floor>`. |
| `bands` | [{name, min_days, max_days, deadline, attempts}] | Lateness bands. `deadline` is an expression (see below). `attempts` = persuasion attempts allowed in that band. |
| `attempts` | [{n, amount, date, bands?}] | Persuasion offers in order. `amount`: `full`, `floor` or `full_or_floor`. `date`: an expression; `deadline` is the product's deadline. `bands`: only in these bands. |
| `outcomes` | [{outcome, reason, when}] | Every outcome/reason pair the brief allows for the closing tool, `reason: null` where none is sent. |
| `fixed_sentences` | [{text, when}] | Sentences the brief requires word for word. |
| `tool_guidance` | {tool: {...}} | What the model must do after each tool result (becomes `what_to_do_next`). verify tool keys: `all_verified`, `hint`, `exhausted`, `unreadable`, `correct`, `wrong`. Other tools: `ok` and `fail` (a list: first failure, second failure). |
| `tool_guidance.<tool>.engine_retries` | int | Writing tools only: how many times the platform itself repeats a failed call. Default 0. Tested on 10 Oct: with 1, the practice case that requires a retry passed 0/3; with 0 and the retry written into `fail` (moved into the tool description) it passed 3/3. Leave it 0. |
| `hooks` | {tool: python} | Optional: a full post-hook body that replaces the generated one, when the tool API's response differs (see `wfkit.hook`). |
| `tool_descriptions` | {tool: str} | Optional: when to call each tool. |
| `field_descriptions` | {tool: {field: str}} | How the model fills each field (formats, conversions). |
| `prompts.system` | str | Rules for the whole call. |
| `prompts.identity` | str | Who is on the line + security questions. |
| `prompts.third_party` | str | Someone else answered. May use `{{callback_facts}}`. |
| `prompts.collection_opening` | str | First sentence after verification, e.g. `... {{debt_lines}} <first ask>`. |
| `prompts.collection` | str | Everything after verification. Must include `{{account_facts}}` and `{{date_facts}}`. |
| `prompts.account_error` | str | Optional: what to do when the account cannot be loaded. |
| `rules` | [{id, text, enforced_in}] | Every numbered rule (or every rule you found in raw material) with where it is enforced: `node:<name>`, `code:<node>`, `hook:<tool>`, `tool:<tool>`. |
| `test_variables` | {var: value} | Example call variables. |

## Expressions

`deadline`, `attempts[].date`, `named_dates` and `callback.days` are Python expressions evaluated in a
code node at call time, with these helpers (from `wfkit.date_lib`):

`today`, `now`, `datetime`, `isbd(d)`, `nextbd(d)`, `prevbd(d)`, `add_bd(d, n)`, `nth_bd(n)`
(the n-th business day after today), `monday_of(d)`, `last_bd_of_week(monday)`, `parse_iso(s)`, `iso(d)`.
In attempt dates, `deadline` is the product's deadline. `vars.get(name)` reads a named date.

Examples (not values to copy):
- "the 5th business day after today" -> `nth_bd(5)`
- "10 calendar days from today, moved to the next business day if needed" ->
  `(today + datetime.timedelta(days=10)) if isbd(today + datetime.timedelta(days=10)) else nextbd(today + datetime.timedelta(days=10))`
- "the last business day of this month" -> `prevbd(datetime.date(today.year + (today.month == 12), today.month % 12 + 1, 1))`

Never write `{{` or `}}` in an expression.

## What the generator gives the model at call time

| Variable | Set by | Content |
|---|---|---|
| `date_facts` | setup | Today, a this/next weekday table, a 45-day calendar marking weekends and holidays with the next business day |
| `callback_facts` | setup | The allowed callback days and hours, with "tomorrow" resolved |
| `<named_date>`, `<named_date>_say` | setup | ISO and spoken form |
| `account_facts` | account_prep | Per product: id, words, amount, days late, band, floor, deadline, attempts allowed, each attempt offer |
| `debt_lines` | account_prep | `debt_intro` + one `debt_sentence` per product |
| `product_count`, `product_ids` | account_prep | |

## Platform behaviour found in testing

- When a tool's HTTP call fails (for example a 500), the model receives a raw `API request failed: ...`
  error event. The post hook's `what_to_do_next` does NOT reach it. So the failure instructions are also
  written into the tool description (the generator does this from `tool_guidance.<tool>.fail`).
  Tested: this makes the model retry correctly (3/3). A platform-side retry (`engine_retries: 1`) did not
  (0/3), so write the retry into `fail` instead.
