"""Regenerate TOOLS.md from the live tool registry.
Run from the project root:  ISHA_SKIP_BACKEND=1 QT_QPA_PLATFORM=offscreen python tools_dev/gen_tools_md.py"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ISHA_SKIP_BACKEND", "1"); os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
import mani  # noqa: E402
from isha_core.permissions import READ_ONLY_TOOLS  # noqa: E402
lines = ["# ISHA Tools", "",
         "Generated from the live registry (`TOOL_REGISTRY`) — every tool ISHA can call, its category and risk tier.",
         "Risk tiers are explained in [SECURITY.md](SECURITY.md). **RO** = read-only (runs automatically at every autonomy level).",
         "", f"Total: **{len(mani.TOOL_REGISTRY)} tools**.", ""]
for cat, names in mani.TOOL_CATEGORY.items():
    lines += [f"## {cat.capitalize()}", "", "| Tool | Risk | Parameters | Description |", "|---|---|---|---|"]
    for n in dict.fromkeys(names):
        if n not in mani.TOOL_REGISTRY:
            continue
        spec = mani.TOOL_REGISTRY[n]
        params = ", ".join(f"`{p}`{'*' if v.get('required') else ''}" for p, v in spec["parameters"].items()) or "—"
        risk = mani.tool_risk(n) + (" (RO)" if n in READ_ONLY_TOOLS else "")
        lines.append(f"| `{n}` | {risk} | {params} | {' '.join(spec['description'].split()).replace('|', '/')} |")
    lines.append("")
lines += ["`*` = required parameter.", "", "## Tool-name repair", "",
          "Small models often invent names (`empty_trash`, `install_app`). `resolve_tool_name()` maps them through "
          "`TOOL_ALIASES` and fuzzy matching; an unknown name returns the closest real tools instead of a dead end.", "",
          "## Adding a tool", "", "See [DEVELOPMENT.md](DEVELOPMENT.md#adding-a-new-tool)."]
(ROOT / "TOOLS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"TOOLS.md written ({len(mani.TOOL_REGISTRY)} tools)")
