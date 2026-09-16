-- Run only on each PostgreSQL 17 source database after schema.sql.
-- psql: psql ... -v account_count=10000 -f sql/seed.sql
\if :{?account_count}
\else
\set account_count 10000
\endif

SET ROLE lab_owner;
INSERT INTO public.accounts (account_id, balance)
SELECT id, 100000
FROM generate_series(1, :account_count) AS id
ON CONFLICT (account_id) DO NOTHING;

SELECT setval(
    pg_get_serial_sequence('public.accounts', 'account_id'),
    GREATEST((SELECT max(account_id) FROM public.accounts), 1),
    true
);
RESET ROLE;

ANALYZE public.accounts;
