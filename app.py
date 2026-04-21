import html
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from intel_tests.runner import run_batch as intel_run_batch, save_result as intel_save_result

APP_DIR = Path(__file__).resolve().parent
ENGINE_PATH = APP_DIR / "engine.py"
INTEL_SCENARIOS_DIR = APP_DIR / "intel_tests" / "scenarios"
SETUP_PRESETS_PATH = APP_DIR / "setup_presets.json"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TAGGED_LINE_RE = re.compile(r"^\[([A-Z_]+)\]\s?(.*)$")
PROMPT_MARKER = "<<BOTC_PROMPT>>"
MESSAGE_MARKER_PREFIX = "<<BOTC_MSG>>"

TAG_META = {
    "PUBLIC": {"label": "Public", "css": "public"},
    "PRIVATE": {"label": "Private", "css": "private"},
    "PROMPT": {"label": "Prompt", "css": "prompt"},
    "STORYTELLER": {"label": "Storyteller", "css": "storyteller"},
    "ERROR": {"label": "Error", "css": "error"},
    "LLM": {"label": "LLM", "css": "llm"},
    "DEBUG": {"label": "Debug", "css": "debug"},
    "PINK": {"label": "Stats", "css": "stats"},
    "SYSTEM": {"label": "System", "css": "system"},
}

PLAYER_PALETTE = [
    "#c026d3",
    "#2563eb",
    "#059669",
    "#d97706",
    "#dc2626",
    "#7c3aed",
    "#16a34a",
    "#db2777",
    "#0891b2",
    "#ea580c",
    "#b45309",
    "#0f766e",
    "#9333ea",
    "#65a30d",
    "#be123c",
]

TB_ROLE_NAMES = [
    "Washerwoman", "Librarian", "Investigator", "Chef", "Empath", "Fortune Teller", "Undertaker", "Monk", "Ravenkeeper", "Slayer", "Soldier", "Virgin", "Mayor",
    "Saint", "Butler", "Drunk", "Recluse",
    "Poisoner", "Baron", "Spy", "Scarlet Woman",
    "Imp",
]
BMR_ROLE_NAMES = [
    "Grandparent", "Sailor", "Housekeeper", "Exorcist", "Innkeeper", "Gambler", "Gossip", "Courtier", "Professor", "Minstrel", "Herbalist", "Pacifist", "Fool",
    "Tinker", "Moonchild", "Goon", "Lunatic",
    "Capo Crimini", "Devil's Advocate", "Assassin", "Mastermind",
    "Zombuul", "Pukka", "Shabaloth", "Po",
]


def get_role_options(character_set_label: str) -> list[str]:
    if character_set_label == "Trouble Brewing":
        return ["Random", *TB_ROLE_NAMES]
    if character_set_label == "Bad Moon Rising":
        return ["Random", *BMR_ROLE_NAMES]
    return ["Random", *(TB_ROLE_NAMES + BMR_ROLE_NAMES)]


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def reader_thread(proc: subprocess.Popen, output_queue: queue.Queue):
    try:
        for line in proc.stdout:
            output_queue.put(line)
    finally:
        output_queue.put(None)


def init_state():
    defaults = {
        "proc": None,
        "q": queue.Queue(),
        "messages": [],
        "raw_lines": [],
        "pending_prompt": None,
        "running": False,
        "last_status": "Idle",
        "prompt_counter": 0,
        "transcript_started": False,
        "human_seat_default": None,
        "run_config": None,
        "intel_last_results": [],
        "pending_traceback": [],
        "end_and_save_requested_at": None,
        "partial_log_saved_path": None,
        "engine_log_path": None,
        "preset_save_status": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if st.session_state.human_seat_default is None:
        st.session_state.human_seat_default = random.randint(0, 7)


def append_message(tag: str, text: str):
    tag = tag if tag in TAG_META else "SYSTEM"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if should_skip_message(tag, text):
        return
    st.session_state.messages.append({"tag": tag, "text": text})
    st.session_state.raw_lines.append(f"[{tag}] {text}" if text else "")


def append_raw_line(text: str):
    if should_skip_raw_line(text):
        return
    st.session_state.raw_lines.append(text)
    stripped = text.strip()
    if stripped.startswith("Traceback (most recent call last):"):
        st.session_state.pending_traceback = [text]
        return
    if st.session_state.pending_traceback:
        st.session_state.pending_traceback.append(text)
        if is_traceback_terminal_line(stripped):
            st.session_state.messages.append({"tag": "ERROR", "text": f"Engine error: {stripped}"})
            st.session_state.pending_traceback = []
        return
    if text == "":
        st.session_state.messages.append({"tag": "SYSTEM", "text": ""})
        return
    tagged = TAGGED_LINE_RE.match(text)
    if tagged:
        tag = tagged.group(1)
        if tag in TAG_META:
            st.session_state.messages.append({"tag": tag, "text": tagged.group(2)})
            return
    st.session_state.messages.append({"tag": "SYSTEM", "text": text})


def save_partial_game_log_from_ui() -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = APP_DIR / f"botc_game_log_{timestamp}_ui_partial.xml"
    run_config = st.session_state.get("run_config") or {}

    def esc(value: object) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<partial_game_log generated_at="{esc(time.strftime("%Y-%m-%d %H:%M:%S"))}" '
        f'status="forced_ui_stop" mode="gameplay">'
    )
    lines.append("  <run_config>")
    for key, value in sorted(run_config.items()):
        lines.append(f'    <item key="{esc(key)}" value="{esc(value)}" />')
    lines.append("  </run_config>")
    lines.append("  <messages>")
    for idx, entry in enumerate(st.session_state.get("messages", []), start=1):
        lines.append(
            f'    <message id="{idx}" tag="{esc(entry.get("tag", "SYSTEM"))}">{esc(entry.get("text", ""))}</message>'
        )
    lines.append("  </messages>")
    lines.append("  <raw_lines>")
    for idx, line in enumerate(st.session_state.get("raw_lines", []), start=1):
        lines.append(f'    <line id="{idx}">{esc(line)}</line>')
    lines.append("  </raw_lines>")
    lines.append("</partial_game_log>")
    path.write_text("\n".join(lines), encoding="utf-8")
    st.session_state.partial_log_saved_path = str(path)
    return path


def is_traceback_terminal_line(text: str) -> bool:
    return bool(
        re.match(r"^[A-Za-z_][A-Za-z0-9_.]*Error:", text)
        or re.match(r"^[A-Za-z_][A-Za-z0-9_.]*Exception:", text)
    )


def should_skip_message(tag: str, text: str) -> bool:
    text = text.strip()
    if text.startswith("LLM MODE:"):
        return True
    if text.startswith("=== BLOOD ON THE CLOCKTOWER ==="):
        return True
    if text.startswith("Quick Setup Options:"):
        return True
    if text.startswith("Available character sets:"):
        return True
    if text.startswith("Choose setup option"):
        return True
    if text.startswith("Select character set"):
        return True
    if text.startswith("Enter number of players"):
        return True
    if text.startswith("Enable FULL LLM MODE"):
        return True
    if text.startswith("Enable TEST MODE"):
        return True
    if text.startswith("Enable FAST LLM VOTING"):
        return True
    if text.startswith("Enable DEBUG/VERBOSE LOGGING MODE"):
        return True
    if text.startswith("Enable prompt/usage statistics printing"):
        return True
    if text.startswith("Do you want to play as a human?"):
        return True
    if text.startswith("Which seat ("):
        return True
    return False


def should_skip_raw_line(text: str) -> bool:
    stripped = text.strip()
    if should_skip_message("SYSTEM", stripped):
        return True
    if stripped.startswith("1. Default Setup"):
        return True
    if stripped.startswith("2. Custom Setup"):
        return True
    if stripped.startswith("1. Trouble Brewing"):
        return True
    if stripped.startswith("2. Bad Moon Rising"):
        return True
    if stripped.startswith("3. Both (Mixed)"):
        return True
    return False


def should_skip_prompt(text: str) -> bool:
    return should_skip_message("SYSTEM", text.strip())


def extract_public_speaker(text: str) -> tuple[str | None, str]:
    match = re.match(r"^([A-Za-z][A-Za-z0-9' -]{0,40}):\s*(.*)$", text, re.DOTALL)
    if not match:
        return None, text
    return match.group(1), match.group(2)


def recent_prompt_context(limit: int = 5) -> list[dict]:
    context = []
    for entry in reversed(st.session_state.messages):
        if entry["tag"] not in {"PROMPT", "PRIVATE", "PUBLIC", "STORYTELLER", "ERROR"}:
            continue
        text = entry["text"].strip()
        if not text:
            continue
        if text.startswith("===") or text.startswith("---"):
            continue
        context.append(entry)
        if len(context) >= limit:
            break
    return list(reversed(context))


def build_prompt_display(prompt_text: str) -> str:
    prompt_text = prompt_text.strip()
    context = recent_prompt_context(limit=3)
    prompt_context = []
    for entry in reversed(context):
        if entry["tag"] != "PROMPT":
            continue
        text = entry["text"].strip()
        if not text or text.startswith("[PRIVATE]"):
            continue
        prompt_context.append(text)
        if len(prompt_context) >= 2:
            break
    prompt_context = list(reversed(prompt_context))
    if prompt_text in {"Vote YES or NO:", "What do you want to say? (pass 1):", "What do you want to say? (pass 2):"}:
        if prompt_context:
            return "\n\n".join(prompt_context + [prompt_text])
    return prompt_text


def get_player_color_map(board_state: dict) -> dict[str, str]:
    color_map: dict[str, str] = {}
    for idx, seat in enumerate(board_state["seats"]):
        color_map[seat["name"]] = PLAYER_PALETTE[idx % len(PLAYER_PALETTE)]
    return color_map


def highlight_player_mentions(text: str, color_map: dict[str, str], enabled: bool = True) -> str:
    if not text:
        return ""
    if not enabled:
        return html.escape(text).replace("\n", "<br>")
    names = sorted(color_map.keys(), key=len, reverse=True)
    if not names:
        return html.escape(text).replace("\n", "<br>")
    pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in names) + r")\b")
    pieces = []
    cursor = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > cursor:
            pieces.append(html.escape(text[cursor:start]))
        matched_name = match.group(0)
        color = color_map.get(matched_name, "#c026d3")
        pieces.append(
            f"<span class='botc-player-mention' style='color:{color}; border-color:{color}55'>{html.escape(matched_name)}</span>"
        )
        cursor = end
    if cursor < len(text):
        pieces.append(html.escape(text[cursor:]))
    return "".join(pieces).replace("\n", "<br>")


def get_model_config(
    backend: str,
    selected_model: str | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: str | int | None = None,
) -> dict:
    selected_model = (selected_model or "").strip()
    if backend == "google":
        return {
            "backend": "google",
            "api_style": "OpenAI-compatible chat completions",
            "model": selected_model or "gemini-2.5-flash",
            "temperature": 0.7,
            "reasoning_effort": reasoning_effort or "n/a",
            "max_output_tokens": max_output_tokens or "per-call max_tokens",
        }
    return {
        "backend": "openai",
        "api_style": "Responses API",
        "model": selected_model or "gpt-5-mini",
        "temperature": "not used",
        "reasoning_effort": reasoning_effort or "low",
        "max_output_tokens": max_output_tokens or "max(max_tokens, 2048)",
    }


OPENAI_MODEL_OPTIONS = ["gpt-5-mini", "gpt-5.4-mini", "gpt-5.4-nano", "Custom"]
BUILTIN_SETUP_PRESETS = {
    "8 with empath-demon neighbors": {
        "character_set": 1,
        "player_count": 8,
        "seats": [
            {"seat": 0, "name": "Golf", "role": "Imp"},
            {"seat": 1, "name": "Hotel", "role": "Empath"},
            {"seat": 2, "name": "Charlie", "role": "Monk"},
            {"seat": 3, "name": "Foxtrot", "role": "Librarian"},
            {"seat": 4, "name": "Alpha", "role": "Poisoner"},
            {"seat": 5, "name": "Delta", "role": "Chef"},
            {"seat": 6, "name": "Bravo", "role": "Recluse"},
            {"seat": 7, "name": "Echo", "role": "Mayor"},
        ],
        "setup_state": {
            "demon_bluffs": {
                "0": ["Ravenkeeper", "Washerwoman", "Investigator"]
            },
            "initial_info": {
                "Librarian": {
                    "3": {"pair": [6, 5], "reveal": "Recluse"}
                }
            }
        },
    }
}


def load_custom_setup_presets() -> dict:
    if not SETUP_PRESETS_PATH.exists():
        return {}
    try:
        return json.loads(SETUP_PRESETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_custom_setup_presets(presets: dict):
    SETUP_PRESETS_PATH.write_text(json.dumps(presets, indent=2), encoding="utf-8")


def get_all_setup_presets() -> dict:
    presets = dict(BUILTIN_SETUP_PRESETS)
    presets.update(load_custom_setup_presets())
    return presets


def get_setup_preset_names() -> list[str]:
    return ["Off"] + list(get_all_setup_presets().keys())


def parse_setup_from_game_log(log_path: Path, character_set_name: str | None) -> dict:
    root = ET.parse(log_path).getroot()
    players_el = root.find("metadata").find("players")
    seats = []
    demon_seats = []
    for p in players_el:
        role = p.attrib["role"]
        seats.append(
            {
                "seat": int(p.attrib["seat"]),
                "name": p.attrib["name"],
                "role": role,
            }
        )
        if p.attrib.get("alignment") == "demon":
            demon_seats.append(int(p.attrib["seat"]))
    character_set_map = {"Trouble Brewing": 1, "Bad Moon Rising": 2, "Both (Mixed)": 3}
    storyteller_entries = [
        (entry.text or "").strip()
        for entry in root.find("legacy_logs").find("storyteller_log")
    ]
    setup_state: dict[str, dict] = {"demon_bluffs": {}, "initial_info": {}, "drunk_roles": {}}

    for txt in storyteller_entries:
        m = re.search(r"Drunk: .+ \(Seat (\d+)\) thinks they are ([A-Za-z' -]+)", txt)
        if m:
            setup_state["drunk_roles"][m.group(1)] = m.group(2).strip()
            continue
        m = re.search(r"Fortune Teller red herring: .+ \(Seat (\d+)\)", txt)
        if m:
            setup_state["ft_red_herring_seat"] = int(m.group(1))
            continue
        m = re.search(r"sees ([A-Za-z' -]+) among seats \((\d+), (\d+)\)", txt)
        if m and "Washerwoman" in txt:
            seat_m = re.search(r"Washerwoman .+\(Seat (\d+)\)", txt)
            if seat_m:
                setup_state["initial_info"].setdefault("Washerwoman", {})[seat_m.group(1)] = {
                    "pair": [int(m.group(2)), int(m.group(3))],
                    "reveal": m.group(1).strip(),
                }
            continue
        if m and "Librarian" in txt:
            seat_m = re.search(r"Librarian .+\(Seat (\d+)\)", txt)
            if seat_m:
                setup_state["initial_info"].setdefault("Librarian", {})[seat_m.group(1)] = {
                    "pair": [int(m.group(2)), int(m.group(3))],
                    "reveal": m.group(1).strip(),
                }
            continue
        if m and "Investigator" in txt:
            seat_m = re.search(r"Investigator .+\(Seat (\d+)\)", txt)
            if seat_m:
                setup_state["initial_info"].setdefault("Investigator", {})[seat_m.group(1)] = {
                    "pair": [int(m.group(2)), int(m.group(3))],
                    "reveal": m.group(1).strip(),
                }
            continue
        m = re.search(r"Grandparent .+\(Seat (\d+)\) knows .+ in seat (\d+) is ([A-Za-z' -]+)", txt)
        if m:
            setup_state["initial_info"].setdefault("Grandparent", {})[m.group(1)] = {
                "target": int(m.group(2)),
                "reveal": m.group(3).strip(),
            }
            continue
        m = re.search(r"bluffs: \[(.+)\]", txt)
        if m and demon_seats:
            raw = m.group(1).strip()
            try:
                parsed = json.loads("[" + raw.replace("'", '"') + "]")
                setup_state["demon_bluffs"][str(demon_seats[0])] = parsed
            except Exception:
                pass

    if not setup_state["demon_bluffs"]:
        setup_state.pop("demon_bluffs")
    if not setup_state["initial_info"]:
        setup_state.pop("initial_info")
    if not setup_state["drunk_roles"]:
        setup_state.pop("drunk_roles")
    return {
        "character_set": character_set_map.get(character_set_name or "", 1),
        "player_count": len(seats),
        "seats": sorted(seats, key=lambda s: s["seat"]),
        "setup_state": setup_state,
    }


def save_setup_preset_from_current_game(preset_name: str) -> tuple[bool, str]:
    preset_name = (preset_name or "").strip()
    if not preset_name:
        return False, "Enter a preset name."
    log_path_value = st.session_state.get("engine_log_path")
    if not log_path_value:
        return False, "No current game log path is available."
    log_path = Path(log_path_value)
    if not log_path.exists():
        return False, "Current game log file was not found yet."
    run_config = st.session_state.get("run_config") or {}
    character_set_name = run_config.get("character_set")
    preset_data = parse_setup_from_game_log(log_path, character_set_name)
    presets = load_custom_setup_presets()
    presets[preset_name] = preset_data
    save_custom_setup_presets(presets)
    return True, f"Saved preset `{preset_name}`."


def parse_board_state(messages: list[dict]) -> dict:
    seats: dict[int, dict] = {}
    current_phase = ""
    status_section = None
    for entry in messages:
        text = entry["text"]
        if entry["tag"] in {"PUBLIC", "STORYTELLER"} and "[SEAT ASSIGNMENTS]" in text:
            for seat_str, name in re.findall(r"Seat (\d+): ([A-Za-z][A-Za-z0-9' -]*)", text):
                seats[int(seat_str)] = {"seat": int(seat_str), "name": name.strip(), "alive": True, "status": ""}
        if text.startswith("---") or text.startswith("==="):
            current_phase = text.strip("-= ").strip()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("ALIVE PLAYERS"):
                status_section = "alive"
                continue
            if line.startswith("DEAD PLAYERS"):
                status_section = "dead"
                continue
            if line.startswith("CURRENT STATE:") or line.startswith("RECENT EVENTS:"):
                status_section = None
                continue
            section_match = re.match(r"Seat (\d+): ([A-Za-z][A-Za-z0-9' -]*)$", line)
            if status_section and section_match:
                seat = int(section_match.group(1))
                seats.setdefault(seat, {"seat": seat, "name": section_match.group(2).strip(), "alive": True, "status": ""})
                seats[seat]["name"] = section_match.group(2).strip()
                seats[seat]["alive"] = status_section == "alive"
                if status_section == "dead":
                    seats[seat]["status"] = "Dead"
                elif seats[seat]["status"] in {"Dead", "Executed", "Revived"}:
                    seats[seat]["status"] = ""
        for name, seat_str in re.findall(r"([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\)", text):
            try:
                seat = int(seat_str)
            except ValueError:
                continue
            seats.setdefault(seat, {"seat": seat, "name": name.strip(), "alive": True, "status": ""})
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\) was executed", text):
            seat = int(match.group(2))
            seats.setdefault(seat, {"seat": seat, "name": match.group(1).strip(), "alive": True, "status": ""})
            seats[seat]["alive"] = False
            seats[seat]["status"] = "Executed"
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\) died", text):
            seat = int(match.group(2))
            seats.setdefault(seat, {"seat": seat, "name": match.group(1).strip(), "alive": True, "status": ""})
            seats[seat]["alive"] = False
            if not seats[seat]["status"]:
                seats[seat]["status"] = "Dead"
        for match in re.finditer(r"([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\) has returned to life!", text):
            seat = int(match.group(2))
            seats.setdefault(seat, {"seat": seat, "name": match.group(1).strip(), "alive": True, "status": ""})
            seats[seat]["alive"] = True
            seats[seat]["status"] = "Revived"
        nom_match = re.search(r"([A-Za-z][A-Za-z0-9' -]*) nominates ([A-Za-z][A-Za-z0-9' -]*) for execution\.", text)
        if nom_match:
            nominee = nom_match.group(2).strip()
            for seat_info in seats.values():
                if seat_info["name"] == nominee:
                    seat_info["status"] = "Nominated"
        exec_match = re.search(r"([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\) receives \d+ votes and will be executed\.", text)
        if exec_match:
            seat = int(exec_match.group(2))
            seats.setdefault(seat, {"seat": seat, "name": exec_match.group(1).strip(), "alive": True, "status": ""})
            seats[seat]["status"] = "Execution"
    return {"phase": current_phase, "seats": [seats[k] for k in sorted(seats)]}


def parse_personal_panel(messages: list[dict]) -> dict:
    seat = None
    name = None
    role = None
    ability = None
    notes = []
    core_info = []
    for entry in messages:
        text = entry["text"]
        play_as_match = re.search(r"You are playing as ([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\)", text)
        if play_as_match:
            name = play_as_match.group(1).strip()
            seat = int(play_as_match.group(2))
        match = re.search(r"You are ([A-Za-z][A-Za-z0-9' -]*) \(Seat (\d+)\), the (.+?)\. Ability: (.+)", text)
        if match:
            name = match.group(1).strip()
            seat = int(match.group(2))
            role = match.group(3).strip()
            ability = match.group(4).strip()
        if entry["tag"] == "PRIVATE":
            clean = text.strip()
            if clean.startswith("[PRIVATE]"):
                clean = clean[len("[PRIVATE]"):].strip()
            if clean and (
                clean.startswith("As a minion,")
                or clean.startswith("As the ")
                or clean.startswith("There is a Lunatic in play:")
            ):
                if clean not in core_info:
                    core_info.append(clean)
        if entry["tag"] in {"PRIVATE", "PROMPT"}:
            clean = text.strip()
            if clean and clean not in notes:
                notes.append(clean)
    return {
        "name": name,
        "seat": seat,
        "role": role,
        "ability": ability,
        "core_info": core_info[-6:],
        "notes": notes[-6:],
    }


def render_board_svg(board_state: dict, personal_state: dict) -> str:
    seats = board_state["seats"]
    if not seats:
        return ""
    n = len(seats)
    width = 560
    height = 300
    cx = width / 2
    cy = height / 2
    rx = 205 if n <= 10 else 220
    ry = 102 if n <= 10 else 112
    node_r = 18
    svg_parts: list[str] = []
    human_seat = personal_state["seat"]
    color_map = get_player_color_map(board_state)

    svg_parts.append(
        f"<svg viewBox='0 0 {width} {height}' class='botc-board-svg' role='img' aria-label='Clocktower table board'>"
    )

    positions: list[tuple[dict, float, float, float]] = []
    for idx, seat in enumerate(seats):
        angle = (2 * math.pi * idx / n) - (math.pi / 2)
        x = cx + rx * math.cos(angle)
        y = cy + ry * math.sin(angle)
        positions.append((seat, angle, x, y))

    for idx, (_, _, x1, y1) in enumerate(positions):
        _, _, x2, y2 = positions[(idx + 1) % n]
        svg_parts.append(
            f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' stroke='#8b5cf6' stroke-opacity='0.58' stroke-width='2' />"
        )

    for seat, angle, x, y in positions:
        player_color = color_map.get(seat["name"], "#c026d3")
        fill = "#09070f" if seat["alive"] else "#16090f"
        stroke = "#7c3aed" if seat["alive"] else "#b91c1c"
        stroke_width = 2
        text_fill = "#f5efff" if seat["alive"] else "#fecdd3"
        if seat["status"] in {"Nominated", "Execution"}:
            stroke = "#e879f9"
            stroke_width = 4
        if human_seat is not None and seat["seat"] == human_seat:
            stroke = "#c026d3"
            stroke_width = 5

        svg_parts.append(
            f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{node_r}' fill='{fill}' stroke='{stroke}' stroke-width='{stroke_width}' />"
        )
        svg_parts.append(
            f"<text x='{x:.1f}' y='{y + 0.5:.1f}' text-anchor='middle' dominant-baseline='middle' "
            f"font-size='13' font-weight='700' fill='{text_fill}'>{seat['seat']}</text>"
        )

        name = html.escape(seat["name"])
        box_w = max(56, min(104, 18 + len(seat["name"]) * 7))
        box_h = 22
        label_x = x + math.cos(angle) * 44
        label_y = y + math.sin(angle) * 30
        rect_x = max(6, min(width - box_w - 6, label_x - box_w / 2))
        rect_y = max(6, min(height - box_h - 6, label_y - box_h / 2))

        svg_parts.append(
            f"<rect x='{rect_x:.1f}' y='{rect_y:.1f}' width='{box_w:.1f}' height='{box_h}' rx='8' "
            f"fill='rgba(15,10,25,0.92)' stroke='rgba(168,85,247,0.9)' stroke-width='1.2' />"
        )
        svg_parts.append(
            f"<text x='{rect_x + box_w / 2:.1f}' y='{rect_y + box_h / 2 + 0.5:.1f}' text-anchor='middle' dominant-baseline='middle' "
            f"font-size='11.5' font-weight='700' fill='{player_color}'>{name}</text>"
        )

    if board_state["phase"]:
        phase = html.escape(board_state["phase"])
        svg_parts.append(
            "<rect x='210' y='131' width='140' height='38' rx='14' fill='rgba(14,10,22,0.96)' "
            "stroke='rgba(168,85,247,0.85)' stroke-width='1.2' />"
        )
        svg_parts.append(
            f"<text x='{cx:.1f}' y='{cy + 1:.1f}' text-anchor='middle' dominant-baseline='middle' "
            f"font-size='12.5' font-weight='700' fill='#f3e8ff'>{phase}</text>"
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)


def render_personal_panel(personal_state: dict) -> str:
    if not personal_state["name"] and not personal_state["core_info"]:
        return "<div class='botc-empty'>Personal role information will appear here once revealed.</div>"
    header = ""
    if personal_state["name"] is not None and personal_state["seat"] is not None:
        header += f"<div class='botc-personal-seat'>Seat {personal_state['seat']} - {html.escape(personal_state['name'])}</div>"
    if personal_state["role"]:
        header += f"<div class='botc-personal-role'>{html.escape(personal_state['role'])}</div>"
    if personal_state["ability"]:
        header += f"<div class='botc-personal-ability'>{html.escape(personal_state['ability'])}</div>"
    core_html = "".join(
        f"<div class='botc-note'>{html.escape(note).replace(chr(10), '<br>')}</div>"
        for note in personal_state["core_info"]
    )
    notes_html = "".join(
        f"<div class='botc-note'>{html.escape(note).replace(chr(10), '<br>')}</div>"
        for note in personal_state["notes"]
    )
    return (
        "<div class='botc-personal'>"
        f"{header}"
        "<div class='botc-personal-notes-title'>Core private info</div>"
        f"{core_html or '<div class=\"botc-empty\">No core private info yet.</div>'}"
        "<div class='botc-personal-notes-title'>Recent private info</div>"
        f"{notes_html or '<div class=\"botc-empty\">No private notes yet.</div>'}"
        "</div>"
    )


def consume_output():
    if not st.session_state.running:
        return

    while True:
        try:
            item = st.session_state.q.get_nowait()
        except queue.Empty:
            break

        if item is None:
            st.session_state.running = False
            st.session_state.last_status = "Game ended"
            st.session_state.end_and_save_requested_at = None
            break

        line = strip_ansi(item.rstrip("\n"))

        if line.startswith(PROMPT_MARKER):
            prompt_text = line[len(PROMPT_MARKER):]
            if should_skip_prompt(prompt_text):
                continue
            st.session_state.pending_prompt = prompt_text
            st.session_state.prompt_counter += 1
            st.session_state.last_status = "Waiting for input"
            continue

        if line.startswith(MESSAGE_MARKER_PREFIX):
            payload = line[len(MESSAGE_MARKER_PREFIX):]
            try:
                data = json.loads(payload)
                append_message(data.get("tag", "SYSTEM"), data.get("text", ""))
                continue
            except json.JSONDecodeError:
                pass

        tagged = TAGGED_LINE_RE.match(line)
        if tagged and tagged.group(1) in TAG_META:
            # Structured marker already carried this line.
            continue

        append_raw_line(line)


def render_message(entry: dict, color_map: dict[str, str], colorize_players: bool) -> str:
    tag = entry["tag"]
    text = entry["text"]
    meta = TAG_META.get(tag, TAG_META["SYSTEM"])

    if text.startswith("===") or text.startswith("---"):
        return (
            "<div class='botc-banner'>"
            f"<span>{html.escape(text)}</span>"
            "</div>"
        )

    if text == "":
        return "<div class='botc-spacer'></div>"

    safe_text = highlight_player_mentions(text, color_map, enabled=colorize_players)
    label = html.escape(meta["label"])
    speaker_html = ""
    line_style = ""
    if tag == "PUBLIC":
        speaker, body = extract_public_speaker(text)
        if speaker:
            if colorize_players:
                speaker_color = color_map.get(speaker, "#7b6a57")
                speaker_html = (
                    f"<div class='botc-subtag' style='color:{speaker_color};'>"
                    f"{html.escape(speaker)}</div>"
                )
                safe_text = highlight_player_mentions(body, color_map, enabled=True)
                line_style = f" style='border-left:4px solid {speaker_color};'"
                safe_text = f"<span style='color:{speaker_color};'>{safe_text}</span>"
            else:
                speaker_html = f"<div class='botc-subtag'>{html.escape(speaker)}</div>"
                safe_text = highlight_player_mentions(body, color_map, enabled=False)
    return (
        f"<div class='botc-line {meta['css']}'{line_style}>"
        f"<div class='botc-tagwrap'><div class='botc-tag'>{label}</div>{speaker_html}</div>"
        f"<div class='botc-text'>{safe_text}</div>"
        "</div>"
    )


def render_transcript(messages: list[dict], board_state: dict, colorize_players: bool) -> str:
    if not messages:
        return "<div class='botc-empty'>No game output yet.</div>"
    color_map = get_player_color_map(board_state)
    return "".join(render_message(entry, color_map, colorize_players) for entry in messages)


def format_intel_response_blocks(raw_response: str) -> str:
    if not raw_response:
        return "<div class='intel-empty'>Dry run: no model response was generated.</div>"
    think_match = re.search(r"<THINK>(.*?)</THINK>", raw_response, re.IGNORECASE | re.DOTALL)
    say_match = re.search(r"<SAY>(.*?)</SAY>", raw_response, re.IGNORECASE | re.DOTALL)
    blocks = []
    if say_match:
        say_text = html.escape(say_match.group(1).strip()).replace("\n", "<br>")
        blocks.append(
            "<div class='intel-response-block'>"
            "<div class='intel-response-label'>Public Output</div>"
            f"<div class='intel-response-text'>{say_text}</div>"
            "</div>"
        )
    if think_match:
        think_text = html.escape(think_match.group(1).strip()).replace("\n", "<br>")
        blocks.append(
            "<div class='intel-response-block intel-think'>"
            "<div class='intel-response-label'>Reasoning</div>"
            f"<div class='intel-response-text'>{think_text}</div>"
            "</div>"
        )
    if not blocks:
        blocks.append(
            "<div class='intel-response-block'>"
            "<div class='intel-response-label'>Raw Response</div>"
            f"<div class='intel-response-text'>{html.escape(raw_response).replace(chr(10), '<br>')}</div>"
            "</div>"
        )
    return "".join(blocks)


def start_game(
    input_script: str,
    backend: str,
    model_name: str,
    openai_key: str,
    gemini_key: str,
    human_role_choice: str,
    reasoning_effort: str,
    max_output_tokens: int | None,
    response_budget_prompt: str,
    use_team_models: bool,
    good_team_model_name: str,
    evil_team_model_name: str,
    good_team_reasoning_effort: str,
    evil_team_reasoning_effort: str,
    setup_preset_name: str,
):
    env = os.environ.copy()
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    if gemini_key:
        env["GEMINI_API_KEY"] = gemini_key
    env["BOTC_BACKEND"] = backend
    if model_name:
        env["BOTC_MODEL"] = model_name
    if setup_preset_name and setup_preset_name != "Off":
        env["BOTC_SETUP_PRESET"] = setup_preset_name
    if use_team_models and backend == "openai":
        env["BOTC_USE_TEAM_MODELS"] = "1"
        if good_team_model_name:
            env["BOTC_GOOD_TEAM_MODEL"] = good_team_model_name
        if evil_team_model_name:
            env["BOTC_EVIL_TEAM_MODEL"] = evil_team_model_name
        if good_team_reasoning_effort:
            env["BOTC_GOOD_TEAM_REASONING_EFFORT"] = good_team_reasoning_effort
        if evil_team_reasoning_effort:
            env["BOTC_EVIL_TEAM_REASONING_EFFORT"] = evil_team_reasoning_effort
    if human_role_choice and human_role_choice != "Random":
        env["BOTC_HUMAN_ROLE"] = human_role_choice
    if reasoning_effort:
        env["BOTC_REASONING_EFFORT"] = reasoning_effort
    if max_output_tokens is not None:
        env["BOTC_MAX_OUTPUT_TOKENS"] = str(int(max_output_tokens))
    if response_budget_prompt:
        env["BOTC_RESPONSE_BUDGET_PROMPT"] = response_budget_prompt
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    engine_log_path = APP_DIR / f"botc_game_log_{time.strftime('%Y%m%d_%H%M%S')}.xml"
    head_to_head_results_path = APP_DIR / "head_to_head_results.xml"
    env["BOTC_LOG_FILENAME"] = str(engine_log_path)
    env["BOTC_HEAD_TO_HEAD_RESULTS"] = str(head_to_head_results_path)
    env["BOTC_SETUP_PRESETS_FILE"] = str(SETUP_PRESETS_PATH)

    st.session_state.messages = []
    st.session_state.raw_lines = []
    st.session_state.pending_prompt = None
    st.session_state.prompt_counter = 0
    st.session_state.transcript_started = False
    st.session_state.end_and_save_requested_at = None
    st.session_state.partial_log_saved_path = None
    st.session_state.engine_log_path = str(engine_log_path)
    st.session_state.preset_save_status = None
    st.session_state.q = queue.Queue()
    st.session_state.last_status = "Launching game"

    model_config = get_model_config(backend, model_name, reasoning_effort, max_output_tokens)
    st.session_state.run_config = {
        "character_set": character_set,
        "player_count": player_count,
        "human_enabled": human_enabled,
        "human_seat": int(human_seat) if human_enabled else None,
        "human_role": human_role_choice if human_enabled else "off",
        "use_llm": use_llm,
        "test_mode": test_mode,
        "test_seat": int(test_seat) if test_mode else None,
        "fast_voting": fast_voting,
        "colorize_players": colorize_players,
        "debug_logs": debug_logs,
        "print_stats": print_stats,
        "team_model_split": use_team_models and backend == "openai",
        "good_team_model": good_team_model_name if use_team_models and backend == "openai" else None,
        "evil_team_model": evil_team_model_name if use_team_models and backend == "openai" else None,
        "good_team_reasoning_effort": good_team_reasoning_effort if use_team_models and backend == "openai" else None,
        "evil_team_reasoning_effort": evil_team_reasoning_effort if use_team_models and backend == "openai" else None,
        "setup_preset": setup_preset_name if setup_preset_name and setup_preset_name != "Off" else None,
        **model_config,
    }
    st.session_state.raw_lines.append(
        "[RUN CONFIG] "
        f"character_set={character_set}; players={player_count}; human_enabled={human_enabled}; "
        f"human_seat={int(human_seat) if human_enabled else 'none'}; human_role={human_role_choice if human_enabled else 'off'}; full_llm={use_llm}; "
        f"test_mode={test_mode}; test_seat={int(test_seat) if test_mode else 'none'}; "
        f"fast_voting={fast_voting}; colorize_players={colorize_players}; "
        f"debug_logs={debug_logs}; print_stats={print_stats}; "
        f"team_model_split={use_team_models and backend == 'openai'}; "
        f"good_team_model={good_team_model_name if use_team_models and backend == 'openai' else 'n/a'}; "
        f"evil_team_model={evil_team_model_name if use_team_models and backend == 'openai' else 'n/a'}; "
        f"good_team_reasoning_effort={good_team_reasoning_effort if use_team_models and backend == 'openai' else 'n/a'}; "
        f"evil_team_reasoning_effort={evil_team_reasoning_effort if use_team_models and backend == 'openai' else 'n/a'}; "
        f"setup_preset={setup_preset_name if setup_preset_name and setup_preset_name != 'Off' else 'n/a'}"
    )
    st.session_state.raw_lines.append(
        "[MODEL CONFIG] "
        f"backend={model_config['backend']}; api_style={model_config['api_style']}; "
        f"model={model_config['model']}; reasoning_effort={model_config['reasoning_effort']}; "
        f"temperature={model_config['temperature']}; max_output_tokens={model_config['max_output_tokens']}"
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", str(ENGINE_PATH)],
        cwd=str(APP_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    st.session_state.proc = proc
    st.session_state.running = True

    thread = threading.Thread(target=reader_thread, args=(proc, st.session_state.q), daemon=True)
    thread.start()

    proc.stdin.write(input_script)
    proc.stdin.flush()


init_state()

st.set_page_config(page_title="Blood on the Clocktower v1.5", layout="wide")

st.markdown(
    """
    <style>
    :root {
        --paper: #f3efe2;
        --ink: #201a16;
        --accent: #7d2f24;
        --muted: #6b6258;
        --panel: #fffaf0;
        --border: #d8c9af;
        --public: #edf5ec;
        --private: #fff0fb;
        --prompt: #eef4ff;
        --storyteller: #fff8de;
        --error: #ffe7e3;
        --llm: #e8fbff;
        --debug: #efefef;
        --stats: #f7ecff;
        --system: #f7f1e7;
    }
    .stApp {
        background:
            radial-gradient(circle at top left, rgba(125, 47, 36, 0.10), transparent 28%),
            radial-gradient(circle at top right, rgba(113, 98, 61, 0.08), transparent 24%),
            linear-gradient(180deg, #f8f3e7 0%, #efe6d1 100%);
        color: var(--ink);
    }
    .block-container {
        padding-top: 1.4rem;
        padding-bottom: 1.5rem;
    }
    .botc-shell {
        background: rgba(255, 250, 240, 0.78);
        border: 1px solid rgba(125, 47, 36, 0.16);
        border-radius: 22px;
        box-shadow: 0 20px 50px rgba(66, 39, 18, 0.08);
        padding: 1.2rem 1.2rem 1rem;
        backdrop-filter: blur(8px);
    }
    .botc-title {
        font-size: 2.15rem;
        font-weight: 800;
        letter-spacing: 0.02em;
        margin-bottom: 0.2rem;
        color: #5b1f16;
    }
    .botc-subtitle {
        color: var(--muted);
        margin-bottom: 1rem;
    }
    .botc-status {
        display: inline-flex;
        align-items: center;
        gap: 0.55rem;
        background: rgba(255,255,255,0.78);
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 0.45rem 0.85rem;
        font-size: 0.94rem;
        color: #4f463d;
    }
    .botc-dot {
        width: 0.7rem;
        height: 0.7rem;
        border-radius: 999px;
        background: #b28b39;
        box-shadow: 0 0 0 6px rgba(178, 139, 57, 0.12);
    }
    .botc-dot.live {
        background: #2f7a41;
        box-shadow: 0 0 0 6px rgba(47, 122, 65, 0.14);
    }
    .botc-transcript {
        background: rgba(255, 252, 245, 0.94);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 1rem;
        min-height: 60vh;
        max-height: 72vh;
        overflow-y: scroll;
        overflow-x: hidden;
        scrollbar-gutter: stable both-edges;
        scrollbar-width: auto;
        scrollbar-color: rgba(92, 51, 157, 0.9) rgba(255, 252, 245, 0.94);
        padding-right: 1.1rem;
    }
    .botc-transcript::-webkit-scrollbar {
        width: 16px;
    }
    .botc-transcript::-webkit-scrollbar-track {
        background: rgba(233, 225, 213, 0.92);
        border-radius: 12px;
    }
    .botc-transcript::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, rgba(126, 34, 206, 0.95), rgba(91, 33, 182, 0.95));
        border-radius: 12px;
        border: 3px solid rgba(233, 225, 213, 0.92);
    }
    .botc-transcript:hover::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, rgba(147, 51, 234, 0.98), rgba(107, 33, 168, 0.98));
    }
    .botc-board,
    .botc-personal-shell {
        background: rgba(255, 252, 245, 0.94);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 1rem;
        margin-bottom: 1rem;
    }
    .botc-board {
        background:
            radial-gradient(circle at top, rgba(168, 85, 247, 0.13), transparent 42%),
            linear-gradient(180deg, rgba(8, 6, 15, 0.98), rgba(17, 10, 27, 0.98));
        border: 1px solid rgba(168, 85, 247, 0.42);
        box-shadow: inset 0 0 0 1px rgba(236, 72, 153, 0.10);
    }
    .botc-board-phase {
        margin-bottom: 0.75rem;
        font-weight: 700;
        color: #f3e8ff;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .botc-board-table {
        display: grid;
        gap: 0.7rem;
        align-items: stretch;
        width: 100%;
    }
    .botc-board-center {
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(125, 47, 36, 0.06);
        border: 1px dashed rgba(125, 47, 36, 0.2);
        border-radius: 16px;
        font-weight: 800;
        color: #6a3328;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        font-size: 0.82rem;
        min-height: 100%;
    }
    .botc-seat-card {
        background: rgba(255,255,255,0.85);
        border: 1px solid rgba(0,0,0,0.08);
        border-radius: 14px;
        padding: 0.7rem;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }
    .botc-seat-top {
        display: flex;
        justify-content: space-between;
        gap: 0.4rem;
        margin-bottom: 0.45rem;
        font-size: 0.78rem;
    }
    .botc-seat-num {
        color: #7b6a57;
        font-weight: 700;
    }
    .botc-seat-state {
        font-weight: 800;
        text-transform: uppercase;
        font-size: 0.72rem;
        letter-spacing: 0.06em;
    }
    .botc-seat-state.alive { color: #2f7a41; }
    .botc-seat-state.dead { color: #8c3427; }
    .botc-seat-name {
        font-weight: 800;
        margin-bottom: 0.25rem;
    }
    .botc-seat-status {
        color: #6b6258;
        font-size: 0.84rem;
        line-height: 1.35;
    }
    .botc-line {
        display: grid;
        grid-template-columns: 7.5rem 1fr;
        gap: 0.9rem;
        align-items: start;
        border: 1px solid rgba(0,0,0,0.06);
        border-radius: 14px;
        padding: 0.65rem 0.8rem;
        margin-bottom: 0.55rem;
    }
    .botc-tag {
        font-size: 0.78rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #5f564c;
        padding-top: 0.1rem;
    }
    .botc-tagwrap {
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
    }
    .botc-subtag {
        font-size: 0.82rem;
        color: #7b6a57;
        font-weight: 600;
    }
    .botc-text {
        white-space: pre-wrap;
        line-height: 1.45;
    }
    .botc-player-mention {
        display: inline-block;
        padding: 0 0.18rem;
        border-bottom: 1px solid;
        font-weight: 700;
    }
    .botc-banner {
        margin: 0.85rem 0 0.6rem;
        padding: 0.7rem 0.9rem;
        border-left: 4px solid #7d2f24;
        background: rgba(125, 47, 36, 0.06);
        border-radius: 12px;
        font-weight: 700;
        letter-spacing: 0.02em;
    }
    .botc-spacer {
        height: 0.4rem;
    }
    .botc-empty {
        color: var(--muted);
        padding: 1rem 0.2rem;
    }
    .botc-line.public { background: var(--public); }
    .botc-line.private { background: var(--private); }
    .botc-line.prompt { background: var(--prompt); }
    .botc-line.storyteller { background: var(--storyteller); }
    .botc-line.error { background: var(--error); }
    .botc-line.llm { background: var(--llm); }
    .botc-line.debug { background: var(--debug); }
    .botc-line.stats { background: var(--stats); }
    .botc-line.system { background: var(--system); }
    .botc-promptbox {
        background: rgba(255, 252, 245, 0.94);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 1rem;
    }
    .botc-personal-seat {
        color: #7b6a57;
        font-size: 0.86rem;
        margin-bottom: 0.35rem;
        font-weight: 700;
    }
    .botc-personal-role {
        font-size: 1.15rem;
        font-weight: 800;
        margin-bottom: 0.45rem;
        color: #5b1f16;
    }
    .botc-personal-ability {
        line-height: 1.45;
        margin-bottom: 0.9rem;
    }
    .botc-personal-notes-title {
        font-size: 0.8rem;
        font-weight: 800;
        letter-spacing: 0.07em;
        text-transform: uppercase;
        color: #6c6259;
        margin-bottom: 0.45rem;
    }
    .botc-note {
        background: rgba(255,255,255,0.75);
        border: 1px solid rgba(0,0,0,0.05);
        border-radius: 12px;
        padding: 0.55rem 0.65rem;
        margin-bottom: 0.45rem;
        line-height: 1.4;
    }
    .botc-prompt {
        background: rgba(238, 244, 255, 0.82);
        border: 1px solid rgba(61, 99, 161, 0.16);
        border-radius: 14px;
        padding: 1rem;
        margin-bottom: 0.9rem;
        white-space: pre-wrap;
        line-height: 1.5;
        font-size: 1rem;
    }
    .botc-action-note {
        border-radius: 12px;
        padding: 0.7rem 0.8rem;
        background: rgba(255,255,255,0.72);
        border: 1px solid rgba(0,0,0,0.05);
        color: #5b5147;
    }
    .botc-context {
        border-top: 1px dashed rgba(95, 86, 76, 0.25);
        margin-top: 0.9rem;
        padding-top: 0.9rem;
    }
    .botc-context-line {
        border-radius: 12px;
        padding: 0.6rem 0.7rem;
        margin-bottom: 0.45rem;
        background: rgba(255,255,255,0.72);
        border: 1px solid rgba(0,0,0,0.05);
    }
    .botc-context-tag {
        font-size: 0.74rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #6c6259;
        margin-bottom: 0.15rem;
    }
    .botc-raw {
        background: #16120f;
        color: #f5ead7;
        border-radius: 14px;
        padding: 0.9rem;
        max-height: 36vh;
        overflow-y: auto;
        white-space: pre-wrap;
        font-family: Consolas, monospace;
        font-size: 0.88rem;
    }
    .botc-expander-panel {
        background: #f6ede2;
        border: 1px solid #d1b8a5;
        border-radius: 14px;
        padding: 0.9rem;
        margin-top: 0.35rem;
    }
    .botc-expander-panel-note {
        color: #5a3128;
        font-size: 0.92rem;
        margin-bottom: 0.65rem;
    }
    .stExpander {
        background: #f6ede2;
        border: 1px solid #8f4a3e;
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 0 10px 24px rgba(73, 33, 24, 0.08);
    }
    .stExpander details {
        background: transparent;
    }
    .stExpander summary {
        background: linear-gradient(180deg, #7a241c, #5e1d17);
        color: #fff5ee !important;
    }
    .stExpander details > div[role="group"] {
        background: #f6ede2;
        padding: 0.45rem 0.65rem 0.75rem 0.65rem;
        color: #3f2e29;
    }
    .stExpander details > div[role="group"] *,
    .stExpander [data-testid="stExpanderDetails"] * {
        color: #3f2e29 !important;
    }
    .stExpander details > div[role="group"] .stTextInput,
    .stExpander details > div[role="group"] .stButton,
    .stExpander details > div[role="group"] .stAlert {
        background: transparent;
    }
    .stExpander details > div[role="group"] label,
    .stExpander details > div[role="group"] p,
    .stExpander details > div[role="group"] span,
    .stExpander details > div[role="group"] .botc-expander-panel-note {
        color: #3f2e29 !important;
    }
    .stExpander summary p,
    .stExpander summary span,
    .stExpander summary svg {
        color: #fff5ee !important;
        fill: #fff5ee !important;
    }
    .stExpander input[type="text"] {
        background: #fffaf4 !important;
        color: #2f221e !important;
        border: 1px solid #c6ab96 !important;
    }
    .stExpander input[type="text"]::placeholder {
        color: #7a625a !important;
    }
    .stExpander .stButton > button {
        background: linear-gradient(180deg, #7a241c, #5e1d17) !important;
        color: #fff5ee !important;
        border: 1px solid #4a1712 !important;
    }
    .stExpander .stButton > button *,
    .stExpander .stButton > button p,
    .stExpander .stButton > button span {
        color: #fff5ee !important;
    }
    .stExpander .stButton > button:hover {
        background: linear-gradient(180deg, #8b2a21, #692019) !important;
        color: #fff5ee !important;
    }
    [data-testid="stSidebar"] .stExpander {
        background: transparent;
        border: 1px solid rgba(255, 255, 255, 0.12);
        box-shadow: none;
    }
    [data-testid="stSidebar"] .stExpander summary {
        background: rgba(255, 255, 255, 0.04);
        color: inherit !important;
    }
    [data-testid="stSidebar"] .stExpander details > div[role="group"] {
        background: transparent;
        color: inherit;
    }
    [data-testid="stSidebar"] .stExpander details > div[role="group"] *,
    [data-testid="stSidebar"] .stExpander [data-testid="stExpanderDetails"] * {
        color: inherit !important;
    }
    [data-testid="stSidebar"] .stExpander summary p,
    [data-testid="stSidebar"] .stExpander summary span,
    [data-testid="stSidebar"] .stExpander summary svg {
        color: inherit !important;
        fill: currentColor !important;
    }
    [data-testid="stSidebar"] .stExpander input[type="text"] {
        background: initial !important;
        color: inherit !important;
        border: initial !important;
    }
    [data-testid="stSidebar"] .stExpander input[type="text"]::placeholder {
        color: inherit !important;
    }
    [data-testid="stSidebar"] .stExpander .stButton > button {
        background: initial !important;
        color: inherit !important;
        border: initial !important;
    }
    [data-testid="stSidebar"] .stExpander .stButton > button *,
    [data-testid="stSidebar"] .stExpander .stButton > button p,
    [data-testid="stSidebar"] .stExpander .stButton > button span {
        color: inherit !important;
    }
    .botc-board-svg {
        width: 100%;
        height: auto;
        display: block;
        filter: drop-shadow(0 10px 24px rgba(0, 0, 0, 0.28));
    }
    .intel-shell {
        background: linear-gradient(180deg, rgba(12, 9, 21, 0.98), rgba(21, 14, 33, 0.98));
        border: 1px solid rgba(168, 85, 247, 0.34);
        border-radius: 22px;
        box-shadow: 0 18px 40px rgba(0, 0, 0, 0.24);
        padding: 1.2rem;
        color: #f5ecff;
    }
    .intel-title {
        font-size: 2rem;
        font-weight: 800;
        color: #f5ecff;
        margin-bottom: 0.2rem;
    }
    .intel-subtitle {
        color: #d4c4ea;
        margin-bottom: 1rem;
    }
    .intel-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.7fr) minmax(320px, 0.95fr);
        gap: 1rem;
        align-items: start;
    }
    .intel-panel {
        background: rgba(17, 12, 27, 0.92);
        border: 1px solid rgba(168, 85, 247, 0.22);
        border-radius: 18px;
        padding: 1rem;
        color: #f7f2ff;
    }
    .intel-panel h3,
    .intel-panel h4,
    .intel-panel p,
    .intel-panel div,
    .intel-panel span,
    .intel-shell label {
        color: inherit;
    }
    .intel-response-hero {
        background: linear-gradient(180deg, rgba(33, 23, 50, 0.98), rgba(24, 16, 37, 0.98));
        border: 1px solid rgba(236, 72, 153, 0.24);
        border-radius: 18px;
        padding: 1rem;
        margin-bottom: 1rem;
    }
    .intel-response-block {
        background: rgba(10, 8, 17, 0.86);
        border: 1px solid rgba(168, 85, 247, 0.18);
        border-radius: 14px;
        padding: 0.9rem;
        margin-top: 0.75rem;
    }
    .intel-think {
        border-color: rgba(96, 165, 250, 0.22);
    }
    .intel-response-label {
        font-size: 0.76rem;
        font-weight: 800;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #d9c7f7;
        margin-bottom: 0.45rem;
    }
    .intel-response-text {
        white-space: pre-wrap;
        line-height: 1.55;
        color: #f7f2ff;
    }
    .intel-run-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 0.75rem;
        margin-bottom: 0.85rem;
    }
    .intel-run-title {
        font-size: 1.1rem;
        font-weight: 800;
        color: #f5ecff;
    }
    .intel-chip {
        display: inline-flex;
        align-items: center;
        padding: 0.3rem 0.6rem;
        border-radius: 999px;
        background: rgba(168, 85, 247, 0.16);
        border: 1px solid rgba(168, 85, 247, 0.28);
        color: #f0ddff;
        font-size: 0.8rem;
        font-weight: 700;
    }
    .intel-kv {
        display: grid;
        grid-template-columns: 9rem 1fr;
        gap: 0.45rem 0.8rem;
        font-size: 0.95rem;
    }
    .intel-kv-key {
        color: #bfaad9;
        font-weight: 700;
    }
    .intel-kv-val {
        color: #f7f2ff;
    }
    .intel-empty {
        color: #d4c4ea;
        padding: 0.2rem 0;
    }
    .intel-run-divider {
        height: 1px;
        margin: 1.15rem 0;
        background: linear-gradient(90deg, rgba(236,72,153,0.0), rgba(236,72,153,0.35), rgba(236,72,153,0.0));
    }
    @media (max-width: 900px) {
        .botc-board-table {
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            grid-template-rows: none !important;
        }
        .botc-board-center,
        .botc-seat-card {
            grid-column: auto !important;
            grid-row: auto !important;
        }
        .botc-line {
            grid-template-columns: 1fr;
            gap: 0.35rem;
        }
        .intel-grid {
            grid-template-columns: 1fr;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    app_mode = st.radio("Mode", ["Gameplay", "Intelligence Tests"], index=0)

    if app_mode == "Gameplay":
        st.header("Game Setup")
        setup_preset_options = get_setup_preset_names()
        with st.expander("Advanced Game Setup", expanded=False):
            selected_setup_preset = st.selectbox(
                "Setup Preset",
                setup_preset_options,
                index=0,
                help="Preset table setups fix the exact seat/role layout. They override random setup generation.",
            )
            if selected_setup_preset != "Off":
                preset_data = get_all_setup_presets()[selected_setup_preset]
                preset_character_set_names = {1: "Trouble Brewing", 2: "Bad Moon Rising", 3: "Both (Mixed)"}
                st.caption(
                    f"Preset overrides setup to `{preset_character_set_names.get(preset_data['character_set'], 'Unknown')}` "
                    f"with `{preset_data['player_count']}` players."
                )
        character_set = st.selectbox("Character Set", ["Trouble Brewing", "Bad Moon Rising", "Both (Mixed)"])
        player_count = st.slider("Player Count", min_value=5, max_value=15, value=8, step=1)

        st.subheader("Human Player")
        human_enabled = st.checkbox("Enable Human Seat", value=True)
        human_seat = st.number_input(
            "Human Seat (0-indexed)",
            min_value=0,
            max_value=14,
            value=int(st.session_state.human_seat_default),
            step=1,
            disabled=not human_enabled,
        )
        human_role_choice = st.selectbox(
            "Human Role",
            get_role_options(character_set),
            index=0,
            disabled=not human_enabled,
        )

        st.subheader("LLM Settings")
        use_llm = st.checkbox("Full LLM Mode", value=True)
        test_mode = st.checkbox("Test Mode (one LLM seat)", value=False)
        test_seat = st.number_input(
            "LLM Test Seat (0-indexed)",
            min_value=0,
            max_value=14,
            value=0,
            step=1,
            disabled=not test_mode,
        )
        fast_voting = st.checkbox("Fast LLM Voting", value=True)

        st.subheader("Logging")
        debug_logs = st.checkbox("Debug / Verbose Logging", value=False)
        print_stats = st.checkbox("Prompt/Usage Stats", value=False)
        colorize_players = st.checkbox("Player Color Coding", value=True)

        st.subheader("Backend")
        backend = st.selectbox("LLM Backend", ["openai", "google"])
        if backend == "openai":
            model_choice = st.selectbox(
                "OpenAI Model",
                OPENAI_MODEL_OPTIONS,
                index=0,
            )
            custom_model = st.text_input(
                "Custom Model String",
                value="",
                disabled=model_choice != "Custom",
                placeholder="Enter any model id",
            )
            selected_model_name = custom_model.strip() if model_choice == "Custom" else model_choice
        else:
            selected_model_name = "gemini-2.5-flash"

        with st.expander("Advanced LLM Settings", expanded=False):
            max_tokens_mode = st.selectbox(
                "Max Output Tokens",
                ["Current Default", "Double Default", "Custom"],
                index=0,
                help="Current default is 2048 for OpenAI calls unless a higher per-call limit is requested.",
            )
            custom_max_tokens = st.number_input(
                "Custom Max Output Tokens",
                min_value=1,
                max_value=32768,
                value=2048,
                step=256,
                disabled=max_tokens_mode != "Custom",
            )
            reasoning_mode = st.selectbox(
                "Reasoning Effort",
                ["Low", "Medium", "Custom"],
                index=0,
                help="Applies to OpenAI Responses API calls.",
            )
            custom_reasoning_effort = st.text_input(
                "Custom Reasoning Effort",
                value="",
                disabled=reasoning_mode != "Custom",
                placeholder="Enter any reasoning effort string",
            )
            use_response_budget_prompt = st.checkbox(
                "Append Response Budget Prompt",
                value=False,
                help="Adds a final prompt instruction to keep total effort, including internal reasoning, under a chosen word budget.",
            )
            response_budget_mode = st.selectbox(
                "Response Budget",
                ["0.5x", "0.75x", "1x", "Custom x"],
                index=0,
                disabled=not use_response_budget_prompt,
                help="Multiplies the selected max token cap, but the instruction is phrased in words.",
            )
            custom_response_budget_multiplier = st.number_input(
                "Custom Budget Multiplier",
                min_value=0.1,
                max_value=10.0,
                value=1.0,
                step=0.1,
                disabled=not use_response_budget_prompt or response_budget_mode != "Custom x",
            )
            use_team_models = st.checkbox(
                "Use Different Models By Starting Team",
                value=False,
                disabled=backend != "openai",
                help="Locks model selection by starting team. Townsfolk/Outsiders use the good-team model; Minions/Demon use the evil-team model. Recluse remains on the good-team model.",
            )
            good_team_model_choice = st.selectbox(
                "Good Team Model",
                OPENAI_MODEL_OPTIONS,
                index=0,
                disabled=backend != "openai" or not use_team_models,
            )
            good_team_custom_model = st.text_input(
                "Custom Good Team Model",
                value="",
                disabled=backend != "openai" or not use_team_models or good_team_model_choice != "Custom",
                placeholder="Enter any model id",
            )
            evil_team_model_choice = st.selectbox(
                "Evil Team Model",
                OPENAI_MODEL_OPTIONS,
                index=1,
                disabled=backend != "openai" or not use_team_models,
            )
            evil_team_custom_model = st.text_input(
                "Custom Evil Team Model",
                value="",
                disabled=backend != "openai" or not use_team_models or evil_team_model_choice != "Custom",
                placeholder="Enter any model id",
            )
            good_team_reasoning_mode = st.selectbox(
                "Good Team Reasoning Effort",
                ["Use Global", "Low", "Medium", "Custom"],
                index=0,
                disabled=backend != "openai" or not use_team_models,
                help="Overrides the global reasoning effort for good-team players only.",
            )
            good_team_custom_reasoning_effort = st.text_input(
                "Custom Good Team Reasoning Effort",
                value="",
                disabled=backend != "openai" or not use_team_models or good_team_reasoning_mode != "Custom",
                placeholder="Enter any reasoning effort string",
            )
            evil_team_reasoning_mode = st.selectbox(
                "Evil Team Reasoning Effort",
                ["Use Global", "Low", "Medium", "Custom"],
                index=0,
                disabled=backend != "openai" or not use_team_models,
                help="Overrides the global reasoning effort for evil-team players only.",
            )
            evil_team_custom_reasoning_effort = st.text_input(
                "Custom Evil Team Reasoning Effort",
                value="",
                disabled=backend != "openai" or not use_team_models or evil_team_reasoning_mode != "Custom",
                placeholder="Enter any reasoning effort string",
            )
        openai_key = st.text_input("OpenAI API Key", type="password")
        gemini_key = st.text_input("Gemini API Key", type="password")

        st.markdown("---")
        start_clicked = st.button("Start Game", type="primary", disabled=st.session_state.running)
        stop_clicked = st.button("Stop Game", disabled=not st.session_state.running)
        stop_and_save_clicked = st.button("End Game + Save Log", disabled=not st.session_state.running)
    else:
        st.header("Intelligence Tests")
        scenario_files = sorted(INTEL_SCENARIOS_DIR.glob("*.json"))
        scenario_labels = [scenario_file.name for scenario_file in scenario_files]
        selected_scenario_label = st.selectbox("Scenario", scenario_labels)
        intel_backend = st.selectbox("LLM Backend", ["openai", "google"], key="intel_backend")
        if intel_backend == "openai":
            intel_model_choice = st.selectbox(
                "OpenAI Model",
                ["gpt-5-mini", "gpt-5.4-mini", "gpt-5.4-nano", "Custom"],
                index=0,
                key="intel_model_choice",
            )
            intel_custom_model = st.text_input(
                "Custom Model String",
                value="",
                disabled=intel_model_choice != "Custom",
                placeholder="Enter any model id",
                key="intel_custom_model",
            )
            intel_model_name = intel_custom_model.strip() if intel_model_choice == "Custom" else intel_model_choice
        else:
            intel_model_name = "gemini-2.5-flash"
        intel_repeats = st.number_input("Repeats", min_value=1, max_value=20, value=1, step=1)
        intel_dry_run = st.checkbox("Dry Run (build prompt only)", value=False)
        openai_key = st.text_input("OpenAI API Key", type="password", key="intel_openai_key")
        gemini_key = st.text_input("Gemini API Key", type="password", key="intel_gemini_key")
        run_intel_clicked = st.button("Run Intelligence Test", type="primary")

if app_mode == "Gameplay":
    character_set_map = {
        "Trouble Brewing": 1,
        "Bad Moon Rising": 2,
        "Both (Mixed)": 3,
    }

    effective_openai_key = openai_key or os.getenv("OPENAI_API_KEY", "")
    effective_gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
    active_setup_preset = selected_setup_preset
    if active_setup_preset != "Off":
        preset_data = get_all_setup_presets()[active_setup_preset]
        preset_character_set_names = {1: "Trouble Brewing", 2: "Bad Moon Rising", 3: "Both (Mixed)"}
        character_set = preset_character_set_names.get(preset_data["character_set"], character_set)
        player_count = int(preset_data["player_count"])
    if max_tokens_mode == "Current Default":
        selected_max_output_tokens = 2048
    elif max_tokens_mode == "Double Default":
        selected_max_output_tokens = 4096
    else:
        selected_max_output_tokens = int(custom_max_tokens)

    if reasoning_mode == "Low":
        selected_reasoning_effort = "low"
    elif reasoning_mode == "Medium":
        selected_reasoning_effort = "medium"
    else:
        selected_reasoning_effort = (custom_reasoning_effort or "low").strip()

    selected_good_team_model_name = (
        (good_team_custom_model.strip() or selected_model_name) if backend == "openai" and use_team_models and good_team_model_choice == "Custom"
        else (good_team_model_choice if backend == "openai" and use_team_models else "")
    )
    selected_evil_team_model_name = (
        (evil_team_custom_model.strip() or selected_model_name) if backend == "openai" and use_team_models and evil_team_model_choice == "Custom"
        else (evil_team_model_choice if backend == "openai" and use_team_models else "")
    )
    if good_team_reasoning_mode == "Use Global":
        selected_good_team_reasoning_effort = selected_reasoning_effort
    elif good_team_reasoning_mode == "Low":
        selected_good_team_reasoning_effort = "low"
    elif good_team_reasoning_mode == "Medium":
        selected_good_team_reasoning_effort = "medium"
    else:
        selected_good_team_reasoning_effort = (good_team_custom_reasoning_effort or selected_reasoning_effort).strip()
    if evil_team_reasoning_mode == "Use Global":
        selected_evil_team_reasoning_effort = selected_reasoning_effort
    elif evil_team_reasoning_mode == "Low":
        selected_evil_team_reasoning_effort = "low"
    elif evil_team_reasoning_mode == "Medium":
        selected_evil_team_reasoning_effort = "medium"
    else:
        selected_evil_team_reasoning_effort = (evil_team_custom_reasoning_effort or selected_reasoning_effort).strip()

    response_budget_prompt = ""
    response_budget_words = None
    if use_response_budget_prompt:
        if response_budget_mode == "0.5x":
            budget_multiplier = 0.5
        elif response_budget_mode == "0.75x":
            budget_multiplier = 0.75
        elif response_budget_mode == "1x":
            budget_multiplier = 1.0
        else:
            budget_multiplier = float(custom_response_budget_multiplier)
        response_budget_words = max(1, int(round(selected_max_output_tokens * budget_multiplier)))
        response_budget_prompt = (
            f"Keep your total response effort, including internal reasoning and visible output, "
            f"under approximately {response_budget_words} words. Be concise."
        )

    selected_model_config = get_model_config(
        backend,
        selected_model_name,
        selected_reasoning_effort if backend == "openai" else "n/a",
        selected_max_output_tokens if backend == "openai" else "per-call max_tokens",
    )

    if (use_llm or test_mode) and backend == "openai" and not effective_openai_key:
        st.sidebar.error("OpenAI backend selected but no API key was found. LLM mode has been disabled.")
        use_llm = False
        test_mode = False
        fast_voting = False

    if (use_llm or test_mode) and backend == "google" and not effective_gemini_key:
        st.sidebar.error("Google backend selected but no API key was found. LLM mode has been disabled.")
        use_llm = False
        test_mode = False
        fast_voting = False

    if human_enabled and human_seat > player_count - 1:
        human_seat = player_count - 1
        st.sidebar.warning("Human seat was clamped to the current player count.")

    with st.sidebar.expander("Run / Model Info", expanded=False):
        st.markdown(
            "\n".join(
                [
                    f"`Character Set`: {character_set}",
                    f"`Player Count`: {player_count}",
                    f"`Setup Preset`: {active_setup_preset if active_setup_preset != 'Off' else 'off'}",
                    f"`Human Seat`: {int(human_seat) if human_enabled else 'off'}",
                    f"`Human Role`: {human_role_choice if human_enabled else 'off'}",
                    f"`Full LLM Mode`: {use_llm}",
                    f"`Test Mode`: {test_mode}",
                    f"`Fast LLM Voting`: {fast_voting}",
                    f"`Player Color Coding`: {colorize_players}",
                    f"`Backend`: {selected_model_config['backend']}",
                    f"`API Style`: {selected_model_config['api_style']}",
                    f"`Model`: {selected_model_config['model']}",
                    f"`Team Split Models`: {use_team_models if backend == 'openai' else False}",
                    f"`Good Team Model`: {selected_good_team_model_name if backend == 'openai' and use_team_models else 'n/a'}",
                    f"`Evil Team Model`: {selected_evil_team_model_name if backend == 'openai' and use_team_models else 'n/a'}",
                    f"`Good Team Reasoning`: {selected_good_team_reasoning_effort if backend == 'openai' and use_team_models else 'n/a'}",
                    f"`Evil Team Reasoning`: {selected_evil_team_reasoning_effort if backend == 'openai' and use_team_models else 'n/a'}",
                    f"`Reasoning Effort`: {selected_model_config['reasoning_effort']}",
                    f"`Temperature`: {selected_model_config['temperature']}",
                    f"`Max Output Tokens`: {selected_model_config['max_output_tokens']}",
                    f"`Response Budget Prompt`: {response_budget_words if response_budget_words is not None else 'off'} words",
                ]
            )
        )

    inputs = [
        "2",
        "y" if debug_logs else "n",
        str(character_set_map[character_set]),
        str(player_count),
        "y" if use_llm else "n",
        "y" if human_enabled else "n",
    ]
    if human_enabled:
        inputs.append(str(int(human_seat)))
    inputs.append("y" if print_stats else "n")
    if test_mode:
        inputs.extend(["y", str(int(test_seat))])
    else:
        inputs.append("n")
    inputs.append("y" if fast_voting else "n")
    input_script = "\n".join(inputs) + "\n"

    if start_clicked:
        start_game(
            input_script,
            backend,
            selected_model_name,
            effective_openai_key,
            effective_gemini_key,
            human_role_choice,
            selected_reasoning_effort if backend == "openai" else "",
            selected_max_output_tokens if backend == "openai" else None,
            response_budget_prompt,
            use_team_models if backend == "openai" else False,
            selected_good_team_model_name,
            selected_evil_team_model_name,
            selected_good_team_reasoning_effort if backend == "openai" and use_team_models else "",
            selected_evil_team_reasoning_effort if backend == "openai" and use_team_models else "",
            active_setup_preset,
        )

    if stop_and_save_clicked and st.session_state.proc:
        try:
            if st.session_state.proc.stdin:
                st.session_state.proc.stdin.write("__BOTC_END_AND_SAVE__\n")
                st.session_state.proc.stdin.flush()
        except Exception:
            pass
        st.session_state.end_and_save_requested_at = time.time()
        st.session_state.last_status = "Ending game and saving log"

    if stop_clicked and st.session_state.proc:
        try:
            st.session_state.proc.terminate()
        except Exception:
            pass
        st.session_state.running = False
        st.session_state.end_and_save_requested_at = None
        st.session_state.last_status = "Stopped"

    consume_output()

    if (
        st.session_state.running
        and st.session_state.proc
        and st.session_state.end_and_save_requested_at is not None
        and time.time() - st.session_state.end_and_save_requested_at > 2.0
    ):
        engine_log_path = st.session_state.get("engine_log_path")
        save_path = None
        if engine_log_path and Path(engine_log_path).exists():
            save_path = Path(engine_log_path)
        else:
            save_path = save_partial_game_log_from_ui()
        try:
            st.session_state.proc.terminate()
        except Exception:
            pass
        st.session_state.running = False
        st.session_state.end_and_save_requested_at = None
        if engine_log_path and Path(engine_log_path).exists():
            st.session_state.last_status = f"Force-stopped; latest engine log saved to {save_path.name}"
        else:
            st.session_state.last_status = f"Force-stopped; partial log saved to {save_path.name}"

    board_state = parse_board_state(st.session_state.messages)
    personal_state = parse_personal_panel(st.session_state.messages)

    status_class = "live" if st.session_state.running else ""
    status_text = st.session_state.last_status
    if st.session_state.running and st.session_state.pending_prompt:
        status_text = "Waiting for input"

    st.markdown("<div class='botc-shell'>", unsafe_allow_html=True)
    st.markdown("<div class='botc-title'>Blood on the Clocktower v1.5</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='botc-subtitle'>v1.5 interactive browser port of the notebook engine with structured transcript rendering.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='botc-status'><span class='botc-dot {status_class}'></span>{html.escape(status_text)}</div>",
        unsafe_allow_html=True,
    )

    left_col, right_col = st.columns([2.0, 1.15], gap="large")

    with left_col:
        st.markdown("### Board")
        board_svg = render_board_svg(board_state, personal_state)
        board_phase_html = (
            f"<div class='botc-board-phase'>{html.escape(board_state['phase'])}</div>"
            if board_state["phase"]
            else ""
        )
        board_body_html = board_svg or "<div class='botc-empty'>Board appears once seat assignments are known.</div>"
        st.markdown(f"<div class='botc-board'>{board_phase_html}{board_body_html}</div>", unsafe_allow_html=True)
        st.markdown("### Transcript")
        st.markdown(
            f"<div id='botc-transcript-box' class='botc-transcript'>{render_transcript(st.session_state.messages, board_state, colorize_players)}</div>",
            unsafe_allow_html=True,
        )
        components.html(
            """
            <script>
            const KEY = "botc_transcript_scroll_top";
            const doc = window.parent.document;
            const box = doc.getElementById("botc-transcript-box");
            if (box) {
              const saved = window.sessionStorage.getItem(KEY);
              if (saved !== null) {
                box.scrollTop = parseInt(saved, 10) || 0;
              }
              if (!box.dataset.scrollBound) {
                box.addEventListener("scroll", () => {
                  window.sessionStorage.setItem(KEY, String(box.scrollTop));
                });
                box.dataset.scrollBound = "1";
              }
            }
            </script>
            """,
            height=0,
        )

    with right_col:
        st.markdown("### Personal")
        st.markdown(
            f"<div class='botc-personal-shell'>{render_personal_panel(personal_state)}</div>",
            unsafe_allow_html=True,
        )
        st.markdown("### Action")
        if st.session_state.running and st.session_state.pending_prompt:
            st.markdown("Current prompt")
            prompt_display = build_prompt_display(st.session_state.pending_prompt)
            st.markdown(
                f"<div class='botc-prompt'>{html.escape(prompt_display).replace(chr(10), '<br>')}</div>",
                unsafe_allow_html=True,
            )
            with st.form("response_form", clear_on_submit=True):
                response_text = st.text_area(
                    "Your response",
                    key="user_input",
                    height=160,
                    label_visibility="collapsed",
                    placeholder="Type your response here...",
                )
                submitted = st.form_submit_button("Send", type="primary", use_container_width=True)
            if submitted:
                try:
                    st.session_state.proc.stdin.write(response_text + "\n")
                    st.session_state.proc.stdin.flush()
                    st.session_state.pending_prompt = None
                    st.session_state.last_status = "Processing input"
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to send input to the game process: {exc}")
        elif st.session_state.running:
            st.markdown(
                "<div class='botc-promptbox'><div class='botc-action-note'>The game is running. The next prompt will appear here.</div></div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div class='botc-promptbox'><div class='botc-action-note'>Start a game from the sidebar to begin.</div></div>",
                unsafe_allow_html=True,
            )

        with st.expander("Save Setup Preset", expanded=False):
            st.markdown(
                "<div class='botc-expander-panel'><div class='botc-expander-panel-note'>Save the current seat/role setup and setup-specific hidden state as a reusable preset.</div>",
                unsafe_allow_html=True,
            )
            preset_name_input = st.text_input(
                "Preset Name",
                value="",
                key="save_setup_preset_name",
                placeholder="Enter a name for the current table setup",
            )
            save_preset_clicked = st.button(
                "Save Current Setup As Preset",
                use_container_width=True,
                disabled=not st.session_state.get("engine_log_path"),
            )
            if save_preset_clicked:
                ok, message = save_setup_preset_from_current_game(preset_name_input)
                st.session_state.preset_save_status = ("success" if ok else "error", message)
            preset_save_status = st.session_state.get("preset_save_status")
            if preset_save_status:
                kind, message = preset_save_status
                if kind == "success":
                    st.success(message)
                else:
                    st.error(message)
            st.markdown("</div>", unsafe_allow_html=True)

        with st.expander("Raw Transcript", expanded=False):
            raw_text = "\n".join(st.session_state.raw_lines[-500:])
            st.markdown(f"<div class='botc-raw'>{html.escape(raw_text)}</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.running and not st.session_state.pending_prompt:
        time.sleep(0.2)
        st.rerun()
else:
    effective_openai_key = openai_key or os.getenv("OPENAI_API_KEY", "")
    effective_gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
    if run_intel_clicked:
        if intel_backend == "openai" and not effective_openai_key and not intel_dry_run:
            st.sidebar.error("OpenAI backend selected but no API key was found.")
        elif intel_backend == "google" and not effective_gemini_key and not intel_dry_run:
            st.sidebar.error("Google backend selected but no API key was found.")
        else:
            if effective_openai_key:
                os.environ["OPENAI_API_KEY"] = effective_openai_key
            if effective_gemini_key:
                os.environ["GEMINI_API_KEY"] = effective_gemini_key
            scenario_path = INTEL_SCENARIOS_DIR / selected_scenario_label
            collected = intel_run_batch(
                scenario_path=scenario_path,
                backend=intel_backend,
                model=intel_model_name,
                repeats=int(intel_repeats),
                call_api=not intel_dry_run,
            )
            for result in collected:
                xml_path = intel_save_result(result)
                result["xml_path"] = str(xml_path)
            st.session_state.intel_last_results = collected

    st.markdown(
        "<div class='intel-shell'>"
        "<div class='intel-title'>Intelligence Tests</div>"
        "<div class='intel-subtitle'>Replay authentic BOTC prompts, compare repeated runs, and inspect the model response before anything else.</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    results = st.session_state.intel_last_results
    if not results:
        st.markdown(
            "<div class='intel-panel'><div class='intel-empty'>Run a scenario from the sidebar. The model response will appear here first, with prompt and scenario details underneath.</div></div>",
            unsafe_allow_html=True,
        )
    else:
        for result in results:
            st.markdown(
                "<div class='intel-run-header'>"
                f"<div class='intel-run-title'>Run {result.get('run_index', 0) + 1}: {html.escape(result['scenario_name'])}</div>"
                f"<div class='intel-chip'>{html.escape(result['backend'])} / {html.escape(result['model'])}</div>"
                "</div>",
                unsafe_allow_html=True,
            )
            left_col, right_col = st.columns([1.7, 1.0], gap="large")
            with left_col:
                st.markdown(
                    f"<div class='intel-response-hero'>{format_intel_response_blocks(result.get('raw_response') or '')}</div>",
                    unsafe_allow_html=True,
                )
                response_tab, prompt_tab, public_log_tab = st.tabs(["Response XML", "Prompt Messages", "Public Log"])
                with response_tab:
                    st.code(result.get("raw_response") or "<dry-run: no API call made>", language="xml")
                with prompt_tab:
                    st.json(result.get("prompt_messages", []), expanded=False)
                with public_log_tab:
                    st.code("\n".join(f"[{entry['phase_tick_id']}] {entry['msg']}" for entry in result.get("public_log", [])), language="text")
            with right_col:
                st.markdown(
                    "<div class='intel-panel'><div class='intel-kv'>"
                    f"<div class='intel-kv-key'>Scenario</div><div class='intel-kv-val'>{html.escape(result['scenario_id'])}</div>"
                    f"<div class='intel-kv-key'>Acting Player</div><div class='intel-kv-val'>{html.escape(result['acting_player']['name'])} (Seat {result['acting_player']['seat']}), {html.escape(result['acting_player']['role'])}</div>"
                    f"<div class='intel-kv-key'>Action Type</div><div class='intel-kv-val'>{html.escape(result['action_type'])}</div>"
                    f"<div class='intel-kv-key'>Passed</div><div class='intel-kv-val'>{html.escape(str(result['evaluation'].get('passed')))}</div>"
                    f"<div class='intel-kv-key'>Parsed</div><div class='intel-kv-val'>{html.escape(json.dumps(result['evaluation'].get('parsed', {}), ensure_ascii=False))}</div>"
                    f"<div class='intel-kv-key'>Saved XML</div><div class='intel-kv-val'>{html.escape(result.get('xml_path', ''))}</div>"
                    "</div></div>",
                    unsafe_allow_html=True,
                )
                with st.expander("Observer View", expanded=False):
                    st.code(result.get("observer_log", ""), language="text")
            st.markdown("<div class='intel-run-divider'></div>", unsafe_allow_html=True)
