import os
import runpy
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent

os.environ.setdefault("WAGENT_AGENT_ROLE", "scanner")
os.environ.setdefault("WAGENT_TARGET_ROOM", "dark cell")
os.environ.setdefault("WAGENT_SCAN_TARGET", "root-covered wall")
os.environ.setdefault("WAGENT_SCANNER_MODE", "targeted")
os.environ.setdefault("WAGENT_SCANNER_STYLE", "rootcracker")
os.environ.setdefault("WAGENT_MAP_MEMORY", str(WORKSPACE_ROOT / "wagent_map_memory.json"))
os.environ.setdefault("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
os.environ.setdefault(
    "WAGENT_PRIORITY_ROOM_ACTIONS",
    "protruding ledge:hole into cliff;"
    "underground passages:climb the chain;"
    "cliff by the coast:old bridge;"
    "the old bridge:east;"
    "corner of castle ruins:gatehouse;"
    "ruined gatehouse:standing archway;"
    "along inner wall:overgrown courtyard;"
    "overgrown courtyard:ruined temple;"
    "the ruined temple:stairs down;"
    "antechamber:blue bird tomb",
)
os.environ.setdefault(
    "WAGENT_UNSTABLE_RETRY_RULES",
    "the old bridge:east;"
    "antechamber:blue bird tomb;"
    "dark cell:root-covered wall,shift blue left,shift blue right,shift blue up,shift blue down,"
    "shift green up,shift green down,shift green left,shift green right,"
    "shift red left,shift red right,shift red up,shift red down,"
    "shift yellow up,shift yellow down,shift yellow left,shift yellow right,"
    "burn roots,burn root",
)
os.environ.setdefault("WAGENT_LOG_FILE", str(WORKSPACE_ROOT / "wagent_rootcracker_scanner.log"))
os.environ.setdefault("WAGENT_RUN_MEMORY", str(WORKSPACE_ROOT / "wagent_rootcracker_scanner_run_memory.json"))
os.environ.setdefault("WAGENT_PROMPT_LOG", str(WORKSPACE_ROOT / "wagent_rootcracker_scanner_prompt_debug.log"))

runpy.run_path(str(Path(__file__).with_name("scanner.py")), run_name="__main__")