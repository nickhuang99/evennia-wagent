import importlib.util
import os
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parent
SCANNER_PATH = Path(__file__).with_name("scanner.py")
STOP_ACTION = "__STOP__"


os.environ.setdefault("WAGENT_AGENT_ROLE", "runner")
os.environ.setdefault("WAGENT_MAP_MEMORY", str(WORKSPACE_ROOT / "wagent_map_memory.json"))
os.environ.setdefault("WAGENT_EXPERIENCE_MEMORY", str(WORKSPACE_ROOT / "wagent_experience_memory.json"))
os.environ.setdefault("WAGENT_LOG_FILE", str(WORKSPACE_ROOT / "wagent_drifter_runner.log"))
os.environ.setdefault("WAGENT_RUN_MEMORY", str(WORKSPACE_ROOT / "wagent_drifter_runner_run_memory.json"))
os.environ.setdefault("WAGENT_PROMPT_LOG", str(WORKSPACE_ROOT / "wagent_drifter_runner_prompt_debug.log"))


def _load_scanner_module():
    spec = importlib.util.spec_from_file_location("wagent_drifter_base", SCANNER_PATH)
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


class DrifterRunnerBrain(base.WagentBrain):
    def _visible_exit_action(self, room_sig, snapshot):
        visible_exits = self._extract_exits(snapshot)
        for action in visible_exits:
            if action in self.blocked_commands:
                continue
            if self._should_skip_room_action(room_sig, action):
                continue
            if self._looks_like_repeat_loop(action):
                continue
            if self._action_failed_in_snapshot(action, snapshot):
                continue
            if self._is_safe_game_command(action):
                return action
        return None

    def think(self, snapshot, last_feedback, room_sig):
        if self._agent_role() != "runner":
            return super().think(snapshot, last_feedback, room_sig)

        explicit_action = self._visible_exit_action(room_sig, snapshot)
        if explicit_action:
            return _decision(
                9.4,
                f"drifter runner explicit visible exit in {room_sig}",
                explicit_action,
                "drifter-runner-explicit-exit",
            )

        visible_exits = self._extract_exits(snapshot)
        target_route_action = self._target_route_action(room_sig, visible_exits)
        if target_route_action:
            return _decision(
                9.1,
                f"drifter runner map-guided route from {room_sig}",
                target_route_action,
                "drifter-runner-route",
            )

        success_actions = self._usable_room_success_actions(room_sig)
        if success_actions:
            return _decision(
                8.9,
                f"drifter runner confirmed exit reuse in {room_sig}",
                success_actions[0],
                "drifter-runner-confirmed-exit",
            )

        strategic_action = self._strategic_route_action(room_sig)
        if strategic_action:
            return _decision(
                8.5,
                f"drifter runner frontier drift from {room_sig}",
                strategic_action,
                "drifter-runner-strategic-route",
            )

        return _decision(
            0.0,
            f"drifter runner stopping in {room_sig}: no explicit visible exit and no map-guided move",
            STOP_ACTION,
            "drifter-runner-stop",
        )


def run_drifter_runner():
    brain = DrifterRunnerBrain()
    tn = base.create_telnet_connection()
    last_feedback = "Started"
    last_action_sent = ""
    last_room_sig = ""

    try:
        base.logger.info("--- 开始认证流程 ---")
        tn.write(f"connect {base.CONFIG['USER']} {base.CONFIG['PASS']}\n".encode("utf-8"))
        base.time.sleep(2)
        tn.write(b"look\n")

        while True:
            base.time.sleep(base.CONFIG["SLEEP_INTERVAL"])

            try:
                tn.sock.send(b"")
            except Exception as e:
                base.logger.warning(f"❌ 连接已断开，尝试重连: {str(e)}")
                tn = base.create_telnet_connection()
                tn.write(f"connect {base.CONFIG['USER']} {base.CONFIG['PASS']}\n".encode("utf-8"))
                base.time.sleep(2)
                tn.write(b"look\n")

            raw = base.read_telnet_burst(tn)
            if not raw:
                raw = "No environment data."

            base.logger.info(f"\n📥 收到环境反馈:\n{raw}")
            base.logger.info(f"📚 当前已知指令库: {brain.known_commands}")
            base.logger.info(f"🚫 死胡同指令库: {brain.blocked_commands}")
            base.logger.info(f"🗂️ 待探索指令队列: {brain.pending_commands[:12]}")
            base.logger.info(f"🧪 模型候选队列: {brain.pending_model_commands[:10]}")

            current_room_sig, is_new_room, new_exits_added = brain.observe_room(raw)
            base.logger.info(f"🧭 当前房间签名: {current_room_sig}")
            brain._record_run_room(current_room_sig)

            lower_raw = raw.lower()
            failed_last = brain._is_semantic_failure_feedback(raw)
            if last_action_sent and last_room_sig:
                brain.record_transition(last_room_sig, last_action_sent, current_room_sig, failed=failed_last)
                same_room = current_room_sig == last_room_sig
                sim = brain._feedback_similarity(last_feedback, raw)

                instructional_markers = [
                    'usage:',
                    'type "help" for help',
                    "type 'help' for help",
                ]
                instructional_only = any(marker in lower_raw for marker in instructional_markers)
                actionable_affordance = brain._has_actionable_affordance_feedback(raw)
                informative_progress = (not failed_last) and (not instructional_only) and actionable_affordance
                strong_novelty = sim < base.CONFIG["NO_CHANGE_SIMILARITY"]
                repeat_count = brain._record_feedback_observation(last_room_sig, last_action_sent, raw)
                if informative_progress and repeat_count > 1:
                    informative_progress = False
                repeated_nochange = (
                    same_room
                    and not failed_last
                    and not informative_progress
                    and not strong_novelty
                    and repeat_count >= base.CONFIG["NO_CHANGE_REPEAT_LIMIT"]
                )

                moved_room = (not failed_last) and (not same_room)
                same_room_interaction_success = (
                    same_room
                    and not failed_last
                    and not instructional_only
                    and not repeated_nochange
                    and strong_novelty
                    and not brain._is_observe_action(last_action_sent)
                )
                same_room_scan_success = False
                exploration_success = (not failed_last) and (moved_room or new_exits_added > 0)
                frontier_progress = (
                    is_new_room
                    or new_exits_added > 0
                    or moved_room
                    or same_room_scan_success
                    or same_room_interaction_success
                )
                was_success = exploration_success or same_room_scan_success or same_room_interaction_success

                if not was_success:
                    brain._remember_failed_room_action(last_room_sig, last_action_sent)

                brain._learn_usage_hint(last_action_sent, raw)

                if frontier_progress:
                    brain.no_progress_turns = 0
                else:
                    brain.no_progress_turns += 1

                if repeated_nochange:
                    base.logger.warning(
                        f"🧱 Repeated same-pattern feedback: action={last_action_sent} sim={sim:.3f} repeat={repeat_count}"
                    )
                    if last_action_sent not in base.CRITICAL_ACTIONS and last_action_sent not in brain.blocked_commands:
                        if not brain._is_observe_action(last_action_sent):
                            brain.blocked_commands.append(last_action_sent)

                brain._record_experience(
                    room_sig=last_room_sig,
                    action=last_action_sent,
                    feedback=raw,
                    success=was_success,
                    explore_success=exploration_success,
                    loop_penalty=repeated_nochange,
                )
                brain._record_run_action(action=last_action_sent, success=was_success, failed=failed_last)
                brain.reflect_experience_with_model(
                    room_sig=last_room_sig,
                    action=last_action_sent,
                    feedback=raw,
                    success=was_success,
                )
            else:
                brain.no_progress_turns = 0

            if last_action_sent and failed_last:
                brain.mark_blocked_command(last_action_sent)
            brain.save_map_memory()
            brain.save_experience_memory()
            brain.save_run_memory()
            brain.save_observation_memory()

            decision = brain.think(raw, last_feedback, current_room_sig)
            base.logger.info(f"📊 贪心进度分数: {decision['progress_score']}")
            base.logger.info(f"🧠 思考过程: {decision['thought']}")
            base.logger.info(f"🤖 模型来源: {decision.get('model_used', 'unknown')}")
            base.logger.info(f"🧷 无进展轮数: {brain.no_progress_turns}")

            if decision["action"] == STOP_ACTION:
                base.logger.info("🛑 Drifter runner stopping cleanly")
                break

            base.logger.info(f"🚀 执行指令: {decision['action']}")
            action = decision["action"]
            tn.write(f"{action}\n".encode("utf-8"))
            brain.recent_actions.append(action)
            last_action_sent = action
            last_room_sig = current_room_sig

            if action in brain.pending_commands:
                brain.pending_commands.remove(action)

            brain.log_summary()
            brain.history.append(f"状态: {raw[:20]}.. -> 动作: {action} -> 分数: {decision['progress_score']}")
            last_feedback = raw

    except KeyboardInterrupt:
        base.logger.info("\n🛑 用户手动中断程序")
    except Exception as e:
        base.logger.error(f"💥 运行时异常: {str(e)}", exc_info=True)
    finally:
        brain._finalize_run_memory()
        brain.save_map_memory()
        brain.save_experience_memory()
        brain.save_run_memory()
        brain.save_observation_memory()
        if tn:
            tn.close()
            base.logger.info("🔌 已关闭Telnet连接")


if __name__ == "__main__":
    runtime_args, unknown_args = base.parse_runtime_args()
    base.apply_runtime_args(runtime_args, unknown_args)
    base.logger.info("=== Wagent Drifter Runner 启动 ===")
    base.logger.info(f"⚙️ 运行模式: {'PURE_MODEL_MODE' if base.CONFIG['PURE_MODEL_MODE'] else 'ASSISTED_HYBRID_MODE'}")
    base.logger.info(f"🤖 角色: {base.CONFIG['AGENT_ROLE']}")
    base.logger.info(f"🎯 目标房间: {base.CONFIG['TARGET_ROOM'] or 'none'}")
    base.logger.info(f"🧭 搜索策略: {base.CONFIG['SEARCH_STRATEGY']}")
    base.logger.info(f"🗺️ 共享地图记忆: {base.CONFIG['MAP_MEMORY_FILE']}")
    base.logger.info(f"🧠 共享失败记忆: {base.CONFIG['EXPERIENCE_MEMORY_FILE']}")
    base.logger.info(f"🧾 本地观察记忆: {base.CONFIG['OBSERVATION_MEMORY_FILE']}")
    run_drifter_runner()
    base.logger.info("=== Wagent Drifter Runner 退出 ===")