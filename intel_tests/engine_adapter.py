from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

from .scenario_schema import Scenario


ENGINE_PATH = Path(__file__).resolve().parent.parent / "engine.py"


def load_engine():
    module_name = f"botc_engine_for_tests_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load engine from {ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _get_role_lookup(engine) -> dict[str, object]:
    roles = {}
    for bucket_name in [
        "TB_TOWNSFOLK",
        "TB_OUTSIDERS",
        "TB_MINIONS",
        "TB_DEMONS",
        "BMR_TOWNSFOLK",
        "BMR_OUTSIDERS",
        "BMR_MINIONS",
        "BMR_DEMONS",
    ]:
        for role in getattr(engine, bucket_name):
            roles[role.name] = role
    return roles


def build_game_state(engine, scenario: Scenario):
    role_lookup = _get_role_lookup(engine)
    players = []
    seat_to_player = {}
    for player_data in scenario.players:
        role = role_lookup[player_data.role]
        player = engine.Player(
            seat=player_data.seat,
            name=player_data.name,
            role=role,
            alive=player_data.alive,
            memory=dict(player_data.memory),
            hidden_state=dict(player_data.hidden_state),
        )
        players.append(player)
        seat_to_player[player.seat] = player

    gs = engine.GameState(players=players, character_set=scenario.character_set)
    gs.phase = getattr(engine.Phase, scenario.phase)
    gs.day = scenario.day
    gs.phase_tick_id = f"Day {scenario.day}"
    for entry in scenario.public_log:
        gs.public_log.append({"phase_tick_id": entry.phase_tick_id, "msg": entry.msg})

    for key, value in scenario.game_memory.items():
        if key.endswith("_seat") and isinstance(value, int):
            base_key = key[:-5]
            gs.memory[base_key] = seat_to_player.get(value)
        else:
            gs.memory[key] = value

    engine.DEBUG_ALL_LOGS = False
    engine.PRINT_STATISTICS = False
    engine.USE_LLM = True
    engine.TEST_MODE = False
    engine.PER_SEAT_IS_HUMAN = [False] * len(players)
    engine.PER_SEAT_USE_LLM = [True] * len(players)

    return gs, seat_to_player[scenario.acting_seat]


def _resolve_action_options(raw_options, seat_to_player):
    if isinstance(raw_options, dict):
        resolved = {}
        for key, value in raw_options.items():
            if key.endswith("_seat") and isinstance(value, int):
                resolved[key[:-5]] = seat_to_player.get(value)
            elif key.endswith("_seats") and isinstance(value, list):
                resolved[key[:-6]] = [seat_to_player[seat] for seat in value if seat in seat_to_player]
            else:
                resolved[key] = _resolve_action_options(value, seat_to_player)
        return resolved
    if isinstance(raw_options, list):
        return [_resolve_action_options(item, seat_to_player) for item in raw_options]
    return raw_options


def build_prompt(engine, scenario: Scenario):
    gs, acting_player = build_game_state(engine, scenario)
    action_options = _resolve_action_options(scenario.action_options, {player.seat: player for player in gs.players})
    prompt = engine.format_player_prompt(gs, acting_player, scenario.action_type, options=action_options or None)
    return gs, acting_player, prompt
