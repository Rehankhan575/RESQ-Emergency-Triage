#!/bin/bash
export ENABLE_CALL_RECORDING=true
export PYTHONPATH=.
.venv/bin/python -u app/agents/worker.py --dev dev
