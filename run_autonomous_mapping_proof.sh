#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python"
PROOF_NAME="cold_start_proof_$(date +%Y%m%d_%H%M%S)"
GAME_DIR_INPUT="${WAGENT_GAME_DIR:-${SCRIPT_DIR}/mygame}"
ACCOUNT_POOL_FILE="${WAGENT_ACCOUNT_POOL_FILE:-wagent_account_pool.local.json}"
ACCOUNT_POOL_COUNT="${WAGENT_ACCOUNT_POOL_COUNT:-2}"
ACCOUNT_POOL_HOST="${WAGENT_EVENNIA_HOST:-127.0.0.1}"
ACCOUNT_POOL_PORT="${WAGENT_EVENNIA_PORT:-4000}"
RUNNER_TARGET_ROOM=""
SCANNER_TARGET_ROOM=""
MAX_PHASES="12"
RUNNER_SECONDS="180"
SCANNER_SECONDS="120"
RUNNER_MAX_ACTIONS="40"
SCANNER_MAX_ACTIONS="18"
RUNNER_STUCK_TURNS="5"
SCANNER_STUCK_TURNS="6"

usage() {
    cat <<'EOF'
Usage:
    ./run_autonomous_mapping_proof.sh [options]

Purpose:
  Run an isolated cold-start proof where shared map truth, route memory, and
  experience memory all start empty. This demonstrates autonomous map growth,
  not replay on the repository's baseline shared map.

Optional:
    --account-pool-file PATH     Account pool JSON path. Defaults to
                                                             WAGENT_ACCOUNT_POOL_FILE or
                                                             ./wagent_account_pool.local.json
    --proof-name NAME            Output folder name under artifacts/current/
  --runner-target-room ROOM    Initial runner target room
  --scanner-target-room ROOM   Fallback scanner target room
  --max-phases N               Orchestrator max phases (default: 12)
  --runner-seconds N           Runner slice timeout in seconds (default: 180)
  --scanner-seconds N          Scanner slice timeout in seconds (default: 120)
  --runner-max-actions N       Runner slice action cap (default: 40)
  --scanner-max-actions N      Scanner slice action cap (default: 18)
  --runner-stuck-turns N       Runner no-progress threshold (default: 5)
  --scanner-stuck-turns N      Scanner no-progress threshold (default: 6)
  --help                       Show this message

Example:
  ./run_autonomous_mapping_proof.sh \
    --runner-target-room "corner of castle ruins" \
    --scanner-target-room "corner of castle ruins" \
    --proof-name obelisk_cold_start
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --account-pool-file)
            ACCOUNT_POOL_FILE="$2"
            shift 2
            ;;
        --proof-name)
            PROOF_NAME="$2"
            shift 2
            ;;
        --runner-target-room)
            RUNNER_TARGET_ROOM="$2"
            shift 2
            ;;
        --scanner-target-room)
            SCANNER_TARGET_ROOM="$2"
            shift 2
            ;;
        --max-phases)
            MAX_PHASES="$2"
            shift 2
            ;;
        --runner-seconds)
            RUNNER_SECONDS="$2"
            shift 2
            ;;
        --scanner-seconds)
            SCANNER_SECONDS="$2"
            shift 2
            ;;
        --runner-max-actions)
            RUNNER_MAX_ACTIONS="$2"
            shift 2
            ;;
        --scanner-max-actions)
            SCANNER_MAX_ACTIONS="$2"
            shift 2
            ;;
        --runner-stuck-turns)
            RUNNER_STUCK_TURNS="$2"
            shift 2
            ;;
        --scanner-stuck-turns)
            SCANNER_STUCK_TURNS="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "Python venv not found at ${VENV_PYTHON}. Run ./start_evennia.sh first." >&2
    exit 1
fi

if [[ "${GAME_DIR_INPUT}" = /* ]]; then
    GAME_DIR="${GAME_DIR_INPUT}"
else
    GAME_DIR="${SCRIPT_DIR}/${GAME_DIR_INPUT}"
fi

if [[ ! -f "${GAME_DIR}/server/conf/settings.py" ]]; then
    echo "Could not locate Evennia game directory at ${GAME_DIR}" >&2
    echo "Set WAGENT_GAME_DIR before running the cold-start proof if you are not using ./mygame." >&2
    exit 1
fi

if [[ "${ACCOUNT_POOL_FILE}" = /* ]]; then
    ACCOUNT_POOL_PATH="${ACCOUNT_POOL_FILE}"
else
    ACCOUNT_POOL_PATH="${SCRIPT_DIR}/${ACCOUNT_POOL_FILE}"
fi

if [[ ! -f "${ACCOUNT_POOL_PATH}" ]]; then
    echo "Provisioning local Evennia account pool for proof run: ${ACCOUNT_POOL_PATH}"
    "${VENV_PYTHON}" "${SCRIPT_DIR}/provision_account_pool.py" \
        --game-dir "${GAME_DIR}" \
        --pool-file "${ACCOUNT_POOL_PATH}" \
        --count "${ACCOUNT_POOL_COUNT}" \
        --host "${ACCOUNT_POOL_HOST}" \
        --port "${ACCOUNT_POOL_PORT}"
fi

PROOF_DIR="${SCRIPT_DIR}/artifacts/current/${PROOF_NAME}"
mkdir -p "${PROOF_DIR}"

MAP_MEMORY="${PROOF_DIR}/wagent_proof_map.json"
ROUTE_MEMORY="${PROOF_DIR}/wagent_proof_route.json"
EXPERIENCE_MEMORY="${PROOF_DIR}/wagent_proof_experience.json"
SCANNER_OBSERVATION_MEMORY="${PROOF_DIR}/wagent_proof_scanner_observation.json"
SUMMARY_FILE="${PROOF_DIR}/wagent_proof_summary.json"
ORCHESTRATOR_LOG="${PROOF_DIR}/wagent_proof_orchestrator.log"

cat > "${MAP_MEMORY}" <<'EOF'
{
  "format": "room-exits-v2",
  "rooms": {}
}
EOF

cat > "${ROUTE_MEMORY}" <<'EOF'
{
  "destinations": {}
}
EOF

cat > "${EXPERIENCE_MEMORY}" <<'EOF'
{
  "failed_actions_by_room": {}
}
EOF

cat > "${SCANNER_OBSERVATION_MEMORY}" <<'EOF'
{
  "meta": {},
  "rooms": {},
  "runs": [],
  "recent_events": []
}
EOF

export WAGENT_ACCOUNT_POOL_FILE="${ACCOUNT_POOL_PATH}"
export WAGENT_MAP_MEMORY="${MAP_MEMORY}"
export WAGENT_ROUTE_MEMORY="${ROUTE_MEMORY}"
export WAGENT_EXPERIENCE_MEMORY="${EXPERIENCE_MEMORY}"
export WAGENT_SCOUT_OBSERVATION_MEMORY="${SCANNER_OBSERVATION_MEMORY}"
export WAGENT_ORCHESTRATOR_SUMMARY="${SUMMARY_FILE}"
export WAGENT_ORCHESTRATOR_LOG="${ORCHESTRATOR_LOG}"

echo "Running isolated autonomous-mapping proof"
echo "proof_dir=${PROOF_DIR}"
echo "map_memory=${MAP_MEMORY}"
echo "route_memory=${ROUTE_MEMORY}"
echo "experience_memory=${EXPERIENCE_MEMORY}"
echo "scanner_observation_memory=${SCANNER_OBSERVATION_MEMORY}"
echo "summary_file=${SUMMARY_FILE}"
echo "account_pool_file=${ACCOUNT_POOL_PATH}"

COMMAND=(
    "${VENV_PYTHON}" bots.py
    --map-memory "${MAP_MEMORY}"
    --scanner-observation-memory "${SCANNER_OBSERVATION_MEMORY}"
    --summary-file "${SUMMARY_FILE}"
    --max-phases "${MAX_PHASES}"
    --runner-seconds "${RUNNER_SECONDS}"
    --scanner-seconds "${SCANNER_SECONDS}"
    --runner-max-actions "${RUNNER_MAX_ACTIONS}"
    --scanner-max-actions "${SCANNER_MAX_ACTIONS}"
    --runner-stuck-turns "${RUNNER_STUCK_TURNS}"
    --scanner-stuck-turns "${SCANNER_STUCK_TURNS}"
)

if [[ -n "${RUNNER_TARGET_ROOM}" ]]; then
    COMMAND+=(--runner-target-room "${RUNNER_TARGET_ROOM}")
fi

if [[ -n "${SCANNER_TARGET_ROOM}" ]]; then
    COMMAND+=(--scanner-target-room "${SCANNER_TARGET_ROOM}")
fi

cd "${SCRIPT_DIR}"
"${COMMAND[@]}"

echo
echo "Proof run finished. Inspect these files:"
echo "- ${MAP_MEMORY}"
echo "- ${ROUTE_MEMORY}"
echo "- ${SCANNER_OBSERVATION_MEMORY}"
echo "- ${SUMMARY_FILE}"
echo "- ${ORCHESTRATOR_LOG}"
echo
echo "A valid proof is not 'the map became complete'."
echo "A valid proof is that these isolated empty shared-memory files gained new confirmed edges or rooms."