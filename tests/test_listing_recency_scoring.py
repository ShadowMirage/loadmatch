from datetime import datetime, timedelta, timezone

from app.services.matching_service import compute_listing_recency_bonus


def test_recent_listing_gets_high_bonus():
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    assert compute_listing_recency_bonus(created_at) == 10


def test_medium_age_listing_gets_medium_bonus():
    created_at = datetime.now(timezone.utc) - timedelta(minutes=90)

    assert compute_listing_recency_bonus(created_at) == 6


def test_old_listing_gets_low_bonus():
    created_at = datetime.now(timezone.utc) - timedelta(hours=4)

    assert compute_listing_recency_bonus(created_at) == 3


def test_very_old_listing_gets_zero_bonus():
    created_at = datetime.now(timezone.utc) - timedelta(hours=13)

    assert compute_listing_recency_bonus(created_at) == 0
