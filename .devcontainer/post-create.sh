#!/usr/bin/env bash
# Runs once when the Codespace is created. Same .venv layout as the laptop, so the
# README's `.venv/bin/...` commands work unchanged. Airflow is not installed here by
# default (it's large); add it with `.venv/bin/pip install -r airflow/requirements.txt`
# only when a task needs it. Secrets come from Codespaces secrets, never from .env.
set -euo pipefail
python -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "devcontainer ready: python $(.venv/bin/python -V | cut -d' ' -f2), $(docker --version 2>/dev/null || echo 'docker starting'), $(terraform -version | head -1), $(aws --version | cut -d' ' -f1)"
