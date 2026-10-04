#!/usr/bin/env python3
"""Review collected Go context with a model and write a Markdown report.

Providers:
  ollama   a local Ollama server (default), e.g. qwen2.5-coder:7b
  bedrock  Amazon Bedrock through the AWS CLI's `converse` command, e.g.
           qwen.qwen3-coder-30b-a3b-v1:0 (needs AWS credentials; no Python deps)

The model only writes the code findings (one call per chunk) and the Next
Action list (one short call at the end). The go vet / go test sections come
straight from the tool output, so the model can't misreport them.

For Ollama it uses /api/chat with an explicit num_ctx: the CLI's default context
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

Go through the checklist below for the code shown. Under each heading, list the concrete problems you find, or write "- none found" if you checked and there are none.

### Error handling
Errors that are ignored or only logged, and what state the program is left in when something fails partway.
### Secrets and sensitive data
Where credentials or other sensitive values can end up.
### Input validation
Inputs (flags, config, parsed values) that are accepted but should not be.
### External services
How failures, limits and timeouts from network calls are handled.
### Resources and concurrency
Leaked files, bodies or goroutines; races; cancellation.
### Tests
Important behavior that has no test.

Rules:
- Use exactly the six headings above, in that order.
- Each problem is one bullet: `- **severity** — path:line — what is wrong — how to fix it`, where severity is one word: high, medium or low.
  Example of the format (not from this code): `- **medium** — store/cache.go:42 — the error from f.Close() is ignored, so a failed flush is lost — return the error from Close.`
- Cite line numbers exactly as shown. Only report what you can point to in the code shown; do not invent code, files or behavior.
- No praise and no summary of what the code does.
- For a pull request, focus on the changed lines, but also report anything the change breaks."""

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


def bedrock_chat(model, region, system, user, max_tokens, timeout, label="", est_tokens=0, heartbeat=20):
    """One Bedrock Converse call through the AWS CLI (preinstalled on GitHub runners)."""
    import subprocess
    import tempfile
    log(f"{label}: sending ~{est_tokens} tokens to Bedrock ({model}, {region})")
    with tempfile.TemporaryDirectory() as tmp:
        files = {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "config": {"maxTokens": max_tokens, "temperature": 0.2},
        }
        for name, val in files.items():
            Path(tmp, f"{name}.json").write_text(json.dumps(val))
        cmd = ["aws", "bedrock-runtime", "converse", "--model-id", model, "--region", region,
               "--system", f"file://{tmp}/system.json", "--messages", f"file://{tmp}/messages.json",
               "--inference-config", f"file://{tmp}/config.json",
               "--cli-read-timeout", str(timeout), "--output", "json"]
        state = {"done": False, "start": time.time()}

        def beat():
            while not state["done"]:
                time.sleep(heartbeat)
                if not state["done"]:
                    log(f"{label}: waiting for Bedrock ({time.time() - state['start']:.0f}s so far)")

        threading.Thread(target=beat, daemon=True).start()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
        finally:
            state["done"] = True
    if p.returncode != 0:
        raise RuntimeError(f"aws bedrock-runtime converse failed: {p.stderr.strip()[-800:]}")
    r = json.loads(p.stdout)
    text = "".join(c.get("text", "") for c in r["output"]["message"]["content"]).strip()
    for line in text.splitlines():
        if line.strip():
            print(f"    | {line}", file=sys.stderr, flush=True)
    usage = r.get("usage", {})
    out = {
        "text": text,
        "prompt_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "seconds": round(time.time() - state["start"], 1),
        "read_seconds": None,
        "write_seconds": None,
        "stop_reason": r.get("stopReason"),
    }
    log(f"{label}: done in {out['seconds']:.0f}s ({out['prompt_tokens']} tokens in, "
        f"{out['output_tokens']} out, stop reason {out['stop_reason']})")
    return out


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
    ap.add_argument("--provider", choices=["ollama", "bedrock", "anthropic"], default="ollama")
    ap.add_argument("--model", default=None,
                    help="default: qwen2.5-coder:7b (ollama) or qwen.qwen3-coder-30b-a3b-v1:0 (bedrock)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"), help="Bedrock region")
    ap.add_argument("--style", choices=["single", "agent"], default="single",
                    help="single: one call per chunk; agent: the model may read files and run go commands first")
    ap.add_argument("--max-steps", type=int, default=20, help="agent style: max actions per chunk")
    ap.add_argument("--repo", default=".", help="agent style: repository root the agent works in")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--num-predict", type=int, default=1024, help="max tokens the model writes per chunk")
    ap.add_argument("--num-thread", type=int, default=os.cpu_count(), help="CPU threads for Ollama")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per model call")
    ap.add_argument("--heartbeat", type=int, default=20, help="seconds between progress lines")
    ap.add_argument("--out", default="-", help="report path, or - for stdout")
    a = ap.parse_args()
    if not a.model:
        a.model = {"bedrock": "qwen.qwen3-coder-30b-a3b-v1:0", "anthropic": "claude-sonnet-5-5"}.get(
            a.provider, "qwen2.5-coder:7b")
    ollama = a.provider == "ollama"
    if a.style == "agent" and ollama:
        sys.exit("ERROR: --style agent needs --provider bedrock or anthropic")

    def call(system, user, max_tokens, label, est):
        if ollama:
            return chat(a.host, a.model, system, user, a.num_ctx, max_tokens, a.timeout, label=label,
                        est_tokens=est, num_thread=a.num_thread, heartbeat=a.heartbeat)
        if a.provider == "anthropic":
            import agent as agent_mod
            log(f"{label}: sending ~{est} tokens to Anthropic ({a.model})")
            t = time.time()
            text, i, o, stop = agent_mod.anthropic_messages(a.model, system, [{"role": "user", "content": [{"text": user}]}],
                                                            max_tokens, a.timeout)
            for line in text.splitlines():
                if line.strip():
                    print(f"    | {line}", file=sys.stderr, flush=True)
            r = {"text": text, "prompt_tokens": i, "output_tokens": o, "seconds": round(time.time() - t, 1),
                 "read_seconds": None, "write_seconds": None}
            log(f"{label}: done in {r['seconds']:.0f}s ({i} tokens in, {o} out, stop reason {stop})")
            return r
        return bedrock_chat(a.model, a.region, system, user, max_tokens, a.timeout, label=label,
                            est_tokens=est, heartbeat=a.heartbeat)

    d = Path(a.dir)
    meta = json.loads((d / "meta.json").read_text())
    vet = (d / "vet.txt").read_text() if (d / "vet.txt").exists() else ""
    test = (d / "test.txt").read_text() if (d / "test.txt").exists() else ""
    warnings = list(meta.get("warnings", []))
    stats, findings = [], []

    where = (f"num_ctx {a.num_ctx}, {a.num_thread} CPU threads" if ollama
             else "Anthropic API" if a.provider == "anthropic" else f"Bedrock {a.region}")
    log(f"{a.provider}: model {a.model}, {where}, "
        f"{len(meta['chunks'])} chunk(s) covering {len(meta['files'])} Go file(s)")
    if meta["chunks"] and ollama:
        load_model(a.host, a.model)
    for i, c in enumerate(meta["chunks"], 1):
        est = c["chars"] // 3 + len(FINDINGS_PROMPT) // 3
        if ollama and est + a.num_predict > a.num_ctx:
            warnings.append(f"{c['file']} is ~{est} tokens; with num_ctx={a.num_ctx} the start may be cut off "
                            f"(lower --chunk-chars or raise --num-ctx)")
        label = f"chunk {i}/{len(meta['chunks'])}"
        group(f"{label}: {', '.join(c['files'])}")
        log(f"{label}: packages {', '.join(c['packages'])}; files: {', '.join(c['files'])}")
        try:
            if a.style == "agent":
                import agent as agent_mod
                ctx = (f"go vet: {status(meta.get('vet_rc'))}\n{vet[-3000:]}\n\n"
                       f"go test: {status(meta.get('test_rc'))}\n{test[-3000:]}")
                log(f"{label}: agent mode, up to {a.max_steps} actions")
                r = agent_mod.run_agent(a.model, a.region, (d / c["file"]).read_text(), ctx, a.max_steps,
                                        max(a.num_predict, 4096), a.timeout, lambda m: log(f"{label}: {m}"),
                                        root=a.repo, transcript_path=d / f"agent-{c['file']}", provider=a.provider)
                ag = r["agent"]
                log(f"{label}: agent done in {r['seconds']:.0f}s: {ag['steps']} actions ({ag['runs']} runs, "
                    f"{ag['writes']} files written), {ag['calls']} model calls, {r['prompt_tokens']} tokens in")
            else:
                r = call(FINDINGS_PROMPT, (d / c["file"]).read_text(), a.num_predict, label, est)
        except Exception as e:  # keep going so one bad chunk doesn't lose the rest
            warnings.append(f"{c['file']}: model call failed: {e}")
            findings.append((c, f"_Model call failed: {e}_"))
            log(f"{label}: FAILED: {e}")
            endgroup()
            continue
        if ollama and r["prompt_tokens"] and r["prompt_tokens"] >= a.num_ctx - 16:
            warnings.append(f"{c['file']}: prompt filled the whole context window "
                            f"({r['prompt_tokens']} tokens); some code was probably not seen")
        if a.style == "single" and r.get("output_tokens") and r["output_tokens"] >= a.num_predict:
            warnings.append(f"{c['file']}: the review hit the {a.num_predict}-token output limit and was cut off")
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
        r = call(NEXT_ACTION_PROMPT, summary_input, 384, "next action", len(summary_input) // 3)
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
              f"Provider {a.provider} ({a.style}), model `{a.model}`" + (f", num_ctx {a.num_ctx}" if ollama else "" if a.provider == "anthropic" else f", region {a.region}")
              + f", {len(meta['chunks'])} chunk(s), {total:.0f}s of model time.", "",
              "| call | prompt tokens | output tokens | reading (s) | writing (s) | total (s) |",
              "|---|---|---|---|---|---|"]
    dash = lambda v: "—" if v is None else v  # noqa: E731  (Bedrock doesn't split reading/writing time)
    agents = [(n, r["agent"]) for n, r in stats if r.get("agent")]
    if agents:
        lines += [""] + [f"Agent {n}: {ag['steps']} actions ({ag['runs']} go commands, {ag['writes']} scratch files), "
                         f"{ag['calls']} model calls, {ag['bad_replies']} unparseable replies." for n, ag in agents] + [""]
    lines += [f"| {name} | {r['prompt_tokens']} | {r['output_tokens']} | {dash(r.get('read_seconds'))} | "
              f"{dash(r.get('write_seconds'))} | {r['seconds']} |" for name, r in stats]
    lines += ["", "</details>", ""]

    report = "\n".join(lines)
    if a.out == "-":
        sys.stdout.write(report)
    else:
        Path(a.out).write_text(report)
        log(f"wrote {a.out}")


if __name__ == "__main__":
    main()
