

# ---- deterministic intent matcher for the new capabilities ------------------
# Runs inside match_task() BEFORE the web-search branch, which used to turn
# "Downloads mein PDF dhundo" into a Google search.
_FS_VERB = re.compile(r"\b(dhund\w*|dhoond\w*|dhundh\w*|khoj\w*|find|search|locate|dikhao|dikha\s*do|"
                      r"list\s+kar\w*|kaha\s+hai|kahan\s+hai|kahaan\s+hai)\b", re.I)
_FS_OBJ = re.compile(r"\b(files?|folders?|pdfs?|photos?|images?|pics?|pictures?|videos?|mp4|mp3|songs?|audio|"
                     r"documents?|docs?|zip|rar|archives?|screenshots?|downloads|desktop|documents|"
                     r"python\s+files?|code\s+files?|excel|ppt|word\s+files?|apps?\s+files?|"
                     r"[\w\-]+\.[a-z0-9]{1,5})\b", re.I)
_WEBISH = re.compile(r"\b(google|youtube|you\s*tube|chrome|firefox|edge|brave|website|web\s*site|online|internet|"
                     r"browser)\b", re.I)
_SW_VERB = re.compile(r"\b(install\w*|uninstall\w*|download(?:\s+kar\w*)?|update\w*|upgrade\w*|remove|hata\w*)\b", re.I)
_PROJECT_WORD = re.compile(r"\b(project|website|web\s*site|web\s*app)\b", re.I)
_BUILD_VERB = re.compile(r"\b(bana\w*|bnao|create|make|build|generate|setup)\b", re.I)
_RUN_VERB = re.compile(r"\b(run|chala\w*|execute|start)\b", re.I)


def _extract_named(text: str):
    m = (re.search(r"[\"“']([^\"”']{1,60})[\"”']", text)
         or re.search(r"\b([\w\-]{1,60})\s+(?:naam|name|nam)\s+(?:ka|ki|ke|se|wala|wali)\b", text, re.I)
         or re.search(r"\b(?:named|called|naam)\s+([\w\-]{1,60})\b", text, re.I))
    return m.group(1).strip() if m else ""


def _software_target(text: str) -> str:
    t = _SW_VERB.sub(" ", text)
    t = re.sub(r"\b(karo|kar\s*do|kardo|kar|do|dijiye|please|plz|isha|mere|meri|mera|system|pc|computer|se|me|"
               r"mein|par|pe|ko|ka|ki|ke|the|app|software|package|latest|version|jaldi)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .,!?")


def match_agent_command(segment: str):
    s = (segment or "").strip(" .!?")
    if not s:
        return None
    low = s.lower()

    mem = isha_mem.parse_memory_command(s)
    if mem:
        return mem
    if re.search(r"\b(kal|yesterday)\b.*\b(baat|conversation|chat)\b.*\b(yaad|remember)\b|"
                 r"\bkal\s+(?:wali|ki)\s+baat\b", low):
        return ("recall_conversation", {"day": "yesterday"})

    # model control
    m = re.search(r"\b(auto|general|coding|code|reasoning|study|fast|vision)\s+(?:model|mode)\b.*\b(use|lagao|kar\w*|"
                  r"switch|chalao|set)\b|\bswitch\s+to\s+(auto|general|coding|reasoning|study|fast|vision)\b", low)
    if m:
        return ("switch_model", {"mode": (m.group(1) or m.group(3))})
    if re.search(r"\b(kaun\s*sa|which|konsa|current)\s+model\b|\bmodel\s+(status|info)\b", low):
        return ("get_model_status", {})

    # vision
    if re.search(r"\b(screen\s+(?:par|pe|per)\s+kya|what'?s?\s+(?:is\s+)?on\s+(?:my\s+)?screen|"
                 r"screen\s+dekh\w*|screen\s+padh\w*|read\s+(?:my\s+)?screen)\b", low):
        return ("describe_screen", {"question": s})

    # system information
    if re.search(r"\bsystem\s+(information|info|details)\b|\b(pc|computer|laptop)\s+(?:ki\s+)?(?:details|info|specs?)\b",
                 low):
        return ("get_system_info", {})
    if re.search(r"\bhardware\s+(info|details)\b|\b(gpu|vram)\s+(?:kaun|kitna|kitni|info)\b", low):
        return ("get_hardware_info", {})
    if re.search(r"\b(disk|drive|storage|hard\s*disk|ssd)\b.*\b(space|jagah|kitni|kitna|free|khaali)\b|"
                 r"\bkitni\s+(space|jagah)\b", low):
        return ("get_storage_info", {})
    if re.search(r"\b(index)\b.*\b(status|kitna|ready)\b|\bfile\s+index\b", low):
        return ("file_index_status", {})
    if re.search(r"\b(activity|action)\s+log\b|\bkya\s+kya\s+kiya\b", low):
        return ("get_activity_log", {})

    # software install / uninstall / update (only for catalogued apps unless verb is explicit)
    vm = _SW_VERB.search(low)
    if vm and not re.search(r"\b(file|files|folder|photo|pdf|video|downloads)\b", low):
        verb = vm.group(1).lower()
        target = _software_target(s)
        key, _ = isha_sw.canonical(target)
        action = ("uninstall" if verb.startswith(("uninstall", "remove", "hata")) else
                  "update" if verb.startswith(("update", "upgrade")) else "install")
        if target and (key or verb.startswith(("install", "uninstall", "upgrade"))):
            if verb.startswith("hata") and not key:
                pass
            else:
                return (f"{action}_software", {"name": target})

    # project creation
    if _BUILD_VERB.search(low) and (_PROJECT_WORD.search(low) or
                                    (re.search(r"\bdesktop\b", low) and re.search(r"\b(python|html|app|program|calculator)\b", low))):
        name = _extract_named(s)
        web = bool(re.search(r"\b(html|css|website|web\s*site|web\s*app|javascript|js)\b", low))
        if not name:
            name = ("Calculator" if re.search(r"calculator|calc", low) else "MyWebsite" if web else "MyPythonApp")
        loc = "desktop"
        for k in ("documents", "downloads"):
            if re.search(rf"\b{k}\b", low):
                loc = k
        return ("create_code_project", {"project_name": name, "project_type": "web" if web else "python",
                                        "description": s, "location": loc})

    # fix a bug in a file / project
    if re.search(r"\b(bug|error|galti)\b", low) and re.search(r"\b(fix|theek|thik|sudhar|solve)\w*", low):
        fm = re.search(r"([\w\-]+\.(?:py|js|sh))\b", s)
        if fm:
            return ("fix_code_file", {"file_path": fm.group(1)})
        if re.search(r"\bproject\b", low) or WORKING.last_project:
            nm = re.search(r"\b([A-Z][\w\-]+)\s+(?:project|ka|ke|ko)\b", s)
            return ("run_project", {"project": nm.group(1) if nm else "", "auto_fix": True})

    # run a project: "TestApp run karo", "is project ko run karke error fix karo"
    if _RUN_VERB.search(low) and not re.search(r"\.(py|js|sh|bat|cmd)\b", low) and \
            not re.search(r"\b(gaana|gana|song|music|video|youtube|chrome|browser)\b", low):
        m = re.search(r"^(?:isha\s+)?(?:mera\s+|meri\s+|is\s+|us\s+)?([\w\-]+)(?:\s+project)?\s+(?:ko\s+)?"
                      r"(?:run|chala\w*|execute|start)\b", s, re.I)
        name = m.group(1) if m else ""
        if name.lower() in ("project", "code", "program", "app", "is", "isko", "usko"):
            name = ""
        if name:
            try:
                _resolve_project_dir(name)
                return ("run_project", {"project": name})
            except ToolError:
                pass
        if re.search(r"\bproject\b", low) and (WORKING.last_project or name):
            return ("run_project", {"project": name})

    # pronoun follow-ups on the last file found: move / rename / delete
    if WORKING.refers_back(s) and WORKING.resolve_file():
        src = WORKING.resolve_file()
        mm = re.search(r"\b(desktop|documents|downloads|pictures|music|videos)\b.*\b(move|shift|daal\w*|bhej\w*|rakh\w*)\b|"
                       r"\b(move|shift)\b.*\b(?:to\s+)?(desktop|documents|downloads|pictures|music|videos)\b", low)
        if mm:
            dest = mm.group(1) or mm.group(4)
            return ("move_or_rename_file", {"source_path": src,
                                            "dest_path": str(isha_paths.known_folder(dest) / Path(src).name)})
        rn = re.search(r"\bnaam\s+(?:change\s+kar\w*\s+)?(?:ko\s+)?[\"']?([\w\-. ]{1,60}?)[\"']?\s+(?:kar\w*|rakh\w*|do)\b|"
                       r"\brename\s+(?:it\s+|this\s+)?(?:to\s+)?[\"']?([\w\-. ]{1,60})[\"']?$", s, re.I)
        if rn:
            new = (rn.group(1) or rn.group(2)).strip()
            if new and not re.fullmatch(r"(change|badal\w*)", new, re.I):
                if not Path(new).suffix:
                    new += Path(src).suffix
                return ("move_or_rename_file", {"source_path": src, "dest_path": str(Path(src).with_name(new))})
        if re.search(r"\b(delete|hata\w*|mita\w*|remove)\b", low):
            return ("batch_delete_files", {"file_paths": src})
        if re.search(r"\b(copy)\b", low):
            dm = re.search(r"\b(desktop|documents|downloads|pictures|music|videos)\b", low)
            if dm:
                return ("copy_file", {"source_path": src, "dest_path": dm.group(1)})

    # local file search
    if _FS_VERB.search(low) and _FS_OBJ.search(low) and not _WEBISH.search(low):
        return ("find_files", {"query": s})
    if re.search(r"\b(meri|mera|my)\s+(last|latest|recent)\s+(wali|wala)?\s*(photo|pdf|file|video|download|screenshot)\b", low):
        return ("find_files", {"query": s})
    return None


def route_request(text: str, cfg: dict):
    reg = isha_router.ModelRegistry(cfg, MODELS_DIR, PROJECT_ROOT, discover=find_gguf_models)
    hw = _hw() if bool(cfg.get("hardware_aware_loading", True)) else None
    loaded = [e["path"] for e in list(GGUF._models.values())]
    decision = isha_router.ModelRouter(reg, hw, loaded).route(text, cfg.get("model_mode", "auto"))
    return decision, reg, hw
