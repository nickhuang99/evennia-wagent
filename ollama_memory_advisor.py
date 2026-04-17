import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from typing import Any

import requests


DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
DEFAULT_API = os.getenv("OLLAMA_API", "http://localhost:11434/api/generate")


def normalize_room_name(raw_room: Any) -> str:
    return re.sub(r"\s+", " ", str(raw_room or "").strip().lower())


def normalize_action(raw_action: Any) -> str:
    return re.sub(r"\s+", " ", str(raw_action or "").strip().lower())


def is_safe_action(action: str) -> bool:
    clean = normalize_action(action)
    if not clean:
        return False
    if len(clean.split()) > 4:
        return False
    if len(clean) > 30:
        return False
    return bool(re.match(r"^[a-z0-9\s\-]+$", clean))


def memory_lock_path(path: str) -> str:
    return f"{path}.lock"


def locked_json_load(path: str, fallback: Any) -> Any:
    lock_path = memory_lock_path(path)
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not os.path.exists(path):
            return fallback
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


def locked_json_dump(path: str, payload: Any) -> None:
    lock_path = memory_lock_path(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


def tail_text(path: str, line_count: int) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if line_count <= 0:
        return "".join(lines)
    return "".join(lines[-line_count:])


def summarize_json(payload: Any, max_chars: int = 4000) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<truncated>"


def extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return json.loads(fenced.group(1))

    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("No JSON object found in model response")


def build_prompt(log_excerpt: str, observation_payload: Any, route_payload: Any, map_payload: Any) -> str:
    return f"""
You are an action/map/route memory advisor for an Evennia exploration bot.

Your job is to inspect the recent run log and current memories, then propose ONLY evidence-backed updates.

Return JSON only with this schema:
{{
  "feedback_hints": [
    {{
      "pattern": "substring from engine feedback",
      "prefer": ["safe action", "..."],
      "avoid": ["bad action", "..."],
      "confidence": 0.0,
      "reason": "short reason"
    }}
  ],
  "observation_updates": [
    {{
      "room": "normalized room or unknown-room",
      "failed_actions": ["bad action"],
      "confirmed_walks": {{"action": "to_room"}},
      "confidence": 0.0,
      "reason": "short reason"
    }}
  ],
  "route_updates": [
    {{
      "destination": "target room",
      "from_room": "room",
      "action": "action",
      "to_room": "room",
      "confidence": 0.0,
      "reason": "short reason"
    }}
  ],
  "map_updates": [
    {{
      "from_room": "room",
      "action": "action",
      "to_room": "room",
      "recipe": ["step1", "step2"],
      "confidence": 0.0,
      "reason": "short reason"
    }}
  ],
  "notes": ["short note"]
}}

Rules:
- Only propose updates directly supported by the log.
- Do not invent rooms, actions, routes, or recipes not present in the log.
- If the engine explicitly says to try an action, prefer that action and avoid contradictory actions.
- For example, if the feedback says "Try feeling around", prefer "feel around" and avoid unsupported light commands unless the scene also explicitly provides a lightable object.
- Use empty arrays when unsure.

Concrete example:
- If the log shows `执行指令: light splinter` and the next feedback says `Try feeling around, maybe you'll find something helpful!`, then this is evidence that `feel around` should be preferred and the attempted light command should be avoided in that feedback pattern.
- In that case, include a feedback hint for the text pattern and an observation update marking the bad light action as failed for `unknown-room` if the room title is not available.

[Recent log excerpt]
{log_excerpt}

[Observation memory summary]
{summarize_json(observation_payload)}

[Route memory summary]
{summarize_json(route_payload)}

[Map memory summary]
{summarize_json(map_payload)}
""".strip()


def call_ollama(api_url: str, model: str, prompt: str, timeout: int) -> dict:
    response = requests.post(
        api_url,
        json={
            "model": model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    text = payload.get("response", "") or payload.get("thinking", "") or ""
    if not text.strip():
        raise ValueError("Ollama returned an empty response")
    return extract_json_object(text)


def sanitize_feedback_hints(raw_hints: Any) -> list[dict]:
    hints = []
    if not isinstance(raw_hints, list):
        return hints
    for item in raw_hints:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "") or "").strip().lower()
        if not pattern:
            continue
        prefer = []
        for action in item.get("prefer", []):
            clean = normalize_action(action)
            if clean and is_safe_action(clean) and clean not in prefer:
                prefer.append(clean)
        avoid = []
        for action in item.get("avoid", []):
            clean = normalize_action(action)
            if clean and is_safe_action(clean) and clean not in avoid:
                avoid.append(clean)
        hints.append({
            "pattern": pattern,
            "prefer": prefer,
            "avoid": avoid,
            "confidence": float(item.get("confidence", 0.0) or 0.0),
            "reason": str(item.get("reason", "") or "").strip()[:200],
        })
    return hints


def sanitize_observation_updates(raw_updates: Any) -> list[dict]:
    updates = []
    if not isinstance(raw_updates, list):
        return updates
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        room = normalize_room_name(item.get("room", ""))
        if not room:
            continue
        failed_actions = []
        for action in item.get("failed_actions", []):
            clean = normalize_action(action)
            if clean and is_safe_action(clean) and clean not in failed_actions:
                failed_actions.append(clean)
        confirmed_walks = {}
        raw_walks = item.get("confirmed_walks", {})
        if isinstance(raw_walks, dict):
            for action, to_room in raw_walks.items():
                clean_action = normalize_action(action)
                clean_target = normalize_room_name(to_room)
                if clean_action and clean_target and is_safe_action(clean_action):
                    confirmed_walks[clean_action] = clean_target
        updates.append({
            "room": room,
            "failed_actions": failed_actions,
            "confirmed_walks": confirmed_walks,
            "confidence": float(item.get("confidence", 0.0) or 0.0),
            "reason": str(item.get("reason", "") or "").strip()[:200],
        })
    return updates


def sanitize_route_updates(raw_updates: Any) -> list[dict]:
    updates = []
    if not isinstance(raw_updates, list):
        return updates
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        destination = normalize_room_name(item.get("destination", ""))
        from_room = normalize_room_name(item.get("from_room", ""))
        action = normalize_action(item.get("action", ""))
        to_room = normalize_room_name(item.get("to_room", ""))
        if destination and from_room and to_room and action and is_safe_action(action):
            updates.append({
                "destination": destination,
                "from_room": from_room,
                "action": action,
                "to_room": to_room,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "reason": str(item.get("reason", "") or "").strip()[:200],
            })
    return updates


def sanitize_map_updates(raw_updates: Any) -> list[dict]:
    updates = []
    if not isinstance(raw_updates, list):
        return updates
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        from_room = normalize_room_name(item.get("from_room", ""))
        action = normalize_action(item.get("action", ""))
        to_room = normalize_room_name(item.get("to_room", ""))
        if not (from_room and action and to_room and is_safe_action(action)):
            continue
        recipe = []
        for step in item.get("recipe", []):
            clean = normalize_action(step)
            if clean and is_safe_action(clean) and clean not in recipe:
                recipe.append(clean)
        updates.append({
            "from_room": from_room,
            "action": action,
            "to_room": to_room,
            "recipe": recipe or [action],
            "confidence": float(item.get("confidence", 0.0) or 0.0),
            "reason": str(item.get("reason", "") or "").strip()[:200],
        })
    return updates


def sanitize_proposal(payload: dict) -> dict:
    return {
        "feedback_hints": sanitize_feedback_hints(payload.get("feedback_hints", [])),
        "observation_updates": sanitize_observation_updates(payload.get("observation_updates", [])),
        "route_updates": sanitize_route_updates(payload.get("route_updates", [])),
        "map_updates": sanitize_map_updates(payload.get("map_updates", [])),
        "notes": [str(note).strip()[:200] for note in payload.get("notes", []) if str(note).strip()],
    }


def heuristic_feedback_hints_from_log(log_excerpt: str) -> dict:
    light_actions = {"light wood", "light splinter", "light"}
    prefer_pattern = "try feeling around"
    action_pattern = re.compile(r"执行指令:\s*(.+?)\s*$")

    current_action = ""
    contradiction_actions = []
    for raw_line in log_excerpt.splitlines():
        line = raw_line.strip().lower()
        action_match = action_pattern.search(raw_line)
        if action_match:
            current_action = normalize_action(action_match.group(1))
            continue
        if prefer_pattern in line or "feel around" in line:
            if current_action in light_actions and current_action not in contradiction_actions:
                contradiction_actions.append(current_action)

    if not contradiction_actions:
        return {
            "feedback_hints": [],
            "observation_updates": [],
            "route_updates": [],
            "map_updates": [],
            "notes": [],
        }

    avoid_actions = []
    for action in ["light wood", "light splinter", "light"]:
        if action not in avoid_actions:
            avoid_actions.append(action)

    return {
        "feedback_hints": [
            {
                "pattern": prefer_pattern,
                "prefer": ["feel around"],
                "avoid": avoid_actions,
                "confidence": 0.95,
                "reason": "Engine explicitly says to try feeling around in darkness feedback.",
            }
        ],
        "observation_updates": [
            {
                "room": "unknown-room",
                "failed_actions": contradiction_actions,
                "confirmed_walks": {},
                "confidence": 0.9,
                "reason": "Recent log shows these light commands immediately contradicted by the engine hint to feel around.",
            }
        ],
        "route_updates": [],
        "map_updates": [],
        "notes": ["Heuristic fallback inferred a dark-room action hint from explicit engine guidance."],
    }


def merge_proposals(primary: dict, fallback: dict) -> dict:
    merged = {
        "feedback_hints": list(primary.get("feedback_hints", [])),
        "observation_updates": list(primary.get("observation_updates", [])),
        "route_updates": list(primary.get("route_updates", [])),
        "map_updates": list(primary.get("map_updates", [])),
        "notes": list(primary.get("notes", [])),
    }

    seen_hint_patterns = {item.get("pattern") for item in merged["feedback_hints"] if isinstance(item, dict)}
    for item in fallback.get("feedback_hints", []):
        pattern = item.get("pattern")
        if pattern and pattern not in seen_hint_patterns:
            merged["feedback_hints"].append(item)
            seen_hint_patterns.add(pattern)

    seen_obs_rooms = {item.get("room") for item in merged["observation_updates"] if isinstance(item, dict)}
    for item in fallback.get("observation_updates", []):
        room = item.get("room")
        if room and room not in seen_obs_rooms:
            merged["observation_updates"].append(item)
            seen_obs_rooms.add(room)

    for bucket in ["route_updates", "map_updates"]:
        merged[bucket].extend(item for item in fallback.get(bucket, []) if item not in merged[bucket])

    for note in fallback.get("notes", []):
        if note not in merged["notes"]:
            merged["notes"].append(note)
    return merged


def apply_observation_updates(path: str, updates: list[dict], min_confidence: float) -> int:
    payload = locked_json_load(path, {"meta": {}, "rooms": {}, "runs": [], "recent_events": []})
    rooms = payload.setdefault("rooms", {})
    applied = 0
    for item in updates:
        if float(item.get("confidence", 0.0)) < min_confidence:
            continue
        room = item["room"]
        entry = rooms.setdefault(room, {
            "visits": 0,
            "first_seen_ts": 0,
            "last_seen_ts": 0,
            "observed_exits": [],
            "confirmed_walks": {},
            "failed_actions": [],
            "failure_counts": {},
            "scan_targets": [],
            "last_snapshot_excerpt": "",
        })
        failed_actions = list(entry.get("failed_actions", []))
        failure_counts = dict(entry.get("failure_counts", {}))
        changed = False
        for action in item.get("failed_actions", []):
            if action not in failed_actions:
                failed_actions.append(action)
                changed = True
            failure_counts[action] = max(1, int(failure_counts.get(action, 0)))
        entry["failed_actions"] = failed_actions
        entry["failure_counts"] = failure_counts
        confirmed_walks = dict(entry.get("confirmed_walks", {}))
        for action, to_room in item.get("confirmed_walks", {}).items():
            if confirmed_walks.get(action) != to_room:
                confirmed_walks[action] = to_room
                changed = True
        entry["confirmed_walks"] = confirmed_walks
        if changed:
            applied += 1
    if applied:
        locked_json_dump(path, payload)
    return applied


def apply_route_updates(path: str, updates: list[dict], min_confidence: float) -> int:
    payload = locked_json_load(path, {"destinations": {}})
    destinations = payload.setdefault("destinations", {})
    applied = 0
    for item in updates:
        if float(item.get("confidence", 0.0)) < min_confidence:
            continue
        record = destinations.setdefault(item["destination"], {"hops": {}, "latest_success_path": [], "updated_ts": 0})
        hops = record.setdefault("hops", {})
        previous = hops.get(item["from_room"])
        candidate = {
            "action": item["action"],
            "to_room": item["to_room"],
            "updated_ts": int(__import__("time").time()),
            "learned_from_role": "ollama-advisor",
        }
        if previous != candidate:
            hops[item["from_room"]] = candidate
            record["updated_ts"] = candidate["updated_ts"]
            applied += 1
    if applied:
        locked_json_dump(path, payload)
    return applied


def apply_map_updates(path: str, updates: list[dict], min_confidence: float) -> int:
    payload = locked_json_load(path, {"format": "room-exits-v2", "rooms": {}})
    payload.setdefault("format", "room-exits-v2")
    rooms = payload.setdefault("rooms", {})
    applied = 0
    for item in updates:
        if float(item.get("confidence", 0.0)) < min_confidence:
            continue
        room_entry = rooms.setdefault(item["from_room"], {"exits": {}})
        exits = room_entry.setdefault("exits", {})
        key = f"{item['action']} || {item['to_room']}"
        candidate = {
            "action": item["action"],
            "to": item["to_room"],
            "recipe": item.get("recipe", [item["action"]]),
        }
        if exits.get(key) != candidate:
            exits[key] = candidate
            applied += 1
    if applied:
        locked_json_dump(path, payload)
    return applied


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask Ollama for action/map/route memory update proposals based on a run log.")
    parser.add_argument("--log-file", required=True, help="Run log to analyze.")
    parser.add_argument("--observation-memory", default="wagent_scanner_observation_memory.json", help="Observation memory file to provide/apply.")
    parser.add_argument("--route-memory", default="wagent_route_memory.json", help="Route memory file to provide/apply.")
    parser.add_argument("--map-memory", default="wagent_map_memory.json", help="Map memory file to provide/apply.")
    parser.add_argument("--tail-lines", type=int, default=120, help="How many log lines to send to the model.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model to use.")
    parser.add_argument("--api", default=DEFAULT_API, help="Ollama generate endpoint.")
    parser.add_argument("--timeout", type=int, default=45, help="Request timeout in seconds.")
    parser.add_argument("--output-file", default="", help="Optional file to save the advisor JSON.")
    parser.add_argument("--apply-observation", action="store_true", help="Apply confident observation updates.")
    parser.add_argument("--apply-route", action="store_true", help="Apply confident route updates.")
    parser.add_argument("--apply-map", action="store_true", help="Apply confident map updates.")
    parser.add_argument("--min-confidence", type=float, default=0.8, help="Minimum confidence required for apply modes.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    log_excerpt = tail_text(args.log_file, args.tail_lines)
    observation_payload = locked_json_load(args.observation_memory, {"meta": {}, "rooms": {}, "runs": [], "recent_events": []})
    route_payload = locked_json_load(args.route_memory, {"destinations": {}})
    map_payload = locked_json_load(args.map_memory, {"format": "room-exits-v2", "rooms": {}})

    prompt = build_prompt(log_excerpt, observation_payload, route_payload, map_payload)
    raw_proposal = call_ollama(args.api, args.model, prompt, args.timeout)
    proposal = sanitize_proposal(raw_proposal)
    heuristic_proposal = heuristic_feedback_hints_from_log(log_excerpt)
    proposal = merge_proposals(proposal, heuristic_proposal)

    applied = {"observation": 0, "route": 0, "map": 0}
    if args.apply_observation:
        applied["observation"] = apply_observation_updates(args.observation_memory, proposal["observation_updates"], args.min_confidence)
    if args.apply_route:
        applied["route"] = apply_route_updates(args.route_memory, proposal["route_updates"], args.min_confidence)
    if args.apply_map:
        applied["map"] = apply_map_updates(args.map_memory, proposal["map_updates"], args.min_confidence)

    result = {
        "model": args.model,
        "log_file": args.log_file,
        "proposal": proposal,
        "applied": applied,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))