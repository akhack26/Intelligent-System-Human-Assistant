"""Permission gateway: risk tier x autonomy level x trust flags -> decision.

Tiers (unchanged from mani.py's TOOL_RISK):
    safe      reversible or read-only
    confirm   changes/destroys something, or talks to another person
    critical  arbitrary code, system-wide, privileged, unrecoverable

Autonomy levels (user setting, default "high" = the behaviour the old build had):
    low     only read-only actions run by themselves; everything else asks
    medium  read-only actions automatic; any change asks
    high    safe actions automatic; confirm + critical ask

Hard rules that no setting can relax:
    * critical ALWAYS asks (there is no "trust me with rm -rf" switch)
    * a blocked tool is always denied
    * an unknown tool is treated as "confirm", never "safe"
"""
from __future__ import annotations

from dataclasses import dataclass

ALLOW, ASK, DENY = "allow", "ask", "deny"
AUTONOMY_LEVELS = ("low", "medium", "high")
RISK_ORDER = {"safe": 0, "confirm": 1, "critical": 2}

# Safe tools that only observe. Everything else in the "safe" tier changes
# *something* (opens a window, sets the volume, creates a folder).
READ_ONLY_TOOLS = frozenset({
    "check_cpu_usage", "check_memory_usage", "check_battery", "get_time", "get_date",
    "get_system_stats", "get_gpu_usage", "get_wifi_status", "list_running_processes",
    "search_files", "advanced_file_search", "list_media_files", "read_file_content",
    "search_long_term_memory", "list_capabilities", "isha_self_check", "get_storage_info",
    "get_network_info", "list_open_windows", "list_directory", "clipboard_read",
    "list_whatsapp_contacts", "find_files", "file_index_status", "get_hardware_info",
    "get_system_info", "get_model_status", "show_memory", "recall_memory",
    "search_software", "recycle_bin_status", "take_screenshot", "describe_image",
    "describe_screen", "get_activity_log", "inspect_project",
})

# confirm-tier tools a user can pre-approve with a dedicated switch
TRUST_FLAGS = {
    "send_whatsapp_message": "trusted_messaging",
}


@dataclass
class Decision:
    action: str           # allow | ask | deny
    risk: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW


def normalise_autonomy(value) -> str:
    v = str(value or "high").strip().lower()
    return v if v in AUTONOMY_LEVELS else "high"


def decide(tool: str, risk: str, cfg: dict | None = None) -> Decision:
    cfg = cfg or {}
    risk = risk if risk in RISK_ORDER else "confirm"
    blocked = set(cfg.get("blocked_tools") or [])
    if tool in blocked:
        return Decision(DENY, risk, f"'{tool}' is blocked in settings")

    if risk == "critical":
        return Decision(ASK, risk, "critical actions always need approval")

    autonomy = normalise_autonomy(cfg.get("autonomy_level"))
    legacy_confirm = bool(cfg.get("confirm_risky_tools", True))
    trusted_tools = set(cfg.get("trusted_tools") or [])
    read_only = tool in READ_ONLY_TOOLS

    if risk == "confirm":
        flag = TRUST_FLAGS.get(tool)
        if flag and bool(cfg.get(flag, False)):
            return Decision(ALLOW, risk, f"pre-approved via '{flag}'")
        if tool in trusted_tools:
            return Decision(ALLOW, risk, "tool is in trusted_tools")
        if not legacy_confirm:
            return Decision(ALLOW, risk, "confirm_risky_tools is off")
        return Decision(ASK, risk, "changes something / contacts someone")

    # safe tier
    if read_only:
        return Decision(ALLOW, risk, "read-only")
    if autonomy == "high" or tool in trusted_tools:
        return Decision(ALLOW, risk, "safe action, autonomy=high")
    return Decision(ASK, risk, f"autonomy={autonomy} asks before changes")
