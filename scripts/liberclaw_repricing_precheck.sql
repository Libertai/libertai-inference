-- LiberClaw repricing (migration 5e21a0dfd95e): read-only precheck, run against prod BEFORE deploy.
--   psql "$DATABASE_URL" -f scripts/liberclaw_repricing_precheck.sql
-- "Live" matches the backfill: a liberclaw plan_subscriptions row that is active, overdue or
-- upgrading, not a trial, on a paid tier (repeated as a CTE per query: a read-only transaction
-- cannot create a temp view). Nothing here writes.

BEGIN TRANSACTION READ ONLY;

\echo '(a) paid-tier liberclaw_users with no live liberclaw subscription: NOT grandfathered, cut to the new caps at deploy'
WITH live_lclw AS (
    SELECT * FROM plan_subscriptions
    WHERE product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
      AND tier IN ('starter', 'pro', 'team')
)
SELECT lu.id, lu.user_id, lu.tier, lu.liberclaw_account_id
FROM liberclaw_users lu
WHERE lu.tier IN ('starter', 'pro', 'team')
  AND NOT EXISTS (SELECT 1 FROM live_lclw ps WHERE ps.liberclaw_account_id = lu.liberclaw_account_id)
ORDER BY lu.tier, lu.user_id;

\echo '(b) live liberclaw subscriptions whose account has no bridged liberclaw_users row: NOT grandfathered'
WITH live_lclw AS (
    SELECT * FROM plan_subscriptions
    WHERE product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
      AND tier IN ('starter', 'pro', 'team')
)
SELECT ps.id, ps.liberclaw_account_id, ps.tier, ps.status, ps.provider
FROM live_lclw ps
WHERE NOT EXISTS (SELECT 1 FROM liberclaw_users lu WHERE lu.liberclaw_account_id = ps.liberclaw_account_id)
ORDER BY ps.created_at;

\echo '(c) live liberclaw subscriptions parked in upgrading (grandfathered; a paid new checkout will clear it)'
WITH live_lclw AS (
    SELECT * FROM plan_subscriptions
    WHERE product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
      AND tier IN ('starter', 'pro', 'team')
)
SELECT ps.id, ps.liberclaw_account_id, ps.tier, ps.provider, ps.updated_at
FROM live_lclw ps
WHERE ps.status = 'upgrading'
ORDER BY ps.updated_at;

\echo '(d) liberclaw_users.tier differing from the live row the backfill picks (the backfill realigns it)'
WITH live_lclw AS (
    SELECT * FROM plan_subscriptions
    WHERE product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
      AND tier IN ('starter', 'pro', 'team')
)
SELECT DISTINCT ON (lu.id) lu.id, lu.user_id, lu.tier AS stored_tier, ps.tier AS live_tier, ps.status
FROM liberclaw_users lu
JOIN live_lclw ps ON ps.liberclaw_account_id = lu.liberclaw_account_id
WHERE lu.tier IS DISTINCT FROM ps.tier
ORDER BY lu.id, CASE ps.tier WHEN 'team' THEN 3 WHEN 'pro' THEN 2 ELSE 1 END DESC, ps.created_at DESC;

\echo '(e) rows the backfill will write: tier and grandfathered cap'
WITH live_lclw AS (
    SELECT * FROM plan_subscriptions
    WHERE product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
      AND tier IN ('starter', 'pro', 'team')
)
SELECT * FROM (
    SELECT DISTINCT ON (lu.id) lu.id, lu.user_id, lu.tier AS stored_tier, ps.tier AS new_tier,
           CASE ps.tier WHEN 'starter' THEN 100.0 WHEN 'pro' THEN 500.0 WHEN 'team' THEN 2000.0 END
               AS credits_limit_override
    FROM liberclaw_users lu
    JOIN live_lclw ps ON ps.liberclaw_account_id = lu.liberclaw_account_id
    ORDER BY lu.id, CASE ps.tier WHEN 'team' THEN 3 WHEN 'pro' THEN 2 ELSE 1 END DESC, ps.created_at DESC
) w
ORDER BY new_tier, user_id;

ROLLBACK;
