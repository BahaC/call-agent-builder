"""The builder: one command, a brief in, a phone agent out.

    python3 builder/build.py <brief.md> [<more briefs> ...] <tool_api.json> [--slot NAME] [--practice A|B]

  brief(s)      the bank's brief. Several files when one builds on another (base first).
  tool_api      the tool API file that comes with the brief (.json).
  --slot        name of the agent (default: stem of the last brief). The same slot updates the same workflow.
  --practice    only for development: the practice set letter whose cases match this brief.
  --budget      minutes for the whole run (default 27; the hard limit is 30).
  --model       model for the builder agent (default $BUILDER_MODEL or claude-opus-5-5).

Needs FREYA_API_KEY (and FREYA_BANK_TEAM_KEY for --practice) and a Claude login or ANTHROPIC_API_KEY.

Writes:
  out/<slot>.*                         spec, rules, generated workflow, checks (work files)
  submission/<slot>.workflow.json      the exported phone agent (what is scored)
  traces/<slot>-<time>.log             the trace: start/end, every model call, what was created, tests
  traces/<slot>-<time>.jsonl           every message of the run, raw
"""
import argparse, asyncio, dataclasses, json, os, pathlib, re, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "builder" / "lib"))


class Trace:
    def __init__(self, slot):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (ROOT / "traces").mkdir(exist_ok=True)
        self.log = (ROOT / "traces" / f"{slot}-{stamp}.log").open("w")
        self.raw = (ROOT / "traces" / f"{slot}-{stamp}.jsonl").open("w")
        self.t0 = time.time()
        self.model_calls = 0

    def line(self, s):
        s = f"[{time.strftime('%H:%M:%S')} +{time.time() - self.t0:5.0f}s] {s}"
        print(s, flush=True)
        self.log.write(s + "\n")
        self.log.flush()

    def rawmsg(self, obj):
        self.raw.write(json.dumps(obj, default=str) + "\n")
        self.raw.flush()


def redact(s):
    for k in ("FREYA_API_KEY", "ANTHROPIC_API_KEY", "FREYA_BANK_TEAM_KEY"):
        v = os.environ.get(k)
        if v and len(v) > 6:
            s = s.replace(v, "<" + k + ">")
    return s


TEST_LINE = re.compile(r"^\s*(PASS|FAIL) \S|\d+ / \d+ passed|error\(s\)|^SMOKE|^ERROR |AssertionError|DATECHECK|^Traceback")


def one_line(s, n=140):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n] + ("..." if len(s) > n else "")


async def run_agent(tr, prompt, model, deadline):
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, ToolResultBlock, UserMessage, query
    options = ClaudeAgentOptions(
        cwd=ROOT, model=model,
        skills=["build-collections-agent"], setting_sources=["project"],
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="dontAsk", max_turns=200,
        env={"BASH_DEFAULT_TIMEOUT_MS": "900000", "BASH_MAX_TIMEOUT_MS": "1200000", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    final_text = []
    async for m in query(prompt=prompt, options=options):
        tr.rawmsg({"type": type(m).__name__, **(dataclasses.asdict(m) if dataclasses.is_dataclass(m) else {"repr": repr(m)})})
        if isinstance(m, AssistantMessage):
            tr.model_calls += 1
            parts = []
            for b in m.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    parts.append("says: " + one_line(b.text))
                    final_text.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    arg = b.input.get("command") or b.input.get("file_path") or b.input.get("pattern") or b.input.get("skill") or ""
                    parts.append(f"{b.name}: {one_line(redact(str(arg)), 160)}")
            tr.line(f"MODEL CALL #{tr.model_calls} ({getattr(m, 'model', model)}) " + " | ".join(parts or ["(thinking)"]))
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    txt = b.content if isinstance(b.content, str) else " ".join(str(x.get("text", "")) for x in (b.content or []) if isinstance(x, dict))
                    for l in (txt or "").splitlines():
                        if TEST_LINE.search(l):
                            tr.line("  test> " + one_line(redact(l), 200))
        elif isinstance(m, ResultMessage):
            tr.line(f"AGENT DONE: {m.num_turns} turns, {m.duration_ms / 1000:.0f}s, ${m.total_cost_usd or 0:.2f}")
        if time.time() > deadline:
            tr.line("TIME BUDGET REACHED: stopping the agent")
            break
    return "\n".join(final_text)


def sh(tr, args, label):
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    tr.line(f"{label}: exit {r.returncode}")
    for l in out.splitlines()[-25:]:
        tr.line("  " + redact(l))
    return r.returncode, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--slot")
    ap.add_argument("--practice", choices=["A", "B"])
    ap.add_argument("--budget", type=float, default=27.0)
    ap.add_argument("--model", default=os.environ.get("BUILDER_MODEL", "claude-opus-5-5"))
    a = ap.parse_args()
    briefs = [f for f in a.files if not f.endswith(".json")]
    apis = [f for f in a.files if f.endswith(".json")]
    if not briefs or len(apis) != 1:
        sys.exit("usage: build.py <brief.md> [more briefs] <tool_api.json>")
    briefs = [str(pathlib.Path(b).resolve()) for b in briefs]
    tool_api = str(pathlib.Path(apis[0]).resolve())
    slot = a.slot or re.sub(r"[^\w\-]", "-", pathlib.Path(briefs[-1]).stem)
    fslot = "bb-" + slot  # the Freya workflow slot: prefixed so it never updates a workflow made by another tool
    t_start = time.time()
    deadline = t_start + a.budget * 60 - 150  # leave time for check, save and export
    tr = Trace(slot)
    tr.line(f"START {time.strftime('%Y-%m-%dT%H:%M:%S%z')} slot={slot}")
    tr.line(f"INPUT briefs={briefs} tool_api={tool_api}")
    tr.line(f"BUILDER MODEL {a.model}; budget {a.budget} min; practice={a.practice or 'none'}")
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "submission").mkdir(exist_ok=True)
    # the skill lives in builder/skill so the repo carries it; the agent SDK reads it from .claude/skills
    skill_dst = ROOT / ".claude" / "skills" / "build-collections-agent" / "SKILL.md"
    skill_dst.parent.mkdir(parents=True, exist_ok=True)
    skill_dst.write_text((ROOT / "builder" / "skill" / "SKILL.md").read_text())

    finish_by = time.strftime("%H:%M:%S", time.localtime(deadline - 60))
    prompt = (
        "Use the build-collections-agent skill to build a phone agent.\n"
        f"Brief file(s), in order: {', '.join(briefs)}\n"
        f"Tool API file: {tool_api}\n"
        f"Slot: {slot}  (file names out/{slot}.*)\n"
        f"Freya save slot: {fslot}  (use it in freya.save_workflow)\n"
        f"Finish (step 9) before {finish_by} local time; check `date` when unsure.\n"
        + (f"Practice cases exist for this brief: use --brief {a.practice} with tools/run_cases.py.\n" if a.practice
           else "No practice cases exist for this brief: do not run tools/run_cases.py.\n")
    )
    try:
        final = asyncio.run(run_agent(tr, prompt, a.model, deadline))
    except Exception as e:
        tr.line(f"AGENT ERROR: {one_line(redact(str(e)), 400)}")
        final = ""

    wf_file = ROOT / "out" / f"{slot}.workflow.json"
    spec_file = ROOT / "out" / f"{slot}.spec.json"
    if not wf_file.exists() and spec_file.exists():
        tr.line("FALLBACK: no workflow file, generating it from the spec")
        sh(tr, [sys.executable, "builder/lib/collections_gen.py", str(spec_file), tool_api, str(wf_file)], "generate")
    if not wf_file.exists():
        tr.line("FAILED: no workflow was produced")
        tr.line(f"END {time.strftime('%Y-%m-%dT%H:%M:%S%z')} total {time.time() - t_start:.0f}s")
        sys.exit(1)

    check_args = [sys.executable, "builder/lib/check.py", str(wf_file), tool_api]
    if spec_file.exists():
        check_args += ["--spec", str(spec_file)] + sum([["--brief", b] for b in briefs], [])
        # compare only with material the builder must never copy from. Earlier outputs of this builder (out/) share
        # generic wording by design, so comparing with them only produces noise.
        others = [p for p in list((ROOT / "builder" / "devtests").glob("*.spec.json")) + list((ROOT / "builder" / "examples").glob("*.spec.json"))
                  if p.resolve() != spec_file.resolve()]
        check_args += sum([["--not-from", str(p)] for p in others], []) + ["--allow-from", str(ROOT / "builder" / "skill" / "SKILL.md"), "--allow-from", str(ROOT / "builder" / "lib" / "collections_gen.py")]
    rc, _ = sh(tr, check_args, "FINAL STATIC CHECK")

    import freya
    wf = json.loads(wf_file.read_text())
    saved = freya.save_workflow(fslot, wf)
    tr.line(f"SAVED ON FREYA workflow_id={saved['workflow_id']} agent_id={saved['agent_id']} version={saved.get('version', 1)}")
    nodes = wf.get("nodes", {})
    n_tools = len({(t.get('function') or {}).get('name') for n in nodes.values() for t in (n.get('tools') or []) + (n.get('pre_actions') or [])})
    tr.line(f"CREATED steps={len(nodes)} ({', '.join(nodes)}) tools={n_tools}")
    out = ROOT / "submission" / f"{slot}.workflow.json"
    freya.export_workflow(saved["workflow_id"], str(out))
    tr.line(f"EXPORTED {out.relative_to(ROOT)}")
    for l in final.splitlines():
        if re.match(r"(SLOT|WORKFLOW_|AGENT_ID|CHECK|DATECHECK|PRACTICE) ", l.strip()):
            tr.line("AGENT REPORT " + l.strip())
    tr.line(f"MODEL CALLS {tr.model_calls} (builder model {a.model})")
    tr.line(f"END {time.strftime('%Y-%m-%dT%H:%M:%S%z')} total {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
