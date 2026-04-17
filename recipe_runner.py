import importlib.util
import os
import re
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
SCANNER_PATH = Path(__file__).with_name("scanner.py")


os.environ.setdefault("WAGENT_AGENT_ROLE", "runner")
os.environ.setdefault("WAGENT_MAP_MEMORY", str(WORKSPACE_ROOT / "wagent_map_memory.json"))
os.environ.setdefault("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
os.environ.setdefault("WAGENT_LOG_FILE", str(WORKSPACE_ROOT / "wagent_recipe_runner.log"))
os.environ.setdefault("WAGENT_RUN_MEMORY", str(WORKSPACE_ROOT / "wagent_recipe_runner_run_memory.json"))
os.environ.setdefault("WAGENT_PROMPT_LOG", str(WORKSPACE_ROOT / "wagent_recipe_runner_prompt_debug.log"))


def _load_scanner_module():
    spec = importlib.util.spec_from_file_location("wagent_recipe_base", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = _load_scanner_module()


def _decision(progress_score, thought, action, model_used):
    return {
        "progress_score": progress_score,
        "thought": thought,
        "action": action,
        "new_commands": [],
        "model_used": model_used,
    }


class RecipeRunnerBrain(base.WagentBrain):
    def __init__(self):
        super().__init__()
        self.active_recipe = None

    def _clear_active_recipe(self):
        self.active_recipe = None

    def _start_recipe(self, room_sig, final_action):
        clean_room = base.normalize_room_name(room_sig)
        clean_action = re.sub(r"\s+", " ", str(final_action).strip().lower())
        steps = self._room_success_recipe(clean_room, clean_action)
        if not steps:
            steps = [clean_action]
        self.active_recipe = {
            "room": clean_room,
            "final_action": clean_action,
            "steps": list(steps),
            "next_index": 0,
        }

    def _next_recipe_step(self):
        active = self.active_recipe
        if not active:
            return None
        idx = int(active.get("next_index", 0))
        steps = list(active.get("steps", []))
        if idx >= len(steps):
            return None
        action = steps[idx]
        active["next_index"] = idx + 1
        return action, idx + 1, len(steps)

    def _active_recipe_room(self):
        active = self.active_recipe or {}
        return base.normalize_room_name(active.get("room", ""))

    def _active_recipe_expected_action(self):
        active = self.active_recipe or {}
        idx = int(active.get("next_index", 0)) - 1
        steps = list(active.get("steps", []))
        if idx < 0 or idx >= len(steps):
            return ""
        return steps[idx]

    def _recipe_decision(self, room_sig, final_action, thought, model_used, progress_score):
        clean_room = base.normalize_room_name(room_sig)
        clean_action = re.sub(r"\s+", " ", str(final_action).strip().lower())

        active = self.active_recipe
        if not active or self._active_recipe_room() != clean_room or active.get("final_action") != clean_action:
            self._start_recipe(clean_room, clean_action)

        next_step = self._next_recipe_step()
        if not next_step:
            self._clear_active_recipe()
            return _decision(progress_score, f"{thought} but recipe was empty", clean_action, model_used)

        action, step_index, step_total = next_step
        return _decision(
            progress_score,
            f"{thought} | recipe {step_index}/{step_total} in {clean_room}",
            action,
            model_used,
        )

    def _recipe_action_matches_state(self, room_sig, final_action, visible_exits):
        clean_room = base.normalize_room_name(room_sig)
        clean_action = re.sub(r"\s+", " ", str(final_action).strip().lower())
        if not clean_action:
            return False
        if not visible_exits:
            return True
        if clean_action in visible_exits:
            return True
        recipe = self._room_success_recipe(clean_room, clean_action)
        return len(recipe) > 1

    def _recipe_target_route_action(self, room_sig, visible_exits):
        target_room = self._target_room()
        clean_room = base.normalize_room_name(room_sig)
        if not target_room or not clean_room or clean_room == target_room:
            return None

        route = self._plan_route(clean_room, target_room)
        if not route:
            return None

        final_action = route[0]
        if final_action in self.blocked_commands:
            return None
        if self._should_skip_room_action(clean_room, final_action):
            return None
        if self._looks_like_repeat_loop(final_action) and not self._allows_failed_room_retry(clean_room, final_action):
            return None
        if not self._recipe_action_matches_state(clean_room, final_action, visible_exits or []):
            return None
        return final_action

    def _recipe_success_actions(self, room_sig, visible_exits):
        clean_room = base.normalize_room_name(room_sig)
        room = self.room_graph.get(clean_room, {})
        success = room.get("success", {}) if isinstance(room, dict) else {}
        if not isinstance(success, dict):
            return []

        actions = []
        for action in success.keys():
            if not self._recipe_action_matches_state(clean_room, action, visible_exits or []):
                continue
            if action in self.blocked_commands:
                continue
            if self._should_skip_room_action(clean_room, action):
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if not self._is_safe_game_command(action):
                continue
            actions.append(action)
        return actions

    def record_transition(self, from_room, action, to_room, failed=False):
        super().record_transition(from_room, action, to_room, failed=failed)

        active = self.active_recipe
        if not active:
            return

        clean_from = base.normalize_room_name(from_room)
        clean_to = base.normalize_room_name(to_room)
        clean_action = re.sub(r"\s+", " ", str(action).strip().lower())
        expected_action = self._active_recipe_expected_action()
        steps = list(active.get("steps", []))
        final_action = active.get("final_action", "")

        if self._active_recipe_room() != clean_from:
            if failed or (clean_to and clean_to != clean_from):
                self._clear_active_recipe()
            return

        if expected_action and clean_action != expected_action:
            if failed or (clean_to and clean_to != clean_from):
                self._clear_active_recipe()
            return

        moved_room = bool(clean_to and clean_to != clean_from)
        final_step = bool(steps) and clean_action == steps[-1]

        if failed:
            self._clear_active_recipe()
            return

        if moved_room and final_step:
            room = self.room_graph.setdefault(clean_from, self._empty_room_record())
            room["success"][final_action] = clean_to
            self._set_room_success_recipe(room, final_action, steps)
            self._clear_temp_failed_room_action(clean_from, final_action)
            failed_actions = self.room_failed_actions.get(clean_from, [])
            if final_action in failed_actions:
                self.room_failed_actions[clean_from] = [item for item in failed_actions if item != final_action]
                if not self.room_failed_actions[clean_from]:
                    self.room_failed_actions.pop(clean_from, None)
            self._record_local_confirmed_walk(clean_from, final_action, clean_to)
            self._clear_active_recipe()
            return

        if moved_room and not final_step:
            self._clear_active_recipe()
            return

        if not moved_room and final_step:
            self._clear_active_recipe()

    def think(self, snapshot, last_feedback, room_sig):
        if self._agent_role() != "runner":
            return super().think(snapshot, last_feedback, room_sig)

        if self.active_recipe and self._active_recipe_room() == base.normalize_room_name(room_sig):
            next_step = self._next_recipe_step()
            if next_step:
                action, step_index, step_total = next_step
                return _decision(
                    9.45,
                    f"recipe runner continuing confirmed recipe in {room_sig} | recipe {step_index}/{step_total}",
                    action,
                    "recipe-runner-continue",
                )
            self._clear_active_recipe()

        visible_exits = self._extract_exits(snapshot)

        target_route_action = self._recipe_target_route_action(room_sig, visible_exits)
        if target_route_action:
            return self._recipe_decision(
                room_sig,
                target_route_action,
                f"recipe runner route toward target/frontier from {room_sig}",
                "recipe-runner-route",
                9.25,
            )

        success_actions = self._recipe_success_actions(room_sig, visible_exits)
        if success_actions:
            return self._recipe_decision(
                room_sig,
                success_actions[0],
                f"recipe runner confirmed exit reuse in {room_sig}",
                "recipe-runner-confirmed-exit",
                9.15,
            )

        frontier_actions = self._visible_frontier_actions(room_sig, visible_exits)
        for action in frontier_actions:
            if self._action_failed_in_snapshot(action, snapshot):
                continue
            return self._recipe_decision(
                room_sig,
                action,
                f"recipe runner explicit visible frontier exit in {room_sig}",
                "recipe-runner-explicit-frontier",
                9.05,
            )

        return super().think(snapshot, last_feedback, room_sig)


base.WagentBrain = RecipeRunnerBrain


if __name__ == "__main__":
    runtime_args, unknown_args = base.parse_runtime_args()
    base.apply_runtime_args(runtime_args, unknown_args)
    base.logger.info("=== Wagent Recipe Runner 启动 ===")
    base.logger.info(f"⚙️ 运行模式: {'PURE_MODEL_MODE' if base.CONFIG['PURE_MODEL_MODE'] else 'ASSISTED_HYBRID_MODE'}")
    base.logger.info(f"🤖 角色: {base.CONFIG['AGENT_ROLE']}")
    base.logger.info(f"🎯 目标房间: {base.CONFIG['TARGET_ROOM'] or 'none'}")
    base.logger.info(f"🧭 搜索策略: {base.CONFIG['SEARCH_STRATEGY']}")
    base.logger.info(f"🗺️ 共享地图记忆: {base.CONFIG['MAP_MEMORY_FILE']}")
    base.logger.info(f"🧠 共享失败记忆: {base.CONFIG['EXPERIENCE_MEMORY_FILE']}")
    base.logger.info(f"🧾 本地观察记忆: {base.CONFIG['OBSERVATION_MEMORY_FILE']}")
    base.run_wagent()
    base.logger.info("=== Wagent Recipe Runner 退出 ===")