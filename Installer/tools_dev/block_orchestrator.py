# ======================================================================
# ISHA AGENT CORE v3 — orchestration layer
#
#   User -> intent -> planner (plan_request / match_agent_command)
#        -> model router (pick_cfg_for)  -> tool selector
#        -> permission gateway (run_tool_gated) -> execute_tool -> observation
#
# Everything below builds on the v2 registry above; nothing was removed.
# The heavy lifting lives in the headless isha_core package.
# ======================================================================

ISHA_DATA_DIR = Path(os.environ.get("ISHA_DATA_DIR") or PROJECT_ROOT)
try:
    ISHA_DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    ISHA_DATA_DIR = PROJECT_ROOT

ACTION_LOG = isha_audit.ActionLog(ISHA_DATA_DIR / "isha_audit.jsonl")
ACTIVE_MODEL = {"name": "", "role": "", "task": "", "mode": "auto", "notes": ""}
WORKING = isha_mem.WorkingMemory()
LONG_MEMORY = isha_mem.LongTermMemory(ISHA_DATA_DIR / "isha_long_memory.json")
_FILE_INDEX = None
_FILE_INDEX_LOCK = threading.Lock()
_HW_CACHE = {"info": None, "at": 0.0}


def _cfg() -> dict:
    try:
        return load_config()
    except Exception:
        return isha_config.merge_defaults(dict(DEFAULT_CONFIG))


def _hw(max_age: float = 5.0):
    now = time.time()
    if _HW_CACHE["info"] is None or now - _HW_CACHE["at"] > max_age:
        _HW_CACHE["info"] = isha_hw.probe()
        _HW_CACHE["at"] = now
    return _HW_CACHE["info"]


def _index_roots(cfg: dict) -> list:
    roots = [str(r) for r in (cfg.get("file_index_roots") or []) if str(r).strip()]
    return roots or [str(Path.home())]


def get_file_index(create: bool = True):
    global _FILE_INDEX
    with _FILE_INDEX_LOCK:
        if _FILE_INDEX is None and create:
            cfg = _cfg()
            if not bool(cfg.get("file_index_enabled", True)):
                return None
            try:
                _FILE_INDEX = isha_index.FileIndex(ISHA_DATA_DIR / "isha_file_index.db",
                                                   _index_roots(cfg), cfg.get("file_index_excludes"))
            except Exception as e:
                _log_error("file_index:init", e)
                return None
        return _FILE_INDEX


def get_search_engine():
    cfg = _cfg()
    return isha_search.FileSearchEngine(get_file_index(), _index_roots(cfg), cfg.get("file_index_excludes"))


# ---- approval previews ------------------------------------------------------
def _describe_special(name: str, args: dict) -> str:
    if name == "send_whatsapp_message":
        who = args.get("contact") or "?"
        return f"{who} ko WhatsApp par ye message bhejna hai:\n\"{args.get('message', '')}\"\nSend karun?"
    if name in ("install_software", "uninstall_software", "update_software"):
        action = {"install_software": "install", "uninstall_software": "uninstall",
                  "update_software": "update"}[name]
        sp = isha_sw.plan(action, str(args.get("name", "")),
                          preferred=str(_cfg().get("preferred_package_manager") or ""))
        return sp.describe() + (f"\nCommand: {' '.join(sp.argv)}" if sp.argv else "")
    if name == "run_terminal_command":
        return "Terminal command " + isha_cmd.analyze_command(str(args.get("command", ""))).describe()
    if name == "empty_recycle_bin":
        st = _SYSTEM_CONTROLLER.trash_status()
        return (st.get("message", "Recycle Bin") + " Sab kuch PERMANENTLY delete ho jayega. Empty karun?")
    if name == "delete_file_safely":
        return f"Yeh permanently delete hoga:\n{args.get('file_path')}"
    if name == "batch_delete_files":
        return "Yeh items Recycle Bin/Trash me jayenge:\n" + str(args.get("file_paths", "")).replace(",", "\n")
    if name == "write_file_content":
        p = str(args.get("file_path", ""))
        exists = False
        try:
            exists = _safe_resolve_path(p).exists()
        except Exception:
            pass
        return f"{'OVERWRITE' if exists else 'Create'} file: {p} ({len(str(args.get('content', '')))} chars)"
    if name in ("run_project", "fix_code_file"):
        target = args.get("project") or args.get("file_path")
        n = args.get("max_attempts") or _cfg().get("max_auto_fix_attempts", 3)
        fix = "" if args.get("auto_fix") is False else f", error aaye to {n} baar tak auto-fix"
        return f"Code run karna hai: {target}{fix}. (Generated code - aap trust karte ho?)"
    if name == "system_power_action":
        return f"System {args.get('action')} karna hai - unsaved kaam kho sakta hai."
    if name == "move_or_rename_file":
        return f"Move/rename:\n{args.get('source_path')}\n-> {args.get('dest_path')}"
    return ""


# ---- permission gateway -----------------------------------------------------
# Tools whose handler additionally wants confirm=true. When the USER approved
# in the dialog, that IS the confirmation — the old code made the model pass
# it too, so an approved delete could still refuse to run.
_CONFIRM_ARG_TOOLS = {"delete_file_safely": "confirm", "batch_delete_files": "confirm",
                      "system_power_action": "confirm"}


def run_tool_gated(name: str, args: dict, ask=None, cfg: dict = None, pre_approved: bool = False):
    """Validation -> risk -> permission -> execute -> audit -> working memory.

    ask(tool, args, risk) -> True (approved) / False (denied) / None (timeout).
    Returns (ok, text, info). Text starts with ERROR / DENIED / CANCELLED on
    failure, which the agent prompt already knows how to treat.
    """
    cfg = cfg if cfg is not None else _cfg()
    resolved = resolve_tool_name(name)
    if resolved is None:
        return False, (f"ERROR: no tool named '{name}'. Closest matches: {suggest_tools_for(name)}. "
                       f"Call list_capabilities to see every capability."), {}
    name, args = resolved, dict(args or {})
    risk = tool_risk(name)
    if pre_approved:
        decision = isha_perm.Decision(isha_perm.ALLOW, risk, "approved by user")
        approval = "user"
    else:
        decision = isha_perm.decide(name, risk, cfg)
        approval = "auto"
        if decision.action == isha_perm.DENY:
            ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="blocked",
                              reason=decision.reason)
            return False, f"DENIED: {decision.reason}. Do not retry.", {"risk": risk, "approval": "blocked"}
        if decision.action == isha_perm.ASK:
            if ask is None:
                return False, "DENIED: this action needs the user's approval.", {"risk": risk}
            ACTION_LOG.set_current(step=f"waiting for approval: {name}")
            answer = ask(name, args, risk)
            if answer is None:
                ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="timeout")
                return False, ("CANCELLED: the user did not respond in time. Do not retry; "
                               "say the action was cancelled."), {"risk": risk, "approval": "timeout"}
            if not answer:
                ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="denied")
                return False, ("DENIED: the user refused this action. Do NOT retry it or attempt a "
                               "workaround. Stop and tell them it was cancelled."), \
                    {"risk": risk, "approval": "denied"}
            approval = "user"
        elif risk != "safe":
            approval = "trusted"
    if ISHA_STOP.stopped:
        return False, "CANCELLED: stopped by user.", {"risk": risk, "approval": approval}
    if approval in ("user", "trusted") and name in _CONFIRM_ARG_TOOLS:
        args[_CONFIRM_ARG_TOOLS[name]] = True
    if approval == "user":
        ACTION_LOG.record("approval", tool=name, args=args, risk=risk, approval="approved")
    _TOOL_CTX.approval = approval
    ACTION_LOG.set_current(tool=name, step=describe_tool_call(name, args)[:80])
    try:
        out, ok = str(execute_tool(name, args)), True
    except ToolError as e:
        out, ok = f"ERROR: {e}", False
    except Exception as e:  # noqa: BLE001
        out, ok = f"ERROR: {type(e).__name__}: {e}", False
    finally:
        _TOOL_CTX.approval = "direct"
    if ok:
        try:
            WORKING.note_tool(name, args, out)
        except Exception:
            pass
    return ok, out, {"risk": risk, "approval": approval}


# ---- LLM helper with role routing (used inside tools) -----------------------
def _llm_for_role(role: str, prompt: str, system: str = "", max_tokens: int = 1200) -> str:
    cfg = _cfg()
    if not _LLAMA_CPP_OK:
        raise ToolError("Local model backend available nahi hai.")
    routed = dict(cfg)
    try:
        reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
        for r in isha_router.FALLBACK.get(role, [role, "general"]):
            e = reg.get(r)
            if e:
                routed["gguf_model_path"] = str(e.path)
                break
    except Exception as e:
        _log_error("llm_for_role", e)
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    opts = {"temperature": 0.2, "top_p": 0.9, "max_tokens": max_tokens}
    try:
        data = GGUF.complete(msgs, opts, routed)
    except GGUFUnavailable as e:
        raise ToolError(str(e))
    text = strip_thinking((data.get("message") or {}).get("content", ""))
    if not text.strip():
        raise ToolError("Local model se khaali response mila.")
    return text


def _code_fixer(code: str, error: str, language: str, path: str) -> str:
    ISHA_STOP.check()
    prompt = (f"This {language} file ({Path(path).name}) fails when run.\n\n--- CODE ---\n{code}\n\n"
              f"--- ERROR ---\n{error[-2500:]}\n\nFix the bug. Keep the program's purpose and behaviour. "
              f"Do not add shell commands, file deletion or network access. Respond with ONLY the complete "
              f"corrected source code, no explanation.")
    return _llm_for_role("coding", prompt, system=f"You are an expert {language} debugger. Output only code.",
                         max_tokens=2048)


# ---- new tools --------------------------------------------------------------
@tool("find_files",
      "Fast local file search in natural language (Hindi/Hinglish/English): name, extension, "
      "media type, size, date modified, folder. Uses the local file index when ready, else scans. "
      "Examples: 'Downloads mein PDF', '10 MB se badi videos', 'kal modify hui files', 'calculator.py on desktop'.",
      {"query": {"type": "string", "required": True, "description": "What to find, in the user's own words."},
       "limit": {"type": "integer", "required": False, "description": "Max results (default 20)."}})
def _find_files(args: dict) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ToolError("Kya dhundna hai?")
    limit = max(1, min(int(args.get("limit") or 20), 100))
    res = get_search_engine().search_text(q, limit=limit, stop_flag=lambda: ISHA_STOP.stopped)
    return isha_search.format_results(res)


@tool("file_index_status", "Show the local file-search index status (entries, last update, state).")
def _file_index_status(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        return "File index band hai (settings: file_index_enabled) - search direct scan se hogi."
    s = idx.status()
    return (f"File index: {s['state']}, {s['entries']} entries, last complete: {s['last_complete'] or 'never'}"
            f"{' (paused)' if s['paused'] else ''}. Roots: {', '.join(s['roots'])}. DB stays local: {s['db']}")


@tool("rebuild_file_index", "Rebuild the local file-search index from scratch in the background.")
def _rebuild_file_index(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        raise ToolError("File index disabled hai.")
    idx.rebuild(background=True)
    return "File index rebuild background me shuru kar diya. Tab tak search direct scan se chalegi."


@tool("pause_file_index", "Pause or resume background file indexing.",
      {"pause": {"type": "boolean", "required": True, "description": "true = pause, false = resume."}})
def _pause_file_index(args: dict) -> str:
    idx = get_file_index()
    if idx is None:
        raise ToolError("File index disabled hai.")
    if args.get("pause"):
        idx.pause()
        return "File indexing pause kar di."
    idx.resume()
    if idx.state == "idle" and not idx.ready:
        idx.start_background()
    return "File indexing resume kar di."


@tool("copy_file", "Copy a file or folder to a new location. Never overwrites an existing target.",
      {"source_path": {"type": "string", "required": True, "description": "File/folder to copy."},
       "dest_path": {"type": "string", "required": True,
                     "description": "Destination folder (e.g. 'documents') or full new path."}})
def _copy_file(args: dict) -> str:
    src = _safe_resolve_path(args["source_path"])
    dst = _safe_resolve_path(args["dest_path"])
    if not src.exists():
        raise ToolError(f"Source nahi mila: {src}")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise ToolError(f"Target pehle se hai, overwrite nahi karungi: {dst}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    except Exception as e:
        raise ToolError(f"Copy fail: {e}")
    if not dst.exists():
        raise ToolError("Copy ke baad target nahi mila.")
    return f"Copy ho gaya: {dst}"


@tool("create_file", "Create a NEW text file (refuses to overwrite; use write_file_content to overwrite).",
      {"path": {"type": "string", "required": True, "description": "e.g. 'desktop/notes.txt' or absolute path."},
       "content": {"type": "string", "required": False, "description": "Initial text content."}})
def _create_file(args: dict) -> str:
    p = _safe_resolve_path(args["path"])
    if p.exists():
        raise ToolError(f"File pehle se hai: {p}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args.get("content") or ""), encoding="utf-8")
    except Exception as e:
        raise ToolError(f"File nahi bani: {e}")
    return f"File bana di: {p}"


def _software_tool(action: str, name: str) -> str:
    cfg = _cfg()
    sp = isha_sw.plan(action, name, preferred=str(cfg.get("preferred_package_manager") or ""))
    if not sp.ok and not sp.manual_command:
        raise ToolError(sp.error or f"'{name}' ka {action} plan nahi ban paaya.")
    ACTION_LOG.record("command", tool=f"{action}_software", argv=sp.argv, manager=sp.manager)
    res = isha_sw.execute(sp)
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("install_software", "Install (or 'download') an application using the OS's official package manager "
      "(winget/choco/scoop on Windows, apt/dnf/pacman/flatpak/snap on Linux). Verifies after install.",
      {"name": {"type": "string", "required": True, "description": "App name, e.g. 'firefox', 'vs code', 'vlc'."}})
def _install_software(args: dict) -> str:
    return _software_tool("install", args["name"])


@tool("uninstall_software", "Uninstall an application via the official package manager, then verify it is gone.",
      {"name": {"type": "string", "required": True, "description": "App name."}})
def _uninstall_software(args: dict) -> str:
    return _software_tool("uninstall", args["name"])


@tool("update_software", "Update/upgrade an installed application via the official package manager.",
      {"name": {"type": "string", "required": True, "description": "App name."}})
def _update_software(args: dict) -> str:
    return _software_tool("update", args["name"])


@tool("search_software", "Search the package manager for an app name and list candidate packages.",
      {"name": {"type": "string", "required": True, "description": "App name to look up."}})
def _search_software(args: dict) -> str:
    res = isha_sw.search(args["name"])
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


def _resolve_project_dir(project: str) -> Path:
    p = (project or "").strip().strip("\"'")
    cands = []
    if p:
        try:
            cands.append(_safe_resolve_path(p))
        except ToolError:
            pass
        cands.append(_desktop_dir() / p)
        try:
            cands.append(_desktop_dir() / isha_code.safe_name(p))
        except ValueError:
            pass
    if WORKING.last_project:
        cands.append(Path(WORKING.last_project))
    for c in cands:
        if c.is_dir() or c.is_file():
            return c
    if p:
        # case-insensitive match on the Desktop, then the file index
        try:
            for child in _desktop_dir().iterdir():
                if child.is_dir() and child.name.lower() == p.lower():
                    return child
        except Exception:
            pass
        idx = get_file_index(create=False)
        if idx is not None and idx.ready:
            hits = idx.search(isha_index.SearchQuery(name_terms=[p], kind="dir", limit=5))
            exact = [h for h in hits if h["name"].lower() == p.lower()]
            if exact:
                return Path(exact[0]["path"])
    raise ToolError(f"'{project}' naam ka project nahi mila (Desktop aur last project dekhe).")


def _generate_project_files(kind: str, name: str, description: str):
    """Ask the coding model for the files; None if unavailable or unusable."""
    if not _LLAMA_CPP_OK:
        return None
    files = {}
    try:
        if kind == "web":
            for fname, what in (("index.html", "the complete index.html (link style.css and script.js)"),
                                ("style.css", "the complete style.css"),
                                ("script.js", "the complete script.js (no eval, no network calls)")):
                ISHA_STOP.check()
                prior = "\n\n".join(f"--- {k} ---\n{v}" for k, v in files.items())
                code = isha_code.strip_fences(_llm_for_role(
                    "coding", f"Project '{name}': {description}\n\n{prior}\n\nWrite {what}. Output only code.",
                    system="You are an expert front-end developer. Output only the requested file's code.",
                    max_tokens=2048))
                if len(code.strip()) < 20:
                    return None
                files[fname] = code
            return isha_code.ProjectSpec(name, "web", files, "index.html", source="model", description=description)
        code = isha_code.strip_fences(_llm_for_role(
            "coding", f"Write a complete, runnable Python 3 program for: {description}\n"
                      f"Use only the standard library. If it needs user input, read it with input(). "
                      f"Output only the code for main.py.",
            system="You are an expert Python developer. Output only code.", max_tokens=2048))
        if len(code.strip()) < 20:
            return None
        return isha_code.ProjectSpec(name, "python", {"main.py": code, "README.md": f"# {name}\n\n{description}\n"},
                                     "main.py", source="model", description=description)
    except (ToolError, ISHA_Cancelled) as e:
        _log_error("generate_project", e)
        return None


@tool("create_code_project",
      "Create a new coding project folder with real files (Python or HTML/CSS/JS). Uses the local coding "
      "model when available and falls back to a working template. Never overwrites an existing project.",
      {"project_name": {"type": "string", "required": True, "description": "Folder name, e.g. 'MyApp'."},
       "project_type": {"type": "string", "required": False, "description": "'python' (default) or 'web'."},
       "description": {"type": "string", "required": False, "description": "What the program should do."},
       "location": {"type": "string", "required": False, "description": "Parent folder (default 'desktop')."}})
def _create_code_project(args: dict) -> str:
    name = str(args.get("project_name") or "").strip()
    kind = str(args.get("project_type") or "python").strip().lower()
    kind = "web" if kind in ("web", "html", "website", "html/css/js", "javascript", "js") else "python"
    desc = str(args.get("description") or "").strip()
    base = _safe_resolve_path(args.get("location") or "desktop")
    try:
        isha_code.safe_name(name)
    except ValueError as e:
        raise ToolError(str(e))
    template = isha_code.choose_template(kind, name, desc)
    wants_custom = bool(desc) and not re.search(r"calc|hisab|hisaab", desc.lower() + name.lower()) and \
        len(re.sub(r"\b(project|website|web|python|html|css|js|bana\w*|create|desktop|par|naam|ka|ek|do)\b",
                   "", desc.lower()).split()) > 2
    spec = (_generate_project_files(kind, name, desc) if wants_custom else None) or template
    res = isha_code.write_project(base, spec)
    if not res["success"]:
        raise ToolError(res["message"])
    WORKING.last_project = res["path"]
    note = "" if spec.source == "model" else (
        " (template se banaya - local coding model available nahi tha)" if wants_custom else " (template)")
    return f"{res['message']}{note}. Entry: {Path(res['entry']).name}"


@tool("run_project",
      "Run a project or code file and verify it works. Python/JS/Bash are executed with a timeout; on error "
      "the local coding model fixes the code and retries (bounded). HTML opens in the browser.",
      {"project": {"type": "string", "required": False,
                   "description": "Project folder name on Desktop, a path, or empty for the last project."},
       "auto_fix": {"type": "boolean", "required": False, "description": "Fix errors automatically (default true)."},
       "max_attempts": {"type": "integer", "required": False, "description": "Run+fix attempts (default from settings)."},
       "open_window": {"type": "boolean", "required": False,
                       "description": "Also launch interactive/GUI programs for the user (default true)."}})
def _run_project(args: dict) -> str:
    cfg = _cfg()
    target = _resolve_project_dir(str(args.get("project") or ""))
    entry = target if target.is_file() else isha_code.find_entry(target)
    if entry is None:
        raise ToolError(f"'{target}' me chalane layak entry file (main.py / index.html ...) nahi mili.")
    WORKING.last_project = str(target if target.is_dir() else target.parent)
    if entry.suffix.lower() in (".html", ".htm"):
        webbrowser.open(entry.resolve().as_uri())
        return f"'{entry.name}' browser me khol diya: {entry}"
    meta = isha_code.load_meta(entry.parent)
    code = entry.read_text(encoding="utf-8", errors="replace")
    attempts = int(args.get("max_attempts") or cfg.get("max_auto_fix_attempts", 3))
    fixer = _code_fixer if (args.get("auto_fix", True) and _LLAMA_CPP_OK) else None
    gui = isha_code.is_gui_or_server(code)
    res = isha_code.fix_loop(entry, fixer=fixer, max_attempts=attempts,
                             timeout=6 if gui else int(cfg.get("code_run_timeout", 20)),
                             stdin_text=meta.get("test_input") or ("" if re.search(r"\binput\s*\(", code) else None),
                             on_step=lambda s: ACTION_LOG.set_current(step=s))
    ACTION_LOG.record("code_run", path=str(entry), status=res.status, attempts=res.attempts, log=res.log)
    msg = res.message()
    if not res.success:
        if fixer is None and res.status == "failed":
            msg += "\n(Auto-fix ke liye local coding model load hona chahiye.)"
        raise ToolError(msg)
    interactive = gui or bool(re.search(r"\binput\s*\(|sys\.stdin", code))
    if interactive and args.get("open_window", True) and (_IS_WINDOWS or gui or shutil.which("x-terminal-emulator")
                                                           or shutil.which("gnome-terminal")):
        launched = isha_code.launch_detached(entry)
        msg += "\n" + launched["message"]
    return msg


@tool("fix_code_file", "Run an existing code file; if it errors, read the error, fix the code with the local "
      "coding model and re-run, up to the configured number of attempts.",
      {"file_path": {"type": "string", "required": True, "description": "Path or Desktop file name, e.g. 'app.py'."},
       "max_attempts": {"type": "integer", "required": False, "description": "Attempts (default from settings)."},
       "test_input": {"type": "string", "required": False, "description": "Optional stdin to feed the program."}})
def _fix_code_file(args: dict) -> str:
    raw = str(args.get("file_path") or "").strip()
    p = None
    try:
        p = _safe_resolve_path(raw)
    except ToolError:
        pass
    if p is None or not p.is_file():
        cand = list(_desktop_dir().rglob(raw))[:1] if raw else []
        p = cand[0] if cand else None
    if p is None or not p.is_file():
        raise ToolError(f"File nahi mili: {raw}")
    if not _LLAMA_CPP_OK:
        raise ToolError("Bug fix ke liye local coding model chahiye, jo abhi load nahi hai.")
    cfg = _cfg()
    res = isha_code.fix_loop(p, fixer=_code_fixer,
                             max_attempts=int(args.get("max_attempts") or cfg.get("max_auto_fix_attempts", 3)),
                             timeout=int(cfg.get("code_run_timeout", 20)),
                             stdin_text=args.get("test_input"), on_step=lambda s: ACTION_LOG.set_current(step=s))
    ACTION_LOG.record("code_run", path=str(p), status=res.status, attempts=res.attempts, log=res.log)
    if not res.success:
        raise ToolError(res.message())
    return res.message()


@tool("inspect_project", "List the files of a project folder and its entry point, without running anything.",
      {"project": {"type": "string", "required": False, "description": "Project name/path (default: last project)."}})
def _inspect_project(args: dict) -> str:
    d = _resolve_project_dir(str(args.get("project") or ""))
    if d.is_file():
        d = d.parent
    files = [str(f.relative_to(d)) for f in sorted(d.rglob("*")) if f.is_file()
             and not any(part.startswith(".") for part in f.relative_to(d).parts)][:40]
    entry = isha_code.find_entry(d)
    return f"Project {d}\nEntry: {entry.name if entry else 'unknown'}\nFiles: " + ", ".join(files)


@tool("remember_fact", "Remember something the user EXPLICITLY asked to remember (preference, project, folder...). "
      "Stored locally. Refuses passwords/PINs/OTPs.",
      {"fact": {"type": "string", "required": True, "description": "The fact, in the user's words."},
       "category": {"type": "string", "required": False,
                    "description": "preference, project, folder, app, person or fact."}})
def _remember_fact(args: dict) -> str:
    res = LONG_MEMORY.remember(str(args.get("fact") or ""), str(args.get("category") or ""))
    if not res["success"]:
        raise ToolError(res["message"])
    try:
        _vector_memory.add_memory(res["item"]["text"] if res.get("item") else str(args["fact"]))
    except Exception:
        pass
    return res["message"]


@tool("forget_memory", "Forget a remembered fact (or 'all').",
      {"query": {"type": "string", "required": True, "description": "Words from the fact, or 'all'."}})
def _forget_memory(args: dict) -> str:
    res = LONG_MEMORY.forget(str(args.get("query") or ""))
    if not res["success"]:
        raise ToolError(res["message"])
    return res["message"]


@tool("show_memory", "Show everything ISHA has been asked to remember.")
def _show_memory(args: dict) -> str:
    return LONG_MEMORY.format()


@tool("recall_conversation", "Recall what was discussed on a given day from local chat history.",
      {"day": {"type": "string", "required": False, "description": "'today', 'yesterday' or YYYY-MM-DD (default yesterday)."},
       "query": {"type": "string", "required": False, "description": "Optional topic words to filter by."}})
def _recall_conversation(args: dict) -> str:
    day = str(args.get("day") or "yesterday").strip().lower()
    from datetime import timedelta as _td
    if day in ("today", "aaj"):
        key = datetime.now().strftime("%Y-%m-%d")
    elif day in ("yesterday", "kal"):
        key = (datetime.now() - _td(days=1)).strftime("%Y-%m-%d")
    else:
        key = day
    entries = load_history().get(key) or []
    q = set(re.findall(r"\w+", str(args.get("query") or "").lower()))
    if q:
        entries = [e for e in entries if q & set(re.findall(r"\w+", e.get("text", "").lower()))]
    if not entries:
        return f"{key} ki koi baat-cheet history me nahi mili."
    lines = [f"{e.get('time', '')} {e.get('role')}: {str(e.get('text', ''))[:160]}" for e in entries[-12:]]
    return f"{key} ki baat-cheet (last {len(lines)}):\n" + "\n".join(lines)


@tool("get_hardware_info", "CPU, RAM, GPU/VRAM, disk and OS details of this PC.")
def _get_hardware_info(args: dict) -> str:
    return _hw(0).summary()


@tool("get_system_info", "Full system information: OS, hardware, storage, network, battery, ISHA models.")
def _get_system_info(args: dict) -> str:
    parts = [_hw(0).summary()]
    for fn in (_get_storage_info, _check_battery):
        try:
            parts.append(fn({}))
        except Exception:
            pass
    try:
        parts.append(_get_model_status({}))
    except Exception:
        pass
    return "\n".join(parts)


@tool("get_model_status", "Which local models are installed per role, which is active/loaded, and the backend.")
def _get_model_status(args: dict) -> str:
    cfg = _cfg()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    rows = []
    for r in reg.describe():
        extra = f" ({r['size_gb']} GB, {r['source']})" if r.get("path") else ""
        rows.append(f"  {r['role']}: {Path(r['path']).name if r['path'] else '-'} [{r['status']}]{extra}")
    loaded = ", ".join(GGUF.loaded_names()) or "none"
    return (f"Mode: {cfg.get('model_mode', 'auto')} | Backend: {_LLAMA_MODE} "
            f"({'ok' if _LLAMA_CPP_OK else 'UNAVAILABLE'}) | Loaded: {loaded} | "
            f"Last used: {ACTIVE_MODEL.get('name') or '-'} for {ACTIVE_MODEL.get('task') or '-'}\n"
            + "\n".join(rows))


@tool("switch_model", "Switch ISHA's model selection: auto, general, coding, reasoning, study, fast or vision.",
      {"mode": {"type": "string", "required": True, "description": "auto | general | coding | reasoning | study | fast | vision"}})
def _switch_model(args: dict) -> str:
    mode = str(args.get("mode") or "").strip().lower()
    mode = {"code": "coding", "coder": "coding", "reason": "reasoning", "chat": "general", "normal": "general",
            "automatic": "auto", "padhai": "study"}.get(mode, mode)
    if mode not in isha_router.MANUAL_MODES:
        raise ToolError(f"'{mode}' valid mode nahi hai. Options: {', '.join(isha_router.MANUAL_MODES)}")
    cfg = load_config()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    if mode != "auto" and reg.get(mode) is None:
        fb = next((r for r in isha_router.FALLBACK.get(mode, []) if reg.get(r)), None)
        note = f" '{mode}' model install nahi hai" + (f", isliye '{fb}' model use hoga." if fb else
                                                       " aur koi fallback bhi nahi hai.")
    else:
        note = ""
    cfg["model_mode"] = mode
    save_config(cfg)
    ACTION_LOG.record("model_mode", mode=mode)
    return f"Model mode ab '{mode}' hai.{note}"


def _vision_paths():
    cfg = _cfg()
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    e = reg.get("vision")
    return (e.path if e else None), reg.vision_projector()


def _analyze_image(path: Path, question: str) -> str:
    model, proj = _vision_paths()
    st = isha_vision.status(model, proj, _LLAMA_MODE)
    if st["model"]:
        try:
            return isha_vision.describe_with_model(path, question or "Describe this image briefly.", model, proj,
                                                   n_threads=_auto_threads())
        except Exception as e:
            _log_error("vision:model", e)
            if not st["ocr"]:
                raise ToolError(f"Vision model fail hua: {e}")
    if st["ocr"]:
        text = isha_vision.ocr(path)
        head = "Vision model available nahi hai, isliye sirf text padha (OCR)"
        return f"{head}:\n{text[:2500] or '(koi text nahi mila)'}"
    raise ToolError("Image dekhne ki capability abhi nahi hai: " + st["model_reason"] +
                    "; OCR bhi nahi: " + st["ocr_reason"])


@tool("describe_image", "Look at an image file with the local vision model (or OCR fallback) and answer a question.",
      {"image_path": {"type": "string", "required": True, "description": "Path to the image."},
       "question": {"type": "string", "required": False, "description": "What to look for / read."}})
def _describe_image(args: dict) -> str:
    p = _safe_resolve_path(args["image_path"])
    if not p.is_file():
        raise ToolError(f"Image nahi mili: {p}")
    return _analyze_image(p, str(args.get("question") or ""))


@tool("describe_screen", "Take a screenshot and describe / read what is on the screen (local vision model or OCR).",
      {"question": {"type": "string", "required": False, "description": "e.g. 'find the Save button'."}})
def _describe_screen(args: dict) -> str:
    shot = hand_take_screenshot()
    if not shot.get("success"):
        raise ToolError(shot.get("message"))
    return _analyze_image(Path(shot["data"]["path"]), str(args.get("question") or "What is on this screen?"))


@tool("get_activity_log", "Show ISHA's recent action log (tools run, approvals, results).",
      {"count": {"type": "integer", "required": False, "description": "How many entries (default 15)."}})
def _get_activity_log(args: dict) -> str:
    return ACTION_LOG.format_recent(max(1, min(int(args.get("count") or 15), 100)))


TOOL_RISK.update({
    "find_files": "safe", "file_index_status": "safe", "rebuild_file_index": "safe",
    "pause_file_index": "safe", "copy_file": "safe", "create_file": "safe",
    "recycle_bin_status": "safe", "empty_recycle_bin": "confirm",
    "install_software": "confirm", "uninstall_software": "confirm", "update_software": "confirm",
    "search_software": "safe",
    "create_code_project": "safe", "inspect_project": "safe",
    "run_project": "critical", "fix_code_file": "critical",
    "remember_fact": "safe", "forget_memory": "safe", "show_memory": "safe", "recall_conversation": "safe",
    "get_hardware_info": "safe", "get_system_info": "safe", "get_model_status": "safe", "switch_model": "safe",
    "describe_image": "safe", "describe_screen": "safe", "get_activity_log": "safe",
})

# Spec categories. Old category names stay valid as filter aliases.
TOOL_CATEGORY.clear()
TOOL_CATEGORY.update({
    "files": ("find_files", "search_files", "advanced_file_search", "list_media_files", "read_file_content",
              "write_file_content", "create_file", "copy_file", "move_or_rename_file", "delete_file_safely",
              "batch_delete_files", "create_desktop_folder", "create_folder", "list_directory", "open_folder",
              "empty_recycle_bin", "recycle_bin_status", "file_index_status", "rebuild_file_index",
              "pause_file_index"),
    "applications": ("open_app", "open_notepad", "open_calculator", "open_browser", "open_task_manager",
                     "close_application", "minimize_window", "maximize_window", "close_window", "switch_window",
                     "list_open_windows", "install_software", "uninstall_software", "update_software",
                     "search_software"),
    "system": ("check_cpu_usage", "check_memory_usage", "check_battery", "get_system_stats", "get_gpu_usage",
               "get_storage_info", "get_hardware_info", "get_system_info", "list_running_processes",
               "kill_process", "system_power_action", "lock_screen", "set_brightness", "isha_self_check",
               "get_model_status", "switch_model", "get_activity_log", "get_time", "get_date"),
    "network": ("get_wifi_status", "get_network_info", "open_url", "open_website", "web_search",
                "close_browser_tab"),
    "media": ("play_song", "pause_song", "resume_song", "stop_song", "youtube_play", "mute_system",
              "set_volume", "take_screenshot"),
    "messaging": ("send_whatsapp_message", "save_whatsapp_contact", "list_whatsapp_contacts"),
    "coding": ("create_code_project", "run_project", "fix_code_file", "inspect_project", "create_and_save_code",
               "write_and_save_code", "run_code_file", "list_and_run_desktop_code", "autonomous_code_agent",
               "run_terminal_command"),
    "vision": ("describe_screen", "describe_image", "take_screenshot"),
    "memory": ("remember_fact", "forget_memory", "show_memory", "recall_conversation",
               "search_long_term_memory", "store_long_term_memory"),
    "automation": ("type_text", "press_hotkey", "clipboard_read", "clipboard_write", "list_capabilities"),
})
_CATEGORY_ALIASES = {"apps": "applications", "windows": "applications", "code": "coding", "comms": "messaging",
                     "display": "system", "input": "automation", "meta": "system"}
TOOL_ALIASES.update({
    "focus_app": "switch_window", "focus_application": "switch_window", "focus_window": "switch_window",
    "search_files_index": "find_files", "locate_file": "find_files", "find": "find_files",
    "install": "install_software", "install_app": "install_software", "download_software": "install_software",
    "uninstall": "uninstall_software", "remove_app": "uninstall_software", "uninstall_app": "uninstall_software",
    "update_app": "update_software", "upgrade_software": "update_software",
    "create_project": "create_code_project", "new_project": "create_code_project",
    "run_app_project": "run_project", "execute_project": "run_project", "debug_code": "fix_code_file",
    "fix_code": "fix_code_file", "fix_bug": "fix_code_file",
    "remember_this": "remember_fact", "save_memory": "remember_fact", "forget": "forget_memory",
    "list_memory": "show_memory", "memories": "show_memory",
    "hardware": "get_hardware_info", "system_info": "get_system_info", "sysinfo": "get_system_info",
    "model_status": "get_model_status", "change_model": "switch_model", "set_model": "switch_model",
    "see_screen": "describe_screen", "screen_describe": "describe_screen", "analyze_image": "describe_image",
    "read_image": "describe_image", "ocr": "describe_image", "trash_status": "recycle_bin_status",
    "activity_log": "get_activity_log", "copy": "copy_file", "new_file": "create_file",
})
