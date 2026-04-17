import argparse
import fcntl
import telnetlib
import time
import json
import requests
import logging
import sys
import re
import os
import difflib
import random
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from collections import deque

from model_api import (
    call_model_api,
    configured_model_api_key,
    configured_model_api_kind,
    configured_model_api_url,
    configured_model_fallbacks,
    configured_model_name,
)
from recovery_status import recovery_status_path, update_recovery_status

# --- 辅助函数：清理 ANSI 颜色/格式代码 ---
def strip_ansi(text):
    """移除MUD输出中的ANSI转义符，避免干扰文本解析"""
    ansi_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_pattern.sub('', text)

def normalize_agent_role(raw_role):
    role = str(raw_role or "general").strip().lower()
    return role if role in {"general", "scanner", "runner"} else "general"


def normalize_room_name(raw_room):
    return re.sub(r'\s+', ' ', str(raw_room or '').strip().lower())


def normalize_scan_target(raw_target):
    return re.sub(r'\s+', ' ', str(raw_target or '').strip().lower())


def normalize_bot_id(raw_bot_id):
    clean = re.sub(r'[^a-z0-9._-]+', '-', str(raw_bot_id or '').strip().lower())
    return clean.strip('-._')


def normalize_scanner_mode(raw_mode):
    mode = str(raw_mode or "targeted").strip().lower()
    return mode if mode in {"targeted", "random"} else "targeted"


def normalize_scanner_style(raw_style):
    style = str(raw_style or "default").strip().lower()
    return style if style in {"default", "nutcracker", "wellcracker", "rootcracker"} else "default"


def normalize_room_action_rules(raw_rules):
    rules = {}
    for chunk in str(raw_rules or "").split(";"):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        room_part, actions_part = item.split(":", 1)
        room_sig = normalize_room_name(room_part)
        if not room_sig:
            continue
        bucket = rules.setdefault(room_sig, [])
        for action in actions_part.split(","):
            clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
            if not clean_action:
                continue
            if clean_action not in bucket:
                bucket.append(clean_action)
    return rules


def normalize_room_name_list(raw_values):
    rooms = []
    for chunk in str(raw_values or "").split(","):
        clean_room = normalize_room_name(chunk)
        if clean_room and clean_room not in rooms:
            rooms.append(clean_room)
    return rooms


def normalize_account_pool(raw_pool):
    accounts = []
    for chunk in str(raw_pool or "").split(";"):
        item = chunk.strip()
        if not item:
            continue
        name_part = ""
        creds_part = item
        if "=" in item:
            name_part, creds_part = item.split("=", 1)
        if ":" not in creds_part:
            continue
        user_part, pass_part = creds_part.split(":", 1)
        user = str(user_part).strip()
        password = str(pass_part)
        if not user or not password:
            continue
        label = normalize_bot_id(name_part) or normalize_bot_id(user) or user.strip().lower()
        accounts.append({"label": label, "user": user, "password": password})
    return accounts


def load_account_pool_from_file(path):
    clean_path = str(path or "").strip()
    if not clean_path or not os.path.exists(clean_path):
        return []
    try:
        with open(clean_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return []

    raw_accounts = payload.get("accounts", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_accounts, list):
        return []

    accounts = []
    for item in raw_accounts:
        if not isinstance(item, dict):
            continue
        user = str(item.get("user", "")).strip()
        password = str(item.get("password", ""))
        if not user or not password:
            continue
        label = normalize_bot_id(item.get("label", "")) or normalize_bot_id(user) or user.lower()
        accounts.append({"label": label, "user": user, "password": password})
    return accounts


def configured_account_pool_file():
    workspace_root = Path(__file__).resolve().parent
    archive_dir = workspace_root / "artifacts" / "archive" / "account-pools"
    raw_path = str(os.getenv("WAGENT_ACCOUNT_POOL_FILE", "")).strip()
    requested_paths = [raw_path] if raw_path else list(DEFAULT_ACCOUNT_POOL_FILES)

    candidates = []
    for requested_path in requested_paths:
        candidate = Path(requested_path).expanduser()
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.append((workspace_root / candidate).resolve())
            candidates.append((archive_dir / candidate.name).resolve())

    for resolved in candidates:
        if resolved.exists():
            return str(resolved)

    return str(candidates[0]) if candidates else ""


def has_explicit_account_pool_source():
    return any(
        str(os.getenv(name, "")).strip()
        for name in ("WAGENT_ACCOUNT_POOL", "WAGENT_ACCOUNT_POOL_FILE")
    )


WORKSPACE_ROOT = Path(__file__).resolve().parent
RUNTIME_ARTIFACT_DIR = WORKSPACE_ROOT / "artifacts" / "current"
DEFAULT_ACCOUNT_POOL_FILES = ("wagent_account_pool.local.json", "wagent_account_pool.json")


def _stable_account_pool_index(selector, pool_size):
    if pool_size <= 0:
        return 0
    clean_selector = normalize_bot_id(selector) or "default-bot"
    return sum(ord(ch) for ch in clean_selector) % pool_size


def select_evennia_credentials():
    explicit_user = str(os.getenv("EVENNIA_USER", "")).strip()
    explicit_pass = os.getenv("EVENNIA_PASS")
    if explicit_user and explicit_pass:
        return explicit_user, explicit_pass, normalize_bot_id(explicit_user) or explicit_user.lower(), "explicit-env"

    explicit_pool_source = has_explicit_account_pool_source()
    pool = normalize_account_pool(os.getenv("WAGENT_ACCOUNT_POOL", ""))
    pool_source = "pool-env"
    if not pool:
        pool_file = configured_account_pool_file()
        pool = load_account_pool_from_file(pool_file)
        if pool:
            pool_source = f"pool-file:{os.path.basename(pool_file)}"
    if not pool:
        if explicit_pool_source:
            requested_file = str(os.getenv("WAGENT_ACCOUNT_POOL_FILE", "")).strip()
            if requested_file:
                raise RuntimeError(
                    f"Configured account pool file did not load any accounts: requested={requested_file!r} resolved={pool_file!r}"
                )
            raise RuntimeError("Explicit WAGENT_ACCOUNT_POOL was provided but did not load any valid accounts.")
        raise RuntimeError(
            "No Evennia credentials configured. Set EVENNIA_USER and EVENNIA_PASS, or create a local account pool file such as wagent_account_pool.local.json."
        )

    requested_label = normalize_bot_id(os.getenv("WAGENT_ACCOUNT_LABEL", ""))
    if requested_label:
        for account in pool:
            if account.get("label") == requested_label:
                return account["user"], account["password"], account["label"], f"{pool_source}-label"

    slot_raw = str(os.getenv("WAGENT_ACCOUNT_SLOT", "")).strip()
    if slot_raw:
        try:
            slot_index = int(slot_raw)
        except ValueError:
            slot_index = None
        if slot_index is not None:
            account = pool[slot_index % len(pool)]
            return account["user"], account["password"], account["label"], f"{pool_source}-slot"

    selector = os.getenv("WAGENT_BOT_ID", "") or default_bot_id()
    account = pool[_stable_account_pool_index(selector, len(pool))]
    return account["user"], account["password"], account["label"], f"{pool_source}-hash"


DEFAULT_AGENT_ROLE = normalize_agent_role(os.getenv("WAGENT_AGENT_ROLE", "scanner"))


def role_default_search_strategy(role):
    return "bfs" if role == "runner" else "dfs"


def runtime_artifact_path(filename):
    return str((RUNTIME_ARTIFACT_DIR / filename).resolve())


def role_default_filename(kind, role=None):
    active_role = normalize_agent_role(role or DEFAULT_AGENT_ROLE)
    if kind == "run_memory":
        filename = {
            "scanner": "wagent_scanner_run_memory.json",
            "runner": "wagent_runner_run_memory.json",
        }.get(active_role, "wagent_run_memory.json")
        return runtime_artifact_path(filename)
    if kind == "observation_memory":
        filename = {
            "scanner": "wagent_scanner_observation_memory.json",
            "runner": "wagent_runner_observation_memory.json",
        }.get(active_role, "wagent_observation_memory.json")
        return runtime_artifact_path(filename)
    if kind == "prompt_log":
        filename = {
            "scanner": "wagent_scanner_prompt_debug.log",
            "runner": "wagent_runner_prompt_debug.log",
        }.get(active_role, "wagent_prompt_debug.log")
        return runtime_artifact_path(filename)
    if kind == "log":
        filename = {
            "scanner": "wagent_scanner.log",
            "runner": "wagent_runner.log",
        }.get(active_role, "wagent.log")
        return runtime_artifact_path(filename)
    return runtime_artifact_path("wagent.log")


def default_bot_id():
    source = os.getenv("WAGENT_LOG_FILE", role_default_filename("log"))
    base = os.path.splitext(os.path.basename(source))[0]
    return normalize_bot_id(base) or f"{DEFAULT_AGENT_ROLE}-bot"


def default_observation_memory_file():
    log_path = os.getenv("WAGENT_LOG_FILE", role_default_filename("log"))
    base, ext = os.path.splitext(log_path)
    if ext.lower() == ".log":
        return f"{base}_observation_memory.json"
    return f"{log_path}_observation_memory.json"


SELECTED_EVENNIA_USER, SELECTED_EVENNIA_PASS, SELECTED_ACCOUNT_LABEL, SELECTED_ACCOUNT_SOURCE = select_evennia_credentials()


# --- 日志配置：完整记录 + 结构化输出 ---
def setup_logger():
    logger = logging.getLogger(f"Wagent.{DEFAULT_AGENT_ROLE}")
    logger.setLevel(getattr(logging, os.getenv("WAGENT_LOG_LEVEL", "INFO").strip().upper(), logging.INFO))
    # 避免重复添加处理器
    if logger.handlers:
        return logger
    
    # 1. 文件处理器（UTF-8编码，追加模式）
    log_path = os.getenv("WAGENT_LOG_FILE", role_default_filename("log"))
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    max_bytes = max(0, int(os.getenv("WAGENT_LOG_MAX_BYTES", "524288")))
    backup_count = max(0, int(os.getenv("WAGENT_LOG_BACKUP_COUNT", "4")))
    if max_bytes > 0:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
    else:
        file_handler = logging.FileHandler(
            log_path,
            mode='a',
            encoding='utf-8'
        )
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    
    # 2. 控制台处理器（实时输出）
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(file_formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger

# 初始化日志
logger = setup_logger()


def env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_path_list(raw_value):
    if raw_value is None:
        return []
    paths = []
    for chunk in re.split(r"[;,\n]", str(raw_value)):
        clean_path = chunk.strip()
        if clean_path and clean_path not in paths:
            paths.append(clean_path)
    return paths

# --- 核心配置项（可根据环境调整） ---
CONFIG = {
    "AGENT_ROLE": DEFAULT_AGENT_ROLE,
    "BOT_ID": normalize_bot_id(os.getenv("WAGENT_BOT_ID", "")) or default_bot_id(),
    "TARGET_ROOM": normalize_room_name(os.getenv("WAGENT_TARGET_ROOM", "")),
    "STOP_ON_TARGET": env_flag("WAGENT_STOP_ON_TARGET", False),
    "SCAN_TARGET": normalize_scan_target(os.getenv("WAGENT_SCAN_TARGET", "")),
    "SCANNER_MODE": normalize_scanner_mode(os.getenv("WAGENT_SCANNER_MODE", "targeted")),
    "SCANNER_STYLE": normalize_scanner_style(os.getenv("WAGENT_SCANNER_STYLE", "default")),
    "HOST": os.getenv("EVENNIA_HOST", "127.0.0.1"),
    "PORT": int(os.getenv("EVENNIA_PORT", "4000")),
    "USER": SELECTED_EVENNIA_USER,
    "PASS": SELECTED_EVENNIA_PASS,
    "ACCOUNT_LABEL": SELECTED_ACCOUNT_LABEL,
    "ACCOUNT_SOURCE": SELECTED_ACCOUNT_SOURCE,
    "ACCOUNT_POOL_FILE": configured_account_pool_file(),
    "MODEL": configured_model_name("qwen2.5:7b"),
    "MODEL_FALLBACKS": configured_model_fallbacks("qwen2.5:3b,llama3.2:3b"),
    "MODEL_API_KIND": configured_model_api_kind("ollama"),
    "MODEL_API_URL": configured_model_api_url("http://localhost:11434/api/generate"),
    "MODEL_API_KEY": configured_model_api_key(),
    "RECONNECT_MAX_RETRY": 3,  # 最大重连次数
    "HISTORY_MAX_LEN": 8,      # 最大记忆长度
    "ACTION_MAX_LEN": 30,      # 指令最大长度
    "SLEEP_INTERVAL": 3,       # 动作执行间隔（秒）
    "READ_WINDOW": 0.8,        # 每轮读取窗口（秒）
    "READ_POLL_INTERVAL": 0.08,
    "RECENT_ACTIONS_MAX_LEN": 6,
    "MODEL_FAIL_THRESHOLD": 3,
    "MODEL_COOLDOWN_TURNS": 5,
    "MODEL_SWITCH_FAIL_STREAK": 2,
    "PURE_MODEL_MODE": env_flag("WAGENT_PURE_MODEL_MODE", True),
    "MODEL_REFLECT_ENABLED": env_flag("WAGENT_MODEL_REFLECT_ENABLED", True),
    "MODEL_REFLECT_EVERY": int(os.getenv("WAGENT_MODEL_REFLECT_EVERY", "1")),
    "STUCK_HELP_THRESHOLD": int(os.getenv("WAGENT_STUCK_HELP_THRESHOLD", "4")),
    "MAP_MEMORY_FILE": os.getenv("WAGENT_MAP_MEMORY", "wagent_map_memory.json"),
    "MAP_MEMORY_OVERLAY_FILES": normalize_path_list(os.getenv("WAGENT_MAP_MEMORY_OVERLAYS", "")),
    "EXPERIENCE_MEMORY_FILE": os.getenv("WAGENT_EXPERIENCE_MEMORY", "wagent_experience_memory.json"),
    "ROUTE_MEMORY_FILE": os.getenv("WAGENT_ROUTE_MEMORY", "wagent_route_memory.json"),
    "RUN_MEMORY_FILE": os.getenv("WAGENT_RUN_MEMORY", role_default_filename("run_memory")),
    "OBSERVATION_MEMORY_FILE": os.getenv("WAGENT_OBSERVATION_MEMORY", default_observation_memory_file()),
    "RUN_MEMORY_MAX_RUNS": int(os.getenv("WAGENT_RUN_MEMORY_MAX_RUNS", "80")),
    "OBSERVATION_EVENT_MAX": int(os.getenv("WAGENT_OBSERVATION_EVENT_MAX", "240")),
    "OBSERVATION_RUN_MAX": int(os.getenv("WAGENT_OBSERVATION_RUN_MAX", "40")),
    "OBSERVATION_PREVIEW_MAX": int(os.getenv("WAGENT_OBSERVATION_PREVIEW_MAX", "280")),
    "RUN_MEMORY_PROMPT_LIMIT": int(os.getenv("WAGENT_RUN_MEMORY_PROMPT_LIMIT", "4")),
    "MAX_EXPERIENCE_COMMANDS": int(os.getenv("WAGENT_MAX_EXPERIENCE_COMMANDS", "120")),
    "MEMORY_BAD_CMD_MIN_ATTEMPTS": int(os.getenv("WAGENT_MEMORY_BAD_CMD_MIN_ATTEMPTS", "4")),
    "MEMORY_BAD_CMD_MAX_SUCCESS": int(os.getenv("WAGENT_MEMORY_BAD_CMD_MAX_SUCCESS", "0")),
    "SUMMARY_EVERY_STEPS": int(os.getenv("WAGENT_SUMMARY_EVERY", "8")),
    "STATE_SAVE_MIN_INTERVAL": float(os.getenv("WAGENT_STATE_SAVE_MIN_INTERVAL", "15")),
    "LOG_ENV_PREVIEW_MAX_CHARS": int(os.getenv("WAGENT_LOG_ENV_PREVIEW_MAX_CHARS", "1200")),
    "NO_CHANGE_SIMILARITY": float(os.getenv("WAGENT_NO_CHANGE_SIMILARITY", "0.97")),
    "NO_CHANGE_REPEAT_LIMIT": int(os.getenv("WAGENT_NO_CHANGE_REPEAT_LIMIT", "2")),
    "PROMPT_DEBUG_ENABLED": env_flag("WAGENT_LOG_PROMPT", False),
    "PROMPT_DEBUG_FILE": os.getenv("WAGENT_PROMPT_LOG", role_default_filename("prompt_log")),
    "SYNTH_VARIANTS_PER_TURN": int(os.getenv("WAGENT_SYNTH_VARIANTS_PER_TURN", "12")),
    "SEARCH_STRATEGY": os.getenv("WAGENT_SEARCH_STRATEGY", role_default_search_strategy(DEFAULT_AGENT_ROLE)).strip().lower(),
    "SEARCH_STACK_MAX": int(os.getenv("WAGENT_SEARCH_STACK_MAX", "240")),
    "PROMPT_STACK_MAX_DEPTH": int(os.getenv("WAGENT_PROMPT_STACK_MAX_DEPTH", "10")),
    "PRIORITY_ROOM_ACTIONS": normalize_room_action_rules(os.getenv("WAGENT_PRIORITY_ROOM_ACTIONS", "")),
    "UNSTABLE_RETRY_RULES": normalize_room_action_rules(os.getenv("WAGENT_UNSTABLE_RETRY_RULES", "the old bridge:east")),
    "BLIND_TRANSIT_ENABLED": env_flag("WAGENT_BLIND_TRANSIT_ENABLED", True),
    "ALLOW_SHARED_MEMORY_PROMOTION": env_flag("WAGENT_ALLOW_SHARED_MEMORY_PROMOTION", False),
    "RUNNER_TRAP_DESTINATIONS": normalize_room_name_list(os.getenv("WAGENT_RUNNER_TRAP_DESTINATIONS", "dark cell")),
    "OBJECTIVE_KEYWORDS": [
        k.strip().lower()
        for k in os.getenv("WAGENT_OBJECTIVE", "temple,ruin,obelisk,inner wall").split(",")
        if k.strip()
    ]
}

CRITICAL_ACTIONS = set()
STARTUP_ACTIONS = {
    "begin adventure", "begin", "start", "old bridge", "exit tutorial", "exit", "start again"
}
SYSTEM_COMMAND_BLOCKLIST = {
    "channel", "page", "access", "charcreate", "chardelete", "color",
    "ic", "intro", "nick", "ooc", "option", "password", "quell",
    "quit", "sessions", "setdesc", "style", "who", "about", "time",
    "tutorialworld", "tutorial-world", "auto-quell", "auto-quelling",
    "client-settings", "client settings", "settings", "give up"
}
LOW_VALUE_COMMANDS = {
    "inventory", "pose", "say", "whisper", "about", "time", "give",
    "give up", "tutorial", "intro", "exits", "look inventory", "look tutorial", "look exit", "look intro"
}

DARK_CELL_ROOT_ACTIONS = [
    "shift blue left",
    "shift blue right",
    "shift red left",
    "shift red right",
    "shift yellow up",
    "shift yellow down",
    "shift green up",
    "shift green down",
]

DARK_CELL_SOLVED_RECIPE = [
    "shift blue left",
    "shift red right",
    "shift green up",
    "shift yellow down",
    "press button",
    "root-covered wall",
]


def parse_runtime_args(argv=None):
    parser = argparse.ArgumentParser(description=f"Wagent {CONFIG['AGENT_ROLE']} bot")
    parser.add_argument(
        "--target-room",
        default=None,
        help="Route toward this normalized room signature before resuming the role objective.",
    )
    parser.add_argument(
        "--scan-target",
        default=None,
        help="Prioritize scanning this room-local object or feature once the scanner is in the target room.",
    )
    parser.add_argument(
        "--scanner-mode",
        choices=["targeted", "random"],
        default=None,
        help="Choose between targeted routing scanner and random roaming scanner behavior.",
    )
    parser.add_argument(
        "--scanner-style",
        choices=["default", "nutcracker", "wellcracker", "rootcracker"],
        default=None,
        help="Choose the local scan style used once the scanner reaches its working room.",
    )
    parser.add_argument(
        "--search-strategy",
        choices=["dfs", "bfs"],
        default=None,
        help="Override the DFS/BFS search memory strategy for this run.",
    )
    args, unknown = parser.parse_known_args(argv)
    return args, unknown


def apply_runtime_args(args, unknown=None):
    if not args:
        return
    if getattr(args, "target_room", None) is not None:
        CONFIG["TARGET_ROOM"] = normalize_room_name(args.target_room)
    if getattr(args, "scan_target", None) is not None:
        CONFIG["SCAN_TARGET"] = normalize_scan_target(args.scan_target)
    if getattr(args, "scanner_mode", None) is not None:
        CONFIG["SCANNER_MODE"] = normalize_scanner_mode(args.scanner_mode)
    if getattr(args, "scanner_style", None) is not None:
        CONFIG["SCANNER_STYLE"] = normalize_scanner_style(args.scanner_style)
    if getattr(args, "search_strategy", None):
        CONFIG["SEARCH_STRATEGY"] = str(args.search_strategy).strip().lower()
    if unknown:
        logger.warning(f"⚠️ Ignoring unknown CLI args: {' '.join(unknown)}")


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

class WagentBrain:
    """Wagent核心大脑：贪心探索 + 状态机迁移 + 认知进化"""
    def __init__(self):
        self.history = deque(maxlen=CONFIG["HISTORY_MAX_LEN"])  # 交互历史
        self.known_commands = ["help", "look"]  # 初始指令库
        self.blocked_commands = []       # 死胡同指令（标记为不可用）
        self.last_raw_env = ""           # 上一轮环境状态
        self.pending_commands = []       # 待探索指令队列
        self.pending_model_commands = [] # 模型生成的低优先级候选
        self.recent_actions = deque(maxlen=CONFIG["RECENT_ACTIONS_MAX_LEN"])
        self.model_failures = 0
        self.model_cooldown_left = 0
        self.model_candidates = []
        self.model_index = 0
        self.model_fail_streak = 0
        self.model_stats = {}
        self.suggested_commands = deque(maxlen=10)
        self.room_graph = {}
        self.no_progress_turns = 0
        self.total_steps = 0
        self.start_time = time.time()
        self.experience = {}
        self.search_memory = {
            "strategy": CONFIG.get("SEARCH_STRATEGY", "dfs"),
            "stack": [],
            "recent_rooms": []
        }
        self.prompt_memory = {
            "stack": [],
            "templates": {},
            "priority": [],
            "max_depth": CONFIG.get("PROMPT_STACK_MAX_DEPTH", 10)
        }
        self.room_observed_exits = {}
        self.room_failed_actions = {}
        self.room_temp_failed_actions = {}
        self.room_scan_actions = {}
        self.room_scan_targets = {}
        self.feedback_signatures = {}
        self.puzzle_attempts = {}
        self.recipe_progress = {}
        self.pending_recipe_step = None
        self.pending_navigation_transition = None
        self.last_good_room_sig = ""
        self.blind_transit = None
        self.target_room_reached = False
        self.run_memory = {"runs": []}
        self.route_memory = {"destinations": {}}
        self.observation_memory = self._empty_observation_memory()
        self.current_run = {
            "start_ts": int(time.time()),
            "start_room": "",
            "last_room": "",
            "rooms": [],
            "actions": [],
            "transitions": [],
            "failures": 0,
            "successes": 0,
        }
        self.readonly_map_overlay_edges = set()
        self.confirmed_overlay_edges = set()
        self._last_state_save_ts = 0.0
        self._init_model_chain()
        self._load_map_memory()
        self._load_experience_memory()
        self._load_observation_memory()
        self._load_run_memory()
        self._load_route_memory()

    def flush_persistent_state(self, force=False):
        now = time.monotonic()
        min_interval = max(1.0, float(CONFIG.get("STATE_SAVE_MIN_INTERVAL", 15)))
        if not force and (now - self._last_state_save_ts) < min_interval:
            return
        self.save_map_memory()
        self.save_experience_memory()
        self.save_observation_memory()
        self._last_state_save_ts = now

    def _empty_room_record(self):
        return {"success": {}, "recipes": {}}

    def _room_exit_memory_key(self, action, to_room):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        clean_room = re.sub(r'\s+', ' ', str(to_room).strip().lower())
        return f"{clean_action} || {clean_room}"

    def _normalize_action_recipe(self, recipe, fallback_action=""):
        normalized = []
        raw_steps = []
        if isinstance(recipe, list):
            raw_steps = list(recipe)
        elif isinstance(recipe, str):
            raw_steps = [recipe]

        for step in raw_steps:
            clean_step = re.sub(r'\s+', ' ', str(step).strip().lower())
            if not clean_step:
                continue
            if not (self._is_safe_game_command(clean_step) or self._is_persistable_navigation_action(clean_step)):
                continue
            if clean_step not in normalized:
                normalized.append(clean_step)

        clean_fallback = re.sub(r'\s+', ' ', str(fallback_action).strip().lower())
        if not normalized and clean_fallback and (
            self._is_safe_game_command(clean_fallback) or self._is_persistable_navigation_action(clean_fallback)
        ):
            normalized.append(clean_fallback)
        return normalized

    def _room_success_recipe(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        room = self.room_graph.get(clean_room, {})
        recipes = room.get("recipes", {}) if isinstance(room, dict) else {}
        if isinstance(recipes, dict):
            recipe = self._normalize_action_recipe(recipes.get(clean_action, []), fallback_action=clean_action)
            if recipe:
                return recipe
        fallback = self._normalize_action_recipe([], fallback_action=clean_action)
        return fallback

    def _recipe_progress_key(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return ""
        return f"{clean_room}||{clean_action}"

    def _reset_recipe_progress(self, room_sig=None, action=None):
        if room_sig and action:
            key = self._recipe_progress_key(room_sig, action)
            if key:
                self.recipe_progress.pop(key, None)
        elif room_sig:
            clean_room = normalize_room_name(room_sig)
            prefix = f"{clean_room}||"
            stale_keys = [key for key in self.recipe_progress.keys() if key.startswith(prefix)]
            for key in stale_keys:
                self.recipe_progress.pop(key, None)
        else:
            self.recipe_progress = {}

        if not self.pending_recipe_step:
            return

        if room_sig is None and action is None:
            self.pending_recipe_step = None
            return

        matches_room = room_sig is None or self.pending_recipe_step.get("room") == normalize_room_name(room_sig)
        matches_action = action is None or self.pending_recipe_step.get("recipe_action") == re.sub(r'\s+', ' ', str(action).strip().lower())
        if matches_room and matches_action:
            self.pending_recipe_step = None

    def _active_recipe_step(self, room_sig, action):
        recipe = self._room_success_recipe(room_sig, action)
        if len(recipe) <= 1:
            return None
        key = self._recipe_progress_key(room_sig, action)
        index = int(self.recipe_progress.get(key, 0)) if key else 0
        index = max(0, min(index, len(recipe) - 1))
        return {
            "room": normalize_room_name(room_sig),
            "recipe_action": re.sub(r'\s+', ' ', str(action).strip().lower()),
            "recipe": recipe,
            "index": index,
            "step": recipe[index],
            "final_action": recipe[-1],
        }

    def _choose_recipe_step(self, room_sig, action):
        active = self._active_recipe_step(room_sig, action)
        if not active:
            return None
        self.pending_recipe_step = dict(active)
        return active["step"]

    def _recipe_step_allowed_by_state(self, room_sig, action, visible_exits):
        active = self._active_recipe_step(room_sig, action)
        if not active:
            return False
        if not visible_exits:
            return True
        return active.get("final_action", "") in (visible_exits or [])

    def _is_retryable_recipe_step(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return False

        pending = self.pending_recipe_step
        if pending and pending.get("room") == clean_room and pending.get("step") == clean_action:
            return True

        room = self.room_graph.get(clean_room, {})
        for recipe_action in room.get("success", {}).keys():
            active = self._active_recipe_step(clean_room, recipe_action)
            if active and active.get("step") == clean_action:
                return True
        return False

    def _update_recipe_progress_from_feedback(self, room_sig, action, current_room, failed=False):
        pending = self.pending_recipe_step
        if not pending:
            return

        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if pending.get("room") != clean_room or pending.get("step") != clean_action:
            return

        recipe_action = pending.get("recipe_action", "")
        if failed:
            self._reset_recipe_progress(clean_room, recipe_action)
            return

        target_room = str(self.room_graph.get(clean_room, {}).get("success", {}).get(recipe_action, "")).strip().lower()
        next_room = normalize_room_name(current_room)
        if target_room and next_room and next_room == target_room and next_room != clean_room:
            self._reset_recipe_progress(clean_room, recipe_action)
            return

        recipe = list(pending.get("recipe", []))
        index = int(pending.get("index", 0))
        if index + 1 < len(recipe):
            key = self._recipe_progress_key(clean_room, recipe_action)
            if key:
                self.recipe_progress[key] = index + 1
            self.pending_recipe_step = None
            return

        self._reset_recipe_progress(clean_room, recipe_action)

    def _set_room_success_recipe(self, room, action, recipe=None):
        if not isinstance(room, dict):
            return
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_action:
            return
        normalized_recipe = self._normalize_action_recipe(recipe, fallback_action=clean_action)
        room.setdefault("recipes", {})[clean_action] = normalized_recipe

    def _record_loaded_edge(self, graph, from_room, action, to_room, recipe=None):
        clean_room = str(from_room).strip().lower()
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        clean_target = str(to_room).strip().lower()
        if not clean_room or not clean_action or not clean_target:
            return
        if self._is_noisy_room_key(clean_room) or self._is_noisy_room_key(clean_target):
            return
        if not self._is_navigation_action(clean_action):
            return
        if not self._is_persistable_navigation_action(clean_action):
            return
        room = graph.setdefault(clean_room, self._empty_room_record())
        room["success"][clean_action] = clean_target
        self._set_room_success_recipe(room, clean_action, recipe)

    def _normalize_room_record(self, raw_room):
        room = self._empty_room_record()
        if not isinstance(raw_room, dict):
            return room

        success = raw_room.get("success", {})
        raw_recipes = raw_room.get("recipes", {})
        if isinstance(success, dict):
            for action, to_room in success.items():
                clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
                clean_room = str(to_room).strip().lower()
                if not clean_action or not clean_room:
                    continue
                if not self._is_navigation_action(clean_action):
                    continue
                if not self._is_persistable_navigation_action(clean_action):
                    continue
                if self._is_noisy_room_key(clean_room):
                    continue
                room["success"][clean_action] = clean_room
                recipe = raw_recipes.get(action, raw_recipes.get(clean_action, [])) if isinstance(raw_recipes, dict) else []
                self._set_room_success_recipe(room, clean_action, recipe)
        return room

    def _is_navigation_action(self, action):
        a = re.sub(r'\s+', ' ', (action or '').strip().lower())
        if not a:
            return False
        non_nav_prefixes = (
            "look", "read", "help", "examine", "inventory",
            "say", "whisper", "pose", "emote"
        )
        return not any(a == p or a.startswith(p + " ") for p in non_nav_prefixes)

    def _feedback_signature(self, text):
        t = (text or "").lower()
        t = strip_ansi(t)
        t = re.sub(r'\s+', ' ', t)
        t = re.sub(r'\b\d+\b', '#', t)
        return t.strip()[:800]

    def _feedback_similarity(self, a, b):
        sa = self._feedback_signature(a)
        sb = self._feedback_signature(b)
        if not sa and not sb:
            return 1.0
        return difflib.SequenceMatcher(None, sa, sb).ratio()

    def _agent_role(self):
        return normalize_agent_role(CONFIG.get("AGENT_ROLE", DEFAULT_AGENT_ROLE))

    def _target_room(self):
        return normalize_room_name(CONFIG.get("TARGET_ROOM", ""))

    def _scan_target(self):
        return normalize_scan_target(CONFIG.get("SCAN_TARGET", ""))

    def _scanner_mode(self):
        return normalize_scanner_mode(CONFIG.get("SCANNER_MODE", "targeted"))

    def _scanner_style(self):
        return normalize_scanner_style(CONFIG.get("SCANNER_STYLE", "default"))

    def _scan_target_variants(self):
        target = self._scan_target()
        if not target:
            return []
        words = [word for word in target.split() if word]
        variants = []
        if target:
            variants.append(target)
        if len(words) >= 2:
            variants.append(" ".join(words[-2:]))
            variants.append(" ".join(words[:2]))
        if words:
            variants.append(words[-1])

        deduped = []
        for item in variants:
            clean = normalize_scan_target(item)
            if clean and clean not in deduped:
                deduped.append(clean)
        return deduped

    def _scan_target_has_local_context(self, snapshot, targets=None):
        scan_target = self._scan_target()
        if not scan_target:
            return False

        visible_targets = set()
        for target in targets or []:
            clean_target = normalize_scan_target(target)
            if clean_target:
                visible_targets.add(clean_target)

        preferred_keys = set(self._scan_target_variants())
        if preferred_keys & visible_targets:
            return True

        low = normalize_scan_target(snapshot or "")
        if not low:
            return False

        for variant in preferred_keys:
            if not variant:
                continue
            if re.search(rf"\b{re.escape(variant)}\b", low):
                return True
        return False

    def _dark_cell_puzzle_bucket(self, room_sig):
        clean_room = normalize_room_name(room_sig)
        bucket = self.puzzle_attempts.setdefault(clean_room, {})
        if not isinstance(bucket, dict):
            bucket = {}
            self.puzzle_attempts[clean_room] = bucket
        state = bucket.get("dark_cell_root")
        if not isinstance(state, dict):
            state = {
                "root_pos": None,
                "button_exposed": False,
                "exit_open": False,
            }
            bucket["dark_cell_root"] = state
        return state

    def _dark_cell_initial_root_pos(self):
        return {"yellow": 0, "green": 0, "red": 0, "blue": 0}

    def _parse_dark_cell_root_positions(self, snapshot):
        text = (snapshot or "").lower()
        lines = [line.strip() for line in text.splitlines() if "root" in line.lower()]
        if not lines:
            return None

        parsed = {}
        for line in lines:
            if "blue" in line:
                if "left" in line:
                    parsed["blue"] = -1
                elif "right" in line:
                    parsed["blue"] = 1
                elif "middle" in line or "straight down" in line:
                    parsed["blue"] = 0
            elif "reddish" in line or "red" in line:
                if "left" in line:
                    parsed["red"] = -1
                elif "right" in line:
                    parsed["red"] = 1
                elif "middle" in line or "straight down" in line:
                    parsed["red"] = 0
            elif "yellow" in line:
                if "upper" in line:
                    parsed["yellow"] = -1
                elif "bottom" in line or "floor" in line:
                    parsed["yellow"] = 1
                elif "middle" in line:
                    parsed["yellow"] = 0
            elif "green" in line:
                if "upper" in line:
                    parsed["green"] = -1
                elif "bottom" in line or "floor" in line:
                    parsed["green"] = 1
                elif "middle" in line:
                    parsed["green"] = 0

        if len(parsed) != 4:
            return None
        return parsed

    def _simulate_dark_cell_root_action(self, root_pos, action):
        if not isinstance(root_pos, dict):
            return None
        match = re.fullmatch(r"shift\s+(blue|red|yellow|green)\s+(left|right|up|down)", str(action).strip().lower())
        if not match:
            return dict(root_pos)

        color, direction = match.groups()
        next_pos = dict(root_pos)

        if color == "red":
            if direction == "left":
                next_pos[color] = max(-1, next_pos[color] - 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["blue"]:
                    next_pos["blue"] += 1
            elif direction == "right":
                next_pos[color] = min(1, next_pos[color] + 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["blue"]:
                    next_pos["blue"] -= 1
        elif color == "blue":
            if direction == "left":
                next_pos[color] = max(-1, next_pos[color] - 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["red"]:
                    next_pos["red"] += 1
            elif direction == "right":
                next_pos[color] = min(1, next_pos[color] + 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["red"]:
                    next_pos["red"] -= 1
        elif color == "yellow":
            if direction == "up":
                next_pos[color] = max(-1, next_pos[color] - 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["green"]:
                    next_pos["green"] += 1
            elif direction == "down":
                next_pos[color] = min(1, next_pos[color] + 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["green"]:
                    next_pos["green"] -= 1
        elif color == "green":
            if direction == "up":
                next_pos[color] = max(-1, next_pos[color] - 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["yellow"]:
                    next_pos["yellow"] += 1
            elif direction == "down":
                next_pos[color] = min(1, next_pos[color] + 1)
                if next_pos[color] != 0 and next_pos[color] == next_pos["yellow"]:
                    next_pos["yellow"] -= 1

        return next_pos

    def _dark_cell_root_state_key(self, root_pos):
        if not isinstance(root_pos, dict):
            return None
        return tuple(root_pos.get(color, 0) for color in ["yellow", "green", "red", "blue"])

    def _dark_cell_roots_cleared(self, root_pos):
        if not isinstance(root_pos, dict):
            return False
        return all(root_pos.get(color, 0) != 0 for color in ["yellow", "green", "red", "blue"])

    def _plan_dark_cell_root_solution(self, root_pos):
        state_key = self._dark_cell_root_state_key(root_pos)
        if state_key is None:
            return []
        if self._dark_cell_roots_cleared(root_pos):
            return []

        queue = deque([(dict(root_pos), [])])
        visited = {state_key}

        while queue:
            current_state, path = queue.popleft()
            if self._dark_cell_roots_cleared(current_state):
                return path

            for action in DARK_CELL_ROOT_ACTIONS:
                next_state = self._simulate_dark_cell_root_action(current_state, action)
                next_key = self._dark_cell_root_state_key(next_state)
                if next_key in visited:
                    continue
                visited.add(next_key)
                queue.append((next_state, path + [action]))

        return []

    def _update_dark_cell_puzzle_state(self, room_sig, snapshot):
        clean_room = normalize_room_name(room_sig)
        if clean_room != "dark cell":
            return None

        state = self._dark_cell_puzzle_bucket(clean_room)
        text = (snapshot or "").lower()

        if "secret door closes abruptly" in text:
            state["root_pos"] = self._dark_cell_initial_root_pos()
            state["button_exposed"] = False
            state["exit_open"] = False

        parsed_positions = self._parse_dark_cell_root_positions(snapshot)
        if parsed_positions is not None:
            state["root_pos"] = parsed_positions

        last_action = self.recent_actions[-1] if self.recent_actions else ""
        if state.get("root_pos") is not None and last_action.startswith("shift ") and parsed_positions is None:
            state["root_pos"] = self._simulate_dark_cell_root_action(state["root_pos"], last_action)

        if "holding aside the root" in text or "square depression" in text or "some sort of button" in text:
            state["button_exposed"] = True
        elif state.get("root_pos") is not None:
            state["button_exposed"] = self._dark_cell_roots_cleared(state["root_pos"])

        if (
            "hidden passage" in text
            or "passage opens" in text
            or "crack has opened" in text
            or "opening may close again soon" in text
            or "cannot push it again" in text
        ):
            state["exit_open"] = True
            state["button_exposed"] = True
        elif "no matter how you try" in text and "root-covered wall" in text:
            state["exit_open"] = False

        return state

    def _known_recipe_for_transition(self, from_room, action):
        clean_room = normalize_room_name(from_room)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if clean_room == "dark cell" and clean_action == "root-covered wall":
            return list(DARK_CELL_SOLVED_RECIPE)
        return None

    def _scanner_in_travel_phase(self, room_sig):
        if self._agent_role() != "scanner":
            return False
        target_room = self._target_room()
        return bool(target_room and room_sig and room_sig != target_room)

    def _scanner_in_scan_phase(self, room_sig):
        if self._agent_role() != "scanner":
            return False
        target_room = self._target_room()
        if target_room:
            return room_sig == target_room
        return True

    def _action_matches_current_state(self, action, visible_exits):
        if not action:
            return False
        if not visible_exits:
            return True
        pending = self.pending_recipe_step
        if pending and pending.get("step") == action and pending.get("final_action") in visible_exits:
            return True
        return action in visible_exits

    def _target_route_action(self, current_room, visible_exits=None, log_route_hit=True):
        if self._agent_role() == "scanner" and self._scanner_mode() == "random":
            return None
        target_room = self._target_room()
        if not target_room or not current_room or current_room == target_room:
            return None
        route = self._plan_route(current_room, target_room)
        if not route:
            return None
        route_action = route[0]
        step = self._choose_recipe_step(current_room, route_action) or route_action
        if step in self.blocked_commands and not self._is_retryable_room_action(current_room, step):
            return None
        if self._should_skip_room_action(current_room, step):
            return None
        if (
            self._looks_like_repeat_loop(step)
            and not self._allows_failed_room_retry(current_room, step)
            and not self._mapped_target_route_repeat_ok(current_room, route_action, step)
        ):
            return None
        if (
            not self._action_matches_current_state(step, visible_exits or [])
            and not self._allows_failed_room_retry(current_room, step)
            and not self._mapped_target_route_state_ok(current_room, route_action, step)
            and not self._recipe_step_allowed_by_state(current_room, route_action, visible_exits or [])
        ):
            return None
        if log_route_hit:
            logger.info(f"🧭 命中共享路由: {current_room} -> {target_room} via {route_action}")
        return step

    def _mapped_target_route_state_ok(self, current_room, route_action, step):
        clean_room = normalize_room_name(current_room)
        clean_route = re.sub(r'\s+', ' ', str(route_action).strip().lower())
        clean_step = re.sub(r'\s+', ' ', str(step).strip().lower())
        if not clean_room or not clean_route or not clean_step:
            return False
        if clean_step != clean_route:
            return False
        if not self._is_persistable_navigation_action(clean_step):
            return False

        room = self.room_graph.get(clean_room, {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        destination = normalize_room_name(success.get(clean_step, "")) if isinstance(success, dict) else ""
        return bool(destination and destination != clean_room)

    def _mapped_target_route_repeat_ok(self, current_room, route_action, step):
        clean_room = normalize_room_name(current_room)
        clean_route = re.sub(r'\s+', ' ', str(route_action).strip().lower())
        clean_step = re.sub(r'\s+', ' ', str(step).strip().lower())
        if not clean_room or not clean_route or not clean_step:
            return False
        if clean_step != clean_route:
            return False
        if not self._is_persistable_navigation_action(clean_step):
            return False

        room = self.room_graph.get(clean_room, {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        destination = normalize_room_name(success.get(clean_step, "")) if isinstance(success, dict) else ""
        if not destination or destination == clean_room:
            return False

        recent = list(self.recent_actions)[-6:]
        repeat_count = 0
        for action in reversed(recent):
            if action != clean_step:
                break
            repeat_count += 1
        return repeat_count < 5

    def _configured_room_actions(self, config_key, room_sig):
        rules = CONFIG.get(config_key, {})
        if not isinstance(rules, dict):
            return []
        clean_room = normalize_room_name(room_sig)
        actions = rules.get(clean_room, [])
        if not isinstance(actions, list):
            return []
        deduped = []
        for action in actions:
            clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
            if clean_action and clean_action not in deduped:
                deduped.append(clean_action)
        return deduped

    def _priority_room_actions(self, room_sig, visible_exits=None):
        actions = []
        for action in self._configured_room_actions("PRIORITY_ROOM_ACTIONS", room_sig):
            if action in self.blocked_commands and not self._is_retryable_room_action(room_sig, action):
                continue
            if self._looks_like_repeat_loop(action) and not self._allows_failed_room_retry(room_sig, action):
                continue
            if not (self._is_safe_game_command(action) or self._is_persistable_navigation_action(action)):
                continue
            actions.append(action)
        return actions

    def _priority_room_fast_action(self, room_sig, visible_exits=None):
        for action in self._priority_room_actions(room_sig, visible_exits):
            if self._should_skip_room_action(room_sig, action):
                continue
            return action
        return None

    def _allows_failed_room_retry(self, room_sig, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        return clean_action in self._configured_room_actions("UNSTABLE_RETRY_RULES", room_sig)

    def _is_retryable_room_action(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return False
        return self._allows_failed_room_retry(clean_room, clean_action) or self._is_retryable_recipe_step(clean_room, clean_action)

    def _merge_priority_candidates(self, candidate_pool, priority_actions):
        merged = []
        seen = set()
        for action in list(priority_actions) + list(candidate_pool):
            if not action or action in seen:
                continue
            seen.add(action)
            merged.append(action)
        return merged

    def _role_candidate_priority(self, room_sig, visible_exits):
        role = self._agent_role()
        room_success = self.room_graph.get(room_sig, {}).get("success", {})
        priority = []

        target_action = self._target_route_action(room_sig, visible_exits, log_route_hit=False)
        if target_action:
            priority.append(target_action)

        for action in self._priority_room_actions(room_sig, visible_exits):
            priority.append(action)

        if role == "scanner":
            for ex in visible_exits:
                if ex in room_success:
                    continue
                if ex in self.blocked_commands:
                    continue
                if self._should_skip_room_action(room_sig, ex):
                    continue
                if self._looks_like_repeat_loop(ex):
                    continue
                priority.append(ex)

            search_hint = self._peek_search_stack_action(room_sig)
            if search_hint and self._action_matches_current_state(search_hint, visible_exits):
                priority.append(search_hint)

            for action in room_success.keys():
                if not self._action_matches_current_state(action, visible_exits):
                    continue
                if action in self.blocked_commands:
                    continue
                if self._should_skip_room_action(room_sig, action):
                    continue
                if self._looks_like_repeat_loop(action):
                    continue
                priority.append(action)
        elif role == "runner":
            strategic = self._strategic_route_action(room_sig)
            if strategic and self._action_matches_current_state(strategic, visible_exits):
                if strategic not in self.blocked_commands and not self._should_skip_room_action(room_sig, strategic):
                    priority.append(strategic)

            for action in room_success.keys():
                if not self._action_matches_current_state(action, visible_exits):
                    continue
                if action in self.blocked_commands:
                    continue
                if self._should_skip_room_action(room_sig, action):
                    continue
                if self._looks_like_repeat_loop(action):
                    continue
                priority.append(action)

            for ex in visible_exits:
                if ex in room_success:
                    continue
                if ex in self.blocked_commands:
                    continue
                if self._should_skip_room_action(room_sig, ex):
                    continue
                priority.append(ex)
        else:
            search_hint = self._peek_search_stack_action(room_sig)
            if search_hint and self._action_matches_current_state(search_hint, visible_exits):
                priority.append(search_hint)

        return self._merge_priority_candidates([], priority)

    def _visible_frontier_actions(self, room_sig, visible_exits):
        room = self.room_graph.get(room_sig, {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        known_success = set()
        if isinstance(success, dict):
            for action, to_room in success.items():
                if self._action_needs_confirmation(room_sig, action, to_room):
                    continue
                known_success.add(action)

        frontier = []
        for action in visible_exits:
            if action in known_success:
                continue
            if action in self.blocked_commands:
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if self._is_safe_game_command(action) or self._is_persistable_navigation_action(action):
                frontier.append(action)
        return frontier

    def _should_probe_before_frontier(self, room_sig, frontier_actions):
        if not self._scanner_in_scan_phase(room_sig):
            return False
        if self._scan_target():
            return True
        if self._scanner_style() != "default":
            return True
        return not frontier_actions

    def _usable_room_success_actions(self, room_sig, visible_exits=None):
        room = self.room_graph.get(room_sig, {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        if not isinstance(success, dict):
            return []

        actions = []
        for action in success.keys():
            if visible_exits and action not in visible_exits:
                continue
            if action in self.blocked_commands:
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if not self._is_safe_game_command(action):
                continue
            actions.append(action)
        return actions

    def _action_failed_in_snapshot(self, action, snapshot):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_action:
            return False
        low = (snapshot or "").lower()
        if f"command '{clean_action}' is not available" in low:
            return True
        if not self._is_semantic_failure_feedback(snapshot):
            return False
        if not self.recent_actions:
            return False
        return self.recent_actions[-1] == clean_action

    def _extract_visible_object_targets(self, snapshot):
        targets = []
        text = snapshot or ""

        for line in text.splitlines():
            if "you see:" not in line.lower():
                continue
            items = re.split(r',| and ', line.split(":", 1)[-1], flags=re.IGNORECASE)
            for item in items:
                words = re.findall(r"[a-zA-Z][a-zA-Z'\-]*", item.lower())
                words = [word for word in words if word not in {"a", "an", "the"}]
                if not words:
                    continue
                phrases = []
                if len(words) >= 2:
                    phrases.append(" ".join(words[-2:]))
                    phrases.append(" ".join(words[:2]))
                phrases.append(words[-1])
                for phrase in phrases:
                    clean_phrase = re.sub(r'\s+', ' ', phrase.strip())
                    if not clean_phrase or clean_phrase in targets:
                        continue
                    targets.append(clean_phrase)

        return targets[:12]

    def _extract_visible_object_titles(self, snapshot):
        titles = []
        text = snapshot or ""

        for line in text.splitlines():
            if "you see:" not in line.lower():
                continue
            items = re.split(r',| and ', line.split(":", 1)[-1], flags=re.IGNORECASE)
            for item in items:
                words = re.findall(r"[a-zA-Z][a-zA-Z'\-]*", item.lower())
                words = [word for word in words if word not in {"a", "an", "the"}]
                if not words:
                    continue
                title = re.sub(r'\s+', ' ', " ".join(words).strip())
                if not title or title in titles:
                    continue
                titles.append(title)

        return titles[:12]

    def _extract_scan_targets(self, snapshot):
        targets = self._extract_visible_object_targets(snapshot)

        for token in self._extract_focus_tokens(snapshot):
            if token not in targets:
                targets.append(token)

        return targets[:12]

    def _scanner_probe_action(self, room_sig, snapshot):
        if not self._scanner_in_scan_phase(room_sig):
            return None

        tried = set(self.room_scan_actions.get(room_sig, []))
        targets = list(self.room_scan_targets.get(room_sig, []))
        if not targets:
            targets = self._extract_scan_targets(snapshot)
        if not targets:
            return None

        preferred = []
        scan_target = self._scan_target()
        preferred_keys = set(self._scan_target_variants())
        visible_keys = {normalize_scan_target(target) for target in targets}
        excluded_keys = set()
        target_has_local_context = self._scan_target_has_local_context(snapshot, targets)
        if scan_target and scan_target in visible_keys:
            preferred_keys = {scan_target}
            excluded_keys = set(self._scan_target_variants()) - {scan_target}
        elif scan_target and not target_has_local_context:
            preferred_keys = set()
        if preferred_keys:
            for target in targets:
                clean_target = normalize_scan_target(target)
                if clean_target in preferred_keys and target not in preferred:
                    preferred.append(target)
            for key in preferred_keys:
                if key not in [normalize_scan_target(item) for item in preferred]:
                    preferred.append(key)

        ordered_targets = preferred + [
            target
            for target in targets
            if target not in preferred and normalize_scan_target(target) not in excluded_keys
        ]

        action_candidates = []
        for target in ordered_targets:
            action_candidates.extend([
                f"look {target}",
                f"examine {target}",
                f"read {target}",
                f"touch {target}",
            ])
            last_word = target.split()[-1]
            if last_word != target:
                action_candidates.extend([
                    f"look {last_word}",
                    f"examine {last_word}",
                ])

        ordered_actions = []
        for action in action_candidates:
            clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
            if not clean_action or clean_action in ordered_actions:
                continue
            ordered_actions.append(clean_action)

        for action in ordered_actions:
            if action in tried:
                continue
            if action in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if not self._is_safe_game_command(action):
                continue
            self.room_scan_actions.setdefault(room_sig, []).append(action)
            return action
        return None

    def _nutcracker_scan_action(self, room_sig, snapshot):
        if self._agent_role() != "scanner":
            return None
        if self._scanner_style() != "nutcracker":
            return None
        if not self._scanner_in_scan_phase(room_sig):
            return None

        low = (snapshot or "").lower()
        title = normalize_scan_target(self._extract_room_title(snapshot))
        tried = set(self.room_scan_actions.get(room_sig, []))

        target_candidates = []
        for item in [title] + self._scan_target_variants():
            clean = normalize_scan_target(item)
            if clean and clean not in target_candidates:
                target_candidates.append(clean)

        scan_target = self._scan_target()
        if scan_target and scan_target in target_candidates:
            excluded_variants = set(self._scan_target_variants()) - {scan_target}
            target_candidates = [
                item for item in target_candidates
                if item == scan_target or item not in excluded_variants
            ]

        if not target_candidates:
            return None

        action_candidates = []

        readable_markers = ["readable", "text on it", "written", "inscription", "letters", "words", "easily readable", "engraving"]
        if any(marker in low for marker in readable_markers):
            for target in target_candidates:
                action_candidates.append(f"read {target}")
                last_word = target.split()[-1]
                if last_word != target and target != scan_target:
                    action_candidates.append(f"read {last_word}")

        if "chain" in low:
            for target in ["chain"]:
                action_candidates.append(f"look {target}")
                action_candidates.append(f"pull {target}")

        if "door" in low:
            for target in ["door"]:
                action_candidates.append(f"open {target}")
                action_candidates.append(f"push {target}")

        if "hole" in low or "opening" in low:
            for target in ["hole"]:
                action_candidates.append(f"look {target}")
                action_candidates.append(f"enter {target}")

        ordered = []
        for action in action_candidates:
            clean_action = re.sub(r'\s+', ' ', action.strip().lower())
            if not clean_action or clean_action in ordered:
                continue
            ordered.append(clean_action)

        for action in ordered:
            if action in tried:
                continue
            if action in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if not self._is_safe_game_command(action):
                continue
            self.room_scan_actions.setdefault(room_sig, []).append(action)
            return action

        return None

    def _wellcracker_scan_action(self, room_sig, snapshot):
        if self._agent_role() != "scanner":
            return None
        if self._scanner_style() != "wellcracker":
            return None
        if not self._scanner_in_scan_phase(room_sig):
            return None

        low = (snapshot or "").lower()
        tried = set(self.room_scan_actions.get(room_sig, []))
        scan_target = self._scan_target()
        target_candidates = []
        for item in self._scan_target_variants() + self._extract_visible_object_titles(snapshot):
            clean = normalize_scan_target(item)
            if clean and clean not in target_candidates:
                target_candidates.append(clean)

        action_candidates = []

        if "chain" in low:
            action_candidates.extend([
                "climb chain",
                "climb down chain",
                "look chain",
                "pull chain",
            ])

        if "hole" in low or "opening" in low:
            action_candidates.extend([
                "enter hole",
                "look hole",
                "down",
            ])

        if "well" in low or (scan_target and "well" in scan_target):
            for target in target_candidates:
                if "well" not in target:
                    continue
                action_candidates.extend([
                    f"enter {target}",
                    f"look {target}",
                ])
            action_candidates.extend([
                "enter well",
                "look well",
                "down",
            ])

        ordered = []
        for action in action_candidates:
            clean_action = re.sub(r'\s+', ' ', action.strip().lower())
            if not clean_action or clean_action in ordered:
                continue
            ordered.append(clean_action)

        for action in ordered:
            if action in tried:
                continue
            if action in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if not self._is_safe_game_command(action):
                continue
            self.room_scan_actions.setdefault(room_sig, []).append(action)
            return action

        return None

    def _rootcracker_scan_action(self, room_sig, snapshot):
        if self._agent_role() != "scanner":
            return None
        if self._scanner_style() != "rootcracker":
            return None
        if not self._scanner_in_scan_phase(room_sig):
            return None

        dark_cell_state = self._update_dark_cell_puzzle_state(room_sig, snapshot)
        if normalize_room_name(room_sig) == "dark cell":
            if dark_cell_state:
                logger.debug("Rootcracker dark-cell state: %s", dark_cell_state)
                if dark_cell_state.get("exit_open"):
                    return "root-covered wall"
                if dark_cell_state.get("button_exposed"):
                    return "press button"
                root_pos = dark_cell_state.get("root_pos")
                if root_pos is None:
                    return "look root-covered wall"
                planned_actions = self._plan_dark_cell_root_solution(root_pos)
                if planned_actions:
                    logger.debug("Rootcracker planned actions from root state %s: %s", root_pos, planned_actions)
                if planned_actions:
                    return planned_actions[0]
                return "look root-covered wall"

        low = (snapshot or "").lower()
        tried_actions = set(self.room_scan_actions.get(room_sig, []))
        recent_actions = set(list(self.recent_actions)[-2:])
        scan_target = self._scan_target()

        target_candidates = []
        for item in self._scan_target_variants() + self._extract_visible_object_titles(snapshot):
            clean = normalize_scan_target(item)
            if clean and clean not in target_candidates:
                target_candidates.append(clean)

        shift_actions = []
        for color, direction in re.findall(r"\bshift\s+(red|blue|yellow|green)\s+(up|down|left|right)\b", low):
            cmd = f"shift {color} {direction}"
            if cmd not in shift_actions:
                shift_actions.append(cmd)

        root_context = any(token in low for token in ["root-covered wall", "roots", " root", "wall"]) or (
            scan_target and "root" in scan_target
        )
        dir_hints = self._extract_direction_hints(snapshot)
        if not shift_actions and root_context:
            colors_present = [
                color for color in ["blue", "green", "red", "yellow"]
                if re.search(rf"\b{color}\b", low)
            ]
            preferred_shifts = [
                "shift blue left",
                "shift blue right",
                "shift green up",
                "shift green down",
                "shift red left",
                "shift red right",
                "shift yellow up",
                "shift yellow down",
                "shift red up",
                "shift red down",
                "shift yellow left",
                "shift yellow right",
                "shift blue up",
                "shift blue down",
                "shift green left",
                "shift green right",
            ]
            for action in preferred_shifts:
                _, color, direction = action.split()
                if colors_present and color not in colors_present:
                    continue
                allowed_dirs = dir_hints.get(color)
                if allowed_dirs and direction not in allowed_dirs:
                    continue
                shift_actions.append(action)

        ordered = []

        def queue_action(action):
            clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
            if not clean_action or clean_action in ordered:
                return
            ordered.append(clean_action)

        if root_context and not shift_actions:
            for target in target_candidates:
                if "root" in target or "wall" in target:
                    queue_action(f"look {target}")
            queue_action("look root-covered wall")
            queue_action("look roots")

        for action in shift_actions:
            queue_action(action)

        if root_context:
            queue_action("root-covered wall")
            queue_action("burn roots")
            queue_action("burn root")
            queue_action("push roots")
            if self.no_progress_turns >= 2:
                queue_action("look root-covered wall")
                queue_action("look roots")

        for action in ordered:
            if action in self.blocked_commands:
                continue
            if action in recent_actions:
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if action != "root-covered wall" and action in tried_actions:
                if not action.startswith(("look ", "examine ")) or self.no_progress_turns < 3:
                    continue
            if action.startswith(("look ", "examine ")) and action in tried_actions and self.no_progress_turns < 3:
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if not (self._is_safe_game_command(action) or self._is_persistable_navigation_action(action)):
                continue
            if action != "root-covered wall":
                self.room_scan_actions.setdefault(room_sig, []).append(action)
            return action

        return None

    def _is_scan_target_probe(self, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_action.startswith(("look ", "examine ", "read ", "touch ")):
            return False
        target = clean_action.split(" ", 1)[1].strip()
        return target in set(self._scan_target_variants())

    def _scanner_fast_path_action(self, room_sig, visible_exits, snapshot=""):
        if not self._scanner_in_scan_phase(room_sig):
            return None, ""

        frontier_actions = self._visible_frontier_actions(room_sig, visible_exits)

        rootcracker_action = self._rootcracker_scan_action(room_sig, snapshot)
        if rootcracker_action:
            return rootcracker_action, f"scanner rootcracker fast path ({self._scan_target() or 'room'})"

        wellcracker_action = self._wellcracker_scan_action(room_sig, snapshot)
        if wellcracker_action:
            return wellcracker_action, f"scanner wellcracker fast path ({self._scan_target() or 'room'})"

        nutcracker_action = self._nutcracker_scan_action(room_sig, snapshot)
        if nutcracker_action:
            return nutcracker_action, f"scanner nutcracker fast path ({self._scan_target() or 'room'})"

        if self._should_probe_before_frontier(room_sig, frontier_actions):
            probe_action = self._scanner_probe_action(room_sig, snapshot)
            if probe_action:
                if self._is_scan_target_probe(probe_action):
                    return probe_action, f"scanner requested scan-target fast path ({self._scan_target()})"
                return probe_action, "scanner room-probe fast path"

        for action in frontier_actions:
            if self._action_failed_in_snapshot(action, snapshot):
                continue
            target_room = self._target_room()
            if target_room and room_sig == target_room:
                return action, f"scanner target-room scan fast path ({target_room})"
            return action, "scanner local scan fast path"

        return None, ""

    def _random_scanner_travel_action(self, room_sig, visible_exits, snapshot=""):
        if self._agent_role() != "scanner" or self._scanner_mode() != "random":
            return None, ""
        if not self._scanner_in_travel_phase(room_sig):
            return None, ""

        candidates = []
        candidates.extend(self._visible_frontier_actions(room_sig, visible_exits))

        strategic = self._strategic_route_action(room_sig)
        if strategic and self._action_matches_current_state(strategic, visible_exits):
            if strategic not in self.blocked_commands and not self._should_skip_room_action(room_sig, strategic):
                candidates.append(strategic)

        candidates.extend(self._usable_room_success_actions(room_sig, visible_exits))

        search_hint = self._peek_search_stack_action(room_sig)
        if search_hint and self._action_matches_current_state(search_hint, visible_exits):
            if search_hint not in self.blocked_commands and not self._should_skip_room_action(room_sig, search_hint):
                candidates.append(search_hint)

        for cmd in self.pending_commands:
            if cmd in self.blocked_commands:
                continue
            if self._should_skip_room_action(room_sig, cmd):
                continue
            if not self._is_navigation_action(cmd):
                continue
            if visible_exits and cmd not in visible_exits:
                continue
            candidates.append(cmd)

        filtered = []
        for action in candidates:
            if not action:
                continue
            if action in filtered:
                continue
            if self._action_failed_in_snapshot(action, snapshot):
                continue
            if self._looks_like_repeat_loop(action):
                continue
            filtered.append(action)

        if not filtered:
            return None, ""
        return random.choice(filtered), "random scanner roaming fast path"

    def _role_prompt_sections(self, room_sig, visible_exits):
        role = self._agent_role()
        target_room = self._target_room() or "none"
        scan_target = self._scan_target() or "none"
        scanner_mode = self._scanner_mode() if role == "scanner" else "n/a"
        scanner_style = self._scanner_style() if role == "scanner" else "n/a"
        visible_text = ", ".join(visible_exits[:8]) if visible_exits else "none"

        if role == "scanner":
            if self._scanner_in_travel_phase(room_sig):
                if scanner_mode == "random":
                    objective = (
                        "Travel mode (random scanner): roam through the maze using visible/frontier exits and remembered "
                        "room connections until you naturally arrive at the target room."
                    )
                else:
                    objective = (
                        "Travel mode (targeted scanner): use shared map memory to reach the target room quickly and avoid "
                        "spending turns rechecking local exits before you arrive."
                    )
            else:
                if scanner_style == "wellcracker" and scan_target != "none":
                    objective = (
                        f"Scan mode (wellcracker): treat the designated room target '{scan_target}' as a local affordance "
                        "frontier and push well/chain/hole verbs such as climb, enter, down, and close inspection before "
                        "moving on."
                    )
                elif scanner_style == "rootcracker" and scan_target != "none":
                    objective = (
                        f"Scan mode (rootcracker): crack the designated room target '{scan_target}' by inspecting the wall, "
                        "following root-manipulation affordances immediately, and retrying the exit after the local state changes."
                    )
                elif scanner_style == "nutcracker" and scan_target != "none":
                    objective = (
                        f"Scan mode (nutcracker): crack the designated room target '{scan_target}' first by following local "
                        "affordances such as readable text, doors, holes, chains, and other puzzle cues before moving on."
                    )
                elif scan_target != "none":
                    objective = (
                        f"Scan mode: inspect the designated room target '{scan_target}' first, then continue with other "
                        "room-local probes before falling back to exits."
                    )
                else:
                    objective = (
                        "Scan mode: prioritize visible exits not yet validated in map memory and treat remembered exits "
                        "as fallback only after local unknown exits are exhausted."
                    )
        elif role == "runner":
            objective = (
                "Use shared map memory to transit quickly through known rooms, minimize local fiddling, "
                "and push toward the target room or nearest frontier room."
            )
        else:
            objective = "Balance exploration and progress using shared map memory and room-specific failure memory."

        if target_room != "none":
            if role == "scanner" and self._scanner_in_scan_phase(room_sig):
                if scan_target != "none":
                    if scanner_style == "wellcracker":
                        routing_hint = (
                            f"You are at target room {target_room}. Treat scan target '{scan_target}' as a local frontier: "
                            "push well, chain, hole, enter, climb, and down-style actions before leaving the area."
                        )
                    elif scanner_style == "rootcracker":
                        routing_hint = (
                            f"You are at target room {target_room}. Crack scan target '{scan_target}' by inspecting the root wall, "
                            "trying root-shift commands from the live text, and re-attempting the exit when the puzzle state changes."
                        )
                    elif scanner_style == "nutcracker":
                        routing_hint = (
                            f"You are at target room {target_room}. Crack scan target '{scan_target}' first using the local "
                            "puzzle affordances in the current text, then continue broader room scanning."
                        )
                    else:
                        routing_hint = (
                            f"You are at target room {target_room}. Scan target '{scan_target}' first, then continue local "
                            "room probes before taking remembered shortcuts."
                        )
                else:
                    routing_hint = (
                        f"You are at target room {target_room}. Stay in local scan mode and try visible exits not yet "
                        "validated in shared map memory before taking remembered shortcuts."
                    )
            else:
                if role == "scanner" and scanner_mode == "random":
                    routing_hint = (
                        f"Target room is {target_room}, but scanner mode is random. Roam until you encounter it, "
                        "then begin local scanning."
                    )
                else:
                    routing_hint = (
                        f"Target room is {target_room}. If you are not there and map memory provides a route, "
                        "move toward it immediately."
                    )
        elif role == "scanner":
            routing_hint = "No fixed target room. Scan the current room first and prefer unseen visible exits before remembered ones."
        else:
            routing_hint = "No fixed target room for this run."

        return {
            "role": role,
            "target_room": target_room,
            "scan_target": scan_target,
            "scanner_mode": scanner_mode,
            "scanner_style": scanner_style,
            "objective": objective,
            "routing_hint": routing_hint,
            "visible_exits": visible_text,
        }

    def _record_feedback_observation(self, room_sig, action, feedback):
        if not room_sig or not action:
            return 0
        key = f"{room_sig}||{action}||{self._feedback_signature(feedback)}"
        n = int(self.feedback_signatures.get(key, 0)) + 1
        self.feedback_signatures[key] = n
        return n

    def _is_semantic_failure_feedback(self, feedback_text):
        """判断回显是否属于语义失败（不依赖具体指令词）。"""
        low = (feedback_text or "").lower()
        failure_markers = [
            "not available",
            "unknown command",
            "could not find",
            "huh?",
            "you can not",
            "you cannot",
            "say what?",
            "whisper to whom?",
            "pose what?",
            "usage:",
            "you fall to the ground, defeated",
            "the world turns black",
            "engulf you",
        ]
        return any(m in low for m in failure_markers)

    def _has_actionable_affordance_feedback(self, feedback_text):
        """判断回显是否包含可操作线索（同房间也可视作中间进展）。"""
        low = (feedback_text or "").lower()
        affordance_markers = [
            "maybe you could try",
            "you must define",
            "you can only",
            "try '",
            'try "',
            "is lit up",
            "you light",
            "until you find some light",
            "already found what you need",
            "exits:",
        ]
        return any(m in low for m in affordance_markers)

    def _extract_focus_tokens(self, snapshot):
        text = (snapshot or "").lower()
        tokens = set()

        for color in ["red", "blue", "yellow", "green", "white", "black"]:
            if re.search(rf'\b{color}\b', text):
                tokens.add(color)

        # 允许场景里出现的连字符实体名，优先用于 "look X" 或参数命令
        for hy in re.findall(r'\b[a-z]+(?:-[a-z]+)+\b', text):
            if hy in SYSTEM_COMMAND_BLOCKLIST:
                continue
            if len(hy) <= 24 and not self._is_noisy_object_token(hy):
                tokens.add(hy)

        # 从名词短语中提取关键词（偏保守，避免噪声）
        for noun in ["root", "roots", "door", "wall", "chain", "gate", "stone"]:
            if re.search(rf'\b{noun}\b', text):
                tokens.add(noun)

        return sorted(tokens)

    def _is_noisy_object_token(self, token):
        """过滤容易造成幻觉交互的对象词（如 gray-green 颜色形容词）。"""
        t = (token or "").strip().lower()
        if not t:
            return True

        colors = {
            "red", "blue", "yellow", "green", "gray", "grey", "black", "white",
            "brown", "purple", "orange", "pink", "gold", "silver", "cyan"
        }

        # gray-green / blue-red 这类复合颜色词通常只是描述，不是可交互对象。
        if re.match(r'^[a-z]+-[a-z]+$', t):
            a, b = t.split('-', 1)
            if a in colors and b in colors:
                return True

        # 系统/配置类连字符词直接过滤，避免污染探索对象。
        if t in SYSTEM_COMMAND_BLOCKLIST:
            return True
        system_hyphen_patterns = [
            r'^system-.+',
            r'^ui-.+',
            r'^cmd-.+',
            r'^api-.+',
            r'.+-[0-9]+$',
            r'^[a-z]+-[a-z]+-[a-z]+$'
        ]
        for pat in system_hyphen_patterns:
            if re.match(pat, t):
                return True
        parts = [p for p in t.split('-') if p]
        if any(p in {"system", "client", "settings", "tutorial", "world", "auto", "quell", "quelling"} for p in parts):
            return True

        return False

    def _extract_direction_hints(self, snapshot):
        """从环境文本推断对象可移动方向。"""
        text = (snapshot or "").lower()
        hints = {}
        colors = ["red", "blue", "yellow", "green"]

        for line in text.splitlines():
            l = line.strip()
            if "root" not in l:
                continue
            found = [c for c in colors if c in l]
            if not found:
                continue

            dirs = None
            if "horizontal" in l:
                dirs = ["up", "down"]
            elif "vertical" in l or "hangs straight down" in l or "left or right" in l:
                dirs = ["left", "right"]
            elif "up or down" in l:
                dirs = ["up", "down"]

            if dirs:
                for c in found:
                    hints[c] = dirs

        return hints

    def _synthesize_commands_from_patterns(self, snapshot):
        if self.no_progress_turns < 4:
            return

        focus = self._extract_focus_tokens(snapshot)
        if not focus:
            return

        directions = ["up", "down", "left", "right", "over", "north", "south", "east", "west"]
        dir_hints = self._extract_direction_hints(snapshot)
        generated = []

        # 基于已知三词命令模式（如 shift red up）做组合扩展
        for cmd in list(self.known_commands):
            parts = cmd.split()
            if len(parts) != 3:
                continue
            verb, _, tail = parts
            if tail not in directions:
                continue
            for obj in focus:
                # 只对具备方向线索的对象做三词扩展，避免 shift door south 之类噪声爆炸。
                has_dir_affordance = (obj in dir_hints) or (obj in {"red", "blue", "yellow", "green"})
                if not has_dir_affordance:
                    continue
                allowed_dirs = dir_hints.get(obj, directions)
                for d in allowed_dirs:
                    cand = f"{verb} {obj} {d}"
                    if cand in self.blocked_commands:
                        continue
                    if not self._is_safe_game_command(cand):
                        continue
                    generated.append(cand)

        # 场景交互的最小泛化
        for obj in focus:
            if self._is_noisy_object_token(obj):
                continue
            for verb in ["look", "examine", "push", "pull", "move", "touch"]:
                cand = f"{verb} {obj}"
                if self._is_safe_game_command(cand):
                    generated.append(cand)

        # 去重后限量灌入待探索队列，避免爆炸
        seen = set()
        injected = 0
        for cand in generated:
            if cand in seen:
                continue
            seen.add(cand)
            if cand in self.pending_commands or cand in self.recent_actions:
                continue
            if self._add_command(cand):
                injected += 1
            if injected >= CONFIG["SYNTH_VARIANTS_PER_TURN"]:
                break

        if injected > 0:
            logger.info(f"🧪 Stuck-mode synth injected commands: {injected}")

    def _load_experience_memory(self):
        self.experience = {}
        self.room_failed_actions = {}
        try:
            path = CONFIG["EXPERIENCE_MEMORY_FILE"]
            self.room_failed_actions = self._normalize_failed_actions_data(_locked_json_load(path))
            self._drop_failed_actions_conflicting_with_success()
            failed_room_count = len(self.room_failed_actions)
            failed_action_count = sum(len(v) for v in self.room_failed_actions.values())
            logger.info(f"🧠 已加载房间失败记忆: failed_rooms={failed_room_count} failed_actions={failed_action_count}")
        except Exception as e:
            logger.warning(f"⚠️ 经验记忆加载失败: {e}")

    def _cleanup_experience_commands(self):
        self.experience = {}

    def _normalize_failed_actions_data(self, data):
        normalized_failed = {}
        if not isinstance(data, dict):
            return normalized_failed

        raw_failed = data.get("failed_actions_by_room", {})
        if not isinstance(raw_failed, dict):
            return normalized_failed

        for room_sig, actions in raw_failed.items():
            if not isinstance(room_sig, str) or not isinstance(actions, list):
                continue
            clean_room = room_sig.strip().lower()
            if not clean_room or self._is_noisy_room_key(clean_room):
                continue
            clean_actions = []
            for action in actions:
                if not isinstance(action, str):
                    continue
                clean_action = re.sub(r'\s+', ' ', action.strip().lower())
                if not clean_action or not self._is_safe_game_command(clean_action):
                    continue
                if self._is_observe_action(clean_action):
                    continue
                if clean_action not in clean_actions:
                    clean_actions.append(clean_action)
            if clean_actions:
                normalized_failed[clean_room] = clean_actions

        return normalized_failed

    def _merge_room_failed_actions(self, base_failed, new_failed):
        merged = {}
        for source in (base_failed, new_failed):
            if not isinstance(source, dict):
                continue
            for room_sig, actions in source.items():
                clean_room = str(room_sig).strip().lower()
                if not clean_room or self._is_noisy_room_key(clean_room):
                    continue
                if not isinstance(actions, list):
                    continue
                bucket = merged.setdefault(clean_room, [])
                for action in actions:
                    clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
                    if not clean_action or not self._is_safe_game_command(clean_action):
                        continue
                    if self._is_observe_action(clean_action):
                        continue
                    if self._is_recovery_utility_action(clean_action):
                        continue
                    if clean_action not in bucket:
                        bucket.append(clean_action)
        return merged

    def _successful_room_actions(self, room_sig):
        room = self.room_graph.get(str(room_sig).strip().lower(), {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        actions = set()
        if isinstance(success, dict):
            actions.update(str(action).strip().lower() for action in success.keys() if str(action).strip())
        recipes = room.get("recipes", {}) if isinstance(room, dict) else {}
        if isinstance(recipes, dict):
            for recipe in recipes.values():
                for step in self._normalize_action_recipe(recipe):
                    if step:
                        actions.add(step)
        return actions

    def _persistent_failed_room_actions(self, room_sig):
        clean_room = str(room_sig).strip().lower()
        actions = list(self.room_failed_actions.get(clean_room, []))
        if not actions:
            return []
        success_actions = self._successful_room_actions(clean_room)
        return [action for action in actions if action not in success_actions]

    def _effective_failed_room_actions(self, room_sig):
        clean_room = str(room_sig).strip().lower()
        effective = list(self._persistent_failed_room_actions(clean_room))
        for action in self.room_temp_failed_actions.get(clean_room, []):
            if action not in effective:
                effective.append(action)
        return effective

    def _remember_temp_failed_room_action(self, room_sig, action):
        clean_room = str(room_sig).strip().lower()
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return
        bucket = self.room_temp_failed_actions.setdefault(clean_room, [])
        if clean_action not in bucket:
            bucket.append(clean_action)

    def _clear_temp_failed_room_action(self, room_sig, action):
        clean_room = str(room_sig).strip().lower()
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        actions = self.room_temp_failed_actions.get(clean_room, [])
        if clean_action in actions:
            self.room_temp_failed_actions[clean_room] = [item for item in actions if item != clean_action]
            if not self.room_temp_failed_actions[clean_room]:
                self.room_temp_failed_actions.pop(clean_room, None)

    def _is_known_navigation_action(self, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_action:
            return False
        if clean_action in {"north", "south", "east", "west", "up", "down"}:
            return True
        for room in self.room_graph.values():
            success = room.get("success", {}) if isinstance(room, dict) else {}
            if isinstance(success, dict) and clean_action in success:
                return True
        for observed in self.room_observed_exits.values():
            if clean_action in observed:
                return True
        return False

    def _drop_failed_actions_conflicting_with_success(self):
        cleaned = {}
        removed = 0
        for room_sig, actions in self.room_failed_actions.items():
            effective = self._persistent_failed_room_actions(room_sig)
            removed += max(0, len(actions) - len(effective))
            if effective:
                cleaned[room_sig] = effective
        self.room_failed_actions = cleaned
        if removed:
            logger.info(f"🧹 Cleared {removed} stale failed actions that conflict with map-memory successes")

    def _drop_failed_actions_conflicting_with_observed_exits(self):
        cleaned = {}
        removed = 0
        for room_sig, actions in self.room_failed_actions.items():
            observed_exits = set(self.room_observed_exits.get(room_sig, set()))
            if observed_exits:
                effective = [action for action in actions if action not in observed_exits]
            else:
                effective = list(actions)
            removed += max(0, len(actions) - len(effective))
            if effective:
                cleaned[room_sig] = effective
        self.room_failed_actions = cleaned
        if removed:
            logger.info(f"🧹 Cleared {removed} stale failed actions contradicted by live observed exits")

    def save_experience_memory(self):
        try:
            path = CONFIG["EXPERIENCE_MEMORY_FILE"]
            merged_failed = self._merge_room_failed_actions(
                self._normalize_failed_actions_data(_locked_json_load(path)),
                self.room_failed_actions,
            )
            self.room_failed_actions = merged_failed
            self._drop_failed_actions_conflicting_with_success()
            self._drop_failed_actions_conflicting_with_observed_exits()
            _locked_json_dump(path, {"failed_actions_by_room": self.room_failed_actions})
        except Exception as e:
            logger.warning(f"⚠️ 经验记忆保存失败: {e}")

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _observation_memory_meta(self):
        return {
            "bot_id": CONFIG.get("BOT_ID", ""),
            "agent_role": self._agent_role(),
            "scanner_mode": CONFIG.get("SCANNER_MODE", ""),
            "scanner_style": CONFIG.get("SCANNER_STYLE", ""),
            "target_room": CONFIG.get("TARGET_ROOM", ""),
            "scan_target": CONFIG.get("SCAN_TARGET", ""),
            "search_strategy": CONFIG.get("SEARCH_STRATEGY", ""),
            "updated_at": int(time.time()),
        }

    def _empty_observation_room_entry(self):
        return {
            "visits": 0,
            "first_seen_ts": 0,
            "last_seen_ts": 0,
            "observed_exits": [],
            "confirmed_walks": {},
            "failed_actions": [],
            "failure_counts": {},
            "scan_targets": [],
            "last_snapshot_excerpt": "",
        }

    def _empty_observation_memory(self):
        return {
            "meta": self._observation_memory_meta(),
            "rooms": {},
            "runs": [],
            "recent_events": [],
        }

    def _clean_observation_strings(self, values, safe_commands=False):
        cleaned = []
        if not isinstance(values, list):
            return cleaned
        for value in values:
            if not isinstance(value, str):
                continue
            clean_value = re.sub(r'\s+', ' ', value.strip().lower())
            if not clean_value:
                continue
            if safe_commands and not self._is_safe_game_command(clean_value):
                continue
            if clean_value not in cleaned:
                cleaned.append(clean_value)
        return cleaned

    def _normalize_observation_room_entry(self, room_sig, data):
        entry = self._empty_observation_room_entry()
        if not isinstance(data, dict):
            return entry

        entry["visits"] = max(0, self._safe_int(data.get("visits", 0), 0))
        entry["first_seen_ts"] = max(0, self._safe_int(data.get("first_seen_ts", 0), 0))
        entry["last_seen_ts"] = max(0, self._safe_int(data.get("last_seen_ts", 0), 0))
        entry["observed_exits"] = self._clean_observation_strings(data.get("observed_exits", []), safe_commands=True)
        entry["failed_actions"] = self._clean_observation_strings(data.get("failed_actions", []), safe_commands=True)
        entry["scan_targets"] = self._clean_observation_strings(data.get("scan_targets", []))

        confirmed_walks = {}
        raw_confirmed_walks = data.get("confirmed_walks", {})
        if isinstance(raw_confirmed_walks, dict):
            for action, to_room in raw_confirmed_walks.items():
                clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
                clean_target = normalize_room_name(to_room)
                if not clean_action or not clean_target:
                    continue
                if not self._is_safe_game_command(clean_action):
                    continue
                confirmed_walks[clean_action] = clean_target
        entry["confirmed_walks"] = confirmed_walks

        failure_counts = {}
        raw_failure_counts = data.get("failure_counts", {})
        if isinstance(raw_failure_counts, dict):
            for action, count in raw_failure_counts.items():
                clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
                if not clean_action or not self._is_safe_game_command(clean_action):
                    continue
                clean_count = max(0, self._safe_int(count, 0))
                if clean_count <= 0:
                    continue
                failure_counts[clean_action] = clean_count
                if clean_action not in entry["failed_actions"]:
                    entry["failed_actions"].append(clean_action)
        for action in entry["failed_actions"]:
            failure_counts.setdefault(action, 1)
        entry["failure_counts"] = failure_counts

        excerpt = str(data.get("last_snapshot_excerpt", "") or "").strip()
        entry["last_snapshot_excerpt"] = excerpt[:CONFIG.get("OBSERVATION_PREVIEW_MAX", 280)]
        return entry

    def _normalize_observation_memory_data(self, data):
        memory = self._empty_observation_memory()
        if not isinstance(data, dict):
            return memory

        rooms = {}
        raw_rooms = data.get("rooms", {})
        if isinstance(raw_rooms, dict):
            for room_sig, room_data in raw_rooms.items():
                clean_room = normalize_room_name(room_sig)
                if not clean_room or self._is_noisy_room_key(clean_room):
                    continue
                rooms[clean_room] = self._normalize_observation_room_entry(clean_room, room_data)
        memory["rooms"] = rooms

        runs = data.get("runs", [])
        if isinstance(runs, list):
            memory["runs"] = [run for run in runs if isinstance(run, dict)][-CONFIG.get("OBSERVATION_RUN_MAX", 40):]

        recent_events = data.get("recent_events", [])
        if isinstance(recent_events, list):
            memory["recent_events"] = [event for event in recent_events if isinstance(event, dict)][-CONFIG.get("OBSERVATION_EVENT_MAX", 240):]

        memory["meta"] = self._observation_memory_meta()
        return memory

    def _load_observation_memory(self):
        self.observation_memory = self._empty_observation_memory()
        try:
            path = CONFIG["OBSERVATION_MEMORY_FILE"]
            self.observation_memory = self._normalize_observation_memory_data(_locked_json_load(path))
            room_count = len(self.observation_memory.get("rooms", {}))
            event_count = len(self.observation_memory.get("recent_events", []))
            logger.info(f"🧾 已加载本地观察记忆: rooms={room_count} events={event_count}")
        except Exception as e:
            logger.warning(f"⚠️ 本地观察记忆加载失败: {e}")

    def _ensure_observation_room_entry(self, room_sig):
        clean_room = normalize_room_name(room_sig)
        rooms = self.observation_memory.setdefault("rooms", {})
        entry = rooms.get(clean_room)
        if not isinstance(entry, dict):
            entry = self._empty_observation_room_entry()
            rooms[clean_room] = entry
        return entry

    def _snapshot_excerpt(self, snapshot):
        clean = re.sub(r'\s+', ' ', str(snapshot or '').strip())
        return clean[:CONFIG.get("OBSERVATION_PREVIEW_MAX", 280)]

    def _append_observation_event(self, event_type, **payload):
        events = self.observation_memory.setdefault("recent_events", [])
        event = {"ts": int(time.time()), "type": str(event_type).strip().lower()}
        event.update(payload)

        comparable = {k: v for k, v in event.items() if k != "ts"}
        if events:
            previous = {k: v for k, v in events[-1].items() if k != "ts"}
            if previous == comparable:
                return

        events.append(event)
        max_events = int(CONFIG.get("OBSERVATION_EVENT_MAX", 240))
        if len(events) > max_events:
            del events[:-max_events]

    def _record_observed_room_state(self, room_sig, snapshot, visible_exits, new_visible_exits, scan_targets, is_new_room):
        entry = self._ensure_observation_room_entry(room_sig)
        now = int(time.time())
        entry["visits"] = int(entry.get("visits", 0)) + 1
        if not entry.get("first_seen_ts"):
            entry["first_seen_ts"] = now
        entry["last_seen_ts"] = now
        entry["last_snapshot_excerpt"] = self._snapshot_excerpt(snapshot)

        if visible_exits:
            merged_exits = list(entry.get("observed_exits", []))
            for action in visible_exits:
                if action not in merged_exits:
                    merged_exits.append(action)
            entry["observed_exits"] = merged_exits

        if scan_targets:
            merged_targets = list(entry.get("scan_targets", []))
            for target in scan_targets:
                clean_target = re.sub(r'\s+', ' ', str(target).strip().lower())
                if clean_target and clean_target not in merged_targets:
                    merged_targets.append(clean_target)
            entry["scan_targets"] = merged_targets

        if is_new_room:
            self._append_observation_event(
                "room_first_seen",
                room=room_sig,
                visible_exits=list(visible_exits),
            )
        elif new_visible_exits:
            self._append_observation_event(
                "new_visible_exits",
                room=room_sig,
                exits=list(new_visible_exits),
            )

    def _record_local_failed_action(self, room_sig, action):
        entry = self._ensure_observation_room_entry(room_sig)
        failed_actions = list(entry.get("failed_actions", []))
        failure_counts = dict(entry.get("failure_counts", {}))
        was_new = action not in failed_actions
        if was_new:
            failed_actions.append(action)
        failure_counts[action] = int(failure_counts.get(action, 0)) + 1
        entry["failed_actions"] = failed_actions
        entry["failure_counts"] = failure_counts
        if was_new:
            self._append_observation_event("failed_action", room=room_sig, action=action)

    def _record_local_confirmed_walk(self, from_room, action, to_room):
        entry = self._ensure_observation_room_entry(from_room)
        confirmed_walks = dict(entry.get("confirmed_walks", {}))
        previous_target = confirmed_walks.get(action)
        confirmed_walks[action] = to_room
        entry["confirmed_walks"] = confirmed_walks
        if previous_target != to_room:
            self._append_observation_event(
                "confirmed_walk",
                from_room=from_room,
                action=action,
                to_room=to_room,
            )

    def save_observation_memory(self):
        try:
            path = CONFIG["OBSERVATION_MEMORY_FILE"]
            self.observation_memory["meta"] = self._observation_memory_meta()
            _locked_json_dump(path, self.observation_memory)
        except Exception as e:
            logger.warning(f"⚠️ 本地观察记忆保存失败: {e}")

    def _can_promote_shared_memory(self):
        return self._agent_role() == "runner" or CONFIG.get("ALLOW_SHARED_MEMORY_PROMOTION", False)

    def _load_run_memory(self):
        self.run_memory = {"runs": []}
        logger.info("🧾 运行总结记忆已禁用")

    def _empty_destination_route_record(self):
        return {"hops": {}, "latest_success_path": [], "updated_ts": 0}

    def _normalize_route_memory(self, payload):
        normalized = {"destinations": {}}
        raw_destinations = payload.get("destinations", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_destinations, dict):
            return normalized

        def hop_is_confirmed(from_room, action, to_room):
            if not from_room or from_room == "unknown-room":
                return False
            room = self.room_graph.get(from_room, {}) if isinstance(self.room_graph, dict) else {}
            success = room.get("success", {}) if isinstance(room, dict) else {}
            return isinstance(success, dict) and success.get(action) == to_room

        for destination, record in raw_destinations.items():
            clean_destination = normalize_room_name(destination)
            if not clean_destination or not isinstance(record, dict):
                continue

            normalized_record = self._empty_destination_route_record()
            raw_hops = record.get("hops", {})
            if isinstance(raw_hops, dict):
                for from_room, hop in raw_hops.items():
                    clean_from_room = normalize_room_name(from_room)
                    if not clean_from_room or not isinstance(hop, dict):
                        continue
                    clean_action = re.sub(r'\s+', ' ', str(hop.get("action", "")).strip().lower())
                    clean_to_room = normalize_room_name(hop.get("to_room", ""))
                    if not clean_action or not clean_to_room:
                        continue
                    if not hop_is_confirmed(clean_from_room, clean_action, clean_to_room):
                        continue
                    updated_ts = hop.get("updated_ts", record.get("updated_ts", 0))
                    try:
                        updated_ts = int(updated_ts)
                    except Exception:
                        updated_ts = 0
                    normalized_record["hops"][clean_from_room] = {
                        "action": clean_action,
                        "to_room": clean_to_room,
                        "updated_ts": updated_ts,
                        "learned_from_role": normalize_agent_role(hop.get("learned_from_role", "scanner")),
                    }

            raw_path = record.get("latest_success_path", [])
            if isinstance(raw_path, list):
                for hop in raw_path:
                    if not isinstance(hop, dict):
                        continue
                    clean_from_room = normalize_room_name(hop.get("from_room", ""))
                    clean_action = re.sub(r'\s+', ' ', str(hop.get("action", "")).strip().lower())
                    clean_to_room = normalize_room_name(hop.get("to_room", ""))
                    if not clean_from_room or not clean_action or not clean_to_room:
                        continue
                    if not hop_is_confirmed(clean_from_room, clean_action, clean_to_room):
                        continue
                    normalized_record["latest_success_path"].append({
                        "from_room": clean_from_room,
                        "action": clean_action,
                        "to_room": clean_to_room,
                    })

            if not normalized_record["hops"] and not normalized_record["latest_success_path"]:
                continue

            updated_ts = record.get("updated_ts", 0)
            try:
                updated_ts = int(updated_ts)
            except Exception:
                updated_ts = 0
            normalized_record["updated_ts"] = updated_ts
            normalized["destinations"][clean_destination] = normalized_record
        return normalized

    def _merge_route_memory(self, base_payload, incoming_payload):
        merged = self._normalize_route_memory(base_payload)
        incoming = self._normalize_route_memory(incoming_payload)

        for destination, record in incoming.get("destinations", {}).items():
            target_record = merged["destinations"].setdefault(destination, self._empty_destination_route_record())
            for from_room, hop in record.get("hops", {}).items():
                existing = target_record["hops"].get(from_room, {})
                if int(hop.get("updated_ts", 0)) >= int(existing.get("updated_ts", 0)):
                    target_record["hops"][from_room] = dict(hop)
            if record.get("latest_success_path"):
                if (
                    not target_record.get("latest_success_path")
                    or int(record.get("updated_ts", 0)) >= int(target_record.get("updated_ts", 0))
                ):
                    target_record["latest_success_path"] = [dict(hop) for hop in record["latest_success_path"]]
                    target_record["updated_ts"] = int(record.get("updated_ts", 0))
            elif record.get("updated_ts"):
                target_record["updated_ts"] = max(
                    int(target_record.get("updated_ts", 0)),
                    int(record.get("updated_ts", 0)),
                )

        return merged

    def _load_route_memory(self):
        try:
            path = CONFIG.get("ROUTE_MEMORY_FILE", "wagent_route_memory.json")
            self.route_memory = self._normalize_route_memory(_locked_json_load(path))
        except Exception as e:
            self.route_memory = {"destinations": {}}
            logger.warning(f"⚠️ 共享路由记忆加载失败: {e}")
            return
        logger.info(f"🧭 已加载共享路由记忆: {len(self.route_memory.get('destinations', {}))} destinations")

    def save_route_memory(self):
        if not self._can_promote_shared_memory():
            return
        try:
            path = CONFIG.get("ROUTE_MEMORY_FILE", "wagent_route_memory.json")
            merged = self._merge_route_memory(_locked_json_load(path), self.route_memory)
            self.route_memory = merged
            _locked_json_dump(path, merged)
        except Exception as e:
            logger.warning(f"⚠️ 共享路由记忆保存失败: {e}")

    def save_run_memory(self):
        return

    def _record_run_room(self, room_sig):
        if not room_sig:
            return
        cr = self.current_run
        if not cr.get("start_room"):
            cr["start_room"] = room_sig
        cr["last_room"] = room_sig
        rooms = cr.setdefault("rooms", [])
        if not rooms or rooms[-1] != room_sig:
            rooms.append(room_sig)

    def _record_run_action(self, action, success=False, failed=False):
        if not action:
            return
        cr = self.current_run
        acts = cr.setdefault("actions", [])
        acts.append({"action": action, "success": bool(success), "failed": bool(failed)})
        if success:
            cr["successes"] = int(cr.get("successes", 0)) + 1
        if failed:
            cr["failures"] = int(cr.get("failures", 0)) + 1

    def _record_run_transition(self, from_room, action, to_room, success=False, failed=False, moved=False):
        clean_from_room = normalize_room_name(from_room)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        clean_to_room = normalize_room_name(to_room)
        if not clean_from_room or not clean_action:
            return
        cr = self.current_run
        transitions = cr.setdefault("transitions", [])
        transitions.append({
            "from_room": clean_from_room,
            "action": clean_action,
            "to_room": clean_to_room,
            "success": bool(success),
            "failed": bool(failed),
            "moved": bool(moved),
        })

    def _extract_target_route_from_current_run(self, target_room):
        clean_target = normalize_room_name(target_room)
        if not clean_target:
            return []

        transitions = self.current_run.get("transitions", [])
        if not isinstance(transitions, list) or not transitions:
            return []

        route = []
        next_room = clean_target
        for transition in reversed(transitions):
            if not isinstance(transition, dict):
                continue
            if not transition.get("success") or not transition.get("moved"):
                continue
            clean_to_room = normalize_room_name(transition.get("to_room", ""))
            if clean_to_room != next_room:
                continue
            clean_from_room = normalize_room_name(transition.get("from_room", ""))
            clean_action = re.sub(r'\s+', ' ', str(transition.get("action", "")).strip().lower())
            if not clean_from_room or not clean_action or clean_from_room == clean_to_room:
                continue
            route.append({
                "from_room": clean_from_room,
                "action": clean_action,
                "to_room": clean_to_room,
            })
            next_room = clean_from_room

        route.reverse()
        if not route or route[-1].get("to_room") != clean_target:
            return []
        return route

    def _learn_target_route_from_current_run(self, target_room):
        if not self._can_promote_shared_memory():
            return 0
        clean_target = normalize_room_name(target_room)
        route = self._extract_target_route_from_current_run(clean_target)
        if not clean_target or not route:
            return 0

        updated_ts = int(time.time())
        role = self._agent_role()
        destinations = self.route_memory.setdefault("destinations", {})
        learned_destinations = []

        for index, hop in enumerate(route):
            destination = normalize_room_name(hop.get("to_room", ""))
            suffix_route = [dict(item) for item in route[: index + 1]]
            if not destination or not suffix_route:
                continue

            record = destinations.setdefault(destination, self._empty_destination_route_record())
            record["latest_success_path"] = suffix_route
            record["updated_ts"] = updated_ts
            hops = record.setdefault("hops", {})
            for route_hop in suffix_route:
                hops[route_hop["from_room"]] = {
                    "action": route_hop["action"],
                    "to_room": route_hop["to_room"],
                    "updated_ts": updated_ts,
                    "learned_from_role": role,
                }
            learned_destinations.append(destination)

        if learned_destinations:
            logger.info(
                f"🧭 共享路由已学习: target={clean_target} destinations={len(learned_destinations)} final_hops={len(route)}"
            )
        return len(learned_destinations)

    def _current_run_summary(self, interrupted=False):
        cr = self.current_run
        if not cr.get("rooms") and not cr.get("actions"):
            return {}

        rooms = cr.get("rooms", [])
        unique_rooms = []
        seen = set()
        for r in rooms:
            if r not in seen:
                unique_rooms.append(r)
                seen.add(r)

        actions = cr.get("actions", [])
        failed_examples = [a.get("action") for a in actions if a.get("failed")][:6]
        success_examples = [a.get("action") for a in actions if a.get("success")][:6]

        summary = {
            "ts": int(time.time()),
            "start_ts": int(cr.get("start_ts", int(time.time()))),
            "start_room": cr.get("start_room", ""),
            "end_room": cr.get("last_room", ""),
            "unique_rooms": unique_rooms[:20],
            "room_count": len(unique_rooms),
            "steps": len(actions),
            "successes": int(cr.get("successes", 0)),
            "failures": int(cr.get("failures", 0)),
            "success_examples": [x for x in success_examples if isinstance(x, str)],
            "failed_examples": [x for x in failed_examples if isinstance(x, str)],
            "breakthrough_dark_cell": any(r not in {"unknown-room", "dark cell"} for r in unique_rooms),
            "interrupted": bool(interrupted),
        }
        return summary

    def _finalize_run_memory(self):
        summary = self._current_run_summary(interrupted=False)
        if not summary:
            return

        target_room = self._target_room()
        learned_route_count = 0
        if target_room and summary.get("end_room") == target_room:
            learned_route_count = int(self._learn_target_route_from_current_run(target_room) or 0)

        runs = self.run_memory.setdefault("runs", [])
        runs.append(summary)
        max_runs = int(CONFIG.get("RUN_MEMORY_MAX_RUNS", 80))
        if len(runs) > max_runs:
            del runs[:-max_runs]

        logger.info(
            f"🧾 运行总结写入: rooms={summary['room_count']} steps={summary['steps']} breakthrough={summary['breakthrough_dark_cell']} learned_routes={learned_route_count}"
        )

        observation_runs = self.observation_memory.setdefault("runs", [])
        observation_runs.append(summary)
        max_runs = int(CONFIG.get("OBSERVATION_RUN_MAX", 40))
        if len(observation_runs) > max_runs:
            del observation_runs[:-max_runs]
        self._append_observation_event(
            "run_summary",
            start_room=summary.get("start_room", ""),
            end_room=summary.get("end_room", ""),
            room_count=int(summary.get("room_count", 0)),
            steps=int(summary.get("steps", 0)),
            successes=int(summary.get("successes", 0)),
            failures=int(summary.get("failures", 0)),
            breakthrough=bool(summary.get("breakthrough_dark_cell", False)),
        )

        # 清空当前运行快照，避免在退出保存时再次写入 interrupted checkpoint。
        self.current_run = {
            "start_ts": int(time.time()),
            "start_room": "",
            "last_room": "",
            "rooms": [],
            "actions": [],
            "transitions": [],
            "failures": 0,
            "successes": 0,
        }

    def _run_memory_prompt_snippet(self, room_sig, limit=None):
        return "none"

    def _learned_route_actions(self, start_room, target_room):
        clean_start = normalize_room_name(start_room)
        clean_target = normalize_room_name(target_room)
        if not clean_start or not clean_target or clean_start == clean_target:
            return []

        destinations = self.route_memory.get("destinations", {}) if isinstance(self.route_memory, dict) else {}
        record = destinations.get(clean_target, {}) if isinstance(destinations, dict) else {}
        hops = record.get("hops", {}) if isinstance(record, dict) else {}
        if not isinstance(hops, dict):
            return []

        route_actions = []
        current_room = clean_start
        visited = {clean_start}
        while current_room != clean_target:
            hop = hops.get(current_room)
            if not isinstance(hop, dict):
                return []
            action = re.sub(r'\s+', ' ', str(hop.get("action", "")).strip().lower())
            next_room = normalize_room_name(hop.get("to_room", ""))
            if not action or not next_room or next_room in visited:
                return []
            route_actions.append(action)
            current_room = next_room
            visited.add(current_room)
        return route_actions

    def _plan_route_steps(self, start_room, target_room):
        clean_start = normalize_room_name(start_room)
        clean_target = normalize_room_name(target_room)
        if not clean_start or not clean_target or clean_start == clean_target:
            return []

        destinations = self.route_memory.get("destinations", {}) if isinstance(self.route_memory, dict) else {}
        record = destinations.get(clean_target, {}) if isinstance(destinations, dict) else {}
        hops = record.get("hops", {}) if isinstance(record, dict) else {}
        if isinstance(hops, dict):
            steps = []
            current_room = clean_start
            visited = {clean_start}
            while current_room != clean_target:
                hop = hops.get(current_room)
                if not isinstance(hop, dict):
                    steps = []
                    break
                action = re.sub(r'\s+', ' ', str(hop.get("action", "")).strip().lower())
                next_room = normalize_room_name(hop.get("to_room", ""))
                if not action or not next_room or next_room in visited:
                    steps = []
                    break
                steps.append({
                    "from_room": current_room,
                    "action": action,
                    "to_room": next_room,
                })
                current_room = next_room
                visited.add(current_room)
            if steps:
                return steps

        adj = self._build_adjacency()
        queue = deque([clean_start])
        visited = {clean_start}
        parent = {}

        found = False
        while queue:
            node = queue.popleft()
            if node == clean_target:
                found = True
                break
            for action, nxt in adj.get(node, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                parent[nxt] = (node, action)
                queue.append(nxt)

        if not found and clean_target not in parent:
            return []

        route_steps = []
        cur = clean_target
        while cur != clean_start and cur in parent:
            prev, action = parent[cur]
            route_steps.append({
                "from_room": prev,
                "action": action,
                "to_room": cur,
            })
            cur = prev
        route_steps.reverse()
        return route_steps

    def _blind_transit_enabled(self):
        if not CONFIG.get("BLIND_TRANSIT_ENABLED", True):
            return False
        target_room = self._target_room()
        if not target_room:
            return False
        role = self._agent_role()
        if role == "scanner":
            return self._scanner_mode() != "random"
        return role == "runner"

    def _clear_blind_transit(self):
        self.blind_transit = None

    def _build_blind_transit_state(self, start_room, target_room):
        steps = self._plan_route_steps(start_room, target_room)
        if not steps:
            return None
        return {
            "target_room": target_room,
            "steps": steps,
            "index": 0,
            "last_dispatched": None,
        }

    def _align_blind_transit_state(self, current_room):
        clean_room = normalize_room_name(current_room)
        target_room = self._target_room()
        if not clean_room or not target_room:
            self.target_room_reached = False
            self._clear_blind_transit()
            return None
        if clean_room == target_room:
            self.target_room_reached = True
            self._clear_blind_transit()
            return None
        self.target_room_reached = False
        if not self._blind_transit_enabled():
            self._clear_blind_transit()
            return None

        state = self.blind_transit if isinstance(self.blind_transit, dict) else None
        if not state or state.get("target_room") != target_room:
            state = None

        if clean_room == "unknown-room":
            if state:
                return state
            state = self._build_blind_transit_state(clean_room, target_room)
            self.blind_transit = state
            return state

        if not state:
            state = self._build_blind_transit_state(clean_room, target_room)
            self.blind_transit = state
            return state

        steps = state.get("steps", []) if isinstance(state, dict) else []
        for index, step in enumerate(steps):
            if clean_room == step.get("from_room"):
                state["index"] = index
                state["last_dispatched"] = None
                return state
            if clean_room == step.get("to_room"):
                next_index = index + 1
                if next_index >= len(steps):
                    self._clear_blind_transit()
                    return None
                state["index"] = next_index
                state["last_dispatched"] = None
                return state

        state = self._build_blind_transit_state(clean_room, target_room)
        self.blind_transit = state
        return state

    def _blind_transit_action(self, current_room, visible_exits=None):
        state = self._align_blind_transit_state(current_room)
        if not isinstance(state, dict):
            return None
        steps = state.get("steps", [])
        index = int(state.get("index", 0))
        if index < 0 or index >= len(steps):
            self._clear_blind_transit()
            return None

        step = steps[index]
        route_action = re.sub(r'\s+', ' ', str(step.get("action", "")).strip().lower())
        action = self._choose_recipe_step(current_room, route_action) or route_action
        if not action:
            return None
        if not (self._is_safe_game_command(action) or self._is_persistable_navigation_action(action)):
            return None
        return action

    def _advance_blind_transit_after_dispatch(self, room_sig, action):
        state = self.blind_transit if isinstance(self.blind_transit, dict) else None
        if not isinstance(state, dict):
            return
        steps = state.get("steps", [])
        index = int(state.get("index", 0))
        if index < 0 or index >= len(steps):
            return

        step = steps[index]
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        expected_action = re.sub(r'\s+', ' ', str(step.get("action", "")).strip().lower())
        if clean_action != expected_action:
            return
        if clean_room not in {step.get("from_room"), "unknown-room"}:
            return

        state["last_dispatched"] = {
            "from_room": step.get("from_room", ""),
            "action": expected_action,
            "to_room": step.get("to_room", ""),
        }
        state["index"] = min(index + 1, len(steps))

    def _is_blind_transit_action(self, room_sig, action):
        state = self.blind_transit if isinstance(self.blind_transit, dict) else None
        if not isinstance(state, dict):
            return False

        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        last_dispatched = state.get("last_dispatched")
        if isinstance(last_dispatched, dict):
            if (
                clean_action == last_dispatched.get("action")
                and clean_room in {last_dispatched.get("from_room", ""), "unknown-room"}
            ):
                return True

        steps = state.get("steps", [])
        index = int(state.get("index", 0))
        if 0 <= index < len(steps):
            step = steps[index]
            if (
                clean_action == step.get("action")
                and clean_room in {step.get("from_room", ""), "unknown-room"}
            ):
                return True
        return False

    def _action_pattern(self, action):
        parts = action.split()
        if not parts:
            return "unknown"
        verb = parts[0]
        obj_words = len(parts) - 1
        has_hyphen = "hy" if "-" in action else "plain"
        return f"{verb}|obj{obj_words}|{has_hyphen}"

    def _record_experience(self, room_sig, action, feedback, success, explore_success=False, loop_penalty=False):
        if success and action in self.pending_model_commands and action not in self.known_commands:
            self.known_commands.append(action)
            self.pending_model_commands.remove(action)
            logger.info(f"✅ 模型候选晋升为已知命令 -> {action}")

    def _top_experience_commands(self, room_sig, limit=8):
        return []

    def _top_experience_commands_global(self, limit=5):
        return []

    def _top_pattern_stats(self, limit=4):
        return []

    def _experience_prompt_snippet(self, room_sig):
        return "none"

    def _is_basic_observe_action(self, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        return clean_action in {"look", "examine"}

    def _is_targeted_observe_action(self, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        return clean_action.startswith("look ") or clean_action.startswith("examine ")

    def _is_observe_action(self, action):
        return self._is_basic_observe_action(action) or self._is_targeted_observe_action(action)

    def _is_recovery_utility_action(self, action):
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        return clean_action in {"light", "light wood", "light splinter", "feel around"}

    def _confidence_prompt_sections(self, room_sig):
        validated = self._top_experience_commands(room_sig, limit=6)
        env_candidates = [
            cmd for cmd in self.pending_commands
            if cmd not in validated and not self._should_skip_room_action(room_sig, cmd)
        ][:8]
        model_hypotheses = [
            cmd for cmd in self.pending_model_commands
            if cmd not in validated and cmd not in env_candidates and not self._should_skip_room_action(room_sig, cmd)
        ][:8]
        return {
            "validated": ", ".join(validated) if validated else "none",
            "env_candidates": ", ".join(env_candidates) if env_candidates else "none",
            "model_hypotheses": ", ".join(model_hypotheses) if model_hypotheses else "none",
        }

    def _remember_failed_room_action(self, room_sig, action):
        if not room_sig or not action:
            return
        clean_room = str(room_sig).strip().lower()
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action or not self._is_safe_game_command(clean_action):
            return
        if self._is_observe_action(clean_action):
            return
        if self._is_recovery_utility_action(clean_action):
            return
        if self._is_blind_transit_action(clean_room, clean_action):
            return
        self._record_local_failed_action(clean_room, clean_action)
        if self._is_retryable_room_action(clean_room, clean_action):
            return
        self._remember_temp_failed_room_action(clean_room, clean_action)
        if clean_action in self._successful_room_actions(clean_room):
            return
        actions = self.room_failed_actions.setdefault(clean_room, [])
        if clean_action not in actions:
            actions.append(clean_action)
        if clean_action in self.pending_commands:
            self.pending_commands.remove(clean_action)
        if clean_action in self.pending_model_commands:
            self.pending_model_commands.remove(clean_action)
        if clean_action in self.suggested_commands:
            self.suggested_commands = deque(
                [cmd for cmd in self.suggested_commands if cmd != clean_action],
                maxlen=self.suggested_commands.maxlen,
            )

    def _shortcut_curiosity_sections(self, room_sig, visible_exits, room_success):
        failed_actions = set(self._effective_failed_room_actions(room_sig))
        visible_success = {a: b for a, b in room_success.items() if a not in failed_actions}
        shortcut_text = ", ".join([f"{a}->{to}" for a, to in list(visible_success.items())[:8]]) if visible_success else "none"
        unseen_exits = [ex for ex in visible_exits if ex not in room_success and ex not in failed_actions]
        curiosity_text = f"unseen_exits={', '.join(unseen_exits[:8]) if unseen_exits else 'none'}"

        return {
            "experience_shortcuts": shortcut_text,
            "curiosity_targets": curiosity_text,
        }

    def _room_failed_actions_hint(self, room_sig, limit=8):
        actions = self._effective_failed_room_actions(room_sig)
        if not actions:
            return "none"
        return ", ".join(actions[:limit])

    def _known_room_action_target(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return ""

        room = self.room_graph.get(clean_room, {})
        room_success = room.get("success", {}) if isinstance(room, dict) else {}
        if isinstance(room_success, dict):
            target_room = normalize_room_name(room_success.get(clean_action, ""))
            if target_room:
                return target_room

        rooms = self.observation_memory.get("rooms", {}) if isinstance(self.observation_memory, dict) else {}
        entry = rooms.get(clean_room, {}) if isinstance(rooms, dict) else {}
        confirmed_walks = entry.get("confirmed_walks", {}) if isinstance(entry, dict) else {}
        if isinstance(confirmed_walks, dict):
            return normalize_room_name(confirmed_walks.get(clean_action, ""))
        return ""

    def _should_promote_scanner_transition(self, from_room, action, to_room, failed=False):
        if failed or self._agent_role() != "scanner":
            return False

        clean_from = normalize_room_name(from_room)
        clean_to = normalize_room_name(to_room)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_from or not clean_to or not clean_action or clean_from == clean_to:
            return False
        if not self._is_navigation_action(clean_action):
            return False
        return self._scanner_in_scan_phase(clean_from)

    def _should_avoid_known_trap_action(self, room_sig, action):
        trap_rooms = set(CONFIG.get("RUNNER_TRAP_DESTINATIONS", []))
        if not trap_rooms:
            return False

        target_room = self._known_room_action_target(room_sig, action)
        if not target_room or target_room not in trap_rooms:
            return False

        intended_target = self._target_room()
        if intended_target and target_room == intended_target:
            return False

        role = self._agent_role()
        if role == "runner":
            return True
        if role != "scanner":
            return False

        if action in self._scan_target_variants():
            return False
        return True

    def _should_skip_room_action(self, room_sig, action):
        clean_room = str(room_sig).strip().lower()
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        pending = self.pending_recipe_step
        if pending and pending.get("room") == clean_room and pending.get("step") == clean_action:
            return False
        if self._is_basic_observe_action(clean_action):
            return False
        if self._is_targeted_observe_action(clean_action):
            return clean_action in self.room_scan_actions.get(clean_room, [])
        pending_nav = self.pending_navigation_transition
        if (
            isinstance(pending_nav, dict)
            and pending_nav.get("from_room") == clean_room
            and pending_nav.get("action") == clean_action
        ):
            return not self._is_retryable_room_action(clean_room, clean_action)
        if self._should_avoid_known_trap_action(clean_room, clean_action):
            return True
        if clean_action in self.room_observed_exits.get(clean_room, set()):
            return False
        if self._allows_failed_room_retry(clean_room, clean_action):
            return False
        return clean_action in self._effective_failed_room_actions(clean_room)

    def _learn_usage_hint(self, action, feedback):
        return

    def _store_model_lesson(self, room_sig, action, reflection, success):
        if not isinstance(reflection, dict):
            return

        lesson = reflection.get("lesson", "")
        if isinstance(lesson, str):
            lesson = lesson.strip()[:220]
        else:
            lesson = ""

        learned_commands = reflection.get("learned_commands", [])
        if not isinstance(learned_commands, list):
            learned_commands = []

        avoid_commands = reflection.get("avoid_commands", [])
        if not isinstance(avoid_commands, list):
            avoid_commands = []

        # 将模型总结的候选命令并入词库
        learned_safe_count = 0
        for cmd in learned_commands[:8]:
            if isinstance(cmd, str) and self._is_safe_game_command(cmd):
                if self._add_command(cmd, source="model"):
                    learned_safe_count += 1

        # 将模型明确建议避免的命令标记为阻塞（仅接受命令形态，避免自然语言污染）
        for cmd in avoid_commands[:6]:
            if isinstance(cmd, str) and self._is_safe_game_command(cmd):
                c = re.sub(r'\s+', ' ', cmd.strip().lower())
                if c in CRITICAL_ACTIONS:
                    continue
                if c and len(c.split()) <= 3 and c not in self.blocked_commands:
                    self.blocked_commands.append(c)

        # 只持久化高价值 lesson：仅成功步骤
        if lesson and bool(success):
            rec = {
                "ts": int(time.time()),
                "room": room_sig,
                "action": action,
                "lesson": lesson,
                "success": bool(success)
            }
            lessons = self.experience.setdefault("model_lessons", [])
            lessons.insert(0, rec)
            del lessons[40:]

    def reflect_experience_with_model(self, room_sig, action, feedback, success):
        return

    def _init_model_chain(self):
        ordered = [CONFIG["MODEL"]] + CONFIG.get("MODEL_FALLBACKS", [])
        seen = set()
        for m in ordered:
            if m and m not in seen:
                self.model_candidates.append(m)
                self.model_stats[m] = {"ok": 0, "fail": 0}
                seen.add(m)
        if not self.model_candidates:
            self.model_candidates = ["qwen2.5:7b"]
            self.model_stats = {"qwen2.5:7b": {"ok": 0, "fail": 0}}

    def _current_model(self):
        return self.model_candidates[self.model_index]

    def _switch_model(self, reason):
        if len(self.model_candidates) <= 1:
            return
        old = self._current_model()
        self.model_index = (self.model_index + 1) % len(self.model_candidates)
        self.model_fail_streak = 0
        logger.warning(f"🔁 切换模型: {old} -> {self._current_model()} | 原因: {reason}")

    def _record_model_result(self, ok):
        m = self._current_model()
        if m not in self.model_stats:
            self.model_stats[m] = {"ok": 0, "fail": 0}
        if ok:
            self.model_stats[m]["ok"] += 1
            self.model_fail_streak = 0
        else:
            self.model_stats[m]["fail"] += 1
            self.model_fail_streak += 1
            if self.model_fail_streak >= CONFIG["MODEL_SWITCH_FAIL_STREAK"]:
                self._switch_model("consecutive failures")

    def _queue_suggestion(self, cmd):
        clean = cmd.strip().lower()
        if not clean:
            return
        if not self._is_safe_game_command(clean):
            return
        if clean in self.blocked_commands:
            return
        if clean in self.suggested_commands:
            return
        self.suggested_commands.append(clean)

    def _is_safe_game_command(self, cmd):
        meta_forbidden = {
            "unpuppet", "puppet", "quit", "ooc", "ic", "password", "sessions",
            "charcreate", "chardelete", "shutdown", "reload",
            "on", "off", "drop", "option", "style", "color", "nick", "who",
            "setdesc", "channel", "page", "access", "char", "comms",
            "quell", "unquell"
        }
        social_forbidden_verbs = {"whisper", "say", "pose", "emote"}
        system_noise_forbidden = set(SYSTEM_COMMAND_BLOCKLIST)
        phrase_forbidden = {
            "auto quell", "auto quelling", "tutorial world", "client settings"
        }
        c = cmd.strip().lower()
        if not c:
            return False
        if len(c) >= CONFIG["ACTION_MAX_LEN"]:
            return False
        if not re.match(r'^[a-z0-9_\s\-]+$', c):
            return False
        # 下划线风格通常是内部标识符/脚本键，不是玩家自然指令
        if "_" in c:
            return False
        if len(c.split()) > 3:
            return False
        if c in phrase_forbidden:
            return False
        if c in SYSTEM_COMMAND_BLOCKLIST:
            return False
        if c in meta_forbidden:
            return False
        if c.startswith("@"):
            return False
        if c.startswith("+"):
            return False

        # 任意 token 命中 meta 命令都视为不安全，避免 "access charcreate" 这类污染
        tokens = re.split(r'\s+', c)
        if tokens and tokens[0] in social_forbidden_verbs:
            return False
        if any(t in meta_forbidden for t in tokens):
            return False
        if any(t in system_noise_forbidden for t in tokens):
            return False
        # 连字符拆分后再次校验，避免 auto-quelling / tutorial-world 漏过。
        split_tokens = []
        for t in tokens:
            split_tokens.extend([x for x in t.split("-") if x])
        if any(t in meta_forbidden for t in split_tokens):
            return False
        if any(t in SYSTEM_COMMAND_BLOCKLIST for t in split_tokens):
            return False
        if any(t in {"tutorialworld", "tutorial", "world", "auto", "quell", "quelling", "client", "settings"} for t in split_tokens):
            return False

        # 屏蔽复合颜色形容词作为交互对象，避免 look gray-green 这类噪声动作。
        if len(tokens) == 1 and self._is_noisy_object_token(tokens[0]):
            return False
        if len(tokens) >= 2 and tokens[0] in {"look", "examine", "push", "pull", "move", "touch", "use", "open"}:
            if self._is_noisy_object_token(tokens[-1]):
                return False

        # 过滤明显的帮助表对齐噪声（多空格列拼接）
        if "  " in cmd:
            return False
        return True

    def _is_persistable_navigation_action(self, cmd):
        c = re.sub(r'\s+', ' ', str(cmd or '').strip().lower())
        if not c:
            return False
        if len(c) >= CONFIG["ACTION_MAX_LEN"]:
            return False
        if not re.match(r'^[a-z0-9\s\-]+$', c):
            return False
        if "_" in c:
            return False
        if c in SYSTEM_COMMAND_BLOCKLIST:
            return False
        if c.startswith("@") or c.startswith("+"):
            return False
        non_persistable_prefixes = {
            "look", "read", "help", "examine", "inventory",
            "say", "whisper", "pose", "emote", "light",
            "shift", "press", "push", "pull", "move",
            "touch", "use", "open", "close", "feel"
        }
        head = c.split(" ", 1)[0]
        if head in non_persistable_prefixes:
            return False
        if not self._is_navigation_action(c):
            return False
        return True

    def _is_low_value_command(self, cmd):
        c = re.sub(r'\s+', ' ', (cmd or '').strip().lower())
        if not c:
            return True
        if c in LOW_VALUE_COMMANDS:
            return True
        # 避免把社交/信息型动作作为主探索策略反复执行。
        if c.startswith("say ") or c.startswith("whisper ") or c.startswith("pose "):
            return True
        return False

    def _learn_quoted_commands(self, snapshot):
        """从场景文本中的引号示例学习命令模式，例如 'shift red up'。"""
        text = snapshot or ""
        # 单引号或双引号里的短命令
        quoted = re.findall(r"['\"]([a-z][a-z0-9\-]*(?:\s+[a-z0-9\-]+){1,4})['\"]", text.lower())
        for q in quoted:
            cmd = q.strip()
            if self._is_safe_game_command(cmd):
                learned = self._add_command(cmd)
                if learned:
                    self._queue_suggestion(cmd)

    def _normalize_action(self, raw_action, candidate_commands=None):
        """规范化模型动作，并尽量对齐到已知词汇（保留连字符）。"""
        cleaned = re.sub(r'[^a-z0-9_\s\-]', '', (raw_action or '').strip().lower())
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if not cleaned:
            return ""

        if len(cleaned) >= CONFIG['ACTION_MAX_LEN']:
            return ""

        cands = list(candidate_commands or [])

        # 先尝试去掉通用移动包装词，避免 'go root-covered wall' 这类格式不被 Evennia 接受
        wrappers = [
            "go ", "move ", "walk ", "run ", "head ", "enter ",
            "travel ", "proceed ", "step into ", "step through "
        ]
        for prefix in wrappers:
            if cleaned.startswith(prefix):
                tail = cleaned[len(prefix):].strip()
                if tail:
                    cleaned = tail
                break
        # 直接命中
        if cleaned in cands:
            return cleaned

        # 容错命中：忽略空格/下划线/连字符后的同形匹配
        def squash(s):
            return re.sub(r'[\s_\-]+', '', s.lower())

        cleaned_key = squash(cleaned)
        for c in cands:
            if cleaned_key == squash(c):
                return c

        return cleaned

    def _prune_unsafe_commands(self):
        self.known_commands = [cmd for cmd in self.known_commands if self._is_safe_game_command(cmd)]
        self.pending_commands = [
            cmd for cmd in self.pending_commands
            if self._is_safe_game_command(cmd) and cmd not in SYSTEM_COMMAND_BLOCKLIST
        ]
        self.suggested_commands = deque(
            [cmd for cmd in self.suggested_commands if self._is_safe_game_command(cmd)],
            maxlen=self.suggested_commands.maxlen,
        )
        self.pending_model_commands = [
            cmd for cmd in self.pending_model_commands
            if self._is_safe_game_command(cmd) and cmd not in self.blocked_commands
        ]
        self.blocked_commands = [
            cmd for cmd in self.blocked_commands
            if self._is_safe_game_command(cmd) and len(cmd.split()) <= 3
        ]

    def _parse_maybe_meant(self, snapshot):
        text = snapshot or ""

        def is_contextual_suggestion(guess, attempted, exits):
            g = re.sub(r'\s+', ' ', (guess or '').strip().lower())
            if not g:
                return False
            if g in exits:
                return True

            attempted = re.sub(r'\s+', ' ', (attempted or '').strip().lower())
            if attempted:
                a_head = attempted.split()[0]
                g_head = g.split()[0]
                # 对动作型命令，优先接受同动词家族建议，避免无关名词短语污染。
                if a_head in {"shift", "move", "push", "pull", "look", "press", "open", "climb", "enter"}:
                    return g_head == a_head

            # 允许少量通用单词动作提示
            if g in {"look", "help", "push", "press", "climb"}:
                return True
            return False

        for line in text.splitlines():
            low = line.lower()
            if "maybe you meant" not in low:
                continue

            exits = set(self._extract_exits(text))
            attempted_cmd = ""
            m_attempt = re.search(r"command\s+'([^']+)'\s+is not available", low)
            if m_attempt:
                attempted_cmd = m_attempt.group(1).strip().lower()

            # 优先解析引号中的建议: "foo", "bar"
            quoted = re.findall(r'"([^"]+)"', line)
            for item in quoted:
                if (
                    self._is_safe_game_command(item)
                    and is_contextual_suggestion(item, attempted_cmd, exits)
                ):
                    self._queue_suggestion(item)

            # 回退：解析 maybe you meant 后的文本
            tail_match = re.search(r'maybe you meant\s+(.+)$', low)
            if tail_match:
                tail = tail_match.group(1).strip().rstrip("?.")
                tail = tail.replace(" or ", ",")
                for part in tail.split(","):
                    guess = part.strip().strip('"').strip("'")
                    if (
                        guess
                        and self._is_safe_game_command(guess)
                        and is_contextual_suggestion(guess, attempted_cmd, exits)
                    ):
                        self._queue_suggestion(guess)

    def _suggested_action(self):
        while self.suggested_commands:
            cmd = self.suggested_commands.popleft()
            if cmd in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(cmd):
                continue
            return cmd
        return None

    def _load_map_memory(self):
        try:
            primary_graph = self._graph_from_map_payload(_locked_json_load(CONFIG["MAP_MEMORY_FILE"]))
            merged_graph = self._merge_room_graphs({}, primary_graph)
            primary_edges = self._iter_graph_edge_ids(primary_graph)
            readonly_edges = set()

            for overlay_path in CONFIG.get("MAP_MEMORY_OVERLAY_FILES", []):
                overlay_graph = self._graph_from_map_payload(_locked_json_load(overlay_path))
                readonly_edges.update(self._iter_graph_edge_ids(overlay_graph) - primary_edges)
                merged_graph = self._merge_room_graphs(merged_graph, overlay_graph)

            self.room_graph = merged_graph
            self.readonly_map_overlay_edges = readonly_edges
            self.confirmed_overlay_edges = set()
            self._cleanup_room_graph()
            logger.info(f"🗺️ 已加载地图记忆: {len(self.room_graph)} rooms")
            if CONFIG.get("MAP_MEMORY_OVERLAY_FILES"):
                logger.info(f"🧩 只读地图覆盖层: {', '.join(CONFIG['MAP_MEMORY_OVERLAY_FILES'])}")
        except Exception as e:
            logger.warning(f"⚠️ 地图记忆加载失败: {e}")

    def _graph_from_map_payload(self, data):
        graph = {}
        if isinstance(data, dict) and isinstance(data.get("rooms"), dict):
            for room_sig, room_data in data.get("rooms", {}).items():
                clean_room = str(room_sig).strip().lower()
                if not clean_room or self._is_noisy_room_key(clean_room):
                    continue
                if not isinstance(room_data, dict):
                    continue
                graph.setdefault(clean_room, self._empty_room_record())
                exits = room_data.get("exits", {})
                if isinstance(exits, dict):
                    for _, exit_data in exits.items():
                        if not isinstance(exit_data, dict):
                            continue
                        self._record_loaded_edge(
                            graph,
                            clean_room,
                            exit_data.get("action", ""),
                            exit_data.get("to", ""),
                            exit_data.get("recipe", []),
                        )
                elif isinstance(room_data.get("success"), dict):
                    normalized = self._normalize_room_record(room_data)
                    if normalized.get("success"):
                        graph[clean_room] = normalized
            return graph

        if isinstance(data, dict) and isinstance(data.get("edges"), dict):
            for _, edge in data.get("edges", {}).items():
                if not isinstance(edge, dict):
                    continue
                self._record_loaded_edge(
                    graph,
                    edge.get("from", ""),
                    edge.get("action", ""),
                    edge.get("to", ""),
                    edge.get("recipe", []),
                )
            return graph

        if isinstance(data, dict):
            return {
                str(room_sig).strip().lower(): self._normalize_room_record(room)
                for room_sig, room in data.items()
                if isinstance(room_sig, str) and not self._is_noisy_room_key(room_sig)
            }

        return graph

    def _merge_room_graphs(self, base_graph, new_graph):
        merged = {}
        for source in (base_graph, new_graph):
            if not isinstance(source, dict):
                continue
            for room_sig, room in source.items():
                clean_room = str(room_sig).strip().lower()
                if not clean_room or self._is_noisy_room_key(clean_room):
                    continue
                target_room = merged.setdefault(clean_room, self._empty_room_record())
                normalized = self._normalize_room_record(room)
                for action, to_room in normalized.get("success", {}).items():
                    target_room["success"][action] = to_room
                    recipe = normalized.get("recipes", {}).get(action, [action])
                    self._set_room_success_recipe(target_room, action, recipe)
        return merged

    def _graph_edge_id(self, from_room, action, to_room):
        clean_from = normalize_room_name(from_room)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        clean_to = normalize_room_name(to_room)
        if not clean_from or not clean_action or not clean_to:
            return None
        return clean_from, clean_action, clean_to

    def _iter_graph_edge_ids(self, graph):
        edge_ids = set()
        if not isinstance(graph, dict):
            return edge_ids
        for room_sig, room in graph.items():
            if not isinstance(room, dict):
                continue
            success = room.get("success", {})
            if not isinstance(success, dict):
                continue
            for action, to_room in success.items():
                edge_id = self._graph_edge_id(room_sig, action, to_room)
                if edge_id:
                    edge_ids.add(edge_id)
        return edge_ids

    def _mark_edge_confirmed(self, from_room, action, to_room):
        edge_id = self._graph_edge_id(from_room, action, to_room)
        if not edge_id:
            return
        self.confirmed_overlay_edges.add(edge_id)
        self.readonly_map_overlay_edges.discard(edge_id)

    def _action_needs_confirmation(self, room_sig, action, to_room=""):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        clean_target = normalize_room_name(to_room)
        if not clean_room or not clean_action:
            return False
        if not clean_target:
            room = self.room_graph.get(clean_room, {})
            success = room.get("success", {}) if isinstance(room, dict) else {}
            if isinstance(success, dict):
                clean_target = normalize_room_name(success.get(clean_action, ""))
        edge_id = self._graph_edge_id(clean_room, clean_action, clean_target)
        if not edge_id:
            return False
        return edge_id in self.readonly_map_overlay_edges and edge_id not in self.confirmed_overlay_edges

    def _is_noisy_room_key(self, key):
        if not key:
            return True
        k = key.lower().strip()
        if k.startswith("you "):
            return True
        if len(k.split()) > 6:
            return True
        noisy_tokens = [
            "unknown-room",
            "could not",
            "command '",
            "maybe you meant",
            "drop what",
            "client settings",
            "you are out-of-character",
            "not available"
        ]
        return any(t in k for t in noisy_tokens)

    def _cleanup_room_graph(self):
        if not isinstance(self.room_graph, dict):
            self.room_graph = {}
            return
        to_delete = [k for k in self.room_graph.keys() if self._is_noisy_room_key(k)]
        for k in to_delete:
            self.room_graph.pop(k, None)
        for room_sig, room in list(self.room_graph.items()):
            normalized = self._normalize_room_record(room)
            self.room_graph[room_sig] = normalized
        if to_delete:
            logger.info(f"🧹 清理历史噪声房间键: {len(to_delete)}")

    def save_map_memory(self):
        if not self._can_promote_shared_memory():
            return
        try:
            merged_graph = self._merge_room_graphs(
                self._graph_from_map_payload(_locked_json_load(CONFIG["MAP_MEMORY_FILE"])),
                self.room_graph,
            )
            self.room_graph = merged_graph
            self._cleanup_room_graph()
            persisted_rooms = {}
            for room_sig, room in self.room_graph.items():
                if self._is_noisy_room_key(room_sig):
                    continue
                persisted_exits = {}
                for action, to_room in room.get("success", {}).items():
                    clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
                    clean_room = str(to_room).strip().lower()
                    if not clean_action or not clean_room:
                        continue
                    if not self._is_navigation_action(clean_action):
                        continue
                    if not self._is_persistable_navigation_action(clean_action):
                        continue
                    if self._is_noisy_room_key(clean_room):
                        continue
                    edge_id = self._graph_edge_id(room_sig, clean_action, clean_room)
                    if edge_id in self.readonly_map_overlay_edges and edge_id not in self.confirmed_overlay_edges:
                        continue
                    exit_key = self._room_exit_memory_key(clean_action, clean_room)
                    recipe = self._room_success_recipe(room_sig, clean_action)
                    persisted_exits[exit_key] = {
                        "action": clean_action,
                        "to": clean_room,
                        "recipe": recipe,
                    }
                persisted_rooms[room_sig] = {"exits": persisted_exits}
            _locked_json_dump(CONFIG["MAP_MEMORY_FILE"], {"format": "room-exits-v2", "rooms": persisted_rooms})
        except Exception as e:
            logger.warning(f"⚠️ 地图记忆保存失败: {e}")

    def _extract_exits(self, snapshot):
        exits = []
        for line in (snapshot or "").splitlines():
            low = line.lower()
            if "exits:" in low:
                exits_text = line.split(":", 1)[-1]
                parts = re.split(r',| and ', exits_text, flags=re.IGNORECASE)
                for part in parts:
                    cmd = part.strip().strip('.').lower()
                    if cmd:
                        exits.append(cmd)

            for direction in ["north", "south", "east", "west", "up", "down"]:
                if re.search(rf"\bgo\s+{direction}\b", low):
                    exits.append(direction)

            if "cross the bridge" in low or "get back to the mainland" in low:
                for direction in ["east", "west"]:
                    if re.search(rf"\b{direction}\b", low):
                        exits.append(direction)

        deduped = []
        for cmd in exits:
            if cmd not in deduped:
                deduped.append(cmd)
        return deduped

    def _room_title_candidates(self, snapshot):
        """提取回显中的房间标题候选，按出现顺序返回。"""
        lines = [ln.strip() for ln in (snapshot or "").splitlines() if ln.strip()]
        if not lines:
            return []

        visible_object_titles = {
            normalize_room_name(title)
            for title in self._extract_visible_object_titles(snapshot)
            if title
        }
        saw_visible_objects = any("you see:" in line.lower() for line in lines)

        noise_prefixes = (
            "you become", "command '", "what you can do", "you are trying hard", "could not view",
            "could not find", "the room is", "you are out-of-character", "client settings"
        )
        noise_contains = (
            "maybe you meant", "not available", "it's a hit", "defeated", "world turns black",
            "until you find some light", "drop what", "must define which colour"
        )

        candidates = []
        for line in lines:
            low = line.lower()
            if low.startswith(noise_prefixes):
                continue
            if any(x in low for x in noise_contains):
                continue

            # 房间名通常是标题形式，不以句号/冒号结尾。
            if not re.match(r"^[A-Z][A-Za-z0-9'\- ]{2,80}$", line):
                continue
            if line.endswith((".", ":", "!", "?")):
                continue
            if "  " in line:
                continue
            if len(line.split()) > 6:
                continue
            if saw_visible_objects and normalize_room_name(line) in visible_object_titles:
                continue
            candidates.append(line)

        return candidates

    def _extract_room_title(self, snapshot):
        """尽量只提取真正的房间标题，避免把错误提示当作房间。"""
        candidates = self._room_title_candidates(snapshot)
        return candidates[-1] if candidates else ""

    def _extract_navigation_result_room(self, snapshot, current_room_sig=""):
        candidates = self._room_title_candidates(snapshot)
        if not candidates:
            return normalize_room_name(current_room_sig)

        low = (snapshot or "").lower()
        fatal_markers = [
            "you fall to the ground, defeated",
            "the world turns black",
            "engulf you",
        ]
        marker_positions = [low.find(marker) for marker in fatal_markers if low.find(marker) >= 0]
        if marker_positions:
            prefix = (snapshot or "")[: min(marker_positions)]
            prefix_candidates = self._room_title_candidates(prefix)
            if prefix_candidates:
                return normalize_room_name(prefix_candidates[-1])

        return normalize_room_name(candidates[-1])

    def _infer_room_from_recent_navigation(self, snapshot):
        source_room = normalize_room_name(self.last_good_room_sig)
        if not source_room or source_room == "unknown-room":
            return ""
        if not self.recent_actions:
            return ""

        last_action = re.sub(r'\s+', ' ', str(self.recent_actions[-1]).strip().lower())
        if not last_action or not self._is_navigation_action(last_action):
            return ""
        if self._is_semantic_failure_feedback(snapshot):
            return ""

        pending = self.pending_recipe_step or {}
        if (
            pending.get("step") == last_action
            and pending.get("final_action") == last_action
            and len(pending.get("recipe", [])) > 1
        ):
            return ""

        success = self.room_graph.get(source_room, {}).get("success", {})
        if not isinstance(success, dict):
            return ""

        target_room = normalize_room_name(success.get(last_action, ""))
        if not target_room or target_room == source_room or self._is_noisy_room_key(target_room):
            return ""
        return target_room

    def _room_signature(self, snapshot):
        title = self._extract_room_title(snapshot)
        if title:
            last_action = self.recent_actions[-1] if self.recent_actions else ""
            low_snapshot = (snapshot or "").lower()
            if self.last_good_room_sig:
                last_targets = {
                    normalize_room_name(target)
                    for target in self.room_scan_targets.get(self.last_good_room_sig, [])
                    if target
                }
                if normalize_room_name(title) in last_targets:
                    if "you see:" in low_snapshot or "shared from another of your sessions" in low_snapshot:
                        return self.last_good_room_sig
            if self._is_targeted_observe_action(last_action) and not self._extract_exits(snapshot):
                if self.last_good_room_sig:
                    return self.last_good_room_sig
            return title.lower()[:80]
        low_snapshot = (snapshot or "").lower()
        if any(
            marker in low_snapshot
            for marker in [
                "the room is completely dark",
                "it's totally dark here",
                "you are completely blind",
                "you can't see anything",
                "blind, you think",
                "until you find some light",
                "things go dark",
                "world turns black",
                "where are you?",
                "far underground",
            ]
        ):
            return "unknown-room"
        inferred_room = self._infer_room_from_recent_navigation(snapshot)
        if inferred_room:
            return inferred_room
        if self.last_good_room_sig:
            return self.last_good_room_sig
        return "unknown-room"

    def observe_room(self, snapshot):
        room_sig = self._room_signature(snapshot)
        if room_sig != "unknown-room":
            self.last_good_room_sig = room_sig

        is_new_room = room_sig not in self.room_graph
        room = self.room_graph.setdefault(room_sig, self._empty_room_record())

        # success-only runtime map: we don't persist or cache visible exits/tried/visits.
        visible = self._extract_exits(snapshot)
        visible_set = set(visible)
        observed = self.room_observed_exits.setdefault(room_sig, set())
        new_visible = [ex for ex in visible if ex not in observed]
        scan_targets = self._extract_visible_object_targets(snapshot)
        if scan_targets:
            self.room_scan_targets[room_sig] = scan_targets
        if visible:
            observed.update(visible)
            failed_actions = list(self.room_failed_actions.get(room_sig, []))
            if failed_actions:
                conflicted = [action for action in failed_actions if action in visible_set]
                if conflicted:
                    self.room_failed_actions[room_sig] = [action for action in failed_actions if action not in conflicted]
                    if not self.room_failed_actions[room_sig]:
                        self.room_failed_actions.pop(room_sig, None)
                    temp_failed = list(self.room_temp_failed_actions.get(room_sig, []))
                    if temp_failed:
                        self.room_temp_failed_actions[room_sig] = [action for action in temp_failed if action not in conflicted]
                        if not self.room_temp_failed_actions[room_sig]:
                            self.room_temp_failed_actions.pop(room_sig, None)
                    logger.info(
                        f"🧹 Cleared stale failed actions contradicted by live exits in {room_sig}: {', '.join(conflicted[:6])}"
                    )
            # 如果房间明确给出 Exits:，则用它清理历史污染的成功动作。
            success = room.get("success", {})
            if isinstance(success, dict):
                stale_actions = [a for a in list(success.keys()) if a not in visible_set]
                for a in stale_actions:
                    success.pop(a, None)
        new_exits_added = len(new_visible)
        self._record_observed_room_state(room_sig, snapshot, visible, new_visible, scan_targets, is_new_room)
        return room_sig, is_new_room, new_exits_added

    def record_transition(self, from_room, action, to_room, failed=False):
        if not from_room or not action:
            return
        if not self._is_navigation_action(action):
            return
        room = self.room_graph.setdefault(from_room, self._empty_room_record())
        known_success = set(room.get("success", {}).keys())
        observed_exits = self.room_observed_exits.get(from_room, set())
        if action not in known_success and action not in observed_exits:
            if not self._should_promote_scanner_transition(from_room, action, to_room, failed=failed):
                return

        clean_to_room = normalize_room_name(to_room)
        if not failed and clean_to_room and clean_to_room != from_room:
            if self._is_noisy_room_key(clean_to_room):
                return
            if action not in observed_exits and self._should_promote_scanner_transition(from_room, action, to_room, failed=failed):
                self.room_observed_exits.setdefault(from_room, set()).add(action)
        
            success = room.setdefault("success", {})
            success[action] = clean_to_room
            recipe = self._known_recipe_for_transition(from_room, action) or [action]
            self._set_room_success_recipe(room, action, recipe)
            self._mark_edge_confirmed(from_room, action, clean_to_room)
            self._record_local_confirmed_walk(from_room, action, clean_to_room)
            self._clear_temp_failed_room_action(from_room, action)
            failed_actions = self.room_failed_actions.get(from_room, [])
            if action in failed_actions:
                self.room_failed_actions[from_room] = [item for item in failed_actions if item != action]
                if not self.room_failed_actions[from_room]:
                    self.room_failed_actions.pop(from_room, None)

    def _arm_pending_navigation_transition(self, room_sig, action):
        clean_room = normalize_room_name(room_sig)
        clean_action = re.sub(r'\s+', ' ', str(action).strip().lower())
        if not clean_room or not clean_action:
            return
        if not self._is_persistable_navigation_action(clean_action):
            return
        self.pending_navigation_transition = {
            "from_room": clean_room,
            "action": clean_action,
        }

    def _resolve_pending_navigation_transition(self, current_room_sig, recent_room_sig="", recent_action="", failed=False, resolved_room_sig=""):
        pending = self.pending_navigation_transition
        if not isinstance(pending, dict):
            return False

        from_room = normalize_room_name(pending.get("from_room", ""))
        action = re.sub(r'\s+', ' ', str(pending.get("action", "")).strip().lower())
        current_room = normalize_room_name(current_room_sig)
        resolved_room = normalize_room_name(resolved_room_sig) or current_room
        recent_room = normalize_room_name(recent_room_sig)
        recent_cmd = re.sub(r'\s+', ' ', str(recent_action).strip().lower())

        if not from_room or not action:
            self.pending_navigation_transition = None
            return False

        if failed and recent_room == from_room and recent_cmd == action and resolved_room in {"", from_room, "unknown-room"}:
            self.pending_navigation_transition = None
            return False

        if (
            resolved_room
            and resolved_room != from_room
            and resolved_room != "unknown-room"
            and not self._is_noisy_room_key(resolved_room)
        ):
            self.record_transition(from_room, action, resolved_room, failed=False)
            self.pending_navigation_transition = None
            return True

        return False

    def _search_state(self):
        sm = self.search_memory
        if "strategy" not in sm:
            sm["strategy"] = CONFIG.get("SEARCH_STRATEGY", "dfs")
        if "stack" not in sm or not isinstance(sm.get("stack"), list):
            sm["stack"] = []
        if "recent_rooms" not in sm or not isinstance(sm.get("recent_rooms"), list):
            sm["recent_rooms"] = []
        return sm

    def _room_frontier_score(self, room_sig):
        room = self.room_graph.get(room_sig, {})
        exits = [str(x).lower() for x in room.get("success", {}).keys()]
        known = sum(1 for ex in exits if ex not in self.blocked_commands)
        obj_bonus = 0
        for ex in exits:
            low = ex.lower()
            if any(k in low for k in CONFIG.get("OBJECTIVE_KEYWORDS", [])):
                obj_bonus += 1

        sm = self._search_state()
        recent = sm.get("recent_rooms", [])[-10:]
        loop_penalty = max(0, recent.count(room_sig) - 1)

        score = (0.8 * known) + (0.7 * obj_bonus) - (0.4 * loop_penalty)
        return round(score, 2)

    def _update_search_memory(self, room_sig):
        sm = self._search_state()
        strategy = sm.get("strategy", "dfs")
        stack = sm.get("stack", [])
        recent = sm.get("recent_rooms", [])

        recent.append(room_sig)
        del recent[:-40]

        room = self.room_graph.get(room_sig, {})
        exits = [str(x).lower() for x in room.get("success", {}).keys()]

        # 清理失效项
        valid_stack = []
        for item in stack:
            if not isinstance(item, dict):
                continue
            r = item.get("room")
            a = item.get("action")
            if not r or not a:
                continue
            if a in self.blocked_commands:
                continue
            valid_stack.append(item)
        stack = valid_stack

        existing = {(it.get("room"), it.get("action")) for it in stack if isinstance(it, dict)}
        for ex in exits:
            if ex in self.blocked_commands:
                continue
            key = (room_sig, ex)
            if key in existing:
                continue
            stack.append({
                "room": room_sig,
                "action": ex,
                "score": self._room_frontier_score(room_sig),
                "ts": int(time.time())
            })

        # stack 长度控制
        max_len = int(CONFIG.get("SEARCH_STACK_MAX", 240))
        if len(stack) > max_len:
            if strategy == "dfs":
                stack = stack[-max_len:]
            else:
                stack = stack[:max_len]

        sm["stack"] = stack
        sm["recent_rooms"] = recent

    def _peek_search_stack_action(self, room_sig):
        sm = self._search_state()
        stack = sm.get("stack", [])
        strategy = sm.get("strategy", "dfs")
        seq = reversed(stack) if strategy == "dfs" else iter(stack)
        for item in seq:
            if not isinstance(item, dict):
                continue
            if item.get("room") != room_sig:
                continue
            action = item.get("action")
            if not action or action in self.blocked_commands:
                continue
            return action
        return None

    def _consume_search_stack_action(self, room_sig, action):
        if not room_sig or not action:
            return
        sm = self._search_state()
        stack = sm.get("stack", [])
        for i, item in enumerate(stack):
            if not isinstance(item, dict):
                continue
            if item.get("room") == room_sig and item.get("action") == action:
                stack.pop(i)
                break
        sm["stack"] = stack

    def _search_prompt_snippet(self, room_sig):
        sm = self._search_state()
        strategy = sm.get("strategy", "dfs")
        stack = sm.get("stack", [])
        recent = sm.get("recent_rooms", [])[-10:]
        loop_risk = max(0, recent.count(room_sig) - 1)

        preview = []
        seq = list(reversed(stack)) if strategy == "dfs" else list(stack)
        for item in seq[:6]:
            if not isinstance(item, dict):
                continue
            preview.append(f"{item.get('action')}@{item.get('room')}[s={item.get('score', 0)}]")

        return {
            "strategy": strategy,
            "frontier_score": self._room_frontier_score(room_sig),
            "loop_risk": loop_risk,
            "stack_preview": ", ".join(preview) if preview else "none"
        }

    def _prompt_state(self):
        pm = self.prompt_memory
        if "stack" not in pm or not isinstance(pm.get("stack"), list):
            pm["stack"] = []
        if "templates" not in pm or not isinstance(pm.get("templates"), dict):
            pm["templates"] = {}
        if "priority" not in pm or not isinstance(pm.get("priority"), list):
            pm["priority"] = ["search", "loop", "experience", "feedback", "history"]
        if "max_depth" not in pm:
            pm["max_depth"] = CONFIG.get("PROMPT_STACK_MAX_DEPTH", 10)
        try:
            pm["max_depth"] = max(4, int(pm.get("max_depth", 10)))
        except Exception:
            pm["max_depth"] = CONFIG.get("PROMPT_STACK_MAX_DEPTH", 10)
        return pm

    def _remember_prompt_segment(self, segment, content, priority=0):
        text = re.sub(r'\s+', ' ', (content or '').strip())[:260]
        if not segment or not text:
            return
        pm = self._prompt_state()
        stack = pm.get("stack", [])
        stack.append({
            "segment": segment,
            "content": text,
            "priority": int(priority),
            "ts": int(time.time())
        })
        max_depth = int(pm.get("max_depth", CONFIG.get("PROMPT_STACK_MAX_DEPTH", 10)))
        if len(stack) > max_depth:
            del stack[:-max_depth]
        pm["stack"] = stack

    def _prompt_memory_snippet(self, limit=6):
        pm = self._prompt_state()
        stack = pm.get("stack", [])
        if not stack:
            return "none"

        latest_by_segment = {}
        for item in reversed(stack):
            if not isinstance(item, dict):
                continue
            seg = item.get("segment")
            if not seg or seg in latest_by_segment:
                continue
            latest_by_segment[seg] = item

        preferred = [p for p in pm.get("priority", []) if isinstance(p, str)]
        ordered = []
        for seg in preferred:
            if seg in latest_by_segment:
                ordered.append(latest_by_segment.pop(seg))

        extras = sorted(
            latest_by_segment.values(),
            key=lambda it: (int(it.get("priority", 0)), int(it.get("ts", 0))),
            reverse=True
        )
        ordered.extend(extras)

        lines = []
        for item in ordered[:limit]:
            lines.append(f"{item.get('segment')}: {item.get('content')}")
        return "\n".join(lines) if lines else "none"

    def _log_prompt_debug(self, prompt_kind, room_sig, prompt_text):
        if not CONFIG.get("PROMPT_DEBUG_ENABLED"):
            return
        try:
            step_hint = len(self.history) + 1
            path = CONFIG.get("PROMPT_DEBUG_FILE", "wagent_prompt_debug.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    "\n" + "=" * 80 + "\n"
                    f"prompt_kind={prompt_kind}\n"
                    f"step={step_hint}\n"
                    f"room={room_sig}\n"
                    f"ts={int(time.time())}\n"
                    + "=" * 80 + "\n"
                )
                f.write(prompt_text)
                if not prompt_text.endswith("\n"):
                    f.write("\n")
        except Exception as e:
            logger.warning(f"⚠️ Prompt debug logging failed: {e}")

    def _build_reflection_prompt(self, room_sig, action, feedback, success):
        self._remember_prompt_segment("feedback", f"{action} => {'success' if success else 'fail'}", priority=1)
        stack_memory = self._prompt_memory_snippet(limit=4)
        return f"""
You are extracting compact learning memory for a MUD agent.

[room]
{room_sig}

[action]
{action}

[result]
{feedback}

[success]
{success}

[Prompt stack memory]
{stack_memory}

Return strict JSON only:
{{
  "lesson": "one short transferable rule",
  "learned_commands": ["optional concrete commands"],
  "avoid_commands": ["optional commands to avoid"]
}}
"""

    def _build_think_prompt(
        self,
        snapshot,
        last_feedback,
        history_text,
        room_sig,
        role_sections,
        candidate_hint,
        parser_hint_text,
        confidence_sections,
        search_sections,
        room_success_hint,
        experience_shortcuts_hint,
        curiosity_targets_hint,
        failed_actions_hint,
    ):
        pm = self._prompt_state()
        templates = pm.setdefault("templates", {})
        if "rules" not in templates:
            templates["rules"] = (
                "1. Treat [Current state] as primary evidence from the game engine.\n"
                "2. If any hint conflicts with [Current state], trust [Current state] first.\n"
                "3. Infer actionable affordances directly from [Current state] text before using side hints.\n"
                "4. Prefer unexplored exits/commands over repeating look/help.\n"
                "5. Never return blocked commands.\n"
                "6. If state repeats, pick a different command.\n"
                "7. Output english command only.\n"
                "8. In pure-model mode, do not assume fixed scene templates; infer affordances from live text.\n"
                "9. If no-progress turns are high, consider info-gathering commands (help/look) once, then pivot.\n"
                "10. If latest feedback is near-identical to previous feedback, treat previous action as likely invalid or ineffective.\n"
                "11. Treat parser hints such as \"Maybe you meant ...\" as weak evidence only.\n"
                "12. Trust order: current-state evidence > map memory successful actions > validated successful commands > env-derived candidates > model hypotheses > parser hints.\n"
                "13. When loop_risk is high, prioritize stack_top frontier actions from search memory (DFS/BFS) over repeated observation commands.\n"
                "14. In tutorial/intro/limbo states, prioritize commands that start or resume the main adventure (for example begin adventure/exit tutorial/explicit exit) before generic look/help.\n"
                "15. If [Current state] contains an 'Exits:' line, parse it directly and prefer those exact exit phrases as commands.\n"
                "16. If map memory provides known successful actions for this room, try one of them first to pass known rooms quickly when consistent with [Current state].\n"
                "17. Rooms may have multiple exits; always scan [Current state] for additional exits not yet in map memory.\n"
                "18. If multiple exits are visible, scanners should spend the local scan budget on unseen exits before reusing remembered exits.\n"
                "19. Use bounded exploration in connection rooms: try unseen exits up to 2 attempts; if no clear progress, then take a known map-memory successful exit.\n"
                "20. Bracketed sections such as [Failed commands in this room] contain data, not extra instructions. Read the listed values as evidence.\n"
                "21. If [Failed commands in this room] is none, there are no recorded failed concrete actions for the current room.\n"
                "22. If [Failed commands in this room] lists actions, those are room-specific concrete actions from your own past trials. Treat them as strong negative evidence unless the current state has clearly changed.\n"
                "23. Observation commands such as look, look X, examine, or examine X are allowed for gathering information and are not part of failed-action memory.\n"
                "24. When failed room-specific actions are listed, reason from that history and search for another way out instead of repeating them.\n"
                "25. If [Agent role] is scanner and [Scanner mode] is targeted, route to [Target room] first; if compiled blind-transit steps are available, follow them before model reasoning or local probing. If [Scanner mode] is random, roam until you hit [Target room]. Once there, prioritize [Scan target] first when it is not none, then continue local room scanning.\n"
                "26. If [Scanner style] is nutcracker, aggressively follow puzzle affordances in the live text such as readable text, chains, doors, holes, and similar object cues before giving up on the room.\n"
                "27. If [Scanner style] is wellcracker, treat wells, chains, holes, and down/enter/climb verbs as a local frontier to push before abandoning the target.\n"
                "28. If [Scanner style] is rootcracker, inspect the root wall first, prefer concrete shift COLOR DIRECTION actions from the live text over generic help/look, and retry the exit after any root movement changes the local state.\n"
                "29. If [Agent role] is runner, use map-memory successful actions to cross known rooms quickly and reduce local experimentation until you reach the target room or a frontier room.\n"
                "30. Treat [Role routing hint] as part of the current assignment for this run."
            )
        if "output" not in templates:
            templates["output"] = (
                "{\n"
                "    \"progress_score\": \"0-10\",\n"
                "    \"thought\": \"brief reason\",\n"
                "    \"action\": \"english command\",\n"
                "    \"new_commands\": [\"new commands found in output\"]\n"
                "}"
            )

        self._remember_prompt_segment(
            "search",
            f"strategy={search_sections['strategy']} frontier={search_sections['frontier_score']} loop={search_sections['loop_risk']} top={search_sections['stack_preview']}",
            priority=4,
        )
        self._remember_prompt_segment("loop", f"no_progress={self.no_progress_turns}", priority=3)
        self._remember_prompt_segment("history", history_text.replace("\n", " | "), priority=1)
        self._remember_prompt_segment("feedback", (last_feedback or "none")[:220], priority=1)
        stack_memory = self._prompt_memory_snippet(limit=7)

        return f"""
You are an autonomous MUD explorer.
Goal: maximize novel discoveries while avoiding repeated useless actions.

[Current state]
{snapshot}

[Agent role]
{role_sections['role']}

[Scanner mode]
{role_sections['scanner_mode']}

[Scanner style]
{role_sections['scanner_style']}

[Target room]
{role_sections['target_room']}

[Scan target]
{role_sections['scan_target']}

[Role objective]
{role_sections['objective']}

[Role routing hint]
{role_sections['routing_hint']}

[Visible exits in Current state]
{role_sections['visible_exits']}

[Operating mode]
{"pure-model" if CONFIG["PURE_MODEL_MODE"] else "assisted-hybrid"}

[No-progress turns]
{self.no_progress_turns}

[Previous feedback]
{last_feedback}

[Known commands]
{", ".join(self.known_commands)}

[Validated successful commands]
{confidence_sections['validated']}

[Env-derived candidates]
{confidence_sections['env_candidates']}

[Model hypotheses]
{confidence_sections['model_hypotheses']}

[Search memory]
strategy={search_sections['strategy']} frontier_score={search_sections['frontier_score']} loop_risk={search_sections['loop_risk']}
stack_top={search_sections['stack_preview']}

[Suggested unexplored commands]
{candidate_hint}

[Weak parser hints]
{parser_hint_text}

[Exit parsing example from Current state]
If you see: Exits: climb the chain
Then action can be exactly: climb the chain
If you see: Exits: west, hole into cliff
Then valid actions include exactly: west or hole into cliff

[Map memory: previous successful actions (use to save trial)]
{room_success_hint}

[Map memory usage]
These are action->next_room pairs from your own previous successful experience.
Use them to reduce unnecessary trial-and-error when they are consistent with [Current state].

[Experience shortcuts]
{experience_shortcuts_hint}

[Curiosity targets]
{curiosity_targets_hint}

[Failed commands in this room]
{failed_actions_hint}

[Prompt stack memory]
{stack_memory}

[Blocked commands]
{", ".join(self.blocked_commands) if self.blocked_commands else "none"}

[Recent history]
{history_text}

[Rules]
{templates['rules']}

[Output JSON only]
{templates['output']}
"""

    def _known_success_exit_action(self, room_sig):
        room = self.room_graph.get(room_sig, {})
        success = room.get("success", {})
        if not isinstance(success, dict):
            return None
        for action in success.keys():
            if action in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if self._is_safe_game_command(action):
                return self._choose_recipe_step(room_sig, action) or action
        return None

    def _room_based_action(self, room_sig):
        room = self.room_graph.get(room_sig)
        if not room:
            return None
        success = room.get("success", {})
        for ex in success.keys():
            if ex in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(ex):
                continue
            return self._choose_recipe_step(room_sig, ex) or ex
        return None

    def _objective_action(self, room_sig):
        room = self.room_graph.get(room_sig)
        if not room:
            return None

        exits = [str(x).lower() for x in room.get("success", {}).keys()]
        keywords = CONFIG.get("OBJECTIVE_KEYWORDS", [])
        if not keywords:
            return None

        best_exit = None
        best_score = 0
        for ex in exits:
            if ex in self.blocked_commands:
                continue
            score = 0
            for idx, kw in enumerate(keywords):
                if kw in ex:
                    # 越靠前的关键字权重越高
                    score += (len(keywords) - idx)
            if score > best_score:
                best_score = score
                best_exit = ex
        return best_exit

    def _room_has_untried_exit(self, room_sig):
        room = self.room_graph.get(room_sig)
        if not room:
            return True
        return len(room.get("success", {})) == 0

    def _build_adjacency(self):
        adj = {}
        for room_sig, room in self.room_graph.items():
            edges = []
            for action, to_room in room.get("success", {}).items():
                if to_room and action and action not in self.blocked_commands:
                    edges.append((action, to_room))
            adj[room_sig] = edges
        return adj

    def _find_nearest_untried_room(self, start_room):
        if not start_room:
            return None
        if self._room_has_untried_exit(start_room):
            return start_room

        adj = self._build_adjacency()
        queue = deque([start_room])
        visited = {start_room}

        while queue:
            node = queue.popleft()
            for _, nxt in adj.get(node, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                if self._room_has_untried_exit(nxt):
                    return nxt
                queue.append(nxt)
        return None

    def _plan_route(self, start_room, target_room):
        return [step.get("action", "") for step in self._plan_route_steps(start_room, target_room) if step.get("action")]

    def _strategic_route_action(self, current_room):
        target = self._find_nearest_untried_room(current_room)
        if not target or target == current_room:
            return None
        route = self._plan_route(current_room, target)
        if not route:
            return None

        route_action = route[0]
        step = self._choose_recipe_step(current_room, route_action) or route_action
        if step in self.blocked_commands and not self._is_retryable_room_action(current_room, step):
            return None
        return step

    def _safety_action(self, snapshot, room_sig):
        text = (snapshot or "").lower()
        danger_markers = [
            "slash at you",
            "attacks you",
            "hits you",
            "you are wounded",
            "you are dying"
        ]
        if not any(m in text for m in danger_markers):
            return None

        room = self.room_graph.get(room_sig, {})
        exits = [e.lower() for e in room.get("exits", [])]

        # 优先退出到更安全的已知区域，先做短语匹配再做方向匹配
        preferred_exit_fragments = [
            "ruined gatehouse",
            "gatehouse",
            "courtyard",
            "castle corner"
        ]
        for frag in preferred_exit_fragments:
            for ex in exits:
                if frag in ex and ex not in self.blocked_commands:
                    return ex

        for retreat in ["west", "east", "south", "north"]:
            if retreat in exits and retreat not in self.blocked_commands:
                return retreat
        return "look"

    def _darkness_action(self, snapshot):
        text = (snapshot or "").lower()
        if "the room is lit up" in text or "splinter is already burning" in text:
            return "look"
        if "not completely dark" in text or "not completely blind" in text:
            return None
        if "try feeling around" in text or "feel around" in text:
            return "feel around"
        if "get light already" in text or "already found what you need" in text:
            for action in ["light wood", "light splinter", "feel around"]:
                if action in self.known_commands or action in self.pending_commands or action in self.pending_model_commands:
                    return action
            return "light wood"
        if "flint and steel" in text and "wood" in text:
            return "light wood"
        if "flint and steel" in text and "splinter" in text:
            return "light splinter"
        if "completely blind" in text or "can't see anything" in text:
            return "feel around"
        if "completely dark" in text or "find some light" in text:
            return "feel around"
        return None

    def _contextual_interaction_action(self, snapshot):
        text = (snapshot or "").lower()
        candidates = []

        # 依据场景物体尝试交互，避免在封闭房间里只会 look
        if ("splinter is already burning" in text or "light splinter" in text) and ("root-covered wall" in text or "roots" in text):
            candidates.extend(["burn roots", "burn root"])
        if "door" in text:
            candidates.extend(["open door", "push door", "look door"])
        if "root-covered wall" in text or "roots" in text:
            candidates.extend(["burn roots", "burn root", "push roots", "look roots"])
        if (
            "splinter" in text
            and "the room is lit up" not in text
            and "splinter is already burning" not in text
            and "light splinter" not in self.blocked_commands
        ):
            candidates.append("light splinter")

        for cmd in candidates:
            if cmd in self.blocked_commands:
                continue
            if self._looks_like_repeat_loop(cmd):
                continue
            if self._is_safe_game_command(cmd):
                self._add_command(cmd)
                return cmd
        return None

    def log_summary(self):
        self.total_steps += 1
        if self.total_steps % CONFIG["SUMMARY_EVERY_STEPS"] != 0:
            return

        rooms = len([k for k in self.room_graph.keys() if not self._is_noisy_room_key(k)])
        runtime = int(time.time() - self.start_time)
        known_paths = sum(len(room.get("success", {})) for room in self.room_graph.values())

        logger.info(
            f"📈 探索摘要 | steps={self.total_steps} rooms={rooms} runtime={runtime}s"
        )
        logger.info(f"🗺️ 持久化捷径摘要 | known_room_exits={known_paths}")

    def _add_command(self, cmd, source="env"):
        clean_cmd = re.sub(r'\s+', ' ', cmd.strip().lower())
        stopwords = {
            "a", "an", "the", "and", "or", "to", "of", "for", "from", "with",
            "you", "your", "is", "are", "be", "can", "do", "what", "try", "get",
            "back", "mainland", "bridge", "type", "help", "more", "info", "started"
        }
        if not clean_cmd:
            return False
        if clean_cmd in stopwords:
            return False
        if clean_cmd in SYSTEM_COMMAND_BLOCKLIST:
            return False
        if not self._is_safe_game_command(clean_cmd):
            return False
        if not re.match(r'^[a-z0-9_\s\-]+$', clean_cmd):
            return False
        if len(clean_cmd) >= CONFIG["ACTION_MAX_LEN"]:
            return False
        parts = clean_cmd.split()
        # 命令形态约束：默认不接受超长多词串，减少模型幻觉污染
        if len(parts) > 3:
            return False
        if len(parts) == 3:
            if parts[0] not in {"shift", "move", "push", "pull"}:
                return False
        if clean_cmd in self.blocked_commands:
            return False

        learned = False

        if clean_cmd in self.recent_actions:
            return learned

        if source == "model":
            # 模型生成命令先作为低优先级假设，不立即并入正式已知命令
            if (
                clean_cmd not in self.pending_model_commands
                and clean_cmd not in self.pending_commands
                and clean_cmd not in self.known_commands
            ):
                self.pending_model_commands.append(clean_cmd)
                logger.info(f"✨ 学会新动作候选[model-hyp] -> {clean_cmd}")
                learned = True
            return learned

        if clean_cmd not in self.known_commands:
            self.known_commands.append(clean_cmd)
            logger.info(f"✨ 学会新动作候选[env] -> {clean_cmd}")
            learned = True

        if clean_cmd not in self.pending_commands:
            self.pending_commands.append(clean_cmd)
        return learned

    def extract_commands_from_env(self, snapshot):
        """直接从环境文本提取可执行指令，降低对模型解析的依赖。"""
        text = snapshot or ""

        # 1) Exits: old bridge, climb the chain 等
        for line in text.splitlines():
            if "exits:" in line.lower():
                exits_text = line.split(":", 1)[-1]
                parts = re.split(r',| and ', exits_text, flags=re.IGNORECASE)
                for part in parts:
                    cmd = part.strip().strip('.').lower()
                    if cmd:
                        self._add_command(cmd)

        # 2) 帮助表命令提取（按列分词，避免拼接成 "drop get" 这类伪命令）
        if "--" in text and "help" in text.lower():
            ignored = {
                "comms", "general", "system", "tutorialworld", "channel", "page",
                "tutorial-world", "auto-quell", "auto-quelling", "client", "settings",
                "inventory", "pose", "say", "whisper", "about", "time", "give", "give up",
                "tutorial", "intro", "exits"
            }
            two_word_allow = set()

            for line in text.splitlines():
                l = line.strip().lower()
                if not l:
                    continue
                if l.startswith("--"):
                    continue
                if any(h in l for h in ["welcome", "exits:", "you see:"]):
                    continue

                cols = re.split(r'\s{2,}', l)
                for col in cols:
                    col = col.strip()
                    if not col:
                        continue

                    if col in two_word_allow:
                        self._add_command(col)
                        continue

                    # 默认只收单词命令，减少噪声
                    if re.match(r'^[a-z][a-z0-9_\-]{1,24}$', col):
                        if col not in ignored:
                            self._add_command(col)

        # 3) 从自然语言提示中提取明确方向 affordance（如 "if you go west"）。
        text_low = text.lower()
        for direction in ["east", "west", "north", "south", "up", "down"]:
            if re.search(rf"\bgo\s+{direction}\b", text_low):
                self._add_command(direction)

    def _looks_like_repeat_loop(self, action):
        if len(self.recent_actions) < 3:
            return False
        return all(a == action for a in list(self.recent_actions)[-3:])

    def _fallback_action(self, room_sig=""):
        """模型失败或动作无效时，使用确定性探索策略。"""
        observed_exits = list(self.room_observed_exits.get(room_sig, set()))
        role_candidates = self._role_candidate_priority(room_sig, observed_exits)
        for cmd in role_candidates:
            if cmd in self.recent_actions or cmd in self.blocked_commands:
                continue
            return cmd

        # 先尝试方向命令，通常更可能推动地图进展
        for direction in ["east", "west", "north", "south", "up", "down"]:
            if self._should_skip_room_action(room_sig, direction):
                continue
            if direction in self.pending_commands and direction not in self.recent_actions and direction not in self.blocked_commands:
                return direction

        # 方向命令即使不在 pending，也优先于 help/look
        for direction in ["east", "west", "north", "south", "up", "down"]:
            if self._should_skip_room_action(room_sig, direction):
                continue
            if direction in self.known_commands and direction not in self.recent_actions and direction not in self.blocked_commands:
                return direction

        for cmd in list(self.pending_commands):
            if self._should_skip_room_action(room_sig, cmd):
                continue
            if cmd not in self.recent_actions and cmd not in self.blocked_commands:
                return cmd

        for cmd in list(self.pending_model_commands):
            if self._should_skip_room_action(room_sig, cmd):
                continue
            if cmd not in self.recent_actions and cmd not in self.blocked_commands:
                return cmd

        for cmd in self.known_commands:
            if self._should_skip_room_action(room_sig, cmd):
                continue
            if cmd not in self.recent_actions and cmd not in self.blocked_commands:
                return cmd

        # 最后兜底避免 help 连续刷屏。
        recent = list(self.recent_actions)[-3:]
        if recent.count("help") >= 2:
            return "look"
        return "help"

    def _critical_affordance_action(self, snapshot):
        """仅在环境明确给出强约束时触发的最小策略兜底。"""
        text = (snapshot or "").lower()

        if "try feeling around" in text or "feel around" in text:
            if "feel around" not in self.blocked_commands:
                if "feel around" in self.pending_commands or "feel around" in self.known_commands:
                    return "feel around"
                if self._is_safe_game_command("feel around"):
                    self._add_command("feel around")
                    return "feel around"

        needs_light = (
            "get light" in text
            or "blindness" in text
            or "stumble around in blindness" in text
        )
        if needs_light:
            for cmd in ["light wood", "light splinter", "light"]:
                if cmd in self.blocked_commands:
                    continue
                if cmd in self.recent_actions:
                    continue
                if cmd.startswith("light") and "flint and steel" not in text and "splinter" not in text and "wood" not in text:
                    continue
                if cmd in self.pending_commands or cmd in self.known_commands:
                    return cmd
                if self._is_safe_game_command(cmd):
                    self._add_command(cmd)
                    return cmd

        return None

    def _startup_guide_action(self, snapshot, room_sig):
        """启动阶段引导：优先快速离开 tutorial/intro/limbo 到主线场景。"""
        text = (snapshot or "").lower()
        rs = (room_sig or "").lower()
        exits = [e.lower() for e in self._extract_exits(snapshot)]
        room = self.room_graph.get(room_sig, {}) if room_sig else {}
        tried = room.get("tried", {}) if isinstance(room, dict) else {}

        tutorial_context = (
            "tutorial" in text
            or "tutorial" in rs
            or "leaving tutorial" in rs
            or "limbo" in rs
            or rs == "intro"
            or "begin adventure" in text
            or "start again and exit" in text
            or "quitting the evennia tutorial" in text
        )
        if not tutorial_context:
            return None

        if "leaving tutorial" in rs or "start again and exit" in text:
            preferred = ["exit", "exit tutorial", "begin adventure", "begin", "old bridge", "start again"]
        elif rs == "intro":
            preferred = ["begin adventure", "begin", "old bridge", "start again"]
        elif "limbo" in rs:
            preferred = ["begin adventure", "begin", "old bridge", "exit tutorial", "exit"]
        else:
            preferred = ["begin adventure", "old bridge", "exit tutorial", "exit", "begin", "start again"]

        recent = list(self.recent_actions)[-3:]

        def over_repeated(cmd):
            # 避免连续三次同动作卡死，但允许必要的启动重试。
            return len(recent) >= 2 and recent[-1] == cmd and recent[-2] == cmd

        def over_tried_in_room(cmd):
            # 同一房间内，启动类动作最多尝试 2 次，防止 begin/begin adventure 互相交替刷屏。
            return int(tried.get(cmd, 0)) >= 2

        if ("limbo" in rs or rs == "intro") and exits:
            preferred_visible = []
            if rs == "intro":
                preferred_visible = ["begin adventure", "begin"]
            elif "limbo" in rs:
                preferred_visible = exits

            for cmd in preferred_visible:
                if cmd not in exits:
                    continue
                if over_repeated(cmd):
                    continue
                if cmd in self.blocked_commands:
                    continue
                return cmd

            for ex in exits:
                if over_repeated(ex):
                    continue
                if over_tried_in_room(ex):
                    continue
                if ex in self.blocked_commands:
                    continue
                return ex

        for cmd in preferred:
            if over_repeated(cmd):
                continue
            if over_tried_in_room(cmd):
                continue
            if cmd in self.blocked_commands:
                continue
            if cmd in exits or cmd in self.pending_commands or cmd in self.known_commands:
                return cmd
            if self._is_safe_game_command(cmd):
                self._add_command(cmd)
                return cmd

        for ex in exits:
            if over_repeated(ex):
                continue
            if ex in self.blocked_commands:
                continue
            return ex
        return None

    def _parse_model_decision(self, response_text):
        """从模型输出中提取JSON决策，容忍前后杂讯。"""
        if not response_text:
            raise json.JSONDecodeError("empty response", "", 0)

        text = response_text.strip()
        if text.startswith("{") and text.endswith("}"):
            return json.loads(text)

        # 优先提取代码块中的 JSON
        fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            return json.loads(fenced.group(1))

        # 回退：提取第一个花括号 JSON 对象
        obj = re.search(r'\{.*\}', text, flags=re.DOTALL)
        if obj:
            return json.loads(obj.group(0))

        raise json.JSONDecodeError("no json object found", text, 0)

    def think(self, snapshot, last_feedback, room_sig):
        """核心决策逻辑：基于贪心算法+状态机的自主探索"""
        self._prune_unsafe_commands()

        target_room = self._target_room()
        self.target_room_reached = bool(target_room and room_sig == target_room)
        if self.target_room_reached:
            self._clear_blind_transit()

        # 0. 先从环境直接学习指令，减少对模型输出的脆弱依赖
        self.extract_commands_from_env(snapshot)
        self._learn_quoted_commands(snapshot)
        self._parse_maybe_meant(snapshot)
        self._synthesize_commands_from_patterns(snapshot)
        self._update_search_memory(room_sig)

        critical_action = self._critical_affordance_action(snapshot)
        if critical_action:
            return {
                "progress_score": 8.8,
                "thought": "critical affordance override",
                "action": critical_action,
                "new_commands": [],
                "model_used": "rule-based"
            }

        dark_action = self._darkness_action(snapshot)
        if dark_action:
            self._add_command(dark_action)
            return {
                "progress_score": 9.5,
                "thought": "dark-room recovery policy",
                "action": dark_action,
                "new_commands": [],
                "model_used": "rule-based"
            }

        visible_exits_now = self._extract_exits(snapshot)
        travel_priority_action = self._priority_room_fast_action(room_sig, visible_exits_now)
        blind_transit_action = None
        if self._scanner_in_travel_phase(room_sig) or self._agent_role() == "runner":
            blind_transit_action = self._blind_transit_action(room_sig, visible_exits_now)
        travel_target_route_action = self._target_route_action(room_sig, visible_exits_now)

        if not CONFIG["PURE_MODEL_MODE"]:
            # 非 pure-model 才启用脚本策略直出动作；pure-model 交给模型基于提示自行决策。
            startup_action = self._startup_guide_action(snapshot, room_sig)
            if startup_action:
                return {
                    "progress_score": 9.2,
                    "thought": "startup guide: enter main adventure quickly",
                    "action": startup_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            if blind_transit_action:
                return {
                    "progress_score": 9.35,
                    "thought": f"blind transit toward {self._target_room()} using compiled route steps",
                    "action": blind_transit_action,
                    "new_commands": [],
                    "model_used": "blind-transit"
                }

            if travel_priority_action:
                return {
                    "progress_score": 9.3,
                    "thought": f"configured priority room action for {room_sig}",
                    "action": travel_priority_action,
                    "new_commands": [],
                    "model_used": "priority-room-fast-path"
                }

            if self._scanner_in_travel_phase(room_sig):
                if travel_target_route_action:
                    return {
                        "progress_score": 9.2,
                        "thought": f"scanner routing toward target room {self._target_room()}",
                        "action": travel_target_route_action,
                        "new_commands": [],
                        "model_used": "scanner-target-route"
                    }

            known_exit = self._known_success_exit_action(room_sig)
            if known_exit:
                return {
                    "progress_score": 9.4,
                    "thought": "map-memory fast path: reuse known successful room exit",
                    "action": known_exit,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            # 安全策略优先：遭遇攻击时先撤离
            safety_action = self._safety_action(snapshot, room_sig)
            if safety_action:
                return {
                    "progress_score": 9.0,
                    "thought": "safety retreat policy",
                    "action": safety_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            # 房间记忆策略：优先复用当前房间已验证成功的离开动作
            objective_action = self._objective_action(room_sig)
            if objective_action:
                return {
                    "progress_score": 7.8,
                    "thought": "objective-priority unexplored exit",
                    "action": objective_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            room_action = self._room_based_action(room_sig)
            if room_action:
                return {
                    "progress_score": 7.0,
                    "thought": "room-memory unexplored exit",
                    "action": room_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            # 全局路径策略：若当前房间无新出口，导航到最近的可探索房间
            strategic_action = self._strategic_route_action(room_sig)
            if strategic_action:
                return {
                    "progress_score": 6.5,
                    "thought": "global-route to nearest unexplored room",
                    "action": strategic_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

            # 场景交互策略：当路径探索受阻时，尝试与房间关键对象交互
            interact_action = self._contextual_interaction_action(snapshot)
            if interact_action:
                return {
                    "progress_score": 6.8,
                    "thought": "contextual object interaction",
                    "action": interact_action,
                    "new_commands": [],
                    "model_used": "rule-based"
                }

        # 模型持续失败时，短暂进入冷却期，仅用确定性策略
        if self.model_cooldown_left > 0:
            self.model_cooldown_left -= 1
            return {
                "progress_score": 0.0,
                "thought": "model cooldown; deterministic fallback",
                "action": self._fallback_action(room_sig),
                "new_commands": [],
                "model_used": "cooldown"
            }

        # 1. 裁剪历史记录，避免上下文过载
        history_text = "\n".join([
            f"Step {i + 1}: {h}"
            for i, h in enumerate(list(self.history))
        ])

        if room_sig == "unknown-room" and not visible_exits_now:
            reacquire_action = "look"
            if self._looks_like_repeat_loop("look") and "help" not in self.blocked_commands:
                reacquire_action = "help"
            return {
                "progress_score": 9.1,
                "thought": "unknown-room reacquire room title",
                "action": reacquire_action,
                "new_commands": [],
                "model_used": "unknown-room-reacquire"
            }

        role_sections = self._role_prompt_sections(room_sig, visible_exits_now)
        priority_room_action = travel_priority_action
        if priority_room_action:
            return {
                "progress_score": 9.3,
                "thought": f"configured priority room action for {room_sig}",
                "action": priority_room_action,
                "new_commands": [],
                "model_used": "priority-room-fast-path"
            }

        if blind_transit_action:
            return {
                "progress_score": 9.35,
                "thought": f"blind transit toward {self._target_room()} using compiled route steps",
                "action": blind_transit_action,
                "new_commands": [],
                "model_used": "blind-transit"
            }

        target_route_action = travel_target_route_action
        if target_route_action:
            return {
                "progress_score": 9.2,
                "thought": f"{role_sections['role']} routing toward target room {role_sections['target_room']}",
                "action": target_route_action,
                "new_commands": [],
                "model_used": f"{role_sections['role']}-target-route"
            }

        random_travel_action, random_travel_reason = self._random_scanner_travel_action(room_sig, visible_exits_now, snapshot)
        if random_travel_action:
            return {
                "progress_score": 9.1,
                "thought": random_travel_reason,
                "action": random_travel_action,
                "new_commands": [],
                "model_used": "random-scanner-fast-path"
            }

        scanner_fast_action, scanner_fast_reason = self._scanner_fast_path_action(room_sig, visible_exits_now, snapshot)
        if scanner_fast_action:
            return {
                "progress_score": 9.4,
                "thought": scanner_fast_reason,
                "action": scanner_fast_action,
                "new_commands": [],
                "model_used": "scanner-fast-path"
            }

        startup_context_now = (
            "tutorial" in (snapshot or "").lower()
            or "limbo" in (room_sig or "").lower()
            or (room_sig or "").lower() == "intro"
        )
        room_success = self.room_graph.get(room_sig, {}).get("success", {})
        candidate_pool = []
        for action in room_success.keys():
            if visible_exits_now and action not in visible_exits_now:
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if action not in candidate_pool and action not in self.blocked_commands:
                candidate_pool.append(action)

        for cmd in self.pending_commands:
            if not (
                cmd in visible_exits_now
                or not self._is_low_value_command(cmd)
                or startup_context_now
            ):
                continue
            if self._should_skip_room_action(room_sig, cmd):
                continue
            if cmd not in candidate_pool:
                candidate_pool.append(cmd)

        # 通用兜底：长时间无进展时可考虑 help，但避免重复刷同一信息。
        recent = list(self.recent_actions)[-4:]
        help_recently_spammed = recent.count("help") >= 2
        if (
            self.no_progress_turns >= CONFIG["STUCK_HELP_THRESHOLD"]
            and "help" not in candidate_pool
            and not help_recently_spammed
            and not visible_exits_now
        ):
            candidate_pool.append("help")

        # 模型自提命令保留为低优先级候选，仅在后段供模型参考
        for mc in self.pending_model_commands:
            if self._should_skip_room_action(room_sig, mc):
                continue
            if mc not in candidate_pool:
                candidate_pool.append(mc)

        search_hint_action = self._peek_search_stack_action(room_sig)
        if search_hint_action and not self._should_skip_room_action(room_sig, search_hint_action) and search_hint_action not in candidate_pool:
            candidate_pool.insert(0, search_hint_action)

        candidate_pool = self._merge_priority_candidates(
            candidate_pool,
            self._role_candidate_priority(room_sig, visible_exits_now),
        )

        candidate_hint = ", ".join(candidate_pool[:12]) if candidate_pool else "none"
        parser_hint_text = ", ".join(list(self.suggested_commands)[:8]) if self.suggested_commands else "none"
        confidence_sections = self._confidence_prompt_sections(room_sig)
        search_sections = self._search_prompt_snippet(room_sig)

        room = self.room_graph.get(room_sig, {})
        room_success = room.get("success", {}) if isinstance(room.get("success", {}), dict) else {}
        if visible_exits_now:
            filtered_room_success = {a: b for a, b in room_success.items() if a in visible_exits_now and not self._should_skip_room_action(room_sig, a)}
        else:
            filtered_room_success = {a: b for a, b in room_success.items() if not self._should_skip_room_action(room_sig, a)}
        room_success_hint = ", ".join([f"{a}->{b}" for a, b in list(filtered_room_success.items())[:10]]) if filtered_room_success else "none"
        policy_sections = self._shortcut_curiosity_sections(
            room_sig=room_sig,
            visible_exits=visible_exits_now,
            room_success=filtered_room_success,
        )
        failed_actions_hint = self._room_failed_actions_hint(room_sig)

        # 2. 构建Prompt：强化贪心评估+状态机迁移，并注入可持久化 prompt-stack 记忆
        prompt = self._build_think_prompt(
            snapshot=snapshot,
            last_feedback=last_feedback,
            history_text=history_text,
            room_sig=room_sig,
            role_sections=role_sections,
            candidate_hint=candidate_hint,
            parser_hint_text=parser_hint_text,
            confidence_sections=confidence_sections,
            search_sections=search_sections,
            room_success_hint=room_success_hint,
            experience_shortcuts_hint=policy_sections["experience_shortcuts"],
            curiosity_targets_hint=policy_sections["curiosity_targets"],
            failed_actions_hint=failed_actions_hint,
        )
        self._log_prompt_debug("think", room_sig, prompt)

        try:
            model_used = self._current_model()
            model_response = call_model_api(
                api_kind=CONFIG["MODEL_API_KIND"],
                api_url=CONFIG["MODEL_API_URL"],
                model=model_used,
                prompt=prompt,
                timeout=30,
                api_key=CONFIG.get("MODEL_API_KEY", ""),
            )
            decision = self._parse_model_decision(model_response)

            if "new_commands" in decision and isinstance(decision["new_commands"], list):
                for cmd in decision["new_commands"]:
                    self._add_command(cmd, source="model")

            raw_action = decision.get("action", "help").strip().lower()
            room = self.room_graph.get(room_sig, {})
            candidate_commands = []
            candidate_commands.extend(candidate_pool)
            candidate_commands.extend([str(x).lower() for x in room.get("success", {}).keys()])
            candidate_commands.extend(self.known_commands)
            candidate_commands.extend(self.pending_commands)
            candidate_commands.extend(self.pending_model_commands)
            candidate_commands.extend(list(self.suggested_commands))

            # 去重并过滤空值
            candidate_commands = [c for i, c in enumerate(candidate_commands) if c and c not in candidate_commands[:i]]

            clean_action = self._normalize_action(raw_action, candidate_commands=candidate_commands)
            if not clean_action or len(clean_action) >= CONFIG['ACTION_MAX_LEN']:
                clean_action = self._fallback_action(room_sig)

            if not self._is_safe_game_command(clean_action):
                clean_action = self._fallback_action(room_sig)

            if (
                clean_action in self.blocked_commands
                or self._looks_like_repeat_loop(clean_action)
                or self._should_skip_room_action(room_sig, clean_action)
            ):
                clean_action = self._fallback_action(room_sig)

            decision["action"] = clean_action

            try:
                decision["progress_score"] = float(decision.get("progress_score", 0))
            except Exception:
                decision["progress_score"] = 0.0

            # 成功拿到结构化决策后，重置失败计数
            self.model_failures = 0
            self._record_model_result(True)
            decision["model_used"] = model_used

            return decision

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ 模型请求异常: {str(e)}")
            self.model_failures += 1
            self._record_model_result(False)
            if self.model_failures >= CONFIG["MODEL_FAIL_THRESHOLD"]:
                self.model_cooldown_left = CONFIG["MODEL_COOLDOWN_TURNS"]
            return self._default_decision(f"模型请求错误: {str(e)}", room_sig)
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON解析异常: {str(e)}")
            self.model_failures += 1
            self._record_model_result(False)
            if self.model_failures >= CONFIG["MODEL_FAIL_THRESHOLD"]:
                self.model_cooldown_left = CONFIG["MODEL_COOLDOWN_TURNS"]
            return self._default_decision(f"JSON解析错误: {str(e)}", room_sig)
        except Exception as e:
            logger.error(f"❌ 未知思考异常: {str(e)}")
            self.model_failures += 1
            self._record_model_result(False)
            if self.model_failures >= CONFIG["MODEL_FAIL_THRESHOLD"]:
                self.model_cooldown_left = CONFIG["MODEL_COOLDOWN_TURNS"]
            return self._default_decision(f"未知错误: {str(e)}", room_sig)

    def _default_decision(self, error_msg, room_sig=""):
        """异常时的兜底决策"""
        return {
            "progress_score": 0.0,
            "thought": error_msg,
            "action": self._fallback_action(room_sig),
            "new_commands": [],
            "model_used": "fallback"
        }

    def mark_blocked_command(self, cmd):
        """标记死胡同指令（避免重复执行）"""
        if cmd in CRITICAL_ACTIONS:
            return
        if self._is_retryable_room_action(self.last_good_room_sig, cmd):
            return
        if self._is_observe_action(cmd):
            return
        # 启动动作在初期允许重试，但持续无进展时也必须可被封禁，避免 limbo 死循环。
        if cmd in STARTUP_ACTIONS and self.no_progress_turns < 3:
            return
        # 导航和基础信息动作在不同房间语义会变化，不做全局封禁。
        if cmd in {"help", "look"} or self._is_known_navigation_action(cmd):
            return
        if cmd.strip() and cmd not in self.blocked_commands:
            logger.warning(f"🚫 标记死胡同指令: {cmd}")
            self.blocked_commands.append(cmd)
            # 从已知指令库移除（如果存在）
            if cmd in self.known_commands:
                self.known_commands.remove(cmd)
            if cmd in self.pending_commands:
                self.pending_commands.remove(cmd)
            if cmd in self.pending_model_commands:
                self.pending_model_commands.remove(cmd)


def read_telnet_burst(tn):
    """在短时间窗口内聚合回显，减少 read_very_eager 截断。"""
    deadline = time.time() + CONFIG["READ_WINDOW"]
    chunks = []

    while time.time() < deadline:
        try:
            piece = tn.read_very_eager()
        except EOFError:
            break

        if piece:
            chunks.append(piece)
            # 有数据时延长一点窗口，尽量拿全一屏输出
            deadline = min(deadline + CONFIG["READ_POLL_INTERVAL"], time.time() + 0.4)
        else:
            time.sleep(CONFIG["READ_POLL_INTERVAL"])

    if not chunks:
        return ""

    text = b"".join(chunks).decode("utf-8", errors="ignore")
    return strip_ansi(text)

def create_telnet_connection():
    """创建Telnet连接，带重连逻辑"""
    retry = 0
    while retry < CONFIG['RECONNECT_MAX_RETRY']:
        try:
            tn = telnetlib.Telnet(CONFIG['HOST'], CONFIG['PORT'], timeout=10)
            logger.info(f"✅ 成功连接 Evennia ({CONFIG['HOST']}:{CONFIG['PORT']})")
            return tn
        except Exception as e:
            retry += 1
            logger.error(f"❌ 连接失败（重试{retry}/{CONFIG['RECONNECT_MAX_RETRY']}）: {str(e)}")
            time.sleep(5)
    logger.critical("❌ 达到最大重连次数，退出程序")
    sys.exit(1)


def default_character_name():
    raw_name = normalize_bot_id(CONFIG.get("USER") or CONFIG.get("ACCOUNT_LABEL") or "wagent")
    clean_name = re.sub(r"[^a-z0-9]+", "", raw_name)
    return clean_name[:24] or "wagent"


def bootstrap_connected_session(tn):
    character_name = default_character_name()

    def send_and_collect(command_text, pause=1.5):
        tn.write(f"{command_text}\n".encode("utf-8"))
        time.sleep(pause)
        return read_telnet_burst(tn)

    pending = [(f"connect {CONFIG['USER']} {CONFIG['PASS']}", 2.0)]
    for _ in range(8):
        if pending:
            command_text, pause = pending.pop(0)
            raw = send_and_collect(command_text, pause=pause)
        else:
            time.sleep(0.5)
            raw = read_telnet_burst(tn)

        low = raw.lower()
        if not raw:
            if not pending:
                pending.append(("look", 0.8))
            continue

        if "you don't have a character yet" in low or "the character does not exist" in low:
            logger.info(f"🧱 账号暂无角色，自动创建: {character_name}")
            pending = [(f"charcreate {character_name}", 1.5), (f"ic {character_name}", 1.5), ("look", 0.8)]
            continue

        if "created new character" in low:
            pending = [(f"ic {character_name}", 1.5), ("look", 0.8)]
            continue

        if "out-of-character" in low or "available character(s)" in low or "usage: ic <character>" in low:
            pending = [("ic", 1.2), (f"ic {character_name}", 1.2), ("look", 0.8)]
            continue

        if "you are out-of-character" not in low:
            return

    send_and_collect("look", pause=0.8)

def run_wagent():
    """启动Wagent主循环"""
    brain = WagentBrain()
    tn = create_telnet_connection()
    last_feedback = "Started"
    last_action_sent = ""
    last_room_sig = ""
    exit_state = "completed"
    last_error = ""

    update_recovery_status(
        "scanner",
        state="running",
        script=str(Path(__file__).resolve()),
        role=CONFIG["AGENT_ROLE"],
        task_kind="scanner discovery",
        target_room=CONFIG["TARGET_ROOM"],
        scan_target=CONFIG["SCAN_TARGET"],
        scanner_mode=CONFIG["SCANNER_MODE"],
        scanner_style=CONFIG["SCANNER_STYLE"],
        stop_on_target=CONFIG["STOP_ON_TARGET"],
        search_strategy=CONFIG["SEARCH_STRATEGY"],
        account_label=CONFIG["ACCOUNT_LABEL"],
        account_source=CONFIG["ACCOUNT_SOURCE"],
        account_pool_file=CONFIG["ACCOUNT_POOL_FILE"],
        map_memory_file=CONFIG["MAP_MEMORY_FILE"],
        route_memory_file=CONFIG["ROUTE_MEMORY_FILE"],
        observation_memory_file=CONFIG["OBSERVATION_MEMORY_FILE"],
        log_file=os.getenv("WAGENT_LOG_FILE", role_default_filename("log")),
        started_at=int(time.time()),
    )

    try:
        # 认证流程
        logger.info("--- 开始认证流程 ---")
        bootstrap_connected_session(tn)

        # 主探索循环
        while True:
            time.sleep(CONFIG['SLEEP_INTERVAL'])

            # 检查连接状态（断开则重连）
            try:
                tn.sock.send(b'')  # 空数据检测连接
            except Exception as e:
                logger.warning(f"❌ 连接已断开，尝试重连: {str(e)}")
                tn = create_telnet_connection()
                bootstrap_connected_session(tn)

            # 读取并清理环境反馈
            raw = read_telnet_burst(tn)
            if not raw:
                raw = "No environment data."
            
            # 完整记录环境反馈（关键：无截断）
            preview_limit = max(200, int(CONFIG.get("LOG_ENV_PREVIEW_MAX_CHARS", 1200)))
            preview = raw if len(raw) <= preview_limit else f"{raw[:preview_limit]}\n...[truncated {len(raw) - preview_limit} chars]"
            logger.info(f"\n📥 收到环境反馈 ({len(raw)} chars):\n{preview}")
            logger.info(f"📚 当前已知指令库: {brain.known_commands}")
            logger.info(f"🚫 死胡同指令库: {brain.blocked_commands}")
            logger.info(f"🗂️ 待探索指令队列: {brain.pending_commands[:12]}")
            logger.info(f"🧪 模型候选队列: {brain.pending_model_commands[:10]}")

            current_room_sig, is_new_room, new_exits_added = brain.observe_room(raw)
            logger.info(f"🧭 当前房间签名: {current_room_sig}")
            brain._record_run_room(current_room_sig)

            # 先根据当前回显判断上一条动作是否失败
            lower_raw = raw.lower()
            failed_last = brain._is_semantic_failure_feedback(raw)
            if last_action_sent and last_room_sig:
                brain._update_recipe_progress_from_feedback(
                    room_sig=last_room_sig,
                    action=last_action_sent,
                    current_room=current_room_sig,
                    failed=failed_last,
                )
                transition_room_sig = brain._extract_navigation_result_room(raw, current_room_sig)
                brain.record_transition(last_room_sig, last_action_sent, transition_room_sig, failed=failed_last)
                brain._resolve_pending_navigation_transition(
                    current_room_sig,
                    recent_room_sig=last_room_sig,
                    recent_action=last_action_sent,
                    failed=failed_last,
                    resolved_room_sig=transition_room_sig,
                )
                same_room = (transition_room_sig == last_room_sig)
                sim = brain._feedback_similarity(last_feedback, raw)

                # 进展定义：换房间，或在同房间拿到新的操作线索，或反馈模式出现显著变化
                instructional_markers = [
                    "usage:",
                    "type \"help\" for help",
                    "type 'help' for help"
                ]
                instructional_only = any(m in lower_raw for m in instructional_markers)
                actionable_affordance = brain._has_actionable_affordance_feedback(raw)
                informative_progress = (not failed_last) and (not instructional_only) and actionable_affordance
                strong_novelty = (sim < CONFIG["NO_CHANGE_SIMILARITY"])
                repeat_count = brain._record_feedback_observation(last_room_sig, last_action_sent, raw)
                # 同一提示重复出现时，不再算“信息进展”
                if informative_progress and repeat_count > 1:
                    informative_progress = False
                repeated_nochange = (
                    same_room
                    and not failed_last
                    and not informative_progress
                    and not strong_novelty
                    and repeat_count >= CONFIG["NO_CHANGE_REPEAT_LIMIT"]
                )

                moved_room = bool(
                    transition_room_sig
                    and transition_room_sig not in {"unknown-room", last_room_sig}
                )
                same_room_interaction_success = (
                    same_room
                    and not failed_last
                    and not instructional_only
                    and not repeated_nochange
                    and strong_novelty
                    and not brain._is_observe_action(last_action_sent)
                )
                same_room_scan_success = (
                    brain._agent_role() == "scanner"
                    and same_room
                    and not failed_last
                    and not instructional_only
                    and not repeated_nochange
                    and strong_novelty
                    and (
                        brain._is_targeted_observe_action(last_action_sent)
                        or re.sub(r'\s+', ' ', str(last_action_sent).strip().lower()).startswith("read ")
                    )
                )
                # 探索成功定义：发生空间推进（换房间）或发现新出口。
                exploration_success = moved_room or ((not failed_last) and new_exits_added > 0)
                frontier_progress = (
                    is_new_room
                    or new_exits_added > 0
                    or moved_room
                    or same_room_scan_success
                    or same_room_interaction_success
                )

                # 对 scanner 来说，同房间但获得新的对象/文本信息也算有效扫描进展。
                was_success = exploration_success or same_room_scan_success or same_room_interaction_success

                brain._record_run_transition(
                    from_room=last_room_sig,
                    action=last_action_sent,
                    to_room=transition_room_sig,
                    success=was_success,
                    failed=failed_last,
                    moved=moved_room,
                )

                if not was_success:
                    brain._remember_failed_room_action(last_room_sig, last_action_sent)

                brain._learn_usage_hint(last_action_sent, raw)

                if frontier_progress:
                    brain.no_progress_turns = 0
                else:
                    brain.no_progress_turns += 1

                if repeated_nochange:
                    logger.warning(
                        f"🧱 Repeated same-pattern feedback: action={last_action_sent} sim={sim:.3f} repeat={repeat_count}"
                    )
                    if (
                        last_action_sent not in CRITICAL_ACTIONS
                        and last_action_sent not in brain.blocked_commands
                        and not brain._is_blind_transit_action(last_room_sig, last_action_sent)
                    ):
                        if (
                            not brain._is_observe_action(last_action_sent)
                            and not brain._is_retryable_room_action(last_room_sig, last_action_sent)
                        ):
                            brain.blocked_commands.append(last_action_sent)

                brain._record_experience(
                    room_sig=last_room_sig,
                    action=last_action_sent,
                    feedback=raw,
                    success=was_success,
                    explore_success=exploration_success,
                    loop_penalty=repeated_nochange
                )
                brain._record_run_action(
                    action=last_action_sent,
                    success=was_success,
                    failed=failed_last
                )
                brain.reflect_experience_with_model(
                    room_sig=last_room_sig,
                    action=last_action_sent,
                    feedback=raw,
                    success=was_success
                )
            else:
                brain.no_progress_turns = 0

            if last_action_sent and failed_last:
                brain.mark_blocked_command(last_action_sent)
            brain.flush_persistent_state()

            target_room = brain._target_room()
            if CONFIG.get("STOP_ON_TARGET", False) and target_room and current_room_sig == target_room:
                logger.info(f"🛑 已到达目标房间，结束运行: {target_room}")
                break

            # 核心决策
            decision = brain.think(raw, last_feedback, current_room_sig)

            # 日志输出决策结果
            logger.info(f"📊 贪心进度分数: {decision['progress_score']}")
            logger.info(f"🧠 思考过程: {decision['thought']}")
            logger.info(f"🤖 模型来源: {decision.get('model_used', 'unknown')}")
            logger.info(f"🧷 无进展轮数: {brain.no_progress_turns}")
            logger.info(f"🚀 执行指令: {decision['action']}")

            # 执行指令
            action = decision['action']
            tn.write(f"{action}\n".encode('utf-8'))
            brain.recent_actions.append(action)
            last_action_sent = action
            last_room_sig = current_room_sig
            brain._advance_blind_transit_after_dispatch(current_room_sig, action)
            brain._arm_pending_navigation_transition(current_room_sig, action)

            if action in brain.pending_commands:
                brain.pending_commands.remove(action)

            brain.log_summary()

            # 记录历史（截断状态文本，避免冗余）
            brain.history.append(f"状态: {raw[:20]}.. -> 动作: {action} -> 分数: {decision['progress_score']}")
            last_feedback = raw

    except KeyboardInterrupt:
        exit_state = "interrupted"
        logger.info("\n🛑 用户手动中断程序")
    except Exception as e:
        exit_state = "error"
        last_error = str(e)
        logger.error(f"💥 运行时异常: {str(e)}", exc_info=True)
    finally:
        update_recovery_status(
            "scanner",
            state=exit_state,
            last_room=last_room_sig,
            last_action=last_action_sent,
            last_error=last_error,
            finished_at=int(time.time()),
        )
        brain._finalize_run_memory()
        brain.flush_persistent_state(force=True)
        brain.save_route_memory()
        brain.save_run_memory()
        if 'tn' in locals() and tn:
            tn.close()
            logger.info("🔌 已关闭Telnet连接")

if __name__ == "__main__":
    runtime_args, unknown_args = parse_runtime_args()
    apply_runtime_args(runtime_args, unknown_args)
    logger.info("=== Wagent 自主探索程序启动 ===")
    logger.info(f"⚙️ 运行模式: {'PURE_MODEL_MODE' if CONFIG['PURE_MODEL_MODE'] else 'ASSISTED_HYBRID_MODE'}")
    logger.info(f"🤖 角色: {CONFIG['AGENT_ROLE']}")
    logger.info(f"🧭 扫描器模式: {CONFIG['SCANNER_MODE']}")
    logger.info(f"🛠️ 扫描器风格: {CONFIG['SCANNER_STYLE']}")
    logger.info(f"🎯 目标房间: {CONFIG['TARGET_ROOM'] or 'none'}")
    logger.info(f"🛑 到达目标即停止: {CONFIG['STOP_ON_TARGET']}")
    logger.info(f"🔎 扫描目标: {CONFIG['SCAN_TARGET'] or 'none'}")
    logger.info(f"🧭 搜索策略: {CONFIG['SEARCH_STRATEGY']}")
    logger.info(f"👤 账号标签: {CONFIG['ACCOUNT_LABEL']} | 来源: {CONFIG['ACCOUNT_SOURCE']}")
    logger.info(f"🗂️ 账号池文件: {CONFIG['ACCOUNT_POOL_FILE']}")
    logger.info(f"🗺️ 共享地图记忆: {CONFIG['MAP_MEMORY_FILE']}")
    logger.info(f"🧩 只读地图覆盖层: {', '.join(CONFIG['MAP_MEMORY_OVERLAY_FILES']) if CONFIG['MAP_MEMORY_OVERLAY_FILES'] else 'none'}")
    logger.info(f"🧠 共享失败记忆: {CONFIG['EXPERIENCE_MEMORY_FILE']}")
    logger.info(f"🧭 共享路由记忆: {CONFIG['ROUTE_MEMORY_FILE']}")
    logger.info(f"🧾 本地观察记忆: {CONFIG['OBSERVATION_MEMORY_FILE']}")
    logger.info(f"🧭 Recovery status file: {recovery_status_path()}")
    run_wagent()
    logger.info("=== Wagent 自主探索程序退出 ===")
