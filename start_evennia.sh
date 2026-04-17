#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
EVENNIA_SOURCE_DIR="${SCRIPT_DIR}/vendor/evennia"
GAME_DIR_INPUT="${WAGENT_GAME_DIR:-${1:-${SCRIPT_DIR}/mygame}}"

if [[ "${GAME_DIR_INPUT}" = /* ]]; then
    GAME_DIR="${GAME_DIR_INPUT}"
else
    GAME_DIR="${SCRIPT_DIR}/${GAME_DIR_INPUT}"
fi

cd "${SCRIPT_DIR}"

if [ ! -f "${GAME_DIR}/server/conf/settings.py" ]; then
    echo "Could not locate Evennia game directory at ${GAME_DIR}" >&2
    echo "Set WAGENT_GAME_DIR or pass the game directory path as the first argument." >&2
    exit 1
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

cat <<EOF
Environment is ready.

Next steps:
1. Create local credentials in wagent_account_pool.local.json or export EVENNIA_USER and EVENNIA_PASS.
2. Start the game server from the selected Evennia game directory with: evennia start
3. Run the workflow from repo root, for example: python bots.py
EOF
