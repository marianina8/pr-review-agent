"""Agent-mode review: the model can look around the repo and run Go commands before it answers.

Tools are offered through a plain-text protocol (one JSON action per reply) rather than a
provider's native tool-calling API, so every model gets exactly the same tools and instructions,
including models whose Bedrock endpoint has no native tool support.

Guardrails:
- `run` only allows `go test|vet|build|run` with no `@version` arguments, GOPROXY=off (no
  downloads), a timeout, and a scrubbed environment (no AWS or GitHub credentials).
- `write` only allows scratch files: `.review-scratch/**` or `zz_review_*_test.go`. They are
  deleted when the review ends.
- Reads are limited to the repository; outputs are trimmed before they go back to the model.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AGENT_PROMPT = """You are a senior Go code reviewer working as an agent inside a checked-out copy of the repository (the current directory). You are given the Go source with numbered lines, plus the go vet / go test results.

Before you write the review, investigate. Read code, and RUN things to check your suspicions instead of guessing: write a small scratch test that triggers the behavior you suspect, run it, and look at the output. Only report a problem as confirmed if you saw it happen.

Reply with exactly ONE action per message, as a single JSON object in a ```json block, and nothing after it. Actions:
- {"action": "list", "path": "."}                                   list files in a directory
- {"action": "read", "path": "main.go", "start": 1, "end": 120}      read lines (numbered)
- {"action": "grep", "pattern": "regexp", "path": "."}                search .go files
- {"action": "write", "path": "zz_review_x_test.go", "content": "..."} create a scratch file; allowed paths: zz_review_*_test.go (any package dir) or .review-scratch/...
- {"action": "run", "cmd": "go test -run TestReviewX -v ./..."}       run go test / go vet / go build / go run (no network)
- {"action": "final", "review": "..."}                                 finish with the review

You have a limited number of actions; the result of each is sent back to you. Scratch files are deleted afterwards.

The final review must use exactly these six headings, in this order:
### Error handling
### Secrets and sensitive data
### Input validation
### External services
### Resources and concurrency
### Tests
Under each, list concrete problems or write "- none found". Each problem is one bullet:
`- **severity** — path:line — what is wrong — how to fix it`, severity is one word: high, medium or low.
Add " [verified]" at the end of a bullet only if you confirmed it by running something.
Cite line numbers from the original files. Only report what you can point to in the code; no praise, no summary."""

MAX_RESULT_CHARS = 6000
RUN_TIMEOUT = 120
ALLOWED_GO = {"test", "vet", "build", "run"}


def _trim(text, limit=MAX_RESULT_CHARS):
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[: limit // 3] + f"\n... [{len(text) - limit} chars trimmed] ...\n" + text[-(limit * 2 // 3):]


def _inside(root, path):
    p = (root / path).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path {path!r} is outside the repository")
    return p


def _writable(rel):
    import posixpath
    rel = posixpath.normpath(rel.replace("\\", "/"))
    if rel.startswith("..") or rel.startswith("/"):
        return False
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith(".review-scratch/") or re.fullmatch(r"zz_review_[A-Za-z0-9_]*_test\.go", name) is not None


class Tools:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.written = []
        home = os.environ.get("HOME", "/tmp")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "GOPATH": os.environ.get("GOPATH", os.path.join(home, "go")),
            "GOCACHE": os.environ.get("GOCACHE", os.path.join(home, ".cache", "go-build")),
            "GOMODCACHE": os.environ.get("GOMODCACHE", os.path.join(home, "go", "pkg", "mod")),
            "GOROOT": os.environ.get("GOROOT", ""),
            "GOPROXY": "off",
            "GOFLAGS": "-mod=mod",
            "GOTOOLCHAIN": "local",
            "TMPDIR": tempfile.gettempdir(),
        }
        self.env = {k: v for k, v in self.env.items() if v}

    def list(self, path=".", **_):
        d = _inside(self.root, path)
        out = []
        for p in sorted(d.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if any(part.startswith(".") and part != ".review-scratch" for part in p.relative_to(self.root).parts):
                continue
            if p.is_file():
                out.append(f"{rel} ({p.stat().st_size} bytes)")
            if len(out) >= 200:
                out.append("... (truncated)")
                break
        return "\n".join(out) or "(empty)"

    def read(self, path, start=1, end=None, **_):
        lines = _inside(self.root, path).read_text(errors="replace").splitlines()
        start = max(int(start or 1), 1)
        end = min(int(end or len(lines)), len(lines), start + 399)
        width = len(str(end))
        return "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1)) or "(no lines)"

    def grep(self, pattern, path=".", **_):
        rx = re.compile(pattern)
        base = _inside(self.root, path)
        files = [base] if base.is_file() else sorted(base.rglob("*.go"))
        hits = []
        for f in files:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(self.root).as_posix()}:{i}: {line.strip()}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n... (truncated)"
        return "\n".join(hits) or "(no matches)"

    def write(self, path, content, **_):
        if not _writable(path):
            raise ValueError("writes are only allowed to zz_review_*_test.go files or .review-scratch/")
        if len(content) > 20000:
            raise ValueError("file too large (max 20000 chars)")
        p = _inside(self.root, path)
        if p.exists() and p not in self.written:
            raise ValueError("refusing to overwrite an existing repository file")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if p not in self.written:
            self.written.append(p)
        return f"wrote {path} ({len(content)} chars)"

    def run(self, cmd, **_):
        argv = shlex.split(cmd)
        if len(argv) < 2 or argv[0] != "go" or argv[1] not in ALLOWED_GO:
            raise ValueError("only `go test`, `go vet`, `go build` and `go run` are allowed")
        if any("@" in a for a in argv[2:]):
            raise ValueError("module@version arguments are not allowed (no network)")
        if any(a in ("-exec", "-toolexec") or a.startswith(("-exec=", "-toolexec=")) for a in argv):
            raise ValueError("-exec/-toolexec are not allowed")
        t = time.time()
        try:
            p = subprocess.run(argv, cwd=self.root, env=self.env, capture_output=True, text=True,
                               timeout=RUN_TIMEOUT)
            out, rc = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, rc = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")), "timeout"
        return f"exit {rc} after {time.time() - t:.0f}s\n{out}"

    def cleanup(self):
        for p in reversed(self.written):
            try:
                p.unlink()
            except OSError:
                pass
        scratch = self.root / ".review-scratch"
        if scratch.exists():
            for p in sorted(scratch.rglob("*"), reverse=True):
                (p.unlink() if p.is_file() else p.rmdir())
            scratch.rmdir()


def parse_action(text):
    """First JSON object in the reply (prefers a ```json block)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "action" in obj:
                return obj
        except json.JSONDecodeError:
            # tolerate raw newlines inside strings
            try:
                obj = json.loads(c, strict=False)
                if isinstance(obj, dict) and "action" in obj:
                    return obj
            except json.JSONDecodeError:
                pass
    return None


def bedrock_converse(model, region, system, messages, max_tokens, timeout):
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "s.json").write_text(json.dumps([{"text": system}]))
        Path(tmp, "m.json").write_text(json.dumps(messages))
        Path(tmp, "c.json").write_text(json.dumps({"maxTokens": max_tokens, "temperature": 0.2}))
        cmd = ["aws", "bedrock-runtime", "converse", "--model-id", model, "--region", region,
               "--system", f"file://{tmp}/s.json", "--messages", f"file://{tmp}/m.json",
               "--inference-config", f"file://{tmp}/c.json", "--cli-read-timeout", str(timeout),
               "--output", "json"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
    if p.returncode != 0:
        raise RuntimeError(f"aws bedrock-runtime converse failed: {p.stderr.strip()[-800:]}")
    r = json.loads(p.stdout)
    text = "".join(c.get("text", "") for c in r["output"]["message"]["content"]).strip()
    u = r.get("usage", {})
    return text, u.get("inputTokens") or 0, u.get("outputTokens") or 0, r.get("stopReason")


def run_agent(model, region, chunk_text, context_text, max_steps, max_tokens, timeout, log, root=".",
              transcript_path=None):
    """Returns a result dict shaped like review.chat()/bedrock_chat(), plus agent stats."""
    tools = Tools(root)
    messages = [{"role": "user", "content": [{"text": chunk_text + "\n\n" + context_text}]}]
    transcript = [f"# Agent transcript: {model}\n"]
    stats = {"steps": 0, "runs": 0, "writes": 0, "bad_replies": 0, "calls": 0}
    t0 = time.time()
    tin = tout = 0
    final = None
    try:
        for step in range(1, max_steps + 2):
            last = step > max_steps
            if last:
                messages.append({"role": "user", "content": [{"text":
                    "You are out of actions. Reply now with the final action containing the full review."}]})
            text, i, o, stop = bedrock_converse(model, region, AGENT_PROMPT, messages, max_tokens, timeout)
            stats["calls"] += 1
            tin, tout = tin + i, tout + o
            messages.append({"role": "assistant", "content": [{"text": text or "(empty reply)"}]})
            act = parse_action(text)
            if act is None:
                stats["bad_replies"] += 1
                if last:
                    final = text  # take whatever it said
                    break
                feedback = "Could not find a JSON action in your reply. Reply with exactly one ```json action block."
                log(f"step {step}: no valid action ({len(text)} chars, stop={stop})")
                transcript.append(f"## Step {step}: invalid reply\n\n```\n{_trim(text, 1500)}\n```\n")
                messages.append({"role": "user", "content": [{"text": feedback}]})
                continue
            name = act.get("action")
            if name == "final":
                final = act.get("review") or ""
                log(f"step {step}: final review ({len(final)} chars)")
                break
            stats["steps"] += 1
            summary = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in act.items()}
            log(f"step {step}: {json.dumps(summary)[:200]}")
            try:
                fn = getattr(tools, name, None)
                if fn is None or name == "cleanup":
                    raise ValueError(f"unknown action {name!r}")
                args = {k: v for k, v in act.items() if k != "action"}
                result = fn(**args)
                stats["runs"] += name == "run"
                stats["writes"] += name == "write"
            except Exception as e:  # noqa: BLE001  (errors go back to the model)
                result = f"ERROR: {e}"
            result = _trim(str(result))
            for line in result.splitlines()[:6]:
                log(f"    > {line}")
            shown = act.get("content") if name == "write" else None
            transcript.append(f"## Step {step}: `{name}`\n\n```json\n{json.dumps(summary, indent=1)}\n```\n"
                              + (f"\n```go\n{_trim(shown, 3000)}\n```\n" if shown else "")
                              + f"\nResult:\n```\n{_trim(result, 2500)}\n```\n")
            remaining = max_steps - step
            messages.append({"role": "user", "content": [{"text":
                f"Result of {name}:\n{result}\n\n({remaining} actions left)"}]})
    finally:
        tools.cleanup()
        if transcript_path:
            transcript.append(f"\n## Final review\n\n{final or '(none)'}\n")
            Path(transcript_path).write_text("\n".join(transcript))
    secs = round(time.time() - t0, 1)
    if final is None:
        final = "_The agent did not produce a final review._"
    return {"text": final.strip(), "prompt_tokens": tin, "output_tokens": tout, "seconds": secs,
            "read_seconds": None, "write_seconds": None, "agent": stats}
