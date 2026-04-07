from __future__ import annotations

import json
from pathlib import Path

from .scenario_schema import PublicLogEntry, Scenario, ScenarioEvaluation, ScenarioPlayer


def load_scenario(path: str | Path) -> Scenario:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Scenario(
        scenario_id=data["scenario_id"],
        name=data["name"],
        description=data["description"],
        character_set=data["character_set"],
        phase=data["phase"],
        day=data["day"],
        action_type=data["action_type"],
        acting_seat=data["acting_seat"],
        players=[ScenarioPlayer(**player) for player in data["players"]],
        public_log=[PublicLogEntry(**entry) for entry in data["public_log"]],
        game_memory=data.get("game_memory", {}),
        action_options=data.get("action_options", {}),
        tags=data.get("tags", []),
        evaluation=ScenarioEvaluation(**data["evaluation"]) if data.get("evaluation") else None,
    )
