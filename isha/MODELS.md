# Models

ISHA runs GGUF models locally via llama.cpp. It never downloads a model by itself.

## Roles

| Role | Used for | Suggested (Q4_K_M) |
|---|---|---|
| `general` | chat, creative, fallback for every text role | Qwen2.5-3B/7B-Instruct, Llama-3.2-3B-Instruct |
| `coding` | code generation, debugging, auto-fix | Qwen2.5-Coder-1.5B/7B-Instruct |
| `reasoning` | reasoning, planning, research | Qwen2.5-7B-Instruct (or larger if RAM allows) |
| `study` | explanations ("samjhao") — falls back to `reasoning` | same as reasoning |
| `fast` | quick commands, file ops, messaging when the LLM is needed | Qwen2.5-1.5B-Instruct |
| `vision` | screenshots / images (needs `mmproj`) | llava-v1.6 / MiniCPM-V / Qwen2-VL GGUF + `*mmproj*.gguf` |

Rough RAM needs: 1.5B ≈ 1.2 GB, 3B ≈ 2 GB, 7B ≈ 4.5 GB, 14B ≈ 8.5 GB (plus context).

> Avoid pure reasoning models that emit `<think>` blocks and base (non-instruct) models for the agent —
> they do not call tools reliably. ISHA warns about known-unsuitable families.

## Installing models

Either use folders (auto-discovered):

```
models/
  general/qwen2.5-3b-instruct-q4_k_m.gguf
  coding/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf
  fast/qwen2.5-1.5b-instruct-q4_k_m.gguf
  vision/llava-v1.6-mistral-7b.Q4_K_M.gguf
  vision/mmproj-model-f16.gguf
```

or set paths in `isha_config.json` (absolute, or relative to `models/` / the project folder):

```json
"models": {
  "general": "models/general/qwen2.5-3b-instruct-q4_k_m.gguf",
  "coding": "D:/AI/qwen2.5-coder-7b-instruct-q4_k_m.gguf",
  "reasoning": "", "study": "", "fast": "", "vision": ""
}
```

Legacy keys still work: `gguf_model_path` → general, `code_model_path` / `code_model` → coding. A single
`.gguf` in `models/`, the project folder, `~/models` or `~/Downloads` is used as `general`.

## Routing

`pick_cfg_for(text, cfg)` classifies the request, maps the task to a role, and walks the fallback chain
(`study → reasoning → general`, `coding → general`, `fast → general`; `vision` never falls back to a
text model). Check it any time: *"kaun sa model chal raha hai?"* or Agent Center → **Models**.

**Manual mode** (Agent Center or *"coding model use karo"*) sets `model_mode`; `auto` restores routing.

## Memory-aware loading

With `hardware_aware_loading: true`, a model that does not fit in free RAM (+ free VRAM) is skipped in
favour of the next role in the chain, or the smallest installed text model; the reason appears in the
activity log. `max_loaded_models` (default 1) is capped by what total RAM allows. Only one model
generates at a time; switching roles with `max_loaded_models: 1` unloads the previous model (LRU).

## GPU

Set `gpu_layers` (e.g. `99` = offload everything) when your llama.cpp build has CUDA/Vulkan/Metal.
The prebuilt server variant is chosen with `ISHA_LLAMA_VARIANT` (`cpu` default).

## Adding a new model role

1. Add the role to `ROLES`, `TASK_ROLE` and `FALLBACK` in `isha_core/router.py`.
2. Add it to `NEW_DEFAULTS["models"]` in `isha_core/config.py` and to `AgentPanelWindow._MODES`.
3. Add a routing test in `tests/test_router.py`.
