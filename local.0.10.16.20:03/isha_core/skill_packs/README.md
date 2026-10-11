# ISHA skill packs

Each `<id>.md` file here is the instruction text for one ISHA skill. The id,
label, description and relevance keywords live in `isha_core/skills.py`
(`REGISTRY`); this folder only holds the text the local model sees.

* Only skills that are **enabled** (Settings → Skills & Visuals) **and**
  relevant to the request are added to the prompt, at most two at a time.
* Keep packs short. Small local models have a 4k-8k token context and the
  text competes with the conversation, tool list and project files.
* A pack is guidance on *how* to work. It can never grant permissions: tool
  calls still go through ISHA's permission gateway, and safety rules and the
  user's current request always win.
* These are ISHA's own offline packs. They are not Claude Code plugins and
  nothing is downloaded or installed.
