import argparse
import fcntl
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler

from recovery_status import recovery_status_path, update_recovery_status


WORKSPACE_ROOT = Path(__file__).resolve().parent
WORKFLOW_SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_ARTIFACT_DIR = WORKSPACE_ROOT / "artifacts" / "current"


def runtime_artifact_path(filename):
    return str((RUNTIME_ARTIFACT_DIR / filename).resolve())


DEFAULT_MAP_MEMORY = os.getenv("WAGENT_MAP_MEMORY", "wagent_map_memory.json")
DEFAULT_SCANNER_OBSERVATION_MEMORY = os.getenv(
    "WAGENT_SCOUT_OBSERVATION_MEMORY",
    os.getenv("WAGENT_OBSERVATION_MEMORY", runtime_artifact_path("wagent_scanner_observation_memory.json")),
)
DEFAULT_SUMMARY_FILE = os.getenv("WAGENT_ORCHESTRATOR_SUMMARY", runtime_artifact_path("wagent_orchestrator_summary.json"))
DEFAULT_LOG_FILE = os.getenv("WAGENT_ORCHESTRATOR_LOG", runtime_artifact_path("wagent_orchestrator.log"))
DEFAULT_LOG_LEVEL = os.getenv("WAGENT_LOG_LEVEL", "INFO").strip().upper()
DEFAULT_MEMORY_POLL_INTERVAL = float(os.getenv("WAGENT_ORCHESTRATOR_MEMORY_POLL_INTERVAL", "2.0"))
DEFAULT_CHILD_LINE_PREVIEW = int(os.getenv("WAGENT_ORCHESTRATOR_CHILD_LINE_PREVIEW", "240"))

ROOM_RE = re.compile(r"当前房间签名:\s*(.+)$")
NO_PROGRESS_RE = re.compile(r"无进展轮数:\s*(\d+)")
ACTION_RE = re.compile(r"执行指令:\s*(.+)$")
IMPORTANT_CHILD_LINE_RE = re.compile(
    r"(当前房间签名|执行指令|无进展轮数|贪心进度分数|探索摘要|持久化捷径摘要|运行时异常|连接已断开|成功连接|^❌|^⚠️|^💥|^🧱|^📌|^🆕)",
)


def setup_logger():
    logger = logging.getLogger("Wagent.Orchestrator")
    logger.setLevel(getattr(logging, DEFAULT_LOG_LEVEL, logging.INFO))
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    os.makedirs(os.path.dirname(DEFAULT_LOG_FILE) or ".", exist_ok=True)
    max_bytes = max(0, int(os.getenv("WAGENT_LOG_MAX_BYTES", "524288")))
    backup_count = max(0, int(os.getenv("WAGENT_LOG_BACKUP_COUNT", "4")))
    if max_bytes > 0:
        file_handler = RotatingFileHandler(
            DEFAULT_LOG_FILE,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    else:
        file_handler = logging.FileHandler(DEFAULT_LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


logger = setup_logger()


def normalize_room_name(raw_room):
    return re.sub(r"\s+", " ", str(raw_room or "").strip().lower())


def normalize_action(raw_action):
    return re.sub(r"\s+", " ", str(raw_action or "").strip().lower())


def _memory_lock_path(path):
    return f"{path}.lock"


def _locked_json_load(path):
    lock_path = _memory_lock_path(path)
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


def _locked_json_dump(path, payload):
    lock_path = _memory_lock_path(path)
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


@dataclass
class MapSnapshot:
    rooms: set[str] = field(default_factory=set)
    edges: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass
class ObservationSnapshot:
    observed_exits: set[tuple[str, str]] = field(default_factory=set)
    scan_targets: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class BotSliceResult:
    role: str
    target_room: str
    reason: str
    elapsed_seconds: float
    actions: int
    last_room: str
    last_no_progress: int
    exit_code: int | None
    room_count_before: int
    room_count_after: int
    exit_count_before: int
    exit_count_after: int
    new_rooms: list[str] = field(default_factory=list)
    new_exits: list[str] = field(default_factory=list)
    new_observed_exits: list[str] = field(default_factory=list)
    new_scan_targets: list[str] = field(default_factory=list)


def load_map_snapshot(path):
    payload = _locked_json_load(path)
    snapshot = MapSnapshot()

    if isinstance(payload, dict) and isinstance(payload.get("rooms"), dict):
        for room_sig, room_data in payload["rooms"].items():
            room_name = normalize_room_name(room_sig)
            if not room_name or not isinstance(room_data, dict):
                continue
            snapshot.rooms.add(room_name)
            exits = room_data.get("exits", {})
            if isinstance(exits, dict):
                for exit_data in exits.values():
                    if not isinstance(exit_data, dict):
                        continue
                    action = normalize_action(exit_data.get("action", ""))
                    destination = normalize_room_name(exit_data.get("to", ""))
                    if action and destination:
                        snapshot.rooms.add(destination)
                        snapshot.edges.add((room_name, action, destination))

    elif isinstance(payload, dict) and isinstance(payload.get("edges"), dict):
        for edge in payload["edges"].values():
            if not isinstance(edge, dict):
                continue
            from_room = normalize_room_name(edge.get("from", ""))
            action = normalize_action(edge.get("action", ""))
            to_room = normalize_room_name(edge.get("to", ""))
            if from_room and action and to_room:
                snapshot.rooms.update({from_room, to_room})
                snapshot.edges.add((from_room, action, to_room))

    elif isinstance(payload, dict):
        for room_sig, room_data in payload.items():
            room_name = normalize_room_name(room_sig)
            if not room_name or not isinstance(room_data, dict):
                continue
            snapshot.rooms.add(room_name)
            success = room_data.get("success", {})
            if not isinstance(success, dict):
                continue
            for action, destination in success.items():
                clean_action = normalize_action(action)
                clean_destination = normalize_room_name(destination)
                if clean_action and clean_destination:
                    snapshot.rooms.add(clean_destination)
                    snapshot.edges.add((room_name, clean_action, clean_destination))

    return snapshot


def map_delta(before, after):
    new_rooms = sorted(after.rooms - before.rooms)
    new_exits = sorted(
        f"{from_room} --{action}--> {to_room}"
        for from_room, action, to_room in (after.edges - before.edges)
    )
    return new_rooms, new_exits


def load_observation_snapshot(path):
    payload = _locked_json_load(path)
    snapshot = ObservationSnapshot()
    if not isinstance(payload, dict):
        return snapshot

    rooms = payload.get("rooms", {})
    if not isinstance(rooms, dict):
        return snapshot

    for room_sig, room_data in rooms.items():
        clean_room = normalize_room_name(room_sig)
        if not clean_room or not isinstance(room_data, dict):
            continue

        for action in room_data.get("observed_exits", []):
            clean_action = normalize_action(action)
            if clean_action:
                snapshot.observed_exits.add((clean_room, clean_action))

        for target in room_data.get("scan_targets", []):
            clean_target = normalize_room_name(target)
            if clean_target:
                snapshot.scan_targets.add((clean_room, clean_target))

    return snapshot


def observation_delta(before, after):
    new_observed_exits = sorted(
        f"{room}::{action}"
        for room, action in (after.observed_exits - before.observed_exits)
    )
    new_scan_targets = sorted(
        f"{room}::{target}"
        for room, target in (after.scan_targets - before.scan_targets)
    )
    return new_observed_exits, new_scan_targets


def confirmed_action_pairs(snapshot):
    return {(from_room, action) for from_room, action, _ in snapshot.edges}


def filter_unconfirmed_observed_exits(map_snapshot, observed_exit_deltas):
    confirmed_pairs = confirmed_action_pairs(map_snapshot)
    filtered = []
    for item in observed_exit_deltas:
        room, _, action = str(item).partition("::")
        pair = (normalize_room_name(room), normalize_action(action))
        if pair in confirmed_pairs:
            continue
        filtered.append(item)
    return filtered


def filter_observation_items_for_target(items, target_room):
    clean_target = normalize_room_name(target_room)
    if not clean_target:
        return list(items)

    filtered = []
    for item in items:
        room, _, _ = str(item).partition("::")
        if normalize_room_name(room) == clean_target:
            filtered.append(item)
    return filtered


def choose_runner_confirmation_target(result, fallback_target):
    for item in list(result.new_observed_exits) + list(result.new_scan_targets):
        room, _, _ = str(item).partition("::")
        clean_room = normalize_room_name(room)
        if clean_room:
            return clean_room
    if result.last_room and result.last_room != "unknown-room":
        return result.last_room
    return choose_scanner_target(result, fallback_target)


def terminate_process(process, role):
    if process.poll() is not None:
        return process.returncode

    logger.info(f"⏹️ Stopping {role} process")
    process.terminate()
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning(f"⚠️ {role} did not exit after terminate; killing it")
        process.kill()
        return process.wait(timeout=5)


def parse_bot_line(line, state):
    room_match = ROOM_RE.search(line)
    if room_match:
        next_room = normalize_room_name(room_match.group(1))
        if next_room and next_room != state.get("last_room", ""):
            state["stuck_candidate_since"] = None
        state["last_room"] = next_room

    no_progress_match = NO_PROGRESS_RE.search(line)
    if no_progress_match:
        state["last_no_progress"] = int(no_progress_match.group(1))

    action_match = ACTION_RE.search(line)
    if action_match:
        state["actions"] += 1


def should_relay_child_line(text):
    if logger.isEnabledFor(logging.DEBUG):
        return True
    return bool(IMPORTANT_CHILD_LINE_RE.search(text or ""))


def default_child_log_file(role):
    log_path = Path(DEFAULT_LOG_FILE).resolve()
    stem = log_path.stem
    if stem.endswith("_orchestrator"):
        stem = stem[: -len("_orchestrator")]
    return str((log_path.parent / f"{stem}_{role}.log").resolve())


def format_child_line(text):
    clean = str(text or "").rstrip("\n")
    if logger.isEnabledFor(logging.DEBUG):
        return clean
    preview_limit = max(80, DEFAULT_CHILD_LINE_PREVIEW)
    if len(clean) <= preview_limit:
        return clean
    return f"{clean[:preview_limit]}...[truncated {len(clean) - preview_limit} chars]"


def read_remaining_output(process, role, state):
    if not process.stdout:
        return
    try:
        remainder = process.stdout.read() or ""
    except Exception:
        return
    for raw_line in remainder.splitlines():
        if should_relay_child_line(raw_line):
            logger.info(f"[{role}] {format_child_line(raw_line)}")
        parse_bot_line(raw_line, state)


def run_bot_slice(
    role,
    target_room,
    max_seconds,
    max_actions,
    stuck_turns,
    map_memory_path,
    observation_memory_path,
    stop_on_new_memory=False,
    stop_on_target=None,
):
    script_path = WORKFLOW_SCRIPT_DIR / f"{role}.py"
    env = os.environ.copy()
    env["WAGENT_MAP_MEMORY"] = str(Path(map_memory_path).resolve())
    experience_path = env.get("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
    env["WAGENT_EXPERIENCE_MEMORY"] = str(Path(experience_path).resolve())
    if "WAGENT_BOT_ID" not in env:
        env["WAGENT_BOT_ID"] = f"orchestrator-{role}"
    if "WAGENT_ACCOUNT_LABEL" not in env and "WAGENT_ACCOUNT_SLOT" not in env:
        env["WAGENT_ACCOUNT_SLOT"] = "0" if role == "runner" else "1"
    if target_room:
        env["WAGENT_TARGET_ROOM"] = target_room
    else:
        env.pop("WAGENT_TARGET_ROOM", None)
    env.setdefault("WAGENT_LOG_FILE", default_child_log_file(role))
    if stop_on_target is not None:
        env["WAGENT_STOP_ON_TARGET"] = "1" if stop_on_target else "0"
    if observation_memory_path:
        resolved_observation_path = str(Path(observation_memory_path).resolve())
        env["WAGENT_SCOUT_OBSERVATION_MEMORY"] = resolved_observation_path
        if role == "scanner":
            env["WAGENT_OBSERVATION_MEMORY"] = resolved_observation_path

    command = [sys.executable, str(script_path)]
    if target_room:
        command.extend(["--target-room", target_room])

    before_snapshot = load_map_snapshot(map_memory_path)
    before_observation = load_observation_snapshot(observation_memory_path)
    logger.info(
        f"▶️ Starting {role} slice target={target_room or 'none'} max_seconds={max_seconds} max_actions={max_actions} stuck_turns={stuck_turns}"
    )
    logger.info(f"🧾 Raw {role} log: {env['WAGENT_LOG_FILE']}")

    process = subprocess.Popen(
        command,
        cwd=str(WORKSPACE_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    state = {
        "actions": 0,
        "last_room": "",
        "last_no_progress": 0,
        "stuck_candidate_since": None,
    }
    reason = ""
    start_ts = time.monotonic()
    next_memory_check_ts = start_ts
    selector = selectors.DefaultSelector()
    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ)

    try:
        while True:
            if process.poll() is not None:
                break

            elapsed = time.monotonic() - start_ts
            if elapsed >= max_seconds:
                reason = "slice_timeout"
                break

            if stop_on_new_memory and time.monotonic() >= next_memory_check_ts:
                current_snapshot = load_map_snapshot(map_memory_path)
                new_rooms, new_exits = map_delta(before_snapshot, current_snapshot)
                current_observation = load_observation_snapshot(observation_memory_path)
                new_observed_exits, new_scan_targets = observation_delta(before_observation, current_observation)
                raw_new_observed_exits = list(new_observed_exits)
                new_observed_exits = filter_unconfirmed_observed_exits(current_snapshot, new_observed_exits)
                new_observed_exits = filter_observation_items_for_target(new_observed_exits, target_room)
                new_scan_targets = filter_observation_items_for_target(new_scan_targets, target_room)
                if raw_new_observed_exits and not new_observed_exits:
                    logger.debug(
                        "Ignoring scanner observation deltas already confirmed in shared map: %s",
                        ", ".join(raw_new_observed_exits[:8]),
                    )
                logger.debug(
                    "Scanner poll delta rooms=%s exits=%s observed_exits=%s scan_targets=%s",
                    len(new_rooms),
                    len(new_exits),
                    len(new_observed_exits),
                    len(new_scan_targets),
                )
                if new_rooms or new_exits or new_observed_exits or new_scan_targets:
                    reason = "found_new_memory" if (new_rooms or new_exits) else "found_new_observation"
                    break
                next_memory_check_ts = time.monotonic() + max(0.5, DEFAULT_MEMORY_POLL_INTERVAL)

            events = selector.select(timeout=0.5)
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    continue
                text = line.rstrip("\n")
                if should_relay_child_line(text):
                    logger.info(f"[{role}] {format_child_line(text)}")
                parse_bot_line(text, state)

                if state["actions"] >= max_actions:
                    reason = "slice_action_limit"
                    break

                if state["actions"] >= 4 and state["last_no_progress"] >= stuck_turns:
                    if state["stuck_candidate_since"] is None:
                        state["stuck_candidate_since"] = time.monotonic()
                    elif (time.monotonic() - state["stuck_candidate_since"]) >= 1.0:
                        reason = "stuck"
                        break
                else:
                    state["stuck_candidate_since"] = None

            if reason:
                break
    finally:
        if selector.get_map():
            selector.close()

    exit_code = process.poll()
    if reason and exit_code is None:
        exit_code = terminate_process(process, role)
    elif exit_code is None:
        exit_code = process.wait(timeout=5)

    read_remaining_output(process, role, state)

    after_snapshot = load_map_snapshot(map_memory_path)
    new_rooms, new_exits = map_delta(before_snapshot, after_snapshot)
    after_observation = load_observation_snapshot(observation_memory_path)
    new_observed_exits, new_scan_targets = observation_delta(before_observation, after_observation)
    new_observed_exits = filter_unconfirmed_observed_exits(after_snapshot, new_observed_exits)
    new_observed_exits = filter_observation_items_for_target(new_observed_exits, target_room)
    new_scan_targets = filter_observation_items_for_target(new_scan_targets, target_room)
    elapsed_seconds = round(time.monotonic() - start_ts, 2)

    if not reason:
        reason = "exited" if exit_code == 0 else "process_error"

    return BotSliceResult(
        role=role,
        target_room=target_room,
        reason=reason,
        elapsed_seconds=elapsed_seconds,
        actions=state["actions"],
        last_room=state["last_room"],
        last_no_progress=state["last_no_progress"],
        exit_code=exit_code,
        room_count_before=len(before_snapshot.rooms),
        room_count_after=len(after_snapshot.rooms),
        exit_count_before=len(before_snapshot.edges),
        exit_count_after=len(after_snapshot.edges),
        new_rooms=new_rooms,
        new_exits=new_exits,
        new_observed_exits=new_observed_exits,
        new_scan_targets=new_scan_targets,
    )


def result_to_payload(result):
    payload = asdict(result)
    payload["target_room"] = result.target_room or ""
    payload["last_room"] = result.last_room or ""
    return payload


def log_slice_summary(result):
    logger.info(
        "📌 Slice summary role=%s reason=%s actions=%s room=%s no_progress=%s new_rooms=%s new_exits=%s new_observed_exits=%s new_scan_targets=%s",
        result.role,
        result.reason,
        result.actions,
        result.last_room or "unknown",
        result.last_no_progress,
        len(result.new_rooms),
        len(result.new_exits),
        len(result.new_observed_exits),
        len(result.new_scan_targets),
    )
    if result.new_rooms:
        logger.info(f"🆕 New rooms: {', '.join(result.new_rooms[:8])}")
    if result.new_exits:
        logger.info(f"🆕 New exits: {', '.join(result.new_exits[:8])}")
    if result.new_observed_exits:
        logger.info(f"🆕 New observed exits: {', '.join(result.new_observed_exits[:8])}")
    if result.new_scan_targets:
        logger.info(f"🆕 New scan targets: {', '.join(result.new_scan_targets[:8])}")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Slice detail payload: %s", result_to_payload(result))


def choose_scanner_target(previous_result, fallback_target):
    if previous_result.target_room and previous_result.target_room != previous_result.last_room:
        return previous_result.target_room
    if previous_result.last_room and previous_result.last_room != "unknown-room":
        return previous_result.last_room
    return fallback_target


def should_handoff_runner_target(result):
    return (
        result.role == "runner"
        and result.exit_code == 0
        and result.reason == "exited"
        and bool(result.target_room)
        and result.last_room == result.target_room
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Alternate runner and scanner bots using shared memory.")
    parser.add_argument("--map-memory", default=DEFAULT_MAP_MEMORY, help="Shared map memory JSON path.")
    parser.add_argument(
        "--scanner-observation-memory",
        default=DEFAULT_SCANNER_OBSERVATION_MEMORY,
        help="Scanner observation-memory JSON path used for discovery handoff.",
    )
    parser.add_argument("--summary-file", default=DEFAULT_SUMMARY_FILE, help="Where to write the orchestration summary JSON.")
    parser.add_argument("--max-phases", type=int, default=8, help="Maximum number of runner/scanner slices to execute.")
    parser.add_argument("--runner-seconds", type=int, default=180, help="Maximum seconds for one runner slice.")
    parser.add_argument("--scanner-seconds", type=int, default=120, help="Maximum seconds for one scanner slice.")
    parser.add_argument("--runner-max-actions", type=int, default=40, help="Maximum actions for one runner slice.")
    parser.add_argument("--scanner-max-actions", type=int, default=18, help="Maximum actions for one scanner slice.")
    parser.add_argument("--runner-stuck-turns", type=int, default=5, help="No-progress threshold that counts runner as stuck.")
    parser.add_argument("--scanner-stuck-turns", type=int, default=6, help="No-progress threshold that counts scanner as stuck.")
    parser.add_argument("--runner-target-room", default="", help="Optional starting target room for runner.")
    parser.add_argument("--scanner-target-room", default="", help="Fallback target room for scanner when runner has no last room.")
    return parser.parse_args(argv)


def orchestrate(args):
    map_memory_path = str(Path(args.map_memory))
    scanner_observation_memory = str(Path(args.scanner_observation_memory))
    summary_file = str(Path(args.summary_file))
    runner_target = normalize_room_name(args.runner_target_room)
    scanner_fallback = normalize_room_name(args.scanner_target_room)
    slice_results = []
    next_role = "runner"
    next_scanner_target = scanner_fallback
    next_runner_stop_on_target = True

    update_recovery_status(
        "orchestrator",
        state="running",
        script=str(Path(__file__).resolve()),
        role="orchestrator",
        task_kind="dual-bot frontier orchestration",
        runner_target_room=runner_target,
        scanner_target_room=next_scanner_target,
        scanner_observation_memory=scanner_observation_memory,
        summary_file=summary_file,
        log_file=str(Path(DEFAULT_LOG_FILE).resolve()),
        max_phases=args.max_phases,
        started_at=int(time.time()),
    )

    for phase_index in range(1, args.max_phases + 1):
        logger.info(f"=== Phase {phase_index}/{args.max_phases}: {next_role} ===")
        update_recovery_status(
            "orchestrator",
            state="running",
            current_phase=phase_index,
            current_role=next_role,
            runner_target_room=runner_target,
            scanner_target_room=next_scanner_target,
        )

        if next_role == "runner":
            result = run_bot_slice(
                role="runner",
                target_room=runner_target,
                max_seconds=args.runner_seconds,
                max_actions=args.runner_max_actions,
                stuck_turns=args.runner_stuck_turns,
                map_memory_path=map_memory_path,
                observation_memory_path=scanner_observation_memory,
                stop_on_new_memory=False,
                stop_on_target=next_runner_stop_on_target,
            )
            slice_results.append(result)
            log_slice_summary(result)
            update_recovery_status(
                "orchestrator",
                state="running",
                last_slice=result_to_payload(result),
                current_phase=phase_index,
                current_role="runner",
            )
            next_runner_stop_on_target = True

            if result.reason == "stuck":
                next_role = "scanner"
                next_scanner_target = choose_scanner_target(result, scanner_fallback)
                logger.info(f"🔄 Runner stuck; handing off to scanner target={next_scanner_target or 'none'}")
                continue

            if should_handoff_runner_target(result):
                next_role = "scanner"
                next_scanner_target = choose_scanner_target(result, scanner_fallback)
                logger.info(
                    f"🎯 Runner reached target cleanly; handing off to scanner target={next_scanner_target or 'none'}"
                )
                continue

            if result.reason in {"slice_action_limit", "slice_timeout"}:
                logger.info("➡️ Runner slice ended without a stuck signal; continuing runner.")
                continue

            logger.info("⏹️ Runner exited unexpectedly or finished; stopping orchestration.")
            break

        result = run_bot_slice(
            role="scanner",
            target_room=next_scanner_target,
            max_seconds=args.scanner_seconds,
            max_actions=args.scanner_max_actions,
            stuck_turns=args.scanner_stuck_turns,
            map_memory_path=map_memory_path,
            observation_memory_path=scanner_observation_memory,
            stop_on_new_memory=True,
        )
        slice_results.append(result)
        log_slice_summary(result)
        update_recovery_status(
            "orchestrator",
            state="running",
            last_slice=result_to_payload(result),
            current_phase=phase_index,
            current_role="scanner",
        )

        if (
            result.new_rooms
            or result.new_exits
            or result.new_observed_exits
            or result.new_scan_targets
            or result.reason in {"found_new_memory", "found_new_observation"}
        ):
            runner_target = choose_runner_confirmation_target(result, scanner_fallback)
            next_runner_stop_on_target = False
            logger.info(
                f"🔓 Scanner reported new findings; switching back to runner target={runner_target or 'none'} confirm_mode=1"
            )
            logger.debug("Scanner handoff reason=%s payload=%s", result.reason, result_to_payload(result))
            next_role = "runner"
            continue

        logger.info("⏹️ Scanner did not unlock anything new; stopping orchestration.")
        break

    summary_payload = {
        "ts": int(time.time()),
        "map_memory": map_memory_path,
        "phases": [result_to_payload(result) for result in slice_results],
    }
    _locked_json_dump(summary_file, summary_payload)
    logger.info(f"📝 Wrote orchestration summary: {summary_file}")
    update_recovery_status(
        "orchestrator",
        state="completed",
        current_phase=len(slice_results),
        current_role="none",
        summary_file=summary_file,
        last_slice=result_to_payload(slice_results[-1]) if slice_results else {},
        last_summary_ts=summary_payload["ts"],
        phase_count=len(slice_results),
    )
    return summary_payload


if __name__ == "__main__":
    cli_args = parse_args()
    logger.info("=== Wagent dual-bot orchestrator start ===")
    logger.info(f"🗺️ Shared map memory: {cli_args.map_memory}")
    logger.info(f"📄 Summary file: {cli_args.summary_file}")
    logger.info(f"🧭 Recovery status file: {recovery_status_path()}")
    try:
        orchestrate(cli_args)
    except KeyboardInterrupt:
        update_recovery_status(
            "orchestrator",
            state="interrupted",
            interruption="keyboard",
            current_role="none",
        )
        raise
    except Exception as exc:
        update_recovery_status(
            "orchestrator",
            state="error",
            current_role="none",
            last_error=str(exc),
        )
        raise
    logger.info("=== Wagent dual-bot orchestrator end ===")
