"""Inline visualizations: a tiny, validated JSON spec rendered natively.

The model adds at most one fenced block when a chart or diagram helps::

    ```isha-visual
    {"type": "bar", "title": "Monthly sales", "unit": "INR",
     "labels": ["Jan", "Feb"], "series": [{"name": "2024", "values": [1200, 1350]}]}
    ```

Supported types: bar, line, pie, flow (flowchart / process), timeline,
compare (comparison table) and table. mani.py draws them with QPainter - no
HTML, no JavaScript, no network, no CDN - so a generated visual can never
touch local files or the internet. Anything that fails validation, and every
visual when rich rendering is unavailable, degrades to a readable text table.

Truthfulness: numeric charts are checked against the conversation's real data
(user messages, tool results, project files). Values that do not appear there
are reported and the chart is shown as a clearly-marked text table instead,
unless the model marked them as calculated ("computed": true) AND they are
a minority of the values - then the chart is drawn with a caption listing the
calculated numbers.
"""
from __future__ import annotations

import json
import math
import re

from .artifacts import fenced_blocks

VISUAL_LANGS = ("isha-visual", "isha-chart")
TYPES = ("bar", "line", "pie", "flow", "timeline", "compare", "table")
_ALIASES = {"flowchart": "flow", "process": "flow", "diagram": "flow", "comparison": "compare",
            "column": "bar", "bars": "bar", "donut": "pie", "graph": "line"}
MAX_POINTS, MAX_SERIES, MAX_NODES, MAX_ROWS, MAX_COLS = 40, 6, 24, 25, 8

_REQUEST_RE = re.compile(
    r"(?<!\w)(chart|graph|plot|visuali[sz]\w*|diagram|flow\s*chart|flowchart|timeline|"
    r"pie|bar\s+chart|line\s+chart|histogram|compare|comparison|process\s+map|dikhao\s+graph)(?!\w)", re.I)


def is_visual_request(text: str) -> bool:
    return bool(_REQUEST_RE.search(text or ""))


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.strip().replace(",", "").rstrip("%")
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _txt(v, n=60) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def validate(obj):
    """(spec | None, error). Normalises and caps everything."""
    if not isinstance(obj, dict):
        return None, "visual is not a JSON object"
    t = str(obj.get("type") or "").strip().lower()
    t = _ALIASES.get(t, t)
    if t not in TYPES:
        return None, f"unknown visual type '{t}'"
    spec = {"type": t, "title": _txt(obj.get("title"), 80), "unit": _txt(obj.get("unit"), 20),
            "x_label": _txt(obj.get("x_label"), 40), "y_label": _txt(obj.get("y_label"), 40),
            "source": _txt(obj.get("source"), 80), "computed": bool(obj.get("computed", False))}
    if t in ("bar", "line"):
        labels = [_txt(x, 24) for x in _list(obj.get("labels"))][:MAX_POINTS]
        series = obj.get("series") if isinstance(obj.get("series"), list) else None
        if series is None and isinstance(obj.get("values"), list):
            series = [{"name": spec["y_label"] or spec["title"] or "value", "values": obj["values"]}]
        out = []
        for s in (series or [])[:MAX_SERIES]:
            if not isinstance(s, dict):
                continue
            vals = [_num(v) for v in _list(s.get("values"))][:MAX_POINTS]
            if not vals or any(v is None for v in vals):
                return None, "every chart value must be a number"
            out.append({"name": _txt(s.get("name") or "series", 30), "values": vals})
        if not labels or not out:
            return None, "chart needs labels and at least one series"
        if any(len(s["values"]) != len(labels) for s in out):
            return None, "each series needs one value per label"
        spec.update(labels=labels, series=out)
        return spec, ""
    if t == "pie":
        labels = [_txt(x, 24) for x in _list(obj.get("labels"))][:12]
        vals = [_num(v) for v in _list(obj.get("values"))][:12]
        if not labels or len(labels) != len(vals) or any(v is None or v < 0 for v in vals):
            return None, "pie needs matching labels and non-negative values"
        if sum(vals) <= 0:
            return None, "pie values sum to zero"
        spec.update(labels=labels, values=vals)
        return spec, ""
    if t == "flow":
        nodes, edges = [], []
        steps = obj.get("steps") if isinstance(obj.get("steps"), list) else []
        if steps:
            nodes = [{"id": str(i), "label": _txt(s, 48)} for i, s in enumerate(steps[:MAX_NODES])]
            edges = [(str(i), str(i + 1), "") for i in range(len(nodes) - 1)]
        raw_nodes = obj.get("nodes") if isinstance(obj.get("nodes"), list) else []
        seen = {n["id"] for n in nodes}
        for n in raw_nodes:
            if len(nodes) >= MAX_NODES:
                break
            if isinstance(n, dict) and n.get("id") is not None:
                item = {"id": _txt(n["id"], 24), "label": _txt(n.get("label") or n["id"], 48)}
            elif isinstance(n, str):
                item = {"id": _txt(n, 24), "label": _txt(n, 48)}
            else:
                continue
            if item["id"] and item["id"] not in seen:
                seen.add(item["id"])
                nodes.append(item)
        ids = {n["id"] for n in nodes}          # edges may only point at nodes that were kept
        raw_edges = obj.get("edges") if isinstance(obj.get("edges"), list) else []
        for e in raw_edges[:MAX_NODES * 2]:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                a, b, lbl = _txt(e[0], 24), _txt(e[1], 24), _txt(e[2] if len(e) > 2 else "", 20)
            elif isinstance(e, dict):
                a, b, lbl = _txt(e.get("from"), 24), _txt(e.get("to"), 24), _txt(e.get("label"), 20)
            else:
                continue
            if a in ids and b in ids and a != b:
                edges.append((a, b, lbl))
        if len(nodes) < 2:
            return None, "flow needs at least two steps"
        spec.update(nodes=nodes, edges=edges)
        return spec, ""
    if t == "timeline":
        ev = []
        for e in _list(obj.get("events"))[:30]:
            if isinstance(e, dict):
                ev.append({"when": _txt(e.get("date") or e.get("when") or e.get("time"), 24),
                           "label": _txt(e.get("label") or e.get("event") or e.get("title"), 70)})
            elif isinstance(e, (list, tuple)) and len(e) >= 2:
                ev.append({"when": _txt(e[0], 24), "label": _txt(e[1], 70)})
        ev = [e for e in ev if e["label"]]
        if not ev:
            return None, "timeline needs events"
        spec.update(events=ev)
        return spec, ""
    # compare / table
    cols = [_txt(c, 30) for c in _list(obj.get("columns"))][:MAX_COLS]
    rows = []
    for r in _list(obj.get("rows"))[:MAX_ROWS]:
        if isinstance(r, (list, tuple)):
            rows.append([_txt(c, 60) for c in r][:MAX_COLS])
        elif isinstance(r, dict) and cols:
            rows.append([_txt(r.get(c), 60) for c in cols])
    if not cols or not rows:
        return None, "table needs columns and rows"
    width = len(cols)
    rows = [(r + [""] * width)[:width] for r in rows]
    spec.update(columns=cols, rows=rows)
    return spec, ""


# ---- grounding ---------------------------------------------------------------
# A number not glued to a preceding digit ("Rs.1200", "INR1200" and "₹1,20,000" all count).
_NUM_RE = re.compile(r"(?<!\d)(?<!\d[.,])-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<!\d)(?<!\d[.,])-?\d+(?:\.\d+)?")


def numbers_in(text: str) -> list:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def ungrounded_values(spec: dict, source_text: str) -> list:
    """Chart values that do not appear anywhere in the real data text."""
    vals = []
    if spec["type"] in ("bar", "line"):
        vals = [v for s in spec["series"] for v in s["values"]]
    elif spec["type"] == "pie":
        vals = list(spec["values"])
    if not vals:
        return []
    have = numbers_in(source_text)
    missing = []
    for v in vals:
        if not any(abs(v - h) <= 1e-9 * max(1.0, abs(h)) + 1e-9 for h in have):
            missing.append(v)
    return missing


def value_count(spec: dict) -> int:
    if spec["type"] in ("bar", "line"):
        return sum(len(s["values"]) for s in spec["series"])
    if spec["type"] == "pie":
        return len(spec["values"])
    return 0


# ---- text fallback -----------------------------------------------------------
def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _table(cols, rows) -> str:
    widths = [max([len(str(c))] + [len(str(r[i])) for r in rows]) for i, c in enumerate(cols)]
    line = lambda cells: "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"  # noqa: E731
    return "\n".join([line(cols), "|" + "|".join("-" * (w + 2) for w in widths) + "|"] + [line(r) for r in rows])


def to_text(spec: dict) -> str:
    """Readable plain-text version of any visual (the fallback)."""
    t = spec["type"]
    head = spec.get("title") or t.title()
    unit = f" ({spec['unit']})" if spec.get("unit") else ""
    if t in ("bar", "line"):
        cols = [spec.get("x_label") or "Label"] + [s["name"] + unit for s in spec["series"]]
        rows = [[lbl] + [_fmt(s["values"][i]) for s in spec["series"]] for i, lbl in enumerate(spec["labels"])]
        body = _table(cols, rows)
    elif t == "pie":
        total = sum(spec["values"]) or 1.0
        rows = [[lbl, _fmt(v), f"{100 * v / total:.1f}%"] for lbl, v in zip(spec["labels"], spec["values"])]
        body = _table(["Item", "Value" + unit, "Share"], rows)
    elif t == "flow":
        labels = {n["id"]: n["label"] for n in spec["nodes"]}
        body = "\n".join(f"{labels[a]} -> {labels[b]}" + (f"  [{lbl}]" if lbl else "")
                         for a, b, lbl in spec["edges"]) or " -> ".join(labels.values())
    elif t == "timeline":
        body = "\n".join(f"{e['when'] or '-'}: {e['label']}" for e in spec["events"])
    else:
        body = _table(spec["columns"], spec["rows"])
    return f"{head}\n{body}"


def extract(text: str):
    """Split a reply into (display_text, [(spec | None, raw_json, error)]).

    Each visual block is replaced by a short placeholder line; invalid ones
    keep an error so the caller can show a text fallback instead."""
    blocks = [b for b in fenced_blocks(text) if b.lang in VISUAL_LANGS]
    if not blocks:
        return text, []
    lines = (text or "").split("\n")
    out, found, cursor = [], [], 0
    for b in blocks[:3]:
        out.extend(lines[cursor:b.start])
        cursor = b.end + 1
        spec, err = None, ""
        if not b.closed:
            err = "visual was cut off"
        else:
            try:
                spec, err = validate(json.loads(b.body))
            except Exception as e:
                err = f"invalid visual JSON ({type(e).__name__})"
        found.append((spec, b.body, err))
        title = spec.get("title") if spec else ""
        out.append(f"[Visual: {title or (spec['type'] if spec else 'not shown')}]")
    out.extend(lines[cursor:])
    return "\n".join(out).strip(), found


_PARTIAL_RE = re.compile(r"(`{3,}|~{3,})[ \t]*(isha-visual|isha-chart|artifact)\b.*?(?:\1|\Z)", re.S | re.I)


def display_stream_text(text: str) -> str:
    """While streaming, hide visual/artifact JSON behind a short placeholder."""
    return _PARTIAL_RE.sub(lambda m: "[preparing visual…]" if "isha" in m.group(2).lower()
                           else "[preparing artifact…]", text or "")


def prompt_instructions(visuals_on: bool, artifacts_on: bool, want_visual: bool, want_artifact: bool) -> str:
    """Short, relevance-gated output-format guidance for the system prompt."""
    parts = []
    if visuals_on and want_visual:
        parts.append(
            "[INLINE VISUALS ON] If a chart or diagram clearly helps (or the user asks), add ONE fenced "
            "block ```isha-visual with JSON, e.g. {\"type\":\"bar\",\"title\":\"...\",\"unit\":\"...\","
            "\"labels\":[...],\"series\":[{\"name\":\"...\",\"values\":[...]}]}. Types: bar, line, pie "
            "(labels+values), flow (steps:[...] or nodes+edges), timeline (events:[{date,label}]), compare "
            "(columns+rows). Use ONLY numbers that appear in the user's messages, project files or tool "
            "results - never invent values; if you calculated a value, add \"computed\": true. Keep labels "
            "short, always give the unit, and still answer in plain text.")
    elif want_visual and not visuals_on:
        parts.append("[INLINE VISUALS OFF] The user turned inline charts off. If they ask for a chart, give a "
                     "short text table instead and say in one line that inline visuals are turned off in "
                     "Skills & Visuals.")
    if artifacts_on and want_artifact:
        parts.append(
            "[ARTIFACTS ON] Put any complete file you produce (web page, code file, document, design) in ONE "
            "fenced block ```artifact filename=\"name.ext\" title=\"Short title\" and close it with ```. "
            "HTML must be self-contained (inline CSS/JS, no external links or CDNs). If the file itself "
            "contains ``` fences, open the artifact with ~~~~artifact and close with ~~~~. Outside the block, "
            "reply with one or two sentences only.")
    elif want_artifact and not artifacts_on:
        parts.append("[ARTIFACTS OFF] The Artifacts panel is turned off; give code or documents inline as normal.")
    return "\n\n".join(parts)


_ARTIFACT_REQ_RE = re.compile(
    r"(?<!\w)(artifact|html|css|web\s*page|webpage|landing\s+page|website|web\s*site|component|"
    r"script|program|code|file|document|report|letter|resume|design|mock\s*up|mockup|template|"
    r"svg|markdown|readme|bana\s+do|likh\s+do)(?!\w)", re.I)


def is_artifact_request(text: str) -> bool:
    return bool(_ARTIFACT_REQ_RE.search(text or ""))
