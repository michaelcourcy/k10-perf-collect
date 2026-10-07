#!/usr/bin/env python3
"""Render an export-topology.json (from generate-export-topology.py) as one self-contained
HTML page: no external assets, works offline, dark mode follows the OS.

    ./render-export-topology.py export-topology.json -o export-topology.html

Layout: cluster header and KPI tiles -> warnings -> Helm limiters -> one card per policy
(profile, cadence, retention) -> one block per namespace (repository, maintenance,
ActionPodSpecs, exports) -> PVC table with an expandable row per PVC holding the
file-size histogram and the snapshot table.

Every chart has a table-view twin; every value shown in a bar is also in a table.
"""

import argparse
import datetime as dt
import html
import json
import re
import sys

# ------------------------------------------------------------------ formatting helpers

def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def fmt_bytes(n):
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PiB"


def fmt_num(n):
    return "–" if n is None else f"{n:,}"


def fmt_dur(s):
    if s is None:
        return "–"
    s = float(s)
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{s/60:.1f} min"
    if s < 86400:
        return f"{s/3600:.1f} h"
    return f"{s/86400:.1f} d"


def fmt_ts(s):
    if not s:
        return "–"
    return s[:19].replace("T", " ") + "Z"


def ts_seconds(s):
    """Epoch seconds from an ISO-8601 string (the generator writes '...Z', possibly with
    fractional seconds); None when absent or unparsable."""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def signed_bytes(n):
    if n is None:
        return "–"
    return ("+" if n > 0 else "") + fmt_bytes(n)


def ns_dur(ns):
    """Kopia durations are nanoseconds."""
    return fmt_dur(ns / 1e9) if ns else "–"


# ------------------------------------------------------------------ style (palette.md roles)

CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --good: #0ca30c; --critical: #d03b3b; --warning: #fab219; --serious: #ec835a;
  --good-text: #006300;
  --series-1: #2a78d6;   /* categorical slot 1: the histogram is one series */
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --good-text: #0ca30c;
    --series-1: #3987e5;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--plane); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1280px; margin: 0 auto; padding: 24px 20px 64px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 17px; font-weight: 600; margin: 32px 0 12px; }
h3 { font-size: 15px; font-weight: 600; margin: 20px 0 8px; }
.sub { color: var(--ink-2); font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 16px 18px; margin: 12px 0; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 16px 0 4px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .note { color: var(--muted); font-size: 12px; margin-top: 2px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px 10px; margin: 6px 0 2px; }
.chip { font-size: 12px; color: var(--ink-2); background: var(--plane); border: 1px solid var(--ring); border-radius: 12px; padding: 2px 9px; }
.chip b { color: var(--ink); font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
tr:last-child td { border-bottom: 0; }
details > summary { cursor: pointer; list-style: none; }
details > summary::-webkit-details-marker { display: none; }
summary.row { display: grid; grid-template-columns: 1.6fr 1.2fr 0.9fr 0.6fr 0.8fr 0.8fr 1.1fr 0.7fr; gap: 10px;
  padding: 8px 10px; border-bottom: 1px solid var(--grid); align-items: baseline; }
summary.row:hover { background: var(--plane); }
summary.row span.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.hdr { display: grid; grid-template-columns: 1.6fr 1.2fr 0.9fr 0.6fr 0.8fr 0.8fr 1.1fr 0.7fr; gap: 10px;
  padding: 6px 10px; color: var(--ink-2); font-size: 12px; font-weight: 600; border-bottom: 1px solid var(--grid); }
.hdr span.num { text-align: right; }
.detail { padding: 12px 10px 18px 28px; background: var(--plane); border-bottom: 1px solid var(--grid); }
.two { display: grid; grid-template-columns: minmax(280px, 1fr) 2fr; gap: 24px; align-items: start; }
@media (max-width: 900px) { .two { grid-template-columns: 1fr; } }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); }
.status i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.status.good i { background: var(--good); } .status.bad i { background: var(--critical); }
.status.warn i { background: var(--warning); } .status.none i { background: var(--axis); }
.muted { color: var(--muted); }
.note { color: var(--ink-2); font-size: 12px; margin: 4px 0; }
figure { margin: 0; }
figcaption { font-size: 12px; color: var(--ink-2); margin-bottom: 8px; }
.bars { display: grid; grid-template-columns: 110px 1fr 70px; row-gap: 2px; column-gap: 8px; align-items: center; }
.bars .lab { font-size: 12px; color: var(--ink-2); text-align: right; font-variant-numeric: tabular-nums; }
.bars .track { position: relative; height: 18px; }
.bars .bar { height: 18px; border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .val { font-size: 12px; color: var(--ink); font-variant-numeric: tabular-nums; }
.bars .track::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; border-left: 1px solid var(--axis); }
.tv summary { font-size: 12px; color: var(--ink-2); margin-top: 8px; }
.warn-box { border-left: 3px solid var(--warning); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
footer { color: var(--muted); font-size: 12px; margin-top: 40px; }
"""

# ------------------------------------------------------------------ pieces

def ns_anchor(ns):
    """Stable id for a namespace card, so the summary at the top can jump straight to it."""
    key = "%s--%s" % (ns.get("name") or "", ((ns.get("repository") or {}).get("profile") or ""))
    return "ns-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-").lower()


def highlights_block(nss):
    """The two questions a reader of a 60-namespace report actually arrives with: what took
    longest, and what ate the most memory. Both are per ExportAction, and each row links to
    the namespace card it belongs to - otherwise finding it means scrolling past everything."""
    rows = []
    for n in nss:
        for e in n.get("exports") or []:
            dm = e.get("datamover") or {}
            rows.append({"ns": n.get("name"), "anchor": ns_anchor(n),
                         "profile": (n.get("repository") or {}).get("profile"),
                         "start": e.get("startTime"), "dur": e.get("durationSeconds"),
                         "mem": dm.get("peakSumMemoryBytes"), "state": e.get("state"),
                         "pvcs": len((e.get("source") or {}).get("pvcs") or [])})
    if not rows:
        return ""

    def tbl(key, label, fmt):
        top = [r for r in rows if r.get(key) is not None]
        top.sort(key=lambda r: r[key], reverse=True)
        top = top[:5]
        if not top:
            return (f'<div><figcaption>{esc(label)}</figcaption>'
                    f'<div class="note">no {esc(label.lower())} available</div></div>')
        return (f'<div><figcaption>{esc(label)}</figcaption>'
                + table(["Namespace", "Export start", label.split(" ")[-1].capitalize(), "PVCs"],
                        [[f'<a href="#{esc(r["anchor"])}">{esc(r["ns"])}</a>',
                          esc(fmt_ts(r["start"])), fmt(r[key]), fmt_num(r["pvcs"])]
                         for r in top], num_cols=(2, 3))
                + '</div>')

    return ('<details class="card" open><summary><b>Where to look first</b> '
            f'<span class="sub">the 5 slowest and the 5 heaviest ExportActions of '
            f'{len(rows)} — click a namespace to jump to it</span></summary>'
            '<div class="two">'
            + tbl("dur", "Longest exports by duration", lambda v: esc(fmt_dur(v)))
            + tbl("mem", "Heaviest exports by peak memory", lambda v: esc(fmt_bytes(v)))
            + '</div>'
            '<div class="note">Duration is the ExportAction\'s own start to end. Peak memory is the '
            'peak of the sum over every datamover pod alive in that export\'s window, so it includes '
            'concurrent exports of other namespaces - the per-export cell on the namespace card '
            'breaks that down. An export with no datamover samples cannot be ranked by memory and '
            'is left out of the right-hand table.</div></details>')


def tile(label, value, note=None):
    return (f'<div class="tile"><div class="label">{esc(label)}</div>'
            f'<div class="value">{esc(value)}</div>'
            + (f'<div class="note">{esc(note)}</div>' if note else "") + "</div>")


def chip(label, value):
    return f'<span class="chip">{esc(label)} <b>{esc(value)}</b></span>'


def status(kind, text):
    return f'<span class="status {kind}"><i></i>{esc(text)}</span>'


def maintenance_status(m):
    if not m:
        return status("none", "no maintenance info")
    last = m.get("lastRun") or {}
    if last.get("success") is False or m.get("anyFailure"):
        return status("bad", f"failed · {fmt_ts(last.get('end'))} · {last.get('task','')}")
    if last.get("end"):
        return status("good", f"ok · {fmt_ts(last.get('end'))} · {last.get('task','')}")
    return status("none", "never run")


def table(headers, rows, num_cols=()):
    """headers: list of str, or (text, tooltip) for a column that needs explaining; rows:
    list of list of (already escaped) cell html.

    Headers stay escaped - passing raw HTML in to get a tooltip printed the markup
    verbatim in the header cell."""
    def th_cell(i, h):
        tip = ""
        if isinstance(h, (tuple, list)):
            h, tip = h[0], h[1]
        cls = "num" if i in num_cols else ""
        title = f' title="{esc(tip)}"' if tip else ""
        return f'<th class="{cls}"{title}>{esc(h)}</th>'
    th = "".join(th_cell(i, h) for i, h in enumerate(headers))
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f'<td class="{"num" if i in num_cols else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>"
    return f'<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


BUCKET_LABELS = [("lt4KiB", "< 4 KiB"), ("4KiB_64KiB", "4 KiB – 64 KiB"), ("64KiB_1MiB", "64 KiB – 1 MiB"),
                 ("1MiB_16MiB", "1 – 16 MiB"), ("16MiB_256MiB", "16 – 256 MiB"), ("gt256MiB", "> 256 MiB")]


def histogram_figure(h, pvc_name):
    """One series (file count per size bucket) -> one colour, slot 1. The bucket order is
    already on the axis, and a value ramp on bars is the classic anti-pattern; a 6-step
    ordinal ramp from the documented blue steps also cannot pass the validator (no six
    steps with dL >= 0.06 above the 2:1 light-end floor). Bars <= 24px, rounded data-end
    only, values at the tip - six bars, all labelled, since that is the whole reading -
    and a table-view twin."""
    if not h or "buckets" not in h:
        note = (h or {}).get("skipped") or (h or {}).get("error") or "histogram not collected"
        return f'<div class="note">{esc(note)}</div>'
    b = h["buckets"]
    mx = max((b[k]["files"] for k, _ in BUCKET_LABELS), default=0) or 1
    rows = []
    for i, (k, lab) in enumerate(BUCKET_LABELS):
        files, byts = b[k]["files"], b[k]["bytes"]
        w = 100.0 * files / mx
        tip = f"{lab}: {fmt_num(files)} files, {fmt_bytes(byts)}"
        rows.append(
            f'<div class="lab">{esc(lab)}</div>'
            f'<div class="track"><div class="bar" style="width:{w:.1f}%;background:var(--series-1)" title="{esc(tip)}" tabindex="0" role="img" aria-label="{esc(tip)}"></div></div>'
            f'<div class="val">{fmt_num(files)}</div>'
        )
    tv = table(["Bucket", "Files", "Bytes"],
               [[esc(lab), fmt_num(b[k]["files"]), fmt_bytes(b[k]["bytes"])] for k, lab in BUCKET_LABELS],
               num_cols=(1, 2))
    n = h.get("fileCount") or 0
    stats = (f'{fmt_num(n)} file{"" if n == 1 else "s"} · median {fmt_bytes(h.get("medianBytes"))} · '
             f'largest {fmt_bytes(h.get("maxBytes"))}')
    return (f'<figure><figcaption>File-size distribution of the last snapshot — {esc(stats)}</figcaption>'
            f'<div class="bars">{"".join(rows)}</div>'
            f'<details class="tv"><summary>Table view</summary>{tv}</details></figure>')


def dm_cell(dm):
    if not dm:
        return '<span class="muted">–</span>'
    if dm.get("note") and not dm.get("samples"):
        return f'<span class="muted" title="{esc(dm.get("note"))}">no samples</span>'
    mem = fmt_bytes(dm.get("peakSumMemoryBytes"))
    cpu = dm.get("cpuSecondsTotal")
    own_pods = [p for p in dm.get("pods") or [] if p.get("own", True)]
    pods = ", ".join(f'{p["pod"]}→{p.get("appNamespace") or "?"}' for p in own_pods)
    conc = dm.get("concurrent") or {}
    nos = dm.get("podsWithoutSamples") or []
    avg = dm.get("avgCpuCores")
    ws, we = ts_seconds(dm.get("windowStart")), ts_seconds(dm.get("windowEnd"))
    win = (we - ws) if (ws is not None and we is not None) else 0
    tip = (f"memory: peak of the sum over all datamover pods alive in the window. "
           f"CPU: {cpu if cpu is not None else '–'} CPU-seconds consumed by those pods in total"
           + (f" over {fmt_dur(win)}, i.e. {avg:.2f} cores on average" if avg is not None and win else "")
           + f". {len(own_pods)} pods of this namespace with samples: {pods}")
    if conc:
        tip += (f". Concurrently in the same window: {len(conc.get('pods') or [])} datamover pods of "
                f"{', '.join(conc.get('namespaces') or [])} — all datamovers together peaked at "
                f"{fmt_bytes(conc.get('allDatamoversPeakSumMemoryBytes'))} and used {conc.get('allDatamoversCpuSecondsTotal', 0):.0f} cpu-s")
    if dm.get("unattributedIncluded"):
        tip += f". Included without attribution: {', '.join(dm['unattributedIncluded'])}"
    if nos:
        tip += f" · {len(nos)} without samples: {', '.join(nos)}"
    cpu_txt = (f'{avg:.2f} cores avg <span class="muted">({cpu:.0f} cpu-s)</span>' if avg is not None and cpu is not None
               else f'{cpu if cpu is not None else "–"} cpu-s')
    conc_txt = (f' <span class="muted">· +{len(conc.get("pods") or [])} concurrent ({", ".join(conc.get("namespaces") or [])})</span>'
                if conc else "")
    return (f'<span title="{esc(tip)}">{esc(mem)} peak · {cpu_txt}'
            f' · {dm.get("samples", 0)} samples{conc_txt}</span>')


def snapshots_table(snaps):
    rows = []
    for s in snaps:
        # for a block-mode snapshot "files" are chunks - say so in the cell
        files_cell = fmt_num(s.get("fileCount")) + (' <span class="muted">chunks (block mode)</span>' if s.get("mode") == "block" else "")
        # files Kopia actually hashed in this run - the per-PVC change indicator
        fh = s.get("filesHashed")
        fu = s.get("filesUnchanged")
        hashed_cell = ('<span class="muted">–</span>' if fh is None else
                       f'<span title="files Kopia read and hashed in this run'
                       f'{f"; {fmt_num(fu)} skipped as unchanged against the previous snapshot" if fu is not None else ""}">{fmt_num(fh)}</span>')
        phys = s.get("physicalIngestBytes")
        if phys is None:
            phys_cell = f'<span class="muted" title="{esc(s.get("physicalIngestNote"))}">n/a</span>'
        elif s.get("physicalIngestAmbiguousBytes"):
            # part of it was written while a sibling PVC was also exporting: a guess, say so
            phys_cell = f'<span title="{esc(s.get("physicalIngestNote"))}">~{fmt_bytes(phys)}</span>'
        else:
            phys_cell = fmt_bytes(phys)
        ea = s.get("exportAction")
        start_cell = (f'<span title="ExportAction {esc(ea)}">{esc(fmt_ts(s.get("startTime")))}</span>' if ea else
                      f'<span title="{esc(s.get("exportActionNote") or "")}">{esc(fmt_ts(s.get("startTime")))}'
                      f'{" <span class=muted>· no action</span>" if "exportAction" in s else ""}</span>')
        rows.append([
            start_cell,
            esc(fmt_dur(s.get("durationSeconds"))),
            files_cell,
            hashed_cell,
            fmt_bytes(s.get("totalSizeBytes")),
            esc(signed_bytes(s.get("logicalDeltaBytes"))) if "logicalDeltaBytes" in s else '<span class="muted">first</span>',
            phys_cell,
            dm_cell(s.get("datamover")),
        ])
    return table(["Start", "Duration", "Files", "Hashed", "Logical size", "Δ logical", "Physical ingest", "Datamovers (sum over window)"],
                 rows, num_cols=(2, 3, 4, 5, 6))


def in_progress_block(ip):
    """A snapshot still being uploaded, seen through Kopia's 45-minute checkpoints."""
    if not ip:
        return ""
    rate = ""
    if ip.get("filesPerSecond"):
        rate = f' · ≈ {ip["filesPerSecond"]:.0f} files/s'
    elif ip.get("bytesPerSecond"):
        rate = f' · ≈ {fmt_bytes(ip["bytesPerSecond"])}/s'
    return (f'<div class="note" style="border-left:3px solid var(--warning);padding-left:8px">'
            f'<b>Export in progress</b> since {esc(fmt_ts(ip.get("startTime")))}: {fmt_num(ip.get("filesSoFar"))} files · '
            f'{fmt_bytes(ip.get("bytesSoFar"))} uploaded at the last checkpoint ({esc(fmt_ts(ip.get("lastCheckpointTime")))}, '
            f'checkpoint {ip.get("checkpoints")}{rate}). Kopia checkpoints are written every 45 min so an interrupted upload '
            f'can resume; they are not restore points and are excluded from the counts.</div>')


def unopenable_detail(o):
    """Why the repository would not open, and what is therefore missing. Shared by both
    banners: the reason is independent of whether the namespace is frozen."""
    if o.get("openable") is not False:
        return ""
    out = ""
    skipped = o.get("repoCheckerSkipped")
    if o.get("diagnosis"):
        out += (f'<br><b>{"Why it was not opened" if skipped else "Why it would not open"}:</b> '
                f'{esc(o["diagnosis"])}.')
        if o.get("innermostCause"):
            out += (f'<br><span class="muted"><b>Innermost cause:</b> '
                    f'<code>{esc((o.get("innermostCause") or "")[:400])}</code></span>')
        if o.get("hint"):
            out += f'<br><span class="muted">{esc(o["hint"])}</span>'
    # The Location Profile's own validation error is usually the only place the real cause
    # is named - repo_checker reports neither the certificate nor the credential.
    pv = o.get("profileValidation")
    if pv and pv != "Success":
        out += (f'<br><b>The Location Profile <code>{esc(o.get("profile"))}</code> is itself in '
                f'validation state {esc(pv)}.</b>')
        if o.get("profileValidationCause"):
            out += (f'<br><span class="muted"><b>Profile cause:</b> '
                    f'<code>{esc((o.get("profileValidationCause") or "")[:500])}</code></span>')
        if o.get("note2"):
            out += f'<br><span class="muted">{esc(o["note2"])}</span>'
    out += ('<br><span class="muted">Not read here. Everything below is reconstructed from '
            '<code>restorepointcontents/&lt;name&gt;/details</code>: the PVC, its storage class, '
            'the Kopia snapshot id, the file count and the sizes. Object counts, dedup ratio and '
            'the file-size histogram would need the repository to be opened.</span>')
    cr = o.get("storageRepository") or {}
    if cr.get("lastProcessedAt"):
        out += ('<br><span class="muted"><b>The repository itself is fine.</b> K10 last ran '
                f'<code>{esc(cr.get("lastProcedure"))}</code> on it at '
                f'{esc(fmt_ts(cr.get("lastProcessedAt")))}'
                + (" successfully" if cr.get("lastProcedureSucceeded") else "")
                + f' ({fmt_num(cr.get("processCount"))} operations in total), so K10 opens and '
                  'maintains it.'
                + ('' if skipped else ' What is missing is a way for repo_checker to reach it.')
                + '</span>')
    return out


def orphan_banner(o):
    """Why the export policies alone would not have found this namespace, and whether its
    figures are current. Three independent states, and the reason it would not open has to
    show in all of them - a live namespace whose repository fails on TLS is the case that
    matters most and the one an "is it orphaned?" test misses."""
    if not o:
        return ""
    gone = o.get("orphanedBy") or []
    detail = unopenable_detail(o)
    box = 'class="note" style="border-left:3px solid var(--warning);padding-left:8px"'
    st = o.get("detailStats") or {}
    read = (f' <span class="muted">({st.get("restorePointsRead")} of '
            f'{st.get("restorePointsAvailable")} restore points read)</span>' if st else "")

    if gone:
        return (f'<div {box}><b>Frozen — no longer exported.</b> The {esc(" and ".join(gone))} no '
                f'longer exists, so this namespace stopped at its last restore point on '
                f'{esc(fmt_ts(o.get("frozenSince")))} ({fmt_num(o.get("restorePoints"))} restore '
                f'points, policy <code>{esc(o.get("policy"))}</code>){read}. The figures below are '
                f'history, not a current state, and the data still occupies the object '
                f'store.{detail}</div>')
    if o.get("openable") is False:
        skipped = o.get("repoCheckerSkipped")
        return (f'<div {box}><b>The repository {"was not opened" if skipped else "could not be opened"}.</b> '
                f'This namespace exported '
                f'{fmt_num(o.get("restorePoints"))} restore points here under policy '
                f'<code>{esc(o.get("policy"))}</code>, most recently '
                f'{esc(fmt_ts(o.get("newest")))}{read} — so the data is there and the exports are '
                f'working.{"" if skipped else " Only this tool could not read it."}{detail}</div>')
    return (f'<div {box}><b>Not on the policy\'s current profile.</b> This namespace exported '
            f'{fmt_num(o.get("restorePoints"))} restore points here under policy '
            f'<code>{esc(o.get("policy"))}</code>, most recently {esc(fmt_ts(o.get("newest")))}, '
            f'but the policy does not target this profile any more. The data is live and read '
            f'normally - it is simply not where the policy now points, so the export policies '
            f'alone would not have found it.</div>')


def reconstructed_runs_block(dm):
    """Datamover usage for a deleted namespace. The pods ran in the K10 namespace, so
    cAdvisor and kube-state-metrics still have them; only the ExportAction windows were
    lost, and those are rebuilt from the pods' own scrape spans."""
    if not dm:
        return ""
    if dm.get("error"):
        return f'<div class="note">Datamover metrics unavailable: {esc(dm["error"])}</div>'
    runs = dm.get("runs") or []
    if not runs:
        return f'<div class="note">{esc(dm.get("note") or "no datamover metrics for this namespace")}</div>'
    # chronological, newest first - the same order as every other table in the report
    top = sorted(runs, key=lambda r: r.get("windowStart") or "", reverse=True)[:12]
    # the count alone left "what are these pods?" unanswerable - name them
    rows = [[esc(fmt_ts(r.get("windowStart"))), esc(fmt_dur(r.get("durationSeconds"))),
             (f'<span title="{esc(", ".join(r.get("pods") or []))}">'
              f'{fmt_num(len(r.get("pods") or []))}</span>'),
             fmt_bytes(r.get("peakSumMemoryBytes")),
             f'{r.get("cpuSecondsTotal")} <span class="muted">cpu-s</span>',
             (f'{r["avgCpuCores"]} <span class="muted">cores avg</span>'
              if r.get("avgCpuCores") is not None
              else f'<span class="muted" title="{esc(r.get("note") or "")}">n/a</span>')]
            for r in top]
    more = f", {len(top)} most recent shown" if len(runs) > len(top) else ""
    return ('<figcaption style="margin-top:12px">Datamover runs reconstructed from the pods '
            f'({len(runs)} found{more})</figcaption>'
            + table(["Window start", "Duration",
                      ("Pods", "how many distinct datamover pods were alive in this window - "
                               "hover a number to see their names. A run has one data-mover-svc "
                               "pod plus one copy-vol-data pod per PVC it exports."),
                      "Peak memory", "CPU", "Average"],
                    rows, num_cols=(2, 3, 4, 5))
            + f'<div class="note">{esc(dm.get("note") or "")} Each figure is a floor: a pod alive '
              'for less than one scrape interval leaves no sample, and the window is the span over '
              'which the pods were scraped, not the ExportAction\'s own start and end. '
              'Pods are attributed per job, not per PVC: <code>copy-vol-data-*</code> pods carry only '
              'the job id and the clone they read does not name its source PVC, so a pod cannot be '
              'tied to the one PVC it exported.</div>')


def details_snapshots_table(snaps):
    """Restore points of a repository that could not be opened. Different columns from the
    Kopia-derived table: there is an upload END time but no start, no duration, no hashed
    count and no physical ingest attribution - only what K10 recorded per artifact."""
    rows = []
    for s in snaps:
        rows.append([
            esc(fmt_ts(s.get("endTime"))),
            f'<code class="muted">{esc((s.get("kopiaSnapshotId") or "–")[:16])}</code>',
            fmt_num(s.get("fileCount")),
            fmt_bytes(s.get("totalSizeBytes")),
            fmt_bytes(s.get("physicalBytes")),
            f'<span class="muted">{esc(s.get("restorePointContent") or "")}</span>',
        ])
    return table(["Upload ended", "Kopia snapshot", "Files", "Logical size", "Physical", "Restore point"],
                 rows, num_cols=(2, 3, 4))


def pvc_row(p):
    cr = p.get("lastChangeRate") or {}
    from_details = p.get("source") == "restorepointcontent details"
    cr_tip = ""
    if cr.get("logicalDeltaBytes") is not None:
        cr_txt = signed_bytes(cr.get("logicalDeltaBytes"))
        if cr.get("physicalIngestBytesPerDay") is not None:
            cr_txt += f" · {fmt_bytes(cr['physicalIngestBytesPerDay'])}/day physical"
    elif from_details:
        # a bare "–" here reads as "nothing changed"; it means "not measurable from here"
        cr_txt = "needs the repository"
        cr_tip = ("the change rate is the delta between two Kopia snapshots, which only the "
                  "repository holds. It could not be opened, so this is not known - see the "
                  "banner above this table for why.")
    else:
        cr_txt = "n/a · 1 snapshot" if (p.get("snapshotCount") or 0) < 2 else cr.get("note", "–")
    lm = p.get("lastMaintenance") or {}
    lm_kind = "bad" if lm.get("anyTaskFailed") or lm.get("success") is False else ("good" if lm.get("end") else "none")
    summary = (
        f'<summary class="row">'
        f'<span title="{esc(p.get("sourceHost"))}"><b>{esc(p["name"])}</b>'
        + (f'<br><span class="muted">{esc(p.get("storageClass") or "")} {esc(p.get("accessMode") or "")}</span>' if p.get("storageClass") else "")
        + f'</span>'
        f'<span class="muted">{esc(p.get("workload") or "–")}</span>'
        + ((f'<span class="num" title="block-mode disk with no allocated block: {fmt_bytes(int(p.get("requestedStorage") or 0))} requested, nothing written to it">'
            f'<span class="muted">empty disk</span></span>' if not p.get("chunkCount") else
            f'<span class="num" title="VM disk stored in block mode: Kopia snapshots the raw device as fixed-size chunks, there are no files to count">'
            f'{fmt_num(p.get("chunkCount"))} <span class="muted">chunks of {fmt_bytes(p.get("blockSizeBytes"))}</span></span>')
           if p.get("mode") == "block" else f'<span class="num">{fmt_num(p.get("fileCount"))}</span>')
        + f'<span class="num">{fmt_num(p.get("snapshotCount"))}</span>'
        f'<span class="num">{esc(fmt_bytes(p.get("totalSizeBytes")))}</span>'
        f'<span class="num">{esc(fmt_bytes(p.get("averageFileSizeBytes")))}</span>'
        f'<span class="muted" title="{esc(cr_tip)}">{esc(cr_txt)}</span>'
        + (f'<span class="muted" title="per-PVC maintenance state comes from the repository, '
           f'which could not be opened">needs the repository</span>' if from_details else
           f'<span>{status(lm_kind, "ok" if lm_kind == "good" else ("failed" if lm_kind == "bad" else "–"))}</span>')
        + '</summary>'
    )
    notes = []
    if not p.get("existsOnCluster"):
        notes.append("PVC no longer exists on the cluster (resolved from the repository)")
    if p.get("resolvedBy") == "structural":
        notes.append("PVC name parsed structurally from the Kopia host, not matched to a live PVC")
    if p.get("source") == "restorepointcontent details":
        detail = (
            f'<div class="detail">'
            f'<figcaption>Restore points ({fmt_num(p.get("snapshotCount"))})</figcaption>'
            f'{details_snapshots_table(p.get("snapshots") or [])}'
            + "".join(f'<div class="note">{esc(n)}</div>' for n in notes)
            + '<div class="note">Described from the restore point details: the repository could not '
              'be opened, so there is no file-size histogram, no hashed/unchanged count and no '
              'per-snapshot physical ingest attribution.</div></div>')
        return f"<details>{summary}{detail}</details>"
    detail = (
        f'<div class="detail"><div class="two">'
        f'<div>{histogram_figure(p.get("sizeHistogram"), p["name"])}</div>'
        f'<div><figcaption>Snapshots ({fmt_num(p.get("snapshotCount"))})</figcaption>{in_progress_block(p.get("inProgress"))}'
        f'{snapshots_table(p.get("snapshots") or []) if p.get("snapshots") else ""}</div>'
        f'</div>'
        + "".join(f'<div class="note">{esc(n)}</div>' for n in notes)
        + (f'<div class="note">Change rate: {esc(cr.get("note"))}</div>' if cr.get("note") else "")
        + f'</div>'
    )
    return f"<details>{summary}{detail}</details>"


def exports_table(exports):
    # a note on an export means its change rate is unknown rather than zero
    if not exports:
        return '<div class="note">No ExportAction for this namespace and policy on the cluster — history may have been retired.</div>'
    rows = []
    for e in exports:
        w = e.get("written") or {}
        src = e.get("source") or {}
        kp = e.get("k10Progress") or {}
        if e.get("exportedBytes") is not None:
            tip = (f'K10 progress: read {fmt_bytes(kp.get("readBytes"))} · processed {fmt_bytes(kp.get("processedBytes"))} · '
                   f'transferred {fmt_bytes(kp.get("transferredBytes"))} of {fmt_bytes(kp.get("totalBytes"))} capacity · '
                   f'{fmt_bytes(kp.get("processingRate"))}/s · {kp.get("completedVolumes")}/{kp.get("totalVolumes")} volumes')
            exported = f'<span title="{esc(tip)}">{fmt_bytes(e["exportedBytes"])} <span class="muted">· K10</span></span>'
        elif "packBytes" in w:
            exported = (f'<span title="from Kopia pack-blob timestamps; no K10 progress for this export">'
                        f'{fmt_bytes(w["packBytes"])} <span class="muted">· Kopia, {fmt_num(w["packObjects"])} objects</span></span>')
        else:
            # w["note"] explains it: contents re-stamped by maintenance, or the repository
            # never opened. Either way this is "not known", never a zero.
            exported = (f'<span class="muted" title="{esc(w.get("note") or e.get("detailsError") or "")}">'
                        f'unknown</span>')
        if src.get("logicalBytes"):
            n = len(src.get("pvcs") or [])
            source = f'{fmt_bytes(src["logicalBytes"])} <span class="muted">· {n} PVC{"s" if n != 1 else ""}</span>'
        else:
            source = '<span class="muted">–</span>'
        cr = e.get("changeRate")
        if isinstance(cr, (int, float)):
            rate = f'<span title="{esc(e.get("changeRateBasis") or "")}">{cr * 100:.1f}%</span>'
        elif e.get("changeRateNote"):
            # unknown, not zero - say which, because 0 % reads as "nothing changed"
            rate = f'<span class="muted" title="{esc(e["changeRateNote"])}">unknown</span>'
        else:
            rate = '<span class="muted">–</span>' 
        rows.append([esc(fmt_ts(e.get("startTime"))), esc(fmt_dur(e.get("durationSeconds"))),
                     esc(e.get("state")), exported, source, rate, dm_cell(e.get("datamover"))])
    return (table(["Export start", "Duration", "State", "Exported", "Source (logical)", "Change rate",
                   "Datamovers (sum over window)"], rows, num_cols=(1, 3, 4, 5))
            + '<div class="note">Exported = bytes transferred to the object store as counted by K10 '
              '(ExportAction /details; hover for read/processed/rate), or from Kopia pack-blob timestamps when K10 has no '
              'progress. Change rate = Exported ÷ logical size of the PVCs exported. 100% is a first export with nothing '
              'deduplicated or compressed; dedup against earlier snapshots and compression pull it down, encryption adds '
              'a little. Not a true change rate, but the best single figure available.</div>')


def namespace_block(ns):
    repo = ns.get("repository") or {}
    m = repo.get("maintenance") or {}
    orphan = ns.get("orphan")
    if repo.get("openable") is False:
        # nothing was read from the repository: show only what the CRs and the restore point
        # details know, rather than a row of "–" that looks like missing data
        chips = [chip("repository", repo.get("name"))]
        loc = repo.get("location") or {}
        if loc.get("bucket"):
            chips.append(chip("bucket", loc["bucket"]))
        if loc.get("prefix"):
            chips.append(chip("path", loc["prefix"]))
        chips.append(chip("restore points", fmt_num((orphan or {}).get("restorePoints"))))
    else:
        chips = [
            chip("repository", repo.get("name")),
            chip("objects on store", fmt_num(repo.get("objectCount"))),
            chip("stored", fmt_bytes(repo.get("objectBytes"))),
            chip("content physical", fmt_bytes(repo.get("contentPhysicalBytes"))),
            chip("content logical", fmt_bytes(repo.get("contentLogicalBytes"))),
        ]
    if repo.get("contentPhysicalBytes") and repo.get("contentLogicalBytes"):
        chips.append(chip("dedup+compression", f'{repo["contentPhysicalBytes"]/repo["contentLogicalBytes"]:.2f}×'))
    if repo.get("orphanedCount"):
        chips.append(chip("orphaned snapshots", fmt_num(repo["orphanedCount"])))
    aps = ns.get("actionPodSpecs") or []
    aps_html = ('<div class="note">ActionPodSpecs: none bound to this namespace — datamovers run with '
                'the defaults (no resource requests or limits).</div>' if not aps else
                '<div class="note">ActionPodSpecs: ' + ", ".join(f'<code>{esc(a.get("actionPodSpec"))}</code>' for a in aps) + '</div>')
    # "Snaps" was read as "Kopia clones that should have been cleaned up after the export".
    # They are the restore points themselves and are meant to persist until retention
    # expires; the transient clone the datamover reads is a different object and is deleted
    # with the export. Spell it out in the header and the tooltip.
    hdr = ('<div class="hdr"><span>PVC</span><span>Workload</span><span class="num">Files</span>'
           '<span class="num" title="Kopia snapshots of this PVC held in the export repository, '
           'i.e. its restore points. They persist until K10 retention expires them - they are not '
           'the temporary clone the datamover reads, which is deleted when the export finishes.">'
           'Restore points</span>'
           '<span class="num">Logical size</span><span class="num">Avg file</span><span>Last change</span><span>Maintenance</span></div>')
    restamp = repo.get("contentsRestampedAt")
    restamp_note = (f'<div class="note">Contents re-stamped by full maintenance at {esc(fmt_ts(restamp))}: '
                    f'physical ingest of snapshots before that is not recoverable.</div>' if restamp else "")
    created = ns.get("createdAt") or (orphan or {}).get("namespaceCreatedAt")
    csrc = ns.get("createdAtSource") or (orphan or {}).get("namespaceCreatedAtSource") or ""
    created_txt = (f' · created {esc(fmt_ts(created))}' if created else "")
    return (
        f'<div class="card" id="{esc(ns_anchor(ns))}"><h3>Namespace <code>{esc(ns["name"])}</code> '
        f'<span class="sub">— {fmt_num(ns.get("pvcCount"))} PVCs · {fmt_num(ns.get("totalFileCount"))} files · '
        f'{esc(fmt_bytes(ns.get("totalSizeBytes")))}'
        f'<span title="{esc(csrc)}">{created_txt}</span></span></h3>'
        f'<div class="chips">{"".join(chips)}</div>'
        f'{orphan_banner(orphan)}'
        + ("" if repo.get("openable") is False else
           f'<div style="margin:6px 0 2px">Maintenance: {maintenance_status(m)}'
           f'<span class="muted"> · quick every {ns_dur((m.get("quick") or {}).get("interval"))}, full every {ns_dur((m.get("full") or {}).get("interval"))}, next full {esc(fmt_ts(m.get("nextFullMaintenance")))}</span></div>')
        + f'{restamp_note}{aps_html}'
        # The exports table needs no repository: ExportActions and their /details byte
        # counters are cluster objects. Hiding it when the repository would not open
        # removed the change rate - the headline figure - from a namespace that still had it.
        + f'<h3 style="margin-top:14px">Exports</h3>{exports_table(ns.get("exports"))}'
        + f'<h3 style="margin-top:14px">PVCs</h3>{hdr}'
        + "".join(pvc_row(p) for p in ns.get("pvcs") or [])
        + reconstructed_runs_block((orphan or {}).get("datamover"))
        + '</div>'
    )


def policy_card(p):
    prof = p.get("profile") or {}
    if prof.get("type") == "FileStore":
        target = f'FileStore · PVC {prof.get("fileStore", {}).get("claimName")} · path {prof.get("fileStore", {}).get("path")}'
    else:
        target = f'{prof.get("objectStoreType") or "?"} · {prof.get("bucket") or "?"} / {prof.get("prefix") or ""}' + \
                 (f' @ {prof.get("endpoint")}' if prof.get("endpoint") else "")
    ret = p.get("retention") or {}
    ret_txt = ", ".join(f'{k} {v}' for k, v in ret.items()) or "–"
    flags = []
    if p.get("paused"):
        flags.append(status("warn", "paused"))
    if p.get("validation") and p["validation"] != "Success":
        flags.append(status("bad", f"validation {p['validation']}"))
    if not p.get("existsOnCluster"):
        flags.append(status("warn", "policy no longer on cluster"))
    # a deleted policy has no spec left: say "unknown", not an empty chip
    unk = "unknown" if not p.get("existsOnCluster") else "–"
    chips = [chip("profile", prof.get("name") or unk), chip("snapshot", p.get("frequency") or unk),
             chip("selects", "VMs (KubeVirt)" if p.get("selectorKind") == "vm" else "namespaces"),
             chip("export", p.get("exportFrequency") or unk), chip("retention", ret_txt if ret else unk)]
    if p.get("subFrequency"):
        chips.append(chip("subFrequency", json.dumps(p["subFrequency"])))
    nns = len(p.get("namespaces") or [])
    npvc = sum(len(n.get("pvcs") or []) for n in p.get("namespaces") or [])
    return (
        f'<section class="card"><h2 style="margin-top:0">Policy <code>{esc(p["name"])}</code> '
        f'<span class="sub">— {nns} namespaces · {npvc} PVCs</span> {" ".join(flags)}</h2>'
        f'<div class="sub">{esc(target)}</div>'
        f'<div class="chips">{"".join(chips)}</div>'
        + "".join(namespace_block(n) for n in p.get("namespaces") or [])
        + '</section>'
    )


def pct_cell(pct, used, total):
    """used / total with the percentage; from 80% the figure is marked in the critical
    colour AND labelled, never colour alone. 80% because the kubelet starts evicting at
    90% used ephemeral storage by default (nodefs.available < 10%)."""
    if pct is None:
        return f'<span class="muted">{esc(used)} / {esc(total)}</span>' if total else '<span class="muted">–</span>'
    hot = pct >= 80
    return (f'{esc(used)} <span class="muted">/ {esc(total)}</span> · '
            + (f'<span style="color:var(--critical)"><b>{pct:.0f}%</b> high</span>' if hot else f'<b>{pct:.0f}%</b>'))


def nodes_block(nd):
    if not nd or not nd.get("items"):
        return ""
    rows = []
    for n in nd["items"]:
        c, m, e = n.get("cpu") or {}, n.get("memory") or {}, n.get("ephemeralStorage") or {}
        pressure = ", ".join(n.get("pressure") or []) or ("unschedulable" if n.get("unschedulable") else "")
        rows.append([
            f'<b>{esc(n["name"])}</b>' + (f'<br><span class="muted">{esc(n.get("kubeletVersion") or "")}</span>' if n.get("kubeletVersion") else ""),
            esc(", ".join(n.get("roles") or []) or "–") + (f'<br><span class="muted">{esc(n.get("instanceType"))}</span>' if n.get("instanceType") else ""),
            pct_cell(c.get("usedPctOfAllocatable"), f'{c["usedCores"]:.2f}' if c.get("usedCores") is not None else "–",
                     f'{c["allocatableCores"]:g} cores' if c.get("allocatableCores") else ""),
            pct_cell(m.get("usedPctOfAllocatable"), fmt_bytes(m.get("workingSetBytes")) if m.get("workingSetBytes") is not None else "–",
                     fmt_bytes(m.get("allocatableBytes")) if m.get("allocatableBytes") else ""),
            pct_cell(e.get("usedPct"), fmt_bytes(e.get("usedBytes")) if e.get("usedBytes") is not None else "–",
                     fmt_bytes(e.get("fsCapacityBytes") or e.get("capacityBytes")) if (e.get("fsCapacityBytes") or e.get("capacityBytes")) else ""),
            (f'<span style="color:var(--critical)"><b>{esc(pressure)}</b></span>' if pressure else '<span class="muted">none</span>'),
        ])
    t = nd.get("totals") or {}
    if t.get("nodes", 0) > 1:
        cpu_pct = 100 * t["cpuUsedCores"] / t["cpuAllocatableCores"] if t.get("cpuAllocatableCores") else None
        mem_pct = 100 * t["memoryWorkingSetBytes"] / t["memoryAllocatableBytes"] if t.get("memoryAllocatableBytes") else None
        eph_pct = 100 * t["ephemeralUsedBytes"] / t["ephemeralCapacityBytes"] if t.get("ephemeralCapacityBytes") else None
        rows.append([f'<b>All {t["nodes"]} nodes</b>', "",
                     pct_cell(cpu_pct, f'{t["cpuUsedCores"]:.2f}', f'{t["cpuAllocatableCores"]:g} cores'),
                     pct_cell(mem_pct, fmt_bytes(t["memoryWorkingSetBytes"]), fmt_bytes(t["memoryAllocatableBytes"])),
                     pct_cell(eph_pct, fmt_bytes(t["ephemeralUsedBytes"]), fmt_bytes(t["ephemeralCapacityBytes"])), ""])
    src = ", ".join(nd.get("usageSources") or []) or "no usage source"
    notes = "".join(f'<div class="note">{esc(x)}</div>' for x in nd.get("notes") or [])
    return (f'<details open class="card"><summary><b>Nodes at audit time</b> <span class="sub">{t.get("nodes", len(nd["items"]))} nodes · '
            f'usage sampled {esc(fmt_ts(nd.get("sampledAt")))} via {esc(src)}</span></summary>'
            + table(["Node", "Roles", "CPU used / allocatable", "Memory (working set) / allocatable",
                     "Ephemeral storage used / capacity", "Pressure"], rows, num_cols=(2, 3, 4))
            + '<div class="note">Usage is a single sample at generation time, not an average: run the generator while '
              'exports are in flight to see what datamovers do to the nodes. Ephemeral storage is the root filesystem the '
              'kubelet evicts on (default hard threshold: less than 10% available); datamover pods write their Kopia '
              'cache and, in filesystem mode, the buffered volume data there - "high" marks 80% or more.</div>'
            + notes + '</details>')


def limiters_block(lim):
    if not lim:
        return ""
    rows = [[f"<code>{esc(k)}</code>", esc(v)] for k, v in sorted(lim.items())]
    return (f'<details class="card"><summary><b>Helm / k10-config limiters</b> <span class="sub">{len(rows)} keys — '
            f'the effective datamover tuning</span></summary><div style="margin-top:10px">{table(["Key", "Value"], rows)}</div></details>')


def focus(t, policies=None, namespaces=None):
    """Cut a full topology down to the requested policies and/or namespaces, for someone
    working on one export problem. Returns (filtered copy, banner text or None)."""
    if not policies and not namespaces:
        return t, None
    full = t.get("policies") or []
    kept = []
    for p in full:
        if policies and p.get("name") not in policies:
            continue
        q = dict(p)
        q["namespaces"] = [n for n in p.get("namespaces") or [] if not namespaces or n.get("name") in namespaces]
        if q["namespaces"] or not namespaces:
            kept.append(q)
    out = dict(t)
    out["policies"] = kept
    # the cluster-level lists only distract from a focused card
    out["notExported"] = [x for x in t.get("notExported") or []
                          if (not namespaces or x.get("namespace") in namespaces)
                          and (not policies or set(x.get("policies") or []) & set(policies))]
    out["orphanedRepositories"] = [o for o in t.get("orphanedRepositories") or []
                                   if not namespaces or o.get("namespace") in namespaces]
    out["unopenableRepositories"] = [x for x in t.get("unopenableRepositories") or []
                                     if (not namespaces or x.get("namespace") in namespaces)
                                     and (not policies or x.get("policy") in policies)]
    # a failing profile is kept whenever it blocks one of the policies in view - it is the
    # reason the focused namespace shows nothing, so filtering it out hides the answer
    out["profilesFailingValidation"] = [x for x in t.get("profilesFailingValidation") or []
                                        if not policies
                                        or set(x.get("blocksExportPolicies") or []) & set(policies)]
    parts = []
    if policies:
        parts.append("polic" + ("y " if len(policies) == 1 else "ies ") + ", ".join(policies))
    if namespaces:
        parts.append("namespace" + ("" if len(namespaces) == 1 else "s") + " " + ", ".join(namespaces))
    banner = (f"Focused view: {' · '.join(parts)} — {len(kept)} of {len(full)} policies shown; "
              f"warnings and limiters are those of the full collection")
    return out, banner


def render(t, banner=None):
    pols = t.get("policies") or []
    # A namespace selected by several policies appears under each of them (that is the
    # requested shape), so KPI totals are computed once per (namespace, profile).
    seen, nss = set(), []
    for p in pols:
        for n in p.get("namespaces") or []:
            key = (n.get("name"), (p.get("profile") or {}).get("name"))
            if key not in seen:
                seen.add(key)
                nss.append(n)
    pvcs = [pv for n in nss for pv in n.get("pvcs") or []]
    snaps = sum(len(pv.get("snapshots") or []) for pv in pvcs)
    total_logical = sum(pv.get("totalSizeBytes") or 0 for pv in pvcs)
    # Only namespaces whose repository was actually read can contribute an object count.
    # Summing `or 0` over the rest reported a confident "0 objects · 0 B stored" for a
    # cluster whose repositories had simply never been opened.
    counted = [n for n in nss if (n.get("repository") or {}).get("objectCount") is not None]
    unread = len(nss) - len(counted)
    total_objects = sum((n.get("repository") or {}).get("objectCount") or 0 for n in counted)
    total_stored = sum((n.get("repository") or {}).get("objectBytes") or 0 for n in counted)
    met = t.get("metrics") or {}
    cl = t.get("cluster") or {}
    kpis = [
        tile("Policies with exports", len(pols)),
        tile("Namespace/profile pairs", len(nss)),
        tile("PVCs in repositories", len(pvcs)),
        # "Snapshots" alone was read as the Kasten dashboard's local-snapshot count, which is
        # a different thing: that one counts local CSI snapshots, this one counts Kopia
        # snapshots inside the export repositories, summed over every PVC.
        tile("Kopia snapshots", snaps, "restore points in the export repositories, over all PVCs"),
        tile("Logical size (last snapshots)", fmt_bytes(total_logical)),
        (tile("Objects on object stores", fmt_num(total_objects),
              fmt_bytes(total_stored) + " stored"
              + (f" · {unread} repositories not read" if unread else ""))
         if counted else
         tile("Objects on object stores", "not read",
              "repo_checker skipped" if (t.get("repoChecker") or {}).get("skipped") else
              f"{unread} repositor{'y' if unread == 1 else 'ies'} could not be opened"
              if unread else "no repository was inventoried")),
        tile("Datamover metrics",
             ("available" if met.get("podLabelsExposed") is not False else "unattributed") if met.get("available") else "unavailable",
             (f'{(met.get("retentionSeconds") or 0)//86400} d retention' +
              (" · per pod only: kube-state-metrics exposes no pod labels" if met.get("podLabelsExposed") is False else
               " · attributed to namespace/job" if met.get("podLabelsExposed") else
               " · attribution not probed")) if met.get("available") else (met.get("flavour") or "")),
    ]
    warnings = t.get("warnings") or []
    warn_html = ""
    if warnings:
        warn_html = (f'<details class="card warn-box"><summary><b>Warnings</b> <span class="sub">{len(warnings)}</span></summary>'
                     '<ul class="note" style="margin:8px 0 0;padding-left:18px">'
                     + "".join(f'<li>{esc(w[:400])}</li>' for w in warnings) + '</ul></details>')
    orphans = t.get("orphanedRepositories") or []
    if orphans:
        def prof_cell(o):
            if not o.get("profile"):
                return '<span class="muted">none (deleted)</span>'
            return esc(o["profile"]) + ("" if o.get("profileExistsOnCluster") else ' <span class="muted">(deleted)</span>')
        warn_html += ('<details class="card warn-box"><summary><b>Repositories repo_checker cannot inventory</b> '
                      f'<span class="sub">{len(orphans)} — each aborts the inventory of its profile; the data sits on the object store all the same</span></summary>'
                      + table(["Repository", "Content", "Namespace", "Profile", "Store", "Bucket", "Path", "Reason"],
                              [[esc(o.get("repository")), esc(o.get("contentType") or "–"), esc(o.get("namespace") or "–"),
                                prof_cell(o), esc(o.get("objectStoreType") or "–"), esc(o.get("bucket") or "–"),
                                f'<span class="muted">{esc(o.get("path") or "–")}</span>',
                                f'<span title="{esc(" -> ".join(o.get("k10toolsError") or []))}">{esc(o.get("reason") or o.get("note") or "")}</span>']
                               for o in orphans])
                      + '</details>')
    uo = t.get("unopenableRepositories") or []
    if uo:
        title = ("Exported data described without opening the repository"
                 if (t.get("repoChecker") or {}).get("skipped") else
                 "Exported data that can no longer be opened")
        warn_html += (f'<details class="card warn-box" open><summary><b>{title}</b> '
                      f'<span class="sub">{len(uo)} — described from the restore point details; '
                      'the data is still on the object store</span></summary>'
                      + ('<div class="note"><b>repo_checker was skipped for this run</b> '
                         '(<code>--skip-repo-checker</code>), so every repository is described here '
                         'from the restore point details, whether or not it could have been '
                         'opened.</div>' if (t.get("repoChecker") or {}).get("skipped") else '')
                      + '<div class="note">Three different situations end up here, and the per-namespace '
                      'banner below says which one applies. <b>The namespace is gone:</b> '
                      '<code>repo_checker -o connect</code> only accepts '
                      '<code>-a &lt;namespace&gt; -p &lt;profile&gt;</code> and resolves the repository by looking that '
                      'namespace up, so it cannot be pointed at the repository any more. That is a limit of the '
                      'tool, not of the data — the StorageRepository CR still records the resolved path, whose last '
                      'segment is the deleted namespace\'s UID, and K10 goes on opening and maintaining the '
                      'repository (see the maintenance dates below). <b>The profile is gone:</b> the harder case, '
                      'because the credentials and the repository password go with it. <b>The namespace and the '
                      'profile both still exist:</b> then the profile itself is usually what is broken — an '
                      'untrusted TLS certificate or a rotated key — and its validation state and cause are quoted '
                      'in the banner. In all three cases '
                      '<code>restorepointcontents/&lt;name&gt;/details</code> is cluster-scoped and survives, so the '
                      'PVCs, Kopia snapshot ids and sizes below come from there.</div>'
                      + table(["Namespace",
                               ("Namespace created",
                                "when the namespace was created. For a deleted one this comes from "
                                "the namespace manifest captured inside the restore point, so it "
                                "survives the namespace itself - read it next to Frozen since to get "
                                "how long this data was being written."),
                               "Policy", "Profile", "Gone", "Restore points", "Frozen since",
                               ("K10 last worked on it",
                                "the last procedure K10 itself ran against this repository, from "
                                "StorageRepository status.processResults. MaintenanceRun is Kopia "
                                "maintenance (compaction and expiry); StorageScan is K10 "
                                "recomputing the repository's storage usage, which is what feeds "
                                "the Data Usage figures in the Kasten dashboard. A recent date here "
                                "means K10 can still open the repository even though repo_checker "
                                "cannot.")],
                              [[esc(x.get("namespace")),
                                (f'<span title="{esc(x.get("namespaceCreatedAtSource") or "")}">'
                                 f'{esc(fmt_ts(x.get("namespaceCreatedAt")))}</span>'
                                 if x.get("namespaceCreatedAt") else '<span class="muted">–</span>'),
                                f'<code>{esc(x.get("policy"))}</code>', esc(x.get("profile")),
                                esc(" + ".join(x.get("orphanedBy") or [])),
                                fmt_num(x.get("restorePoints")), esc(fmt_ts(x.get("frozenSince"))),
                                (lambda c: (f'{esc(fmt_ts(c.get("lastProcessedAt")))} '
                                            f'<span class="muted">{esc(c.get("lastProcedure") or "")}</span>')
                                 if c.get("lastProcessedAt") else '<span class="muted">–</span>')(x.get("storageRepository") or {})]
                               for x in uo], num_cols=(5,))
                      + '</details>')
    pf = t.get("profilesFailingValidation") or []
    if pf:
        warn_html += ('<details class="card warn-box" open><summary><b>Location Profiles that fail validation</b> '
                      f'<span class="sub">{len(pf)} — upstream of everything below</span></summary>'
                      '<div class="note">A profile in validation state <code>Failed</code> stops the whole chain '
                      'silently: every export policy on it also goes <code>Failed</code>, its runs fail before a '
                      'backup exists, and the namespaces it covers then look merely <i>never exported</i>. The cause '
                      'is only on the profile, so it is reported here. Note that <code>repo_checker</code> does not '
                      'repeat it — its connect stops at <code>failed to connect to repository</code> and its '
                      'inventory at <code>failed to find a profile with given location information</code>, neither of '
                      'which mentions a certificate.</div>'
                      + table(["Profile", "State", "Endpoint", "Bucket", "skipSSLVerify", "Blocks policies", "Innermost cause"],
                              [[f'<code>{esc(x.get("profile"))}</code>',
                                f'<b>{esc(x.get("validation"))}</b>',
                                f'<span class="muted">{esc(x.get("endpoint") or "–")}</span>',
                                esc(x.get("bucket") or "–"),
                                esc("true" if x.get("skipSSLVerify") else "false"),
                                esc(", ".join(x.get("blocksExportPolicies") or []) or "–"),
                                f'<code>{esc((x.get("innermostCause") or "")[:300])}</code>']
                               for x in pf])
                      + '</details>')
    ne = t.get("notExported") or []
    if ne:
        warn_html += ('<details class="card"><summary><b>Selected by an export policy but never exported</b> '
                      f'<span class="sub">{len(ne)} namespace/profile pairs with no repository</span></summary>'
                      + table(["Namespace", "Profile", "Policies"],
                              [[esc(x.get("namespace")), esc(x.get("profile")), esc(", ".join(x.get("policies") or []))] for x in ne])
                      + '</details>')
    # A namespace asked for with --namespace that produced nothing must still appear, with
    # the reason. Otherwise it simply vanishes from the page and the reader cannot tell
    # "nothing to report" from "I mistyped it" - and the commonest reason, a Location Profile
    # that fails validation so the policy never runs, is three sections away.
    skipped = set((t.get("scopeNotes") or {}).get("namespacesWithoutRestorePointSkipped") or [])
    requested = (t.get("filter") or {}).get("namespaces") or []
    shown = set()
    for pol in t.get("policies") or []:
        for n in pol.get("namespaces") or []:
            shown.add(n.get("name"))
    for x in (t.get("unopenableRepositories") or []) + (t.get("orphanedRepositories") or []):
        shown.add(x.get("namespace"))
    silent, seen_s = [], set()
    for n in list(requested) + sorted(skipped - set(requested)):
        if n and n not in shown and n not in seen_s:
            seen_s.add(n)
            silent.append(n)
    if silent:
        warn_html += ('<details class="card warn-box" open><summary><b>Namespaces with nothing to report</b> '
                      f'<span class="sub">{len(silent)} — asked for, or selected, but no exported data was found</span></summary>'
                      '<div class="note">These have no section of their own below. A namespace with no '
                      'RestorePoint was never backed up, so it was never exported and there is no repository '
                      'to read — but check the profile first: a Location Profile in validation state '
                      '<code>Failed</code> makes every policy on it fail too, so the runs never happen and the '
                      'namespace looks untouched when the real fault is upstream.</div>'
                      + table(["Namespace", "Why nothing is reported"],
                              [[esc(n),
                                ('no RestorePoint — never backed up, hence never exported'
                                 if n in skipped else
                                 'no policy with an enabled export action selects it, and no exported '
                                 'restore point names it')]
                               for n in silent])
                      + '</details>')
    extra = t.get("repositoriesWithoutVolumeData") or []
    extra_html = ""
    if extra:
        extra_html = ('<details class="card"><summary><b>Repositories without volume data</b> <span class="sub">'
                      f'{len(extra)} (metadata / collection repositories)</span></summary>'
                      + table(["Repository", "Type", "Profile", "Snapshots"],
                              [[esc(r.get("name")), esc(r.get("type")), esc(r.get("profile")), fmt_num(r.get("totalSnapshots"))] for r in extra],
                              num_cols=(3,)) + '</details>')
    body = (
        f'<h1>K10 export topology</h1>'
        f'<div class="sub"><code>{esc(cl.get("context"))}</code> · K10 {esc(cl.get("k10Version"))} in <code>{esc(cl.get("k10Namespace"))}</code>'
        f' · cluster UID <code>{esc(cl.get("uid"))}</code> · generated {esc(fmt_ts(t.get("generatedAt")))}</div>'
        + (f'<div class="card warn-box" style="padding:8px 14px"><b>{esc(banner)}</b></div>' if banner else "")
        + (f'<div class="card warn-box" style="padding:8px 14px"><b>Focused collection:</b> the generator ran with '
           f'{esc(" ".join(f"--namespace {n}" for n in (t["filter"].get("namespaces") or [])))} '
           f'{esc(" ".join(f"--policy {p}" for p in (t["filter"].get("policies") or [])))} — other pairs were not read</div>'
           if t.get("filter") else "")
        + f'<div class="kpis">{"".join(kpis)}</div>'
        f'<div class="note">Only namespaces with volume data in a Kopia repository appear. Datamover CPU/memory is the sum over '
        f'all datamover pods alive in each window — attributed to namespace and job via kube-state-metrics pod labels, not to a PVC.</div>'
        + highlights_block(nss)
        + f'{warn_html}{nodes_block(t.get("nodes"))}{limiters_block(t.get("helmLimiters"))}'
        + "".join(policy_card(p) for p in pols)
        + extra_html
        + f'<footer>Rendered from export-topology.json · {esc(fmt_ts(t.get("generatedAt")))}</footer>'
    )
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>K10 export topology — {esc(cl.get("context"))}</title><style>{CSS}</style></head>'
            f'<body><main>{body}</main></body></html>')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("topology", help="export-topology.json")
    ap.add_argument("-o", "--output", default="export-topology.html")
    ap.add_argument("--policy", action="append", help="show only these policies (repeatable)")
    ap.add_argument("--namespace", action="append", help="show only these namespaces (repeatable)")
    args = ap.parse_args()
    with open(args.topology) as f:
        topo = json.load(f)
    topo, banner = focus(topo, args.policy, args.namespace)
    if not topo.get("policies"):
        # Do NOT exit. A collection with no policies is the case that most needs a page:
        # the nodes, limiters, warnings, notExported and scope notes in that file are
        # exactly what explain why it is empty. Exiting here sent a customer chasing a
        # filter they had not passed.
        why = ("no policy or namespace matched --policy/--namespace"
               if (args.policy or args.namespace) else
               "the collection itself found no exported namespace")
        banner = (f"Nothing to report: {why}. "
                  + ("Filter: " + json.dumps({"policies": args.policy, "namespaces": args.namespace}) + ". "
                     if (args.policy or args.namespace) else "")
                  + "The sections below are what the collection did find, and the warnings and "
                    "'selected but never exported' list say why there is nothing else.")
        print(f"warning: {why}; rendering the diagnostic page anyway", file=sys.stderr)
    out = render(topo, banner)
    with open(args.output, "w") as f:
        f.write(out)
    print(f"wrote {args.output} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
