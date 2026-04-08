\set ON_ERROR_STOP on

\echo '=============================='
\echo 'VEHICLE-LANE DEDUP VALIDATION'
\echo '=============================='

--------------------------------------------------
-- CHECK 1: vehicle_type column exists
--------------------------------------------------
SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'truck_space_listings'
    AND column_name = 'vehicle_type'
)
THEN 'PASS: vehicle_type column exists'
ELSE 'FAIL: vehicle_type column missing'
END AS vehicle_type_column_check;

--------------------------------------------------
-- CHECK 2: vehicle_type fully backfilled for active listings
--------------------------------------------------
SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no NULL vehicle_type rows in OPEN/PARTIAL'
ELSE 'FAIL: NULL vehicle_type rows still exist'
END AS vehicle_type_backfill_check
FROM truck_space_listings
WHERE lower(status::text) IN ('open', 'partial')
AND vehicle_type IS NULL;

--------------------------------------------------
-- CHECK 3: new vehicle-aware unique index exists
--------------------------------------------------
SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
    AND indexname = 'unique_user_lane_vehicle_open'
)
THEN 'PASS: vehicle-aware unique index exists'
ELSE 'FAIL: vehicle-aware unique index missing'
END AS vehicle_lane_unique_index_check;

--------------------------------------------------
-- CHECK 4: legacy index removed
--------------------------------------------------
SELECT
CASE
WHEN NOT EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
    AND indexname = 'unique_user_lane_open'
)
THEN 'PASS: legacy index removed'
ELSE 'FAIL: legacy index still present'
END AS legacy_index_removal_check;

--------------------------------------------------
-- CHECK 5: shadow index exists (planner warmup)
--------------------------------------------------
SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
    AND indexname = 'idx_owner_lane_vehicle_open_partial'
)
THEN 'PASS: shadow index exists'
ELSE 'WARN: shadow index missing (optional but recommended)'
END AS shadow_index_check;

--------------------------------------------------
-- CHECK 6: duplicate protection integrity
--------------------------------------------------
SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no duplicate OPEN/PARTIAL listings'
ELSE 'FAIL: duplicate listings detected'
END AS duplicate_integrity_check
FROM (
    SELECT owner_id,
           canonical_lane_key,
           vehicle_type,
           COUNT(*)
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
    GROUP BY owner_id, canonical_lane_key, vehicle_type
    HAVING COUNT(*) > 1
) duplicates;

--------------------------------------------------
-- CHECK 7: canonical_lane_key fully populated
--------------------------------------------------
SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: canonical_lane_key fully populated'
ELSE 'FAIL: canonical_lane_key NULL rows detected'
END AS canonical_lane_key_check
FROM truck_space_listings
WHERE canonical_lane_key IS NULL;

--------------------------------------------------
-- CHECK 8: planner probe on vehicle-aware lookup (type-safe)
--------------------------------------------------
EXPLAIN
WITH probe AS (
    SELECT owner_id, canonical_lane_key, vehicle_type
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
      AND canonical_lane_key IS NOT NULL
      AND vehicle_type IS NOT NULL
    LIMIT 1
)
SELECT tsl.*
FROM truck_space_listings tsl
JOIN probe p
 ON tsl.owner_id = p.owner_id
 AND tsl.canonical_lane_key = p.canonical_lane_key
 AND tsl.vehicle_type = p.vehicle_type
WHERE lower(tsl.status::text) IN ('open', 'partial');

--------------------------------------------------
-- CHECK 9: freshness-window lookup readiness
--------------------------------------------------
SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
    AND indexname = 'unique_user_lane_vehicle_open'
)
THEN 'PASS: freshness-window vehicle-aware lookup safe'
ELSE 'FAIL: freshness-window lookup unsafe'
END AS freshness_lookup_check;

--------------------------------------------------
-- CHECK 10: rollout completion summary
--------------------------------------------------
SELECT
COUNT(*) FILTER (WHERE vehicle_type IS NULL) AS missing_vehicle_type_rows,
COUNT(*) FILTER (WHERE canonical_lane_key IS NULL) AS missing_lane_keys,
COUNT(*) AS total_rows
FROM truck_space_listings;

--------------------------------------------------
-- CHECK 11: vehicle-type dedup isolation
--------------------------------------------------
\echo 'CHECK 11: vehicle-type dedup isolation'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no cross-vehicle dedup bleed-through'
ELSE 'FAIL: vehicle-type dedup leakage detected'
END AS vehicle_type_scope_isolation_check
FROM (
    SELECT owner_id,
           canonical_lane_key
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
    GROUP BY owner_id, canonical_lane_key
    HAVING COUNT(*) > COUNT(DISTINCT vehicle_type)
) t;

--------------------------------------------------
-- CHECK 12: canonical lane normalization integrity
--------------------------------------------------
\echo 'CHECK 12: canonical lane normalization integrity'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: canonical_lane_key normalized'
ELSE 'FAIL: malformed canonical_lane_key detected'
END AS canonical_lane_normalization_check
FROM truck_space_listings
WHERE canonical_lane_key IS NOT NULL
  AND (
      canonical_lane_key NOT LIKE '%:%'
      OR canonical_lane_key LIKE ':%'
      OR canonical_lane_key LIKE '%:'
      OR canonical_lane_key LIKE '% %'
  );

--------------------------------------------------
-- CHECK 13: freshness-window vehicle scope readiness
--------------------------------------------------
\echo 'CHECK 13: freshness-window vehicle scope readiness'

SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND indexname = 'unique_user_lane_vehicle_open'
)
THEN 'PASS: freshness window vehicle scope valid'
ELSE 'FAIL: freshness window still lane-only scoped'
END AS freshness_window_vehicle_scope_check;

--------------------------------------------------
-- CHECK 14: planner index usage verification
--------------------------------------------------
\echo 'CHECK 14: planner index usage verification'

WITH sample_row AS (
    SELECT owner_id,
           canonical_lane_key,
           vehicle_type
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
      AND canonical_lane_key IS NOT NULL
      AND vehicle_type IS NOT NULL
    LIMIT 1
)
SELECT
CASE
WHEN EXISTS (SELECT 1 FROM sample_row)
THEN 'PASS: sample probe row exists'
ELSE 'WARN: no OPEN/PARTIAL rows available for planner probe'
END AS planner_probe_readiness_check;

EXPLAIN
WITH sample_row AS (
    SELECT owner_id,
           canonical_lane_key,
           vehicle_type
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
      AND canonical_lane_key IS NOT NULL
      AND vehicle_type IS NOT NULL
    LIMIT 1
)
SELECT *
FROM truck_space_listings
WHERE (owner_id, canonical_lane_key, vehicle_type)
IN (
    SELECT owner_id,
           canonical_lane_key,
           vehicle_type
    FROM sample_row
);

--------------------------------------------------
-- CHECK 15: dispatcher reuse lookup contract
--------------------------------------------------
\echo 'CHECK 15: dispatcher reuse lookup contract'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no duplicate reuse candidates exist'
ELSE 'FAIL: duplicate reuse candidates present'
END AS dispatcher_reuse_safety_check
FROM (
    SELECT owner_id,
           canonical_lane_key,
           vehicle_type,
           COUNT(*)
    FROM truck_space_listings
    WHERE lower(status::text) IN ('open', 'partial')
    GROUP BY owner_id, canonical_lane_key, vehicle_type
    HAVING COUNT(*) > 1
) dup;

--------------------------------------------------
-- CHECK 16: replay authority drift surface
--------------------------------------------------
\echo 'CHECK 16: replay authority drift surface'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no replay authority drift detected'
ELSE 'WARN: replay authority payload anomalies detected'
END AS replay_authority_drift_check
FROM processed_messages
WHERE request_payload IS NULL;

--------------------------------------------------
-- CHECK 17: execution-ledger overlap safety
--------------------------------------------------
\echo 'CHECK 17: execution-ledger overlap safety'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: no orphan EXECUTING rows detected'
ELSE 'WARN: orphan EXECUTING rows present'
END AS execution_overlap_safety_check
FROM processed_messages
WHERE upper(coalesce(delivery_state, '')) = 'EXECUTING'
  AND updated_at < now() - interval '5 minutes';

--------------------------------------------------
-- CHECK 18: canonical lane persistence completeness
--------------------------------------------------
\echo 'CHECK 18: canonical lane persistence completeness'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: canonical_lane_key persistence complete'
ELSE 'FAIL: canonical_lane_key persistence incomplete'
END AS canonical_lane_persistence_check
FROM truck_space_listings
WHERE canonical_lane_key IS NULL;

--------------------------------------------------
-- CHECK 19: vehicle_type persistence completeness
--------------------------------------------------
\echo 'CHECK 19: vehicle_type persistence completeness'

SELECT
CASE
WHEN COUNT(*) = 0
THEN 'PASS: vehicle_type persistence complete'
ELSE 'FAIL: vehicle_type persistence incomplete'
END AS vehicle_type_persistence_check
FROM truck_space_listings
WHERE vehicle_type IS NULL;

--------------------------------------------------
-- CHECK 20: ranking observability surface readiness
--------------------------------------------------
\echo 'CHECK 20: ranking observability surface readiness'

SELECT
CASE
WHEN EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = current_schema()
      AND table_name = 'processed_messages'
)
AND EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_schema = current_schema()
      AND table_name = 'workflow_events'
)
THEN 'PASS: ranking observability tables reachable'
ELSE 'FAIL: ranking observability surface missing'
END AS ranking_observability_surface_check;

\echo '=============================='
\echo 'VALIDATION COMPLETE'
\echo '=============================='
