#!/usr/bin/env bash

set -e

echo "--- Phase 13: Leader Election Correctness ---"

# 1. Scale loadmatch-app to 4
echo "Scaling loadmatch-app to 4 instances..."
docker compose up --scale app=4 -d

# 2. Wait for startup
echo "Waiting 10 seconds for leader election..."
sleep 10

# 3. Check logs for PRIMARY_NODE
echo "Checking instance logs..."
LOGS=$(docker compose logs app)
PRIMARY_COUNT=$(echo "$LOGS" | grep -c "PRIMARY_NODE: RecoveryDaemon active" || true)
STANDBY_COUNT=$(echo "$LOGS" | grep -c "SECONDARY_NODE: RecoveryDaemon standby" || true)

echo "Found $PRIMARY_COUNT PRIMARY node(s)"
echo "Found $STANDBY_COUNT SECONDARY node(s)"

if [ "$PRIMARY_COUNT" -ne 1 ]; then
    echo "Certification Phase 13: FAILED (Expected exactly 1 primary, found $PRIMARY_COUNT)"
    exit 1
fi

# 4. Check advisory locks in DB
echo "Checking PostgreSQL advisory locks..."
LOCK_COUNT=$(docker exec loadmatch-db psql -U postgres -d loadmatch -t -c "SELECT count(*) FROM pg_locks WHERE locktype='advisory';" | tr -d '[:space:]')

echo "Found $LOCK_COUNT advisory lock(s)"

if [ "$LOCK_COUNT" -ne 1 ]; then
    echo "Certification Phase 13: FAILED (Expected 1 advisory lock, found $LOCK_COUNT)"
    exit 1
fi

echo "Certification Phase 13: PASSED"
exit 0
