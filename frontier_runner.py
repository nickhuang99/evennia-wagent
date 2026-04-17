import importlib.util
import os
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
SCANNER_PATH = Path(__file__).with_name("scanner.py")


os.environ.setdefault("WAGENT_AGENT_ROLE", "runner")
os.environ.setdefault("WAGENT_MAP_MEMORY", str(WORKSPACE_ROOT / "wagent_map_memory.json"))
os.environ.setdefault("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
os.environ.setdefault("WAGENT_LOG_FILE", str(WORKSPACE_ROOT / "wagent_frontier_runner.log"))
os.environ.setdefault("WAGENT_RUN_MEMORY", str(WORKSPACE_ROOT / "wagent_frontier_runner_run_memory.json"))
os.environ.setdefault("WAGENT_PROMPT_LOG", str(WORKSPACE_ROOT / "wagent_frontier_runner_prompt_debug.log"))


def _load_scanner_module():
    spec = importlib.util.spec_from_file_location("wagent_frontier_base", SCANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base = _load_scanner_module()


class FrontierRunnerBrain(base.WagentBrain):
    def think(self, snapshot, last_feedback, room_sig):
        if self._agent_role() == "runner":
            visible_exits = self._extract_exits(snapshot)

            target_route_action = self._target_route_action(room_sig, visible_exits)
            if target_route_action:
                return {
                    "progress_score": 9.25,
                    "thought": f"frontier runner routing toward target/frontier from {room_sig}",
                    "action": target_route_action,
                    "new_commands": [],
                    "model_used": "frontier-runner-route",
                }

            frontier_actions = self._visible_frontier_actions(room_sig, visible_exits)
            for action in frontier_actions:
                if self._action_failed_in_snapshot(action, snapshot):
                    continue
                return {
                    "progress_score": 9.35,
                    "thought": f"frontier runner explicit visible exit in {room_sig}",
                    "action": action,
                    "new_commands": [],
                    "model_used": "frontier-runner-explicit-exit",
                }

            success_actions = self._usable_room_success_actions(room_sig, visible_exits)
            if success_actions:
                return {
                    "progress_score": 8.95,
                    "thought": f"frontier runner confirmed exit reuse in {room_sig}",
                    "action": success_actions[0],
                    "new_commands": [],
                    "model_used": "frontier-runner-confirmed-exit",
                }

        return super().think(snapshot, last_feedback, room_sig)


base.WagentBrain = FrontierRunnerBrain


if __name__ == "__main__":
    runtime_args, unknown_args = base.parse_runtime_args()
    base.apply_runtime_args(runtime_args, unknown_args)
    base.logger.info("=== Wagent Frontier Runner 启动 ===")
    base.logger.info(f"⚙️ 运行模式: {'PURE_MODEL_MODE' if base.CONFIG['PURE_MODEL_MODE'] else 'ASSISTED_HYBRID_MODE'}")
    base.logger.info(f"🤖 角色: {base.CONFIG['AGENT_ROLE']}")
    base.logger.info(f"🎯 目标房间: {base.CONFIG['TARGET_ROOM'] or 'none'}")
    base.logger.info(f"🧭 搜索策略: {base.CONFIG['SEARCH_STRATEGY']}")
    base.logger.info(f"🗺️ 共享地图记忆: {base.CONFIG['MAP_MEMORY_FILE']}")
    base.logger.info(f"🧠 共享失败记忆: {base.CONFIG['EXPERIENCE_MEMORY_FILE']}")
    base.logger.info(f"🧾 本地观察记忆: {base.CONFIG['OBSERVATION_MEMORY_FILE']}")
    base.run_wagent()
    base.logger.info("=== Wagent Frontier Runner 退出 ===")