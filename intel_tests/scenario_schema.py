from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScenarioPlayer:
    seat: int
    name: str
    role: str
    alive: bool = True
    memory: dict[str, Any] = field(default_factory=dict)
    hidden_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublicLogEntry:
    phase_tick_id: str
    msg: str


@dataclass
class ScenarioEvaluation:
    type: str
    expected_target: str | None = None
    require_shot: bool = False


@dataclass
class Scenario:
    scenario_id: str
    name: str
    description: str
    character_set: int
    phase: str
    day: int
    action_type: str
    acting_seat: int
    players: list[ScenarioPlayer]
    public_log: list[PublicLogEntry]
    game_memory: dict[str, Any] = field(default_factory=dict)
    action_options: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    evaluation: ScenarioEvaluation | None = None
