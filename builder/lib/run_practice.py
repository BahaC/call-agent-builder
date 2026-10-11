"""Run the practice cases with a hard time limit, and give up at once when the workspace is busy.

    python3 builder/lib/run_practice.py <seconds> <agent_id> --brief A [--out out/x.cases.json] [other run_cases args]

Why not plain `timeout`: macOS has no `timeout` command. Why give up when busy: a workspace runs one
simulation at a time and tools/run_cases.py waits forever for a running one (also one that is stuck
on the platform), which would eat the builder's whole time budget.

Prints the run_cases output. Last line is one of:
  PRACTICE DONE            the run finished
  PRACTICE SKIPPED: ...    the workspace is busy with another simulation
  PRACTICE TIMEOUT         the time limit was reached
"""
import subprocess, sys, time, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    limit = float(sys.argv[1])
    args = [sys.executable, "-u", str(ROOT / "tools" / "run_cases.py")] + sys.argv[2:]
    p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    t0 = time.time()
    import threading, queue
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout] or q.put(None), daemon=True).start()
    while True:
        left = limit - (time.time() - t0)
        if left <= 0:
            p.kill()
            print("PRACTICE TIMEOUT", flush=True)
            return
        try:
            line = q.get(timeout=min(left, 5))
        except queue.Empty:
            continue
        if line is None:
            p.wait()
            print("PRACTICE DONE", flush=True)
            return
        print(line, end="", flush=True)
        if "another simulation is running" in line:
            p.kill()
            print("PRACTICE SKIPPED: another simulation is running in this workspace", flush=True)
            return


if __name__ == "__main__":
    main()
