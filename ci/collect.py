#!/usr/bin/env python3
"""Collect Go review context for the Ollama reviewer.

Runs `go vet` and `go test`, then splits the code under review into chunks
small enough for the model's context window. Each chunk holds whole Go
packages where possible (one file at a time when a package is too big), with
numbered lines so the model can cite `path:line`.

  pr mode:   only the .go files the PR changes (diff + full file).
  code mode: every tracked .go file under --target.

Writes into --out: chunk-NN.md, vet.txt, test.txt and meta.json.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MAX_CHECK_OUTPUT = 20000  # chars of vet/test output kept (tail)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def git(*args):
    rc, out = run(["git", *args])
    if rc:
        sys.exit(f"git {' '.join(args)} failed:\n{out}")
    return out


def numbered(text):
    lines = text.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, 1))


def file_block(path, diff, budget, warnings):
    try:
        src = Path(path).read_text(errors="replace")
    except OSError as e:
        warnings.append(f"could not read {path}: {e}")
        return ""
    n = src.count("\n") + (0 if src.endswith("\n") or not src else 1)
    parts = [f"### FILE: {path} ({n} lines)\n"]
    if diff:
        parts.append(f"Diff:\n```diff\n{diff.rstrip()}\n```\n")
    parts.append("Full file:\n```go\n")
    body = numbered(src)
    head = "".join(parts)
    room = budget - len(head) - 200
    if len(body) > room:
        cut = body[: max(room, 0)].rsplit("\n", 1)[0]
        body = cut + f"\n... [truncated: {path} is larger than one chunk; later lines were not reviewed]"
        warnings.append(f"{path} truncated to fit one chunk ({n} lines total)")
    return head + body + "\n```\n\n"


def build_chunks(files, diffs, budget, warnings):
    by_pkg = {}
    for f in files:
        by_pkg.setdefault(os.path.dirname(f) or ".", []).append(f)
    chunks, cur, cur_pkgs, cur_files = [], "", [], []

    def flush():
        nonlocal cur, cur_pkgs, cur_files
        if cur:
            chunks.append({"text": cur, "packages": cur_pkgs, "files": cur_files})
        cur, cur_pkgs, cur_files = "", [], []

    for pkg in sorted(by_pkg):
        # non-test files first so tests are what gets dropped into a later chunk
        pfiles = sorted(by_pkg[pkg], key=lambda p: (p.endswith("_test.go"), p))
        blocks = [(f, file_block(f, diffs.get(f, ""), budget, warnings)) for f in pfiles]
        pkg_text = "".join(b for _, b in blocks)
        if len(cur) + len(pkg_text) <= budget:
            cur += pkg_text
            cur_pkgs.append(pkg)
            cur_files += pfiles
            continue
        flush()
        if len(pkg_text) <= budget:
            cur, cur_pkgs, cur_files = pkg_text, [pkg], list(pfiles)
            continue
        for f, b in blocks:  # package too big: split by file
            if len(cur) + len(b) > budget:
                flush()
            cur += b
            if pkg not in cur_pkgs:
                cur_pkgs.append(pkg)
            cur_files.append(f)
    flush()
    return chunks


def check(cmd, out_path):
    print(f"running: {' '.join(cmd)}", file=sys.stderr, flush=True)
    t = time.time()
    rc, out = run(cmd)
    print(f"  exit {rc} after {time.time() - t:.0f}s", file=sys.stderr, flush=True)
    if len(out) > MAX_CHECK_OUTPUT:
        out = "... [earlier output trimmed]\n" + out[-MAX_CHECK_OUTPUT:]
    out_path.write_text(out)
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["pr", "code"], default="pr")
    ap.add_argument("--target", default=".", help="file or folder to review (default: whole repo)")
    ap.add_argument("--base", default="origin/main", help="base ref for pr mode")
    ap.add_argument("--out", default=".go-pr-review/ci")
    ap.add_argument("--chunk-chars", type=int, default=30000,
                    help="max characters of code per model call (~3 chars per token)")
    ap.add_argument("--skip-checks", action="store_true", help="don't run go vet / go test")
    a = ap.parse_args()

    os.chdir(git("rev-parse", "--show-toplevel").strip())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("chunk-*.md"):
        old.unlink()

    target = os.path.normpath(a.target)
    pathspec = [target] if target != "." else ["."]
    warnings = []
    meta = {"mode": a.mode, "target": target, "base": a.base, "chunk_chars": a.chunk_chars}

    if a.mode == "pr":
        if run(["git", "rev-parse", "--verify", "--quiet", a.base])[0]:
            sys.exit(f"base ref {a.base!r} not found; check out with fetch-depth: 0")
        rng = f"{a.base}...HEAD"
        files = [f for f in git("diff", "--name-only", "--diff-filter=AMR", rng, "--", *pathspec).splitlines()
                 if f.endswith(".go")]
        meta["diffstat"] = git("diff", "--stat", rng, "--", *pathspec).rstrip()
        diffs = {f: git("diff", "-U3", rng, "--", f) for f in files}
    else:
        files = [f for f in git("ls-files", "--", *pathspec).splitlines()
                 if f.endswith(".go") and "/vendor/" not in f"/{f}" and "/testdata/" not in f"/{f}"]
        diffs = {}

    chunks = build_chunks(files, diffs, a.chunk_chars, warnings)
    meta["chunks"] = []
    for i, c in enumerate(chunks, 1):
        name = f"chunk-{i:02d}.md"
        print(f"{name}: {', '.join(c['files'])} (~{len(c['text']) // 3} tokens)", file=sys.stderr, flush=True)
        header = f"Packages: {', '.join(c['packages'])}\n\n"
        (out / name).write_text(header + c["text"])
        meta["chunks"].append({"file": name, "packages": c["packages"], "files": c["files"],
                               "chars": len(header) + len(c["text"])})
    meta["files"] = files

    if a.skip_checks:
        meta["vet_rc"] = meta["test_rc"] = None
    else:
        meta["vet_rc"] = check(["go", "vet", "./..."], out / "vet.txt")
        meta["test_rc"] = check(["go", "test", "-count=1", "./..."], out / "test.txt")

    meta["warnings"] = warnings
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"{a.mode} mode: {len(files)} Go file(s) in {len(chunks)} chunk(s); "
          f"vet rc={meta['vet_rc']} test rc={meta['test_rc']} -> {out}")
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
