# Security Model

ISHA can drive your computer, so every action passes one gateway: `run_tool_gated()` → `execute_tool()`.

## Risk tiers

| Tier | Meaning | Examples |
|---|---|---|
| **safe** | read-only or easily reversible | search files, RAM/disk info, screenshot, open app, set volume, create a *new* project |
| **confirm** | changes/destroys something or contacts a person | delete, move/overwrite files, empty Recycle Bin, install/uninstall, send WhatsApp, close apps |
| **critical** | arbitrary code, privileged or system-wide | terminal commands, running (generated) code, auto-fix loop, shutdown/restart |

New tools default to **confirm** until classified in `TOOL_RISK`.

## Autonomy levels (`autonomy_level`)

| | read-only safe | changing safe | confirm | critical |
|---|---|---|---|---|
| **low** | auto | ask | ask | ask |
| **medium** | auto | ask | ask | ask |
| **high** (default, = previous behaviour) | auto | auto | ask | ask |

Note: in the current gateway **low and medium behave identically** (both ask before every change);
the separate level is kept for finer-grained policies later.
Relaxations: `trusted_messaging` (WhatsApp), `trusted_tools` (named confirm-tier tools),
`confirm_risky_tools: false` (all confirm-tier tools). **Critical actions always ask — no setting
changes that.** `blocked_tools` are always denied.

When you approve in the dialog, that approval *is* the confirmation — tools that need `confirm=true`
receive it from the gateway (previously an approved delete could still refuse to run).

## Guarantees

* **No `eval`/`exec` of model output.** Tools are an allow-list; arguments are schema-validated.
* **Terminal commands** are tokenized; without shell operators they run with `shell=False`; a blocklist
  refuses disk formatting, recursive root/home deletes, fork bombs, download-and-execute, disabling
  Defender/firewall/SELinux, persistence (Run keys, scheduled tasks), audit-trail erasure and account
  changes — even if you click Yes. Everything runs with a timeout and is logged.
* **Generated code**: scanned before *every* run; catastrophic patterns are never executed; a fix that
  adds a new risky capability (shell, file deletion, network, dynamic exec) stops and asks you again;
  runs with the project as cwd, stdin closed or a known test input, a scrubbed environment (no
  API keys/tokens/passwords), CPU-time and file-size limits on Linux/macOS; bounded attempts.
  This is a guard, **not a full sandbox** — run untrusted code in a VM if in doubt.
* **Software** only from official package managers; unknown names produce a search, never a guessed
  download. Elevation is the OS's own UAC / polkit prompt — ISHA never handles passwords.
* **Recycle Bin / file deletion**: confirm tier; bin emptying is verified by re-counting.
* **WhatsApp**: preview + confirmation (unless `trusted_messaging`); uses your logged-in WhatsApp
  Desktop/Web; no bypass of authentication, rate limits or platform protections.
* **Secrets**: memory refuses to store passwords/PINs/OTPs/card numbers; the audit log redacts secret-like
  keys and values; generated code does not inherit secret environment variables.
* **No hidden persistence**: ISHA installs no services, autostart entries or scheduled tasks.
* **STOP** (■ button / Esc) stops the agent loop, generation, queued tools and tracked child processes.

## Audit log

`isha_audit.jsonl`: time, kind (request/route/tool/approval/command/code_run/stop), task, model, tool,
arguments (redacted), result, duration, risk, approval. Visible live in Agent Center → Activity.

## Known limits

* Detached GUI programs launched for you are not killed by STOP (by design — they are yours).
* Pattern-based guards can be evaded by sufficiently obfuscated code; approval of *critical* actions is
  the real boundary.
