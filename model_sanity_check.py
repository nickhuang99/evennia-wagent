import argparse
import json
import sys

from model_api import (
    call_model_api,
    configured_model_api_key,
    configured_model_api_kind,
    configured_model_api_url,
    configured_model_name,
)


DEFAULT_PROMPT = """You are a compatibility test for a MUD automation workflow.

Return strict JSON only with exactly these keys:
- status: string, must be \"ok\"
- thought: short string under 80 characters
- action: a short lowercase game command
- new_commands: array of strings

Constraints:
- no markdown fences
- no explanation outside JSON
- action must be one of: look, help, north, south, east, west, up, down

Example valid output:
{"status":"ok","thought":"basic instruction following works","action":"look","new_commands":[]}
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a compatibility sanity test against the configured Wagent model endpoint."
    )
    parser.add_argument("--model", default=configured_model_name("qwen2.5:7b"))
    parser.add_argument("--api-kind", default=configured_model_api_kind("ollama"))
    parser.add_argument("--api-url", default=configured_model_api_url())
    parser.add_argument("--api-key", default=configured_model_api_key())
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--print-response", action="store_true")
    return parser.parse_args()


def validate_payload(raw_text):
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Model returned JSON, but not a JSON object")

    expected_keys = {"status", "thought", "action", "new_commands"}
    actual_keys = set(payload.keys())
    if actual_keys != expected_keys:
        raise ValueError(f"Unexpected JSON keys: expected {sorted(expected_keys)}, got {sorted(actual_keys)}")

    if payload.get("status") != "ok":
        raise ValueError("Field 'status' must be exactly 'ok'")

    thought = payload.get("thought")
    if not isinstance(thought, str) or not thought.strip():
        raise ValueError("Field 'thought' must be a non-empty string")
    if len(thought.strip()) > 80:
        raise ValueError("Field 'thought' must stay under 80 characters")

    action = payload.get("action")
    allowed_actions = {"look", "help", "north", "south", "east", "west", "up", "down"}
    if not isinstance(action, str) or action.strip().lower() not in allowed_actions:
        raise ValueError(f"Field 'action' must be one of {sorted(allowed_actions)}")

    new_commands = payload.get("new_commands")
    if not isinstance(new_commands, list):
        raise ValueError("Field 'new_commands' must be an array")
    if not all(isinstance(item, str) for item in new_commands):
        raise ValueError("Field 'new_commands' must contain only strings")

    return payload


def main():
    args = parse_args()
    try:
        raw_text = call_model_api(
            api_kind=args.api_kind,
            api_url=args.api_url,
            model=args.model,
            prompt=args.prompt,
            timeout=args.timeout,
            api_key=args.api_key,
        )
        payload = validate_payload(raw_text)
    except Exception as exc:
        print("MODEL SANITY CHECK FAILED", file=sys.stderr)
        print(f"api_kind={args.api_kind}", file=sys.stderr)
        print(f"api_url={args.api_url}", file=sys.stderr)
        print(f"model={args.model}", file=sys.stderr)
        print(f"error={exc}", file=sys.stderr)
        return 1

    print("MODEL SANITY CHECK PASSED")
    print(f"api_kind={args.api_kind}")
    print(f"api_url={args.api_url}")
    print(f"model={args.model}")
    print(f"action={payload['action']}")
    if args.print_response:
        print("response_json=")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())