"""Defaults for the settings added by the ISHA agent upgrade.

mani.py keeps its flat ``DEFAULT_CONFIG`` (so every existing isha_config.json
keeps loading unchanged); these keys are merged on top. ``CATEGORIES`` maps
every key to the section it belongs to, for the docs and the settings UI.
"""
from .file_index import DEFAULT_EXCLUDES

NEW_DEFAULTS = {
    # models / routing
    "models": {"general": "", "coding": "", "reasoning": "", "study": "", "fast": "", "vision": ""},
    "model_mode": "auto",                 # auto | general | coding | reasoning | study | fast | vision
    "hardware_aware_loading": True,       # fall back to a smaller model when RAM is short
    # security / permissions
    "autonomy_level": "high",             # low | medium | high  (high == old behaviour)
    "trusted_messaging": False,           # True = send WhatsApp without asking
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
}

CATEGORIES = {
    "models": ["gguf_model_path", "code_model_path", "models", "chat_format", "max_loaded_models",
               "preload_model", "gpu_layers", "cpu_threads", "batch_size", "context_length"],
    "routing": ["model_mode", "model_routing", "hardware_aware_loading", "agent_mode", "agent_max_steps"],
    "voice": ["tts_enabled", "tts_voice", "tts_rate", "stream_tts"],
    "memory": ["history_turns"],
    "search": ["file_index_enabled", "file_index_roots", "file_index_excludes", "file_index_autostart_delay"],
    "security": ["confirm_risky_tools", "blocked_tools"],
    "permissions": ["autonomy_level", "trusted_tools", "trusted_messaging"],
    "automation": ["max_auto_fix_attempts", "code_run_timeout", "preferred_package_manager", "fast_tools"],
    "messaging": ["whatsapp_mode", "whatsapp_country_code", "whatsapp_desktop_wait", "whatsapp_web_wait",
                  "whatsapp_autosend_by_name"],
    "performance": ["streaming", "perf_metrics", "max_tokens", "temperature", "top_p", "top_k"],
    "ui": ["show_agent_panel", "audit_log_enabled"],
}


def merge_defaults(cfg: dict) -> dict:
    """Add missing new keys without touching anything the user already set."""
    for k, v in NEW_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v.copy() if isinstance(v, (dict, list)) else v
        elif k == "models" and isinstance(cfg[k], dict):
            for role, path in v.items():
                cfg[k].setdefault(role, path)
    return cfg
