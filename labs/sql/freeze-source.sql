-- Defensive freeze drill. Run once on SOURCE as an account that can alter
-- lab_app and terminate its sessions. This is cluster-wide, not per database.
ALTER ROLE lab_app NOLOGIN;

SELECT pid, pg_terminate_backend(pid) AS terminated
FROM pg_stat_activity
WHERE usename = 'lab_app'
  AND pid <> pg_backend_pid();

SELECT count(*) AS remaining_app_sessions
FROM pg_stat_activity
WHERE usename = 'lab_app';

-- Run the next query in each lab database; it must return no rows.
SELECT pid, usename, application_name, state, xact_start
FROM pg_stat_activity
WHERE datname = current_database()
  AND backend_type = 'client backend'
  AND xact_start IS NOT NULL
  AND pid <> pg_backend_pid();

-- Rollback/release only, after lab_app is no longer enabled on TARGET:
-- ALTER ROLE lab_app LOGIN;
