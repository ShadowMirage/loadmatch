#!/usr/bin/env bash

set -e

echo "===================================================="
echo "    LoadMatch Stability Certification Suite"
echo "===================================================="

# Ensure environment is ready
echo "1. Checking environment..."
if ! [ -x "$(command -v docker)" ]; then
    echo "ERROR: docker not found"
    exit 1
fi

if ! [ -f ".venv/bin/pytest" ]; then
    echo "ERROR: .venv/bin/pytest not found"
    exit 1
fi

echo "2. Tier 1: Contract-Layer & Logic Invariants"
PYTHONPATH=. .venv/bin/pytest tests/test_normalization_integrity.py
PYTHONPATH=. .venv/bin/pytest tests/test_heuristic_intents.py
PYTHONPATH=. .venv/bin/pytest tests/test_corridor_metadata_integrity.py
PYTHONPATH=. .venv/bin/pytest tests/test_lane_symmetry.py
PYTHONPATH=. .venv/bin/pytest tests/test_matching.py

echo "3. Tier 2: Runtime Determinism & Replay"
PYTHONPATH=. .venv/bin/pytest tests/test_runtime_stability.py
PYTHONPATH=. .venv/bin/pytest tests/test_webhook_idempotency.py
PYTHONPATH=. .venv/bin/python3 scripts/simulate_dispatcher_flows.py

echo "4. Tier 3: Cluster Resilience & Failover (Docker required)"
./scripts/test_leader_election.sh
./scripts/test_failover.sh
./scripts/test_retry.sh

echo "===================================================="
echo "    Certification COMPLETE: PASSED"
echo "===================================================="
exit 0
