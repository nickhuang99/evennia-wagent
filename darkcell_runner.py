import os
import runpy
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent

os.environ.setdefault("WAGENT_AGENT_ROLE", "runner")
os.environ.setdefault("WAGENT_TARGET_ROOM", "dark cell")
os.environ.setdefault("WAGENT_MAP_MEMORY", str(WORKSPACE_ROOT / "wagent_map_memory.json"))
os.environ.setdefault("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
os.environ.setdefault("WAGENT_PRIORITY_ROOM_ACTIONS", "dark cell:root-covered wall")
os.environ.setdefault("WAGENT_UNSTABLE_RETRY_RULES", "dark cell:root-covered wall")
os.environ.setdefault("WAGENT_LOG_FILE", str(WORKSPACE_ROOT / "wagent_darkcell_runner.log"))
os.environ.setdefault("WAGENT_RUN_MEMORY", str(WORKSPACE_ROOT / "wagent_darkcell_runner_run_memory.json"))
os.environ.setdefault("WAGENT_PROMPT_LOG", str(WORKSPACE_ROOT / "wagent_darkcell_runner_prompt_debug.log"))

runpy.run_path(str(Path(__file__).with_name("scanner.py")), run_name="__main__")