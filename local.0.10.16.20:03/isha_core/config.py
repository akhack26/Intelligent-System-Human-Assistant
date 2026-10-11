"""Defaults for the settings added by the ISHA agent upgrade.

mani.py keeps its flat ``DEFAULT_CONFIG`` (so every existing isha_config.json
keeps loading unchanged); these keys are merged on top. ``CATEGORIES`` maps
every key to the section it belongs to, for the docs and the settings UI.
"""
from .file_index import DEFAULT_EXCLUDES
from . import skills as _skills
from . import personalization as _pers

NEW_DEFAULTS = {
    # models / routing
    "models": {"general": "", "coding": "", "reasoning": "", "study": "", "fast": "", "vision": ""},
    "model_mode": "auto",                 # auto | general | coding | reasoning | study | fast | vision
    "hardware_aware_loading": True,       # fall back to a smaller model when RAM is short
    # security / permissions
    "autonomy_level": "high",             # low | medium | high | autonomous  (high == old behaviour)
    "trusted_messaging": False,           # True = send WhatsApp without asking
    "trusted_recycle_delete": False,      # True = move files/folders to the Recycle Bin without asking
    "trusted_tools": [],                  # confirm-tier tools the user pre-approved
    "blocked_tools": [],                  # tools that must never run
    # coding agent
    "max_auto_fix_attempts": 3,
    "code_run_timeout": 20,
    # file search
    "file_index_enabled": True,
    "file_index_roots": [],               # [] = home folder
    "file_index_excludes": list(DEFAULT_EXCLUDES),
    "file_index_autostart_delay": 20,     # seconds after launch before indexing starts
    # software
    "preferred_package_manager": "",      # e.g. "winget", "flatpak"; "" = automatic
    # observability / UI
    "audit_log_enabled": True,
    "show_agent_panel": False,
    # vision
    "vision_enabled": True,
    # three-slot model picker (model_slots itself is migrated from the legacy
    # keys by model_slots.migrate(), see mani.load_config)
    "model_selection": "auto",            # auto | slot1 | slot2 | slot3
    # skills + visual features
    "skills_enabled": _skills.defaults(), # {skill_id: bool}
    "artifacts_enabled": True,            # Artifacts panel for generated code/docs/designs
    "inline_visuals_enabled": True,       # charts / diagrams inside the conversation
    # appearance / personalization
    "response_language": "",              # "" = reply in the user's language
    "user_name": "",
    "work_category": "",                  # personalization.WORK_OPTIONS id, "" = prefer not to say
    "user_instructions": "",              # user-wide instructions for every chat
    "response_effort": "medium",          # low | medium | high | extra | max
    # local projects
    "active_project": "",                 # project id, "" = normal chat
    "active_project_chat": "",
}

CATEGORIES = {
    "models": ["gguf_model_path", "code_model_path", "models", "chat_format", "max_loaded_models",
               "preload_model", "gpu_layers", "cpu_threads", "batch_size", "context_length"],
    "routing": ["model_mode", "model_routing", "hardware_aware_loading", "agent_mode", "agent_max_steps"],
    "voice": ["tts_enabled", "tts_voice", "tts_rate", "stream_tts"],
    "memory": ["history_turns"],
    "search": ["file_index_enabled", "file_index_roots", "file_index_excludes", "file_index_autostart_delay"],
    "security": ["confirm_risky_tools", "blocked_tools"],
    "permissions": ["autonomy_level", "trusted_tools", "trusted_messaging", "trusted_recycle_delete"],
    "automation": ["max_auto_fix_attempts", "code_run_timeout", "preferred_package_manager", "fast_tools"],
    "messaging": ["whatsapp_mode", "whatsapp_country_code", "whatsapp_desktop_wait", "whatsapp_web_wait",
                  "whatsapp_autosend_by_name"],
    "performance": ["streaming", "perf_metrics", "max_tokens", "temperature", "top_p", "top_k"],
    "ui": ["show_agent_panel", "audit_log_enabled"],
    "model_picker": ["model_slots", "model_selection"],
    "skills": ["skills_enabled"],
    "visuals": ["artifacts_enabled", "inline_visuals_enabled"],
    "personalization": ["user_name", "work_category", "response_language", "user_instructions",
                        "speech_speed", "response_effort", "thinking_mode"],
    "projects": ["active_project", "active_project_chat"],
}


def merge_defaults(cfg: dict) -> dict:
    """Add missing new keys without touching anything the user already set."""
    for k, v in NEW_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v.copy() if isinstance(v, (dict, list)) else v
        elif k in ("models", "skills_enabled") and isinstance(cfg[k], dict):
            for role, val in v.items():
                cfg[k].setdefault(role, val)
        elif k in ("models", "skills_enabled"):
            cfg[k] = v.copy()                 # wrong type in an old/hand-edited file
    # speech_speed is only *derived* for display; it is written when the user
    # saves settings, so a custom legacy tts_rate keeps working untouched.
    if str(cfg.get("response_effort", "medium")).lower() not in _pers.EFFORTS:
        cfg["response_effort"] = "medium"
    return cfg
