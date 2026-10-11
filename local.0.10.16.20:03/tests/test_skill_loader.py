"""Tests for dynamic skill discovery and project mapping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from isha_core.skill_loader import (
    SkillLoader,
    SkillMeta,
    discover_skills,
    parse_skill_md,
    validate_meta,
)
from isha_core.project_map import map_project, ProjectMap
from isha_core import skills as skills_mod


def test_parse_skill_md_with_frontmatter(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text(
        "---\nname: demo\ndescription: \"A demo skill\"\ncategory: coding\nrisk_level: safe\n"
        "triggers:\n  - demo\n  - sample\n---\n\n# Body\n\nDo the thing.\n",
        encoding="utf-8",
    )
    meta, body, errs = parse_skill_md(p)
    assert not errs
    assert meta.get("name") == "demo"
    assert meta.get("description") == "A demo skill"
    assert "Do the thing" in body


def test_parse_skill_md_without_frontmatter(tmp_path: Path):
    p = tmp_path / "SKILL.md"
    p.write_text("# Just a title\n\nInstructions here.\n", encoding="utf-8")
    meta, body, errs = parse_skill_md(p)
    assert not errs
    assert meta == {}
    assert "Instructions here" in body


def test_discover_skills_json(tmp_path: Path):
    skill_dir = tmp_path / "my_skill"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text(
        json.dumps({
            "id": "my_skill",
            "name": "My Skill",
            "description": "Test",
            "category": "productivity",
            "risk_level": "safe",
            "triggers": ["myskill"],
            "tasks": ["PLANNING"],
        }),
        encoding="utf-8",
    )
    found = discover_skills(tmp_path)
    assert len(found) == 1
    assert found[0].id == "my_skill"
    assert found[0].enabled
    assert found[0].category == "productivity"


def test_invalid_id_disables_skill(tmp_path: Path):
    skill_dir = tmp_path / "Bad Skill!!"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text(
        json.dumps({"id": "Bad Skill!!", "name": "Bad"}),
        encoding="utf-8",
    )
    found = discover_skills(tmp_path)
    assert len(found) == 1
    assert not found[0].enabled
    assert found[0].errors


def test_skill_loader_reload(tmp_path: Path):
    loader = SkillLoader(tmp_path)
    assert loader.reload() == []
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text(
        json.dumps({"id": "alpha", "name": "Alpha", "description": "A"}),
        encoding="utf-8",
    )
    loaded = loader.reload()
    assert len(loaded) == 1
    st = loader.status()
    assert st["discovered"] == 1
    assert st["enabled"] == 1


def test_builtin_system_skill_registered():
    assert "system" in skills_mod.BY_ID
    s = skills_mod.BY_ID["system"]
    assert s.label == "System & Storage"
    text = skills_mod.instruction_text(s)
    assert "delete" in text.lower() or "confirmation" in text.lower() or "tool" in text.lower()


def test_match_system_skill():
    res = skills_mod.match("Check disk space and large files in Downloads", task="SYSTEM")
    ids = res.ids
    assert "system" in ids or any("storage" in r for r in res.labels)


def test_map_project_basic(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "util.py").write_text("x = 1\n", encoding="utf-8")
    pm = map_project(tmp_path)
    assert pm.exists
    assert pm.file_count >= 3
    assert "python" in pm.languages
    assert any("main.py" in e for e in pm.entry_points) or "main.py" in pm.key_files
    text = pm.prompt_text()
    assert "Project root" in text or str(tmp_path) in text


def test_map_project_missing():
    pm = map_project("/nonexistent/path/xyz_isha_test")
    assert not pm.exists
    assert pm.errors
