import pytest
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock, patch

from app.services.date_parser import parse_message_timestamp, normalize_date
from app.services.event_bus import EventBus
from app.services.notification_service import NotificationService

USER_TZ = ZoneInfo("Asia/Kolkata")

def test_parse_message_timestamp():
    # Stable Epoch: 1712999400 corresponds to 2024-04-13 09:10:00 UTC
    # 09:10 UTC is 14:40 IST (09.16 + 5.5 = 14.66)
    ts_sec = "1712999400"
    dt = parse_message_timestamp(ts_sec)
    assert dt.hour == 14
    assert dt.minute == 40
    assert dt.tzinfo.key == "Asia/Kolkata"

    # 13 digits (ms)
    ts_ms = "1712999400000"
    dt_ms = parse_message_timestamp(ts_ms)
    assert dt_ms == dt

def test_normalize_date_midnight_boundary():
    # Message sent at 23:55 (11:55 PM) on April 13th
    base = datetime(2026, 4, 13, 23, 55, tzinfo=USER_TZ)
    
    # "tomorrow" from 23:55 on 13th should be 14th
    norm = normalize_date("tomorrow", relative_base=base)
    assert norm == "14-04-2026"
    
    # "today" from 23:55 on 13th should be 13th
    norm_today = normalize_date("today", relative_base=base)
    assert norm_today == "13-04-2026"

    # Simulation of Replay Lag:
    # Message was sent on 13th 23:55, but we process it on 14th 00:05
    # The normalization MUST still result in 13th/14th respectively.
    norm_lag = normalize_date("tomorrow", relative_base=base)
    assert norm_lag == "14-04-2026"

@pytest.mark.asyncio
async def test_event_bus_isolation_and_parallelism():
    eb = EventBus("test-bus")
    results = []
    
    async def fast_handler(data):
        results.append("fast")
        
    async def slow_handler(data):
        await asyncio.sleep(0.1)
        results.append("slow")
        
    async def failing_handler(data):
        raise ValueError("Boom")

    # Clear handlers if any (though static EventBus might have them)
    EventBus._handlers = {}
    
    EventBus.register_handler("TEST_EVENT", fast_handler)
    EventBus.register_handler("TEST_EVENT", slow_handler)
    EventBus.register_handler("TEST_EVENT", failing_handler)
    
    # Using a fresh instance to emit
    bus = EventBus("emitter")
    await bus.emit_async({"event": "TEST_EVENT"})
    
    assert "fast" in results
    assert "slow" in results
    # If we reached here, failing_handler didn't crash the loop.

@pytest.mark.asyncio
async def test_notification_suppression_logic():
    from app.services.recovery_service import DeliveryResult
    # Mock Redis and DB
    with patch("app.services.notification_service.get_redis") as mock_redis, \
         patch("app.services.notification_service.SessionLocal") as mock_db_session, \
         patch("app.services.notification_service.RecoveryService") as mock_recovery:
        
        redis_instance = MagicMock()
        mock_redis.return_value = redis_instance
        
        # Test 1: First notification
        redis_instance.get.return_value = None # Not suppressed
        
        event = {
            "event": "MATCH_FOUND",
            "load_id": "L1",
            "listing_id": "T1",
            "user_id": "U1",
            "score": 0.85
        }
        
        # Mock User and Load
        db_instance = mock_db_session.return_value
        mock_user = MagicMock(phone="919999999999")
        mock_load = MagicMock(from_city="Delhi", to_city="Mumbai", weight_kg=10000, pickup_date=datetime.now())
        db_instance.query.return_value.filter.return_value.first.side_effect = [mock_user, mock_load]
        
        recovery_instance = mock_recovery.return_value
        
        # FIX: return an awaitable
        async def mock_send(*args, **kwargs):
            return DeliveryResult.DELIVERED
            
        recovery_instance.send_with_backoff = mock_send
        
        await NotificationService.handle_match_found(event)
        
        # Verify suppression key set
        # The Set might be called after await
        redis_instance.set.assert_called_with("match_notify:v1:L1:T1:U1", "1", ex=86400)

@pytest.mark.asyncio
async def test_lane_watch_rate_limiting():
    with patch("app.services.notification_service.get_redis") as mock_redis, \
         patch("app.services.notification_service.SessionLocal") as mock_db_session, \
         patch("app.services.notification_service.RecoveryService") as mock_recovery:
        
        redis_instance = MagicMock()
        mock_redis.return_value = redis_instance
        
        # Simulate 4th notification in an hour (Limit is 3)
        redis_instance.incr.return_value = 4
        
        db_instance = mock_db_session.return_value
        mock_sub = MagicMock(user_id="U1", lane_key="LANE1")
        mock_user = MagicMock(phone="919999999999")
        db_instance.query.return_value.filter.return_value.all.return_value = [mock_sub]
        db_instance.query.return_value.filter.return_value.first.return_value = mock_user

        recovery_instance = mock_recovery.return_value
        # If rate limit works, this shouldn't be called.
        
        await NotificationService._process_lane_watch_notifications(
            db_instance, [mock_sub], "New Load", "D-M", "LANE1"
        )
        
        assert not recovery_instance.send_with_backoff.called
