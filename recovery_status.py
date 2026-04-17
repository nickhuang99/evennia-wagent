import fcntl
import json
import os
import tempfile
import time
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
RUNTIME_ARTIFACT_DIR = WORKSPACE_ROOT / "artifacts" / "current"
RECOVERY_STATUS_FILE = RUNTIME_ARTIFACT_DIR / "recovery_status.json"


def runtime_artifact_path(filename):
    return str((RUNTIME_ARTIFACT_DIR / filename).resolve())


def recovery_status_path():
    return str(RECOVERY_STATUS_FILE.resolve())


def _lock_path(path):
    return f"{path}.lock"


def _locked_json_load(path):
    lock_path = _lock_path(path)
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


def _locked_json_dump(path, payload):
    lock_path = _lock_path(path)
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


def _default_payload(existing=None):
    payload = existing if isinstance(existing, dict) else {}
    payload.setdefault("version", 1)
    payload.setdefault("workspace_root", str(WORKSPACE_ROOT.resolve()))
    payload.setdefault(
        "entrypoints",
        {
            "readme": str((WORKSPACE_ROOT / "README.md").resolve()),
            "recovery_playbook": str((WORKSPACE_ROOT / "docs" / "recovery-playbook.md").resolve()),
            "architecture": str((WORKSPACE_ROOT / "docs" / "architecture.md").resolve()),
            "development_principles": str((WORKSPACE_ROOT / "docs" / "development-principles.md").resolve()),
            "current_state": str((WORKSPACE_ROOT / "docs" / "current-state.md").resolve()),
            "artifact_guide": str((WORKSPACE_ROOT / "docs" / "artifact-layout.md").resolve()),
            "repo_handoff_memory": "/memories/repo/handoff-docs.md",
        },
    )
    payload.setdefault("artifacts", {})
    payload["artifacts"].setdefault("runtime_dir", str(RUNTIME_ARTIFACT_DIR.resolve()))
    payload.setdefault(
        "recovery_contract",
        {
            "goal": "A replacement model should be able to resume work and audit the last run from repository state alone.",
            "required_capabilities": [
                "recover active goal and roadmap",
                "identify last active script and task",
                "locate current logs, summaries, and observation memory",
                "reconstruct the last task before a crash",
                "review likely crash-risk code paths before resuming long runs",
            ],
        },
    )
    payload.setdefault(
        "forensics",
        {
            "suspicion_order": [
                "loop-time log volume",
                "orchestrator child-log duplication",
                "long-lived per-turn in-memory state",
            ]
        },
    )
    payload.setdefault("active_component", "none")
    payload.setdefault("components", {})
    return payload


def update_recovery_status(component, **fields):
    path = recovery_status_path()
    payload = _default_payload(_locked_json_load(path))
    timestamp = int(time.time())
    payload["updated_at"] = timestamp

    components = payload.setdefault("components", {})
    component_entry = components.get(component, {})
    component_entry.update(fields)
    component_entry["updated_at"] = timestamp
    components[component] = component_entry

    state = str(component_entry.get("state", "")).strip().lower()
    if state == "running":
        payload["active_component"] = component
    elif payload.get("active_component") == component and state in {"completed", "error", "interrupted", "stopped"}:
        payload["active_component"] = "none"

    _locked_json_dump(path, payload)
    return path