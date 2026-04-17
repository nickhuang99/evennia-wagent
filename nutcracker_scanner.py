import os
import runpy
from pathlib import Path


os.environ.setdefault("WAGENT_AGENT_ROLE", "scanner")
os.environ.setdefault("WAGENT_SCANNER_MODE", "targeted")
os.environ.setdefault("WAGENT_SCANNER_STYLE", "nutcracker")
os.environ.setdefault("WAGENT_LOG_FILE", "wagent_nutcracker_scanner.log")
os.environ.setdefault("WAGENT_RUN_MEMORY", "wagent_nutcracker_scanner_run_memory.json")
os.environ.setdefault("WAGENT_PROMPT_LOG", "wagent_nutcracker_scanner_prompt_debug.log")

runpy.run_path(str(Path(__file__).with_name("scanner.py")), run_name="__main__")