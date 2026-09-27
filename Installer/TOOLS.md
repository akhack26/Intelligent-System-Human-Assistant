# ISHA Tools

Generated from the live registry (`TOOL_REGISTRY`) — every tool ISHA can call, its category and risk tier.
Risk tiers are explained in [SECURITY.md](SECURITY.md). **RO** = read-only (runs automatically at every autonomy level).

Total: **94 tools**.

## Files

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `find_files` | safe (RO) | `query`*, `limit` | Fast local file search in natural language (Hindi/Hinglish/English): name, extension, media type, size, date modified, folder. Uses the local file index when ready, else scans. Examples: 'Downloads mein PDF', '10 MB se badi videos', 'kal modify hui files', 'calculator.py on desktop'. |
| `search_files` | safe (RO) | `query`*, `location` | Search for files by name inside a whitelisted user folder (Desktop, Documents, Downloads, or full home). |
| `advanced_file_search` | safe (RO) | `query`, `location`, `extensions`, `media_type`, `min_size_mb`, `max_size_mb`, `modified_within_days` | Deep-search files by name, extension, media type, size range, or how recently they were modified, inside a whitelisted user folder (Desktop, Documents, Downloads, Pictures, Music, Videos, or full home). |
| `list_media_files` | safe (RO) | `media_type`*, `location` | List a user's photos, videos, music, or documents from a folder without needing a filename query. |
| `read_file_content` | safe (RO) | `file_path`*, `max_lines` | Read the text content of a file from disk. |
| `write_file_content` | confirm | `file_path`*, `content`* | Write or overwrite text content to a file at a specific path. |
| `create_file` | safe | `path`*, `content` | Create a NEW text file (refuses to overwrite; use write_file_content to overwrite). |
| `copy_file` | safe | `source_path`*, `dest_path`* | Copy a file or folder to a new location. Never overwrites an existing target. |
| `move_or_rename_file` | confirm | `source_path`*, `dest_path`* | Move or rename a file or folder. |
| `delete_file_safely` | confirm | `file_path`*, `confirm` | Safely delete a file or directory with explicit confirmation. |
| `batch_delete_files` | critical | `file_paths`*, `confirm` | Safely delete one or more files/folders (sent to Recycle Bin/Trash when possible). ALWAYS requires the caller to have already confirmed this with the user before setting confirm=true. |
| `create_desktop_folder` | safe | `folder_name`* | Create a new folder on the Desktop. |
| `create_folder` | safe | `path`*, `folder_name`* | Create a folder anywhere the user can write. Accepts friendly locations like 'desktop', 'downloads/reports' or a full absolute path. |
| `list_directory` | safe (RO) | `path`*, `extension`, `max_results` | List the contents of a folder, newest first. Use this before acting on files so paths are never guessed. |
| `open_folder` | safe | `path`* | Open a folder in the file manager (Explorer / Finder / Nautilus). |
| `empty_recycle_bin` | confirm | — | Permanently empty the Windows Recycle Bin / Linux Trash / macOS Trash. Counts items before and after, and only reports success when the bin is verified empty. |
| `recycle_bin_status` | safe (RO) | — | How many items / how much space is in the Recycle Bin (Trash) right now. |
| `file_index_status` | safe (RO) | — | Show the local file-search index status (entries, last update, state). |
| `rebuild_file_index` | safe | — | Rebuild the local file-search index from scratch in the background. |
| `pause_file_index` | safe | `pause`* | Pause or resume background file indexing. |

## Applications

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `open_app` | safe | `app_name`* | Open ANY application, system window, settings page or shell location on this PC by name - for example 'recycle bin', 'notepad', 'settings', 'control panel', 'device manager', 'chrome', 'vscode', 'downloads'. Use this whenever the user asks to open / launch / start / kholo something and no more specific tool fits. You CAN open apps on this machine - never say you lack the capability, call this tool instead. |
| `open_notepad` | safe | — | Open Text Editor / Notepad. |
| `open_calculator` | safe | — | Open Calculator app. |
| `open_browser` | safe | `url` | Open the default web browser, optionally navigating to a URL. |
| `open_task_manager` | safe | — | Open System Monitor / Task Manager. |
| `close_application` | confirm | `app_name`* | Close a running application by name (gracefully first). Use this for 'Chrome band karo' style requests. |
| `minimize_window` | safe | `title` | Minimize a window. If title is omitted, minimizes the current active window. |
| `maximize_window` | safe | `title` | Maximize a window. If title is omitted, maximizes the current active window. |
| `close_window` | safe | `title`* | Close a window by (partial) title. |
| `switch_window` | safe | `title`* | Switch to (bring to foreground) a window by partial title, like a targeted Alt-Tab. |
| `list_open_windows` | safe (RO) | — | List the titles of all currently open application windows. |
| `install_software` | confirm | `name`* | Install (or 'download') an application using the OS's official package manager (winget/choco/scoop on Windows, apt/dnf/pacman/flatpak/snap on Linux). Verifies after install. |
| `uninstall_software` | confirm | `name`* | Uninstall an application via the official package manager, then verify it is gone. |
| `update_software` | confirm | `name`* | Update/upgrade an installed application via the official package manager. |
| `search_software` | safe (RO) | `name`* | Search the package manager for an app name and list candidate packages. |

## System

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `check_cpu_usage` | safe (RO) | — | Get the current CPU usage percentage. |
| `check_memory_usage` | safe (RO) | — | Get current RAM usage. |
| `check_battery` | safe (RO) | — | Get current battery percentage and charging status. |
| `get_system_stats` | safe (RO) | — | Get current CPU, RAM, Battery, Disk, and GPU usage. |
| `get_gpu_usage` | safe (RO) | — | Get current GPU utilization and VRAM usage (NVIDIA GPUs only). |
| `get_storage_info` | safe (RO) | — | Get disk / storage usage for every drive: total, used and free space. |
| `get_hardware_info` | safe (RO) | — | CPU, RAM, GPU/VRAM, disk and OS details of this PC. |
| `get_system_info` | safe (RO) | — | Full system information: OS, hardware, storage, network, battery, ISHA models. |
| `list_running_processes` | safe (RO) | `sort_by`, `max_results` | List currently running processes/apps, sorted by memory or CPU usage. |
| `kill_process` | confirm | `pid`, `name`, `force` | Terminate/kill an unresponsive process safely, by PID or by name. Refuses to touch critical OS processes. |
| `system_power_action` | critical | `action`*, `confirm` | Put the system to sleep, restart it, or shut it down. ALWAYS requires the caller to have already confirmed this with the user before setting confirm=true — this action cannot be undone once started. |
| `lock_screen` | safe | — | Lock the workstation. |
| `set_brightness` | safe | `percent`* | Set the display screen brightness to an exact percentage (0-100%). |
| `isha_self_check` | safe (RO) | — | Run ISHA's self-diagnostics: which subsystems, models and libraries are actually available right now. |
| `get_model_status` | safe (RO) | — | Which local models are installed per role, which is active/loaded, and the backend. |
| `switch_model` | safe | `mode`* | Switch ISHA's model selection: auto, general, coding, reasoning, study, fast or vision. |
| `get_activity_log` | safe (RO) | `count` | Show ISHA's recent action log (tools run, approvals, results). |
| `get_time` | safe (RO) | — | Get the current local time. |
| `get_date` | safe (RO) | — | Get the current date. |

## Network

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `get_wifi_status` | safe (RO) | — | Check whether Wi-Fi is connected, and to which network. |
| `get_network_info` | safe (RO) | — | Get network details: hostname, local IP addresses, and per-interface status. |
| `open_url` | safe | `url`* | Open a specific URL in the default web browser. |
| `open_website` | safe | `target`*, `browser` | Open a website in a browser. Give a URL OR just a name/topic (e.g. 'elegoo giga', 'arduino docs') — ISHA finds the most relevant real site by itself and opens it. |
| `web_search` | safe | `query`*, `browser` | Search something on Google in a browser. |
| `close_browser_tab` | safe | — | Close the current tab of the active browser window (Ctrl+W). |

## Media

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `play_song` | safe | `query`* | Play a song/gaana by name. Searches the local Music, Downloads and Desktop folders first for a matching audio file; if nothing matches, opens and plays the top YouTube result instead. |
| `pause_song` | safe | — | Pause the locally-playing song (does not affect a YouTube browser tab). |
| `resume_song` | safe | — | Resume a locally-playing song that was paused. |
| `stop_song` | safe | — | Stop the song currently playing locally (does not affect a YouTube browser tab). |
| `youtube_play` | safe | `query`*, `browser` | Play a song or video on YouTube directly (opens the top result, not just the search page). Use this whenever the user mentions YouTube. |
| `mute_system` | safe | `muted`* | Mute or unmute the system audio output. |
| `set_volume` | safe | `percent`* | Set the system master volume to an exact percentage. |
| `take_screenshot` | safe (RO) | — | Capture the current screen and save it as a PNG file. |

## Messaging

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `send_whatsapp_message` | confirm | `contact`, `message`, `browser` | Send a WhatsApp message to a contact. `contact` is a saved contact name (e.g. 'Rahul', 'Mummy') or a phone number. Use this whenever the user asks to message / text / WhatsApp someone. |
| `save_whatsapp_contact` | safe | `name`*, `phone`* | Save (or update) a contact's phone number for WhatsApp messaging. |
| `list_whatsapp_contacts` | safe (RO) | — | List contacts saved for WhatsApp messaging. |

## Coding

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `create_code_project` | safe | `project_name`*, `project_type`, `description`, `location` | Create a new coding project folder with real files (Python or HTML/CSS/JS). Uses the local coding model when available and falls back to a working template. Never overwrites an existing project. |
| `run_project` | critical | `project`, `auto_fix`, `max_attempts`, `open_window` | Run a project or code file and verify it works. Python/JS/Bash are executed with a timeout; on error the local coding model fixes the code and retries (bounded). HTML opens in the browser. |
| `fix_code_file` | critical | `file_path`*, `max_attempts`, `test_input` | Run an existing code file; if it errors, read the error, fix the code with the local coding model and re-run, up to the configured number of attempts. |
| `inspect_project` | safe (RO) | `project` | List the files of a project folder and its entry point, without running anything. |
| `create_and_save_code` | confirm | `file_name`*, `code_content`*, `folder_name` | Automatically create a specified folder on Desktop (if provided/doesn't exist), write source code cleanly into a file with UTF-8 encoding, and return confirmation with the absolute file path. |
| `write_and_save_code` | confirm | `file_name`*, `code_content`*, `folder_name` | Alias tool for create_and_save_code. Write source code into a file inside a specified folder on Desktop. |
| `run_code_file` | critical | `file_name_or_path`* | Search for a code file on Desktop (or by path) and execute Python scripts non-blockingly, HTML files in web browser, or executable files in background process. |
| `list_and_run_desktop_code` | critical | `query` | Scan Desktop for existing code files (.py, .js, .bat, .sh), match user's requested query or filename, and execute it directly in a non-blocking background process. |
| `autonomous_code_agent` | critical | `file_name`*, `task_description`, `code_content`, `folder_name`, `max_attempts` | Write code for a task, run it, and if it fails automatically debug and retry using the local LLM (feeding the error back in) — up to a bounded number of attempts — until it runs successfully or attempts run out. Supports Python (.py), Node.js (.js), and Bash (.sh). |
| `run_terminal_command` | critical | `command`* | Execute a cross-platform terminal or shell command safely and capture output. |

## Vision

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `describe_screen` | safe (RO) | `question` | Take a screenshot and describe / read what is on the screen (local vision model or OCR). |
| `describe_image` | safe (RO) | `image_path`*, `question` | Look at an image file with the local vision model (or OCR fallback) and answer a question. |
| `take_screenshot` | safe (RO) | — | Capture the current screen and save it as a PNG file. |

## Memory

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `remember_fact` | safe | `fact`*, `category` | Remember something the user EXPLICITLY asked to remember (preference, project, folder...). Stored locally. Refuses passwords/PINs/OTPs. |
| `forget_memory` | safe | `query`* | Forget a remembered fact (or 'all'). |
| `show_memory` | safe (RO) | — | Show everything ISHA has been asked to remember. |
| `recall_conversation` | safe | `day`, `query` | Recall what was discussed on a given day from local chat history. |
| `search_long_term_memory` | safe (RO) | `query`* | Search long-term vector memory for past conversation facts, preferences, or user context. |
| `store_long_term_memory` | safe | `fact`* | Store a fact, preference, or context into long-term vector memory. |

## Automation

| Tool | Risk | Parameters | Description |
|---|---|---|---|
| `type_text` | confirm | `text`* | Type text into whatever window currently has focus, as if from the keyboard. |
| `press_hotkey` | confirm | `keys`* | Press a keyboard shortcut in the focused window. Only common, reversible shortcuts are allowed. |
| `clipboard_read` | safe (RO) | — | Read the current text contents of the system clipboard. |
| `clipboard_write` | safe | `text`* | Put text onto the system clipboard. |
| `list_capabilities` | safe (RO) | `category` | List everything ISHA can actually do on this machine, grouped by category. Call this when unsure whether a capability exists before telling the user no. |

`*` = required parameter.

## Tool-name repair

Small models often invent names (`empty_trash`, `install_app`). `resolve_tool_name()` maps them through `TOOL_ALIASES` and fuzzy matching; an unknown name returns the closest real tools instead of a dead end.

## Adding a tool

See [DEVELOPMENT.md](DEVELOPMENT.md#adding-a-new-tool).
