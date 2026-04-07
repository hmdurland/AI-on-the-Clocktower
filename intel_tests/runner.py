from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from intel_tests.engine_adapter import build_prompt, load_engine
from intel_tests.evaluators import evaluate_response
from intel_tests.scenario_loader import load_scenario


RESULTS_DIR = Path(__file__).resolve().parent / "results"


def build_condensed_observer_log(result: dict) -> str:
    lines = []
    lines.append(f"Scenario: {result['scenario_name']} ({result['scenario_id']})")
    lines.append(f"Description: {result['description']}")
    lines.append(f"Backend/Model: {result['backend']} / {result['model']}")
    lines.append(f"Acting Player: {result['acting_player']['name']} (Seat {result['acting_player']['seat']}), {result['acting_player']['role']}")
    lines.append(f"Action Type: {result['action_type']}")
    lines.append("")
    lines.append("Roster:")
    for player in result.get("players", []):
        state = "alive" if player["alive"] else "dead"
        lines.append(
            f"- Seat {player['seat']}: {player['name']} | role={player['role']} | {state}"
        )
    lines.append("")
    lines.append("Public Log:")
    for entry in result.get("public_log", []):
        lines.append(f"[{entry['phase_tick_id']}] {entry['msg']}")
    lines.append("")
    lines.append("Game Memory:")
    for key, value in result.get("game_memory", {}).items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Model Response:")
    lines.append(result.get("raw_response") or "<dry-run: no API call made>")
    lines.append("")
    lines.append("Evaluation:")
    lines.append(json.dumps(result.get("evaluation", {}), indent=2))
    return "\n".join(lines)


def build_result_xml(result: dict) -> str:
    raw_response = result.get("raw_response") or ""
    prompt_messages = result.get("prompt_messages", [])
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<intelligence_test_result scenario_id="{xml_escape(result["scenario_id"])}" '
        f'scenario_name="{xml_escape(result["scenario_name"])}" '
        f'backend="{xml_escape(result["backend"])}" '
        f'model="{xml_escape(result["model"])}" '
        f'timestamp="{xml_escape(result["timestamp"])}" '
        f'run_index="{result.get("run_index", 0)}">'
    )
    lines.append("  <response>")
    lines.append(f"    <raw>{xml_escape(raw_response)}</raw>")
    lines.append("  </response>")
    lines.append("  <summary>")
    lines.append(f"    <description>{xml_escape(result['description'])}</description>")
    lines.append(
        f'    <acting_player seat="{result["acting_player"]["seat"]}" '
        f'name="{xml_escape(result["acting_player"]["name"])}" '
        f'role="{xml_escape(result["acting_player"]["role"])}" />'
    )
    lines.append(f"    <action_type>{xml_escape(result['action_type'])}</action_type>")
    lines.append("  </summary>")
    lines.append("  <evaluation>")
    lines.append(f"    <json>{xml_escape(json.dumps(result.get('evaluation', {}), ensure_ascii=False))}</json>")
    lines.append("  </evaluation>")
    lines.append("  <public_log>")
    for entry in result.get("public_log", []):
        lines.append(
            f'    <entry phase_tick_id="{xml_escape(str(entry["phase_tick_id"]))}">{xml_escape(str(entry["msg"]))}</entry>'
        )
    lines.append("  </public_log>")
    lines.append("  <players>")
    for player in result.get("players", []):
        state = "alive" if player["alive"] else "dead"
        lines.append(
            f'    <player seat="{player["seat"]}" name="{xml_escape(player["name"])}" '
            f'role="{xml_escape(player["role"])}" state="{state}" />'
        )
    lines.append("  </players>")
    lines.append("  <prompt_messages>")
    for msg in prompt_messages:
        lines.append(
            f'    <message role="{xml_escape(str(msg.get("role", "")))}">{xml_escape(str(msg.get("content", "")))}</message>'
        )
    lines.append("  </prompt_messages>")
    lines.append("</intelligence_test_result>")
    return "\n".join(lines)


def run_once(scenario_path: Path, backend: str, model: str, call_api: bool) -> dict:
    engine = load_engine()
    engine.google_or_openai = backend
    engine.MODEL_OVERRIDE = model
    engine.client = engine._make_client()
    scenario = load_scenario(scenario_path)
    gs, acting_player, prompt = build_prompt(engine, scenario)

    response = None
    if call_api:
        response = engine.call_gpt(prompt, label=f"Intel Test: {scenario.name}")

    evaluation = evaluate_response(scenario, response or "")
    result = {
        "scenario_id": scenario.scenario_id,
        "scenario_name": scenario.name,
        "description": scenario.description,
        "backend": backend,
        "model": model,
        "timestamp": datetime.now().isoformat(),
        "acting_player": {"seat": acting_player.seat, "name": acting_player.name, "role": acting_player.role.name},
        "action_type": scenario.action_type,
        "prompt_messages": prompt,
        "raw_response": response,
        "evaluation": evaluation,
        "public_log": gs.public_log,
        "players": [
            {"seat": player.seat, "name": player.name, "role": player.role.name, "alive": player.alive}
            for player in gs.players
        ],
        "game_memory": {
            key: (value.seat if hasattr(value, "seat") else value)
            for key, value in gs.memory.items()
        },
        "game_memory_keys": sorted(gs.memory.keys()),
    }
    result["observer_log"] = build_condensed_observer_log(result)
    return result


def run_batch(scenario_path: Path, backend: str, model: str, repeats: int, call_api: bool, max_workers: int | None = None) -> list[dict]:
    total_runs = max(1, int(repeats))
    if max_workers is None:
        max_workers = min(total_runs, max(1, (os.cpu_count() or 4)))
    if total_runs == 1:
        single = run_once(scenario_path, backend, model, call_api=call_api)
        single["run_index"] = 0
        return [single]

    results_by_index: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_once, scenario_path, backend, model, call_api): run_index
            for run_index in range(total_runs)
        }
        for future in as_completed(futures):
            run_index = futures[future]
            result = future.result()
            result["run_index"] = run_index
            results_by_index[run_index] = result
    return [results_by_index[idx] for idx in sorted(results_by_index)]


def save_result(result: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{result['scenario_id']}_{result['model']}_{timestamp}"
    xml_path = RESULTS_DIR / f"{stem}.xml"
    xml_path.write_text(build_result_xml(result), encoding="utf-8")
    return xml_path


def main():
    parser = argparse.ArgumentParser(description="Run BOTC intelligence test scenarios.")
    parser.add_argument(
        "--scenario",
        default=str(Path(__file__).resolve().parent / "scenarios" / "toy_slayer_day2_india.json"),
        help="Path to scenario JSON file.",
    )
    parser.add_argument("--backend", default="openai", choices=["openai", "google"])
    parser.add_argument("--models", default="gpt-5-mini", help="Comma-separated model ids.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Build prompts without calling the API.")
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    models = [model.strip() for model in args.models.split(",") if model.strip()]
    for model in models:
        results = run_batch(scenario_path, args.backend, model, args.repeats, call_api=not args.dry_run)
        for result in results:
            xml_path = save_result(result)
            print(f"Saved result to {xml_path}")
            if args.dry_run:
                print(f"Prompt-only run for {model}: {result['scenario_name']}")
            else:
                print(
                    f"{model} -> passed={result['evaluation'].get('passed')} "
                    f"target={result['evaluation'].get('parsed', {}).get('slayer_target')}"
                )


if __name__ == "__main__":
    main()
