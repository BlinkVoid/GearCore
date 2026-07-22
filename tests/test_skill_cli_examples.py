from __future__ import annotations

import shlex
from pathlib import Path

from gearcore_hub.main import build_parser


def test_project_scoped_skill_examples_are_accepted(tmp_path: Path):
    """BUG-HIVE-036: published self-skill commands must match argparse."""
    skill_path = Path(__file__).parents[1] / "src" / "gearcore_hub" / "self_skill" / "SKILL.md"
    commands = [
        line.strip()
        for line in skill_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("gearcore ") and "--project" in line
    ]

    assert commands
    parser = build_parser()
    for command in commands:
        argv = shlex.split(command)[1:]
        argv = [
            str(tmp_path)
            if token in {"/absolute/path/to/project", "/path/to/project"}
            else token
            for token in argv
        ]
        argv = ["memory" if token == "<skill_name>" else token for token in argv]
        parsed = parser.parse_args(argv)
        assert parsed.project == str(tmp_path)
