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
import sys
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


def chat(host, model, system, user, num_ctx, num_predict, timeout):
    body = {
        "model": model,
        "stream": False,
        "options": {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": 0.2},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.load(resp)
    return {
        "text": r["message"]["content"].strip(),
        "prompt_tokens": r.get("prompt_eval_count"),
        "output_tokens": r.get("eval_count"),
        "seconds": round(time.time() - t0, 1),
    }


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
    ap.add_argument("--num-predict", type=int, default=1024)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per model call")
    ap.add_argument("--out", default="-", help="report path, or - for stdout")
    a = ap.parse_args()

    d = Path(a.dir)
    meta = json.loads((d / "meta.json").read_text())
    vet = (d / "vet.txt").read_text() if (d / "vet.txt").exists() else ""
    test = (d / "test.txt").read_text() if (d / "test.txt").exists() else ""
    warnings = list(meta.get("warnings", []))
    stats, findings = [], []

    for c in meta["chunks"]:
        est = c["chars"] // 3 + len(FINDINGS_PROMPT) // 3
        if est + a.num_predict > a.num_ctx:
            warnings.append(f"{c['file']} is ~{est} tokens; with num_ctx={a.num_ctx} the start may be cut off "
                            f"(lower --chunk-chars or raise --num-ctx)")
        print(f"reviewing {c['file']} ({', '.join(c['packages'])}, ~{est} tokens)...", file=sys.stderr)
        try:
            r = chat(a.host, a.model, FINDINGS_PROMPT, (d / c["file"]).read_text(),
                     a.num_ctx, a.num_predict, a.timeout)
        except Exception as e:  # keep going so one bad chunk doesn't lose the rest
            warnings.append(f"{c['file']}: model call failed: {e}")
            findings.append((c, f"_Model call failed: {e}_"))
            continue
        if r["prompt_tokens"] and r["prompt_tokens"] >= a.num_ctx - 16:
            warnings.append(f"{c['file']}: prompt filled the whole context window "
                            f"({r['prompt_tokens']} tokens); some code was probably not seen")
        stats.append((c["file"], r))
        findings.append((c, r["text"]))
        print(f"  done in {r['seconds']}s, {r['prompt_tokens']} prompt / {r['output_tokens']} output tokens",
              file=sys.stderr)

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
        r = chat(a.host, a.model, NEXT_ACTION_PROMPT, summary_input, a.num_ctx, 512, a.timeout)
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
              "| call | prompt tokens | output tokens | seconds |", "|---|---|---|---|"]
    lines += [f"| {name} | {r['prompt_tokens']} | {r['output_tokens']} | {r['seconds']} |" for name, r in stats]
    lines += ["", "</details>", ""]

    report = "\n".join(lines)
    if a.out == "-":
        sys.stdout.write(report)
    else:
        Path(a.out).write_text(report)
        print(f"wrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
