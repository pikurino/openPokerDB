#!/usr/bin/env python3
"""Make regex string literals Python 3 clean (no invalid-escape SyntaxWarnings).

Usage:  python tools/fix_regex_escapes.py <file.py> [...]

For each non-raw single- or multi-line string literal that contains regex backslash
escapes (`\\s`, `\\d`, `\\W` ...):
  * if the literal contains no *real* Python escape (\\n, \\t, \\xNN, ...), it gets an `r`
    prefix -- the usual case for a regex;
  * otherwise the regex backslashes are doubled, so `"\\n\\n\\n\\*{2}\\s"` keeps matching
    three newlines instead of turning them into literal backslash-n.

Rewrites are applied by (row, col) span, so two literals on one line never overlap. Prints
the per-file count of rewritten literals. Idempotent: already-raw literals are skipped.
"""
import io
import re
import sys
import tokenize

VALID_ESCAPE = set("nrtfvabBxXuU01234567" + chr(92) + "'" + '"' + "\n")
QUOTES = ("'''", '"""', "'", '"')
# Escapes whose regex meaning equals their string meaning, so prefixing `r` is safe.
# `\b` is absent on purpose: a backspace byte in a string is a word boundary in a regex.
RAW_SAFE = set("nrtfva'\"" + "\n")


def plan(src):
    edits = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.STRING:
            continue
        s = tok.string
        prefix = re.match("[rRbBuUfF]*", s).group(0)
        if "r" in prefix.lower() or "b" in prefix.lower():
            continue
        rest = s[len(prefix):]
        quote = next((q for q in QUOTES if rest.startswith(q)), None)
        if not quote or not rest.endswith(quote) or rest == quote:
            continue
        body = rest[len(quote):-len(quote)]
        multiline = tok.start[0] != tok.end[0]
        seqs = [c for c, in re.findall(r"\\(.)", body)]
        invalid = [c for c in seqs if c not in VALID_ESCAPE]
        if not invalid:
            continue
        has_real = [c for c in seqs if c in VALID_ESCAPE and c != "\n"]
        if has_real and multiline and not set(has_real) <= RAW_SAFE:
            continue  # ambiguous; leave for a human
        if has_real and not (multiline and set(has_real) <= RAW_SAFE):
            keep = "".join(re.escape(c) for c in VALID_ESCAPE if c != "\n")
            new_body = re.sub(r"\\([^%s])" % keep, lambda m: chr(92) * 2 + m.group(1), body)
            new = prefix + quote + new_body + quote
        else:
            new = "r" + s
        if new != s:
            edits.append((tok.start[0], tok.start[1], tok.end[1], tok.end[0], new))
    return edits


def main():
    counts = {}
    for f in sys.argv[1:]:
        src = open(f, encoding="utf-8").read()
        edits = plan(src)
        if not edits:
            counts[f] = 0
            continue
        lines = src.splitlines(keepends=True)
        for row, col, endcol, endrow, new in sorted(edits, key=lambda e: (-e[0], -e[1])):
            first = lines[row - 1]
            last = lines[endrow - 1]
            end = endcol if endcol > 0 else len(last.rstrip("\r\n"))
            lines[row - 1:endrow] = [first[:col] + new + last[end:]]
        open(f, "w", encoding="utf-8", newline="").write("".join(lines))
        counts[f] = len(edits)
    print("rewritten:", counts, "total", sum(counts.values()))


if __name__ == "__main__":
    main()
