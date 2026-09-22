-- 1) Explicit live P/L baseline
ALTER TABLE account_settings ADD COLUMN IF NOT EXISTS live_start_equity DOUBLE PRECISION;

-- 2) Separate simulation and live snapshots (they share one table)
ALTER TABLE portfolio_snapshots ADD COLUMN IF NOT EXISTS is_simulated BOOLEAN NOT NULL DEFAULT FALSE;
