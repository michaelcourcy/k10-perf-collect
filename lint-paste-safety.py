#!/usr/bin/env python3
"""Flag comment lines inside pasted bash blocks that misbehave in default zsh.

zsh has INTERACTIVE_COMMENTS off, so a pasted '#' line is parsed as a command.
Comment text inside a quoted heredoc (<<'EOF') is literal, so it is safe.

Severity:
  HANG    unbalanced quote -> shell sits at a quote> prompt, looks like a frozen paste
  EXEC    $(...) or backticks -> the comment's contents are actually executed
  PARSE   redirect or subshell metachar -> zsh: parse error
"""
import pathlib, re, sys

def classify(s):
    out = []
    if s.count("'") % 2 or s.count('"') % 2:
        out.append(("HANG", "unbalanced quote"))
    if "$(" in s or "`" in s:
        out.append(("EXEC", "command substitution is evaluated"))
    m = re.search(r"[<>()]", s)
    if m:
        out.append(("PARSE", f"metachar {m.group()}"))
    if re.search(r"[|&]", s):
        out.append(("PARSE", "pipeline or background operator"))
    if re.search(r";\s*(do|done|then|fi|esac|else|elif)\b", s):
        out.append(("PARSE", "keyword in command position"))
    return out

rows = []
for f in sorted(pathlib.Path("guides").glob("*.md")) + [pathlib.Path("README.md")]:
    inblock = heredoc = False
    for n, line in enumerate(f.read_text().splitlines(), 1):
        if line.startswith("```"):
            inblock = line.startswith("```bash") or line.startswith("```sh"); heredoc = False; continue
        if not inblock: continue
        if re.search(r"<<'\w+'", line): heredoc = True; continue
        if heredoc:
            if re.match(r"^\w+$", line.strip()): heredoc = False
            continue
        s = line.lstrip()
        if s.startswith("#"):
            for sev, why in classify(s):
                rows.append((sev, f, n, why, s))

order = {"HANG": 0, "EXEC": 1, "PARSE": 2}
rows.sort(key=lambda r: order[r[0]])
for sev, f, n, why, s in rows:
    print(f"{sev:5} {f}:{n}  {why}\n      {s[:96]}")
counts = {k: sum(1 for r in rows if r[0] == k) for k in order}
print(f"\nHANG={counts['HANG']} EXEC={counts['EXEC']} PARSE={counts['PARSE']}")
print("HANG and EXEC must be fixed. PARSE is neutralised by 'setopt interactive_comments' (guide 00 section 1b).")
sys.exit(1 if counts["HANG"] or counts["EXEC"] else 0)
