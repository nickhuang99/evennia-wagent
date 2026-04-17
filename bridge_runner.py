import os
import runpy
from pathlib import Path


os.environ.setdefault("WAGENT_AGENT_ROLE", "runner")
os.environ.setdefault("WAGENT_TARGET_ROOM", "the old bridge")
os.environ.setdefault("WAGENT_PRIORITY_ROOM_ACTIONS", "the old bridge:east")
os.environ.setdefault("WAGENT_UNSTABLE_RETRY_RULES", "the old bridge:east")
os.environ.setdefault("WAGENT_LOG_FILE", "wagent_bridge_runner.log")
os.environ.setdefault("WAGENT_RUN_MEMORY", "wagent_bridge_runner_run_memory.json")
os.environ.setdefault("WAGENT_PROMPT_LOG", "wagent_bridge_runner_prompt_debug.log")

runpy.run_path(str(Path(__file__).with_name("scanner.py")), run_name="__main__")