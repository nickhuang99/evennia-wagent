#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
EVENNIA_SOURCE_DIR="${SCRIPT_DIR}/vendor/evennia"
GAME_DIR_INPUT="${WAGENT_GAME_DIR:-${1:-${SCRIPT_DIR}/mygame}}"
ACCOUNT_POOL_FILE_INPUT="${WAGENT_ACCOUNT_POOL_FILE:-wagent_account_pool.local.json}"
ACCOUNT_POOL_COUNT="${WAGENT_ACCOUNT_POOL_COUNT:-2}"
ACCOUNT_POOL_HOST="${WAGENT_EVENNIA_HOST:-127.0.0.1}"
ACCOUNT_POOL_PORT="${WAGENT_EVENNIA_PORT:-4000}"

if [[ "${GAME_DIR_INPUT}" = /* ]]; then
    GAME_DIR="${GAME_DIR_INPUT}"
else
    GAME_DIR="${SCRIPT_DIR}/${GAME_DIR_INPUT}"
fi

if [[ "${ACCOUNT_POOL_FILE_INPUT}" = /* ]]; then
    ACCOUNT_POOL_PATH="${ACCOUNT_POOL_FILE_INPUT}"
else
    ACCOUNT_POOL_PATH="${SCRIPT_DIR}/${ACCOUNT_POOL_FILE_INPUT}"
fi

cd "${SCRIPT_DIR}"

if [ ! -f "${GAME_DIR}/server/conf/settings.py" ]; then
    echo "Could not locate Evennia game directory at ${GAME_DIR}" >&2
    echo "Set WAGENT_GAME_DIR or pass the game directory path as the first argument." >&2
    exit 1
fi

mkdir -p "${GAME_DIR}/server/logs" "${GAME_DIR}/server/.static" "${GAME_DIR}/server/.media"
if [ ! -f "${GAME_DIR}/server/conf/secret_settings.py" ]; then
    cat > "${GAME_DIR}/server/conf/secret_settings.py" <<'EOF'
# Local Evennia overrides live here.
# This file is intentionally created by start_evennia.sh so a fresh checkout
# can bootstrap without warning about a missing secret_settings module.
EOF
fi

if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
fi

source "${VENV_DIR}/bin/activate"

echo "Installing Python dependencies"
python -m pip install --upgrade pip
if [ -d "${EVENNIA_SOURCE_DIR}" ]; then
    python -m pip install -e "${EVENNIA_SOURCE_DIR}"
fi
python -m pip install -r requirements.txt

cd "${GAME_DIR}"
echo "Running Evennia bootstrap tasks in ${GAME_DIR}"
evennia migrate
evennia collectstatic --noinput

cd "${SCRIPT_DIR}"
if [[ -n "${EVENNIA_USER:-}" && -n "${EVENNIA_PASS:-}" ]]; then
    echo "Using explicit EVENNIA_USER/EVENNIA_PASS; skipping local account pool bootstrap"
elif [[ -f "${ACCOUNT_POOL_PATH}" ]]; then
    echo "Using existing local account pool: ${ACCOUNT_POOL_PATH}"
else
    echo "Provisioning local Evennia account pool: ${ACCOUNT_POOL_PATH}"
    python "${SCRIPT_DIR}/provision_account_pool.py" \
        --game-dir "${GAME_DIR}" \
        --pool-file "${ACCOUNT_POOL_PATH}" \
        --count "${ACCOUNT_POOL_COUNT}" \
        --host "${ACCOUNT_POOL_HOST}" \
        --port "${ACCOUNT_POOL_PORT}"
fi

cat <<EOF
Environment is ready.

Next steps:
1. Start the game server from the selected Evennia game directory with: cd ${GAME_DIR} && ${VENV_DIR}/bin/evennia start
2. Run the workflow from repo root, for example: cd ${SCRIPT_DIR} && ${VENV_DIR}/bin/python bots.py
3. Override the auto-generated account pool with EVENNIA_USER/EVENNIA_PASS or WAGENT_ACCOUNT_POOL_FILE if needed.
EOF
