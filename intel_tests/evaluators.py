from __future__ import annotations

import re

from .scenario_schema import Scenario


SLAYER_RE = re.compile(r"SLAYER_SHOT:\s*([A-Za-z][A-Za-z0-9' -]*)", re.IGNORECASE)
SAY_RE = re.compile(r"<SAY>(.*?)</SAY>", re.IGNORECASE | re.DOTALL)


def evaluate_response(scenario: Scenario, raw_response: str) -> dict:
    result = {
        "passed": None,
        "hard_failures": [],
        "soft_failures": [],
        "parsed": {},
    }
    if not scenario.evaluation:
        return result

    if scenario.evaluation.type == "slayer_target":
        public_text = raw_response or ""
        say_match = SAY_RE.search(public_text)
        if say_match:
            public_text = say_match.group(1).strip()
        shot_match = SLAYER_RE.search(public_text)
        target_name = shot_match.group(1).strip() if shot_match else None
        result["parsed"]["slayer_target"] = target_name

        player_names = {player.name for player in scenario.players if player.alive and player.seat != scenario.acting_seat}
        if target_name and target_name not in player_names:
            result["hard_failures"].append(f"Illegal Slayer target: {target_name}")

        if scenario.evaluation.require_shot and not target_name:
            result["soft_failures"].append("No Slayer shot was attempted.")

        expected_target = scenario.evaluation.expected_target
        if expected_target and target_name != expected_target:
            result["soft_failures"].append(
                f"Expected Slayer shot on {expected_target}, got {target_name or 'no shot'}."
            )

        result["passed"] = not result["hard_failures"] and not result["soft_failures"]
        return result

    return result
