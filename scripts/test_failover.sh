#!/usr/bin/env bash

set -e

echo "--- Phase 14: Dynamic Failover Test ---"

# 1. Get leader PID from logs
echo "Identifying leader backend PID..."
LEADER_PID=$(docker compose logs app | grep "db_pid=" | tail -n 1 | sed -E 's/.*db_pid=([0-9]*).*/\1/')

if [ -z "$LEADER_PID" ]; then
    echo "Certification Phase 14: FAILED (Could not identify leader PID)"
    exit 1
fi

echo "Killing Leader Backend PID: $LEADER_PID"

# 2. Kill lead connection in Postgres
docker exec loadmatch-db psql -U postgres -d loadmatch -c "SELECT pg_terminate_backend($LEADER_PID);"

# 3. Wait for promotion
echo "Waiting 10 seconds for failover promotion..."
sleep 10

# 4. Check for 'Leadership promoted' in logs
echo "Checking instance logs for promotion event..."
PROMOTION_LOGS=$(docker compose logs app | grep "Leadership promoted" || true)

if [ -z "$PROMOTION_LOGS" ]; then
    echo "Certification Phase 14: FAILED (No promotion logs found)"
    exit 1
fi

echo "$PROMOTION_LOGS"
echo "Certification Phase 14: PASSED"
exit 0
