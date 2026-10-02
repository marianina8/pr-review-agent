#!/usr/bin/env python3
"""Review collected Go context with a local Ollama model and write a Markdown report.

The model only writes the code findings (one call per chunk) and the Next
Action list (one short call at the end). The go vet / go test sections come
straight from the tool output, so the model can't misreport them.

Uses Ollama's /api/chat with an explicit num_ctx: the CLI's default context
window is ~4k tokens and Ollama silently drops the start of longer prompts.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

FINDINGS_PROMPT = """You are a senior Go code reviewer. You are given Go source files with numbered lines ("N| code") and, for a pull request, the diff of each changed file.

Report only real problems you can point to in the code shown: bugs, wrong or missing error handling, resource leaks, concurrency problems, security problems (for example secrets ending up in logs or URLs), API misuse, and missing input validation.

Rules:
- Cite each finding as `path:line`, using the line numbers shown.
- Do not invent code, files or behavior that is not shown. No style nitpicks, no praise, no summary of what the code does.
- For a pull request, focus on the changed lines, but also report anything the change breaks.
- Output a Markdown bullet list, most severe first. Each bullet: **high|medium|low** — `path:line` — the problem — the fix.
- If there are no real problems, output exactly: No issues found."""

NEXT_ACTION_PROMPT = """You are a senior Go reviewer writing the last section of a review.
Given the findings and the go vet / go test results, write a numbered list of 3 to 6 concrete actions, most important first.
Only use what is given. Output just the numbered list."""


T0 = time.time()


def log(msg):
    """Progress line with time since start; unbuffered so it shows up live in the Actions log."""
    m, sec = divmod(int(time.time() - T0), 60)
    print(f"[{m:02d}:{sec:02d}] {msg}", file=sys.stderr, flush=True)


class Rate:
    """Prompt-reading speed from the previous call, to estimate how long the next one takes."""
    tokens_per_sec = None


def load_model(host, model):
    log(f"loading {model} into memory...")
    t = time.time()
    req = urllib.request.Request(f"{host}/api/generate",
                                 data=json.dumps({"model": model, "prompt": "", "keep_alive": "60m"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            resp.read()
    except Exception as e:
        sys.exit(f"ERROR: could not load {model} from Ollama at {host}: {e} (is `ollama serve` running?)")
    log(f"model loaded in {time.time() - t:.0f}s")


def chat(host, model, system, user, num_ctx, num_predict, timeout, label="", est_tokens=0,
         num_thread=None, heartbeat=20):
    """Streaming chat call that logs what it is doing while the model works."""
    opts = {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0.2}
    if num_thread:
        opts["num_thread"] = num_thread
    body = {"model": model, "stream": True, "keep_alive": "60m", "options": opts,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    state = {"phase": "reading", "start": time.time(), "first": None, "out": 0, "done": False}
    eta = ""
    if Rate.tokens_per_sec and est_tokens:
        secs = est_tokens / Rate.tokens_per_sec
        eta = f", about {secs / 60:.0f} min at the last measured speed" if secs >= 90 else \
            f", about {secs:.0f}s at the last measured speed"
    log(f"{label}: model is reading ~{est_tokens} tokens of input{eta}")

    def beat():
        while not state["done"]:
            time.sleep(heartbeat)
            if state["done"]:
                break
            el = time.time() - state["start"]
            if state["phase"] == "reading":
                log(f"{label}: still reading input ({el:.0f}s so far; Ollama reports no progress during this phase)")
            else:
                w = time.time() - state["first"]
                log(f"{label}: writing, {state['out']} tokens so far ({state['out'] / max(w, 1):.1f} tok/s, "
                    f"limit {num_predict})")

    threading.Thread(target=beat, daemon=True).start()
    text, line, final = [], "", {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                ev = json.loads(raw)
                piece = ev.get("message", {}).get("content", "")
                if piece:
                    if state["first"] is None:
                        state["first"], state["phase"] = time.time(), "writing"
                        log(f"{label}: finished reading after {state['first'] - state['start']:.0f}s; "
                            f"now writing the review:")
                    state["out"] += 1
                    text.append(piece)
                    line += piece
                    while "\n" in line:  # echo the review line by line as it is written
                        done_line, line = line.split("\n", 1)
                        if done_line.strip():
                            print(f"    | {done_line}", file=sys.stderr, flush=True)
                if ev.get("done"):
                    final = ev
    finally:
        state["done"] = True
    if line.strip():
        print(f"    | {line}", file=sys.stderr, flush=True)

    pe, pd = final.get("prompt_eval_count"), final.get("prompt_eval_duration")
    if pe and pd:
        Rate.tokens_per_sec = pe / (pd / 1e9)
    r = {
        "text": "".join(text).strip(),
        "prompt_tokens": pe,
        "output_tokens": final.get("eval_count"),
        "seconds": round(time.time() - state["start"], 1),
        "read_seconds": round(pd / 1e9, 1) if pd else None,
        "write_seconds": round(final["eval_duration"] / 1e9, 1) if final.get("eval_duration") else None,
    }
    log(f"{label}: done in {r['seconds']:.0f}s (read {pe} tokens in {r['read_seconds']}s, "
        f"wrote {r['output_tokens']} tokens in {r['write_seconds']}s)")
    return r


IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def group(title):
    if IN_ACTIONS:
        print(f"::group::{title}", file=sys.stderr, flush=True)


def endgroup():
    if IN_ACTIONS:
        print("::endgroup::", file=sys.stderr, flush=True)


def fence(text, limit=6000):
    text = text.rstrip() or "(no output)"
    if len(text) > limit:
        text = "... [trimmed]\n" + text[-limit:]
    return f"```\n{text}\n```"


def status(rc):
    return "not run" if rc is None else ("**PASS**" if rc == 0 else f"**FAIL** (exit {rc})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".go-pr-review/ci", help="output folder of collect.py")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--num-predict", type=int, default=768, help="max tokens the model writes per chunk")
    ap.add_argument("--num-thread", type=int, default=os.cpu_count(), help="CPU threads for Ollama")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per model call")
    ap.add_argument("--heartbeat", type=int, default=20, help="seconds between progress lines")
    ap.add_argument("--out", default="-", help="report path, or - for stdout")
    a = ap.parse_args()

    d = Path(a.dir)
    meta = json.loads((d / "meta.json").read_text())
    vet = (d / "vet.txt").read_text() if (d / "vet.txt").exists() else ""
    test = (d / "test.txt").read_text() if (d / "test.txt").exists() else ""
    warnings = list(meta.get("warnings", []))
    stats, findings = [], []

    log(f"model {a.model}, num_ctx {a.num_ctx}, {a.num_thread} CPU threads, "
        f"{len(meta['chunks'])} chunk(s) covering {len(meta['files'])} Go file(s)")
    if meta["chunks"]:
        load_model(a.host, a.model)
    for i, c in enumerate(meta["chunks"], 1):
        est = c["chars"] // 3 + len(FINDINGS_PROMPT) // 3
        if est + a.num_predict > a.num_ctx:
            warnings.append(f"{c['file']} is ~{est} tokens; with num_ctx={a.num_ctx} the start may be cut off "
                            f"(lower --chunk-chars or raise --num-ctx)")
        label = f"chunk {i}/{len(meta['chunks'])}"
        group(f"{label}: {', '.join(c['files'])}")
        log(f"{label}: packages {', '.join(c['packages'])}; files: {', '.join(c['files'])}")
        try:
            r = chat(a.host, a.model, FINDINGS_PROMPT, (d / c["file"]).read_text(), a.num_ctx,
                     a.num_predict, a.timeout, label=label, est_tokens=est, num_thread=a.num_thread,
                     heartbeat=a.heartbeat)
        except Exception as e:  # keep going so one bad chunk doesn't lose the rest
            warnings.append(f"{c['file']}: model call failed: {e}")
            findings.append((c, f"_Model call failed: {e}_"))
            log(f"{label}: FAILED: {e}")
            endgroup()
            continue
        if r["prompt_tokens"] and r["prompt_tokens"] >= a.num_ctx - 16:
            warnings.append(f"{c['file']}: prompt filled the whole context window "
                            f"({r['prompt_tokens']} tokens); some code was probably not seen")
        stats.append((c["file"], r))
        findings.append((c, r["text"]))
        endgroup()

    # --- assemble ---------------------------------------------------------
    lines = ["## Diff Summary", ""]
    if meta["mode"] == "pr":
        lines.append(f"Compared with `{meta['base']}`.")
        lines.append(fence(meta.get("diffstat") or "No changes.", 3000))
        if not meta["files"]:
            lines.append("\nNo Go files changed, so there was no code for the model to review.")
    else:
        pkgs = sorted({p for c in meta["chunks"] for p in c["packages"]})
        lines.append(f"Code mode (no diff): reviewed {len(meta['files'])} Go file(s) in "
                     f"{len(pkgs)} package(s) under `{meta['target']}`.")

    lines += ["", "## File Read", ""]
    if not findings:
        lines += ["No Go code to review.", ""]
    for c, text in findings:
        if len(findings) > 1:
            lines += [f"**{', '.join(c['packages'])}**", ""]
        lines += [text, ""]

    lines += ["## go vet", "", status(meta.get("vet_rc"))]
    if meta.get("vet_rc"):
        lines.append(fence(vet))
    lines += ["", "## go test", "", status(meta.get("test_rc"))]
    if meta.get("test_rc") is not None:
        lines.append(fence(test, 4000 if meta["test_rc"] == 0 else 6000))

    all_findings = "\n\n".join(t for _, t in findings) or "No code reviewed."
    summary_input = (f"Findings:\n{all_findings}\n\ngo vet: {status(meta.get('vet_rc'))}\n{vet[-3000:]}\n\n"
                     f"go test: {status(meta.get('test_rc'))}\n{test[-3000:]}")
    lines += ["", "## Next Action", ""]
    if not meta["chunks"]:
        lines.append("No Go code changed; nothing for the model to act on.")
    else:
      try:
        group("next action list")
        r = chat(a.host, a.model, NEXT_ACTION_PROMPT, summary_input, a.num_ctx, 384, a.timeout,
                 label="next action", est_tokens=len(summary_input) // 3, num_thread=a.num_thread,
                 heartbeat=a.heartbeat)
        endgroup()
        stats.append(("next-action", r))
        lines.append(r["text"])
      except Exception as e:
        warnings.append(f"next-action model call failed: {e}")
        lines.append("_Model call failed; see the findings above._")

    if warnings:
        lines += ["", "> [!WARNING]"] + [f"> - {w}" for w in warnings]

    total = sum(r["seconds"] for _, r in stats)
    lines += ["", "<details><summary>Run details</summary>", "",
              f"Model `{a.model}`, num_ctx {a.num_ctx}, {len(meta['chunks'])} chunk(s), {total:.0f}s of model time.", "",
              "| call | prompt tokens | output tokens | reading (s) | writing (s) | total (s) |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {name} | {r['prompt_tokens']} | {r['output_tokens']} | {r.get('read_seconds')} | "
              f"{r.get('write_seconds')} | {r['seconds']} |" for name, r in stats]
    lines += ["", "</details>", ""]

    report = "\n".join(lines)
    if a.out == "-":
        sys.stdout.write(report)
    else:
        Path(a.out).write_text(report)
        log(f"wrote {a.out}")


if __name__ == "__main__":
    main()
