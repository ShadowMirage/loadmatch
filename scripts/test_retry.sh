#!/usr/bin/env bash

set -e

echo "--- Phase 15: WhatsApp Webhook Retry Simulation ---"

# 1. Payload preparation
WAMID="wamid.retry.$(date +%s)"
PAYLOAD="{
  \"entry\": [
    {
      \"changes\": [
        {
          \"value\": {
            \"messages\": [
              {
                \"from\": \"919999999999\",
                \"id\": \"$WAMID\",
                \"text\": {\"body\": \"blr to delhi\"},
                \"type\": \"text\"
              }
            ]
          }
        }
      ]
    }
  ]
}"

# 2. Curl loop (3 times quickly)
echo "Sending 3 identical webhooks for wamid: $WAMID..."
for i in {1..3}; do
  curl -X POST http://localhost:8000/webhook \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    -s -o /dev/null
done

# 3. Wait for DB commit
sleep 5

# 4. Check DB status
echo "Checking Delivery State in DB..."
STATE=$(docker exec loadmatch-db psql -U postgres -d loadmatch -t -c "SELECT delivery_state FROM processed_messages WHERE wamid='$WAMID';" | tr -d '[:space:]')

echo "Delivery State: $STATE"

if [ "$STATE" != "DELIVERED" ]; then
    echo "Certification Phase 15: FAILED (Expected DELIVERED, found $STATE)"
    exit 1
fi

echo "Certification Phase 15: PASSED"
exit 0
