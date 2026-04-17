#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
EVENNIA_SOURCE_DIR="${SCRIPT_DIR}/vendor/evennia"

cd "${SCRIPT_DIR}"

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

cd mygame
echo "Running Evennia bootstrap tasks"
evennia migrate
evennia collectstatic --noinput

cat <<'EOF'
Environment is ready.

Next steps:
1. Create local credentials in wagent_account_pool.local.json or export EVENNIA_USER and EVENNIA_PASS.
2. Start the game server from mygame/ with: evennia start
3. Run the workflow from repo root, for example: python bots.py
EOF
