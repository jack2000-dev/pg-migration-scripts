-- Run as the controller/admin account on each cluster and save the output.
SELECT jsonb_pretty(jsonb_build_object(
    'database', current_database(),
    'user', current_user,
    'server_version', current_setting('server_version'),
    'system_identifier_access', has_function_privilege(
        current_user, 'pg_catalog.pg_control_system()', 'EXECUTE'),
    'create_subscription_member', pg_has_role(
        current_user, 'pg_create_subscription', 'MEMBER'),
    'signal_backend_member', pg_has_role(
        current_user, 'pg_signal_backend', 'MEMBER'),
    'wal_level', current_setting('wal_level'),
    'max_replication_slots', current_setting('max_replication_slots')::integer,
    'max_wal_senders', current_setting('max_wal_senders')::integer,
    'max_logical_replication_workers',
        current_setting('max_logical_replication_workers')::integer,
    'max_worker_processes', current_setting('max_worker_processes')::integer,
    'max_sync_workers_per_subscription',
        current_setting('max_sync_workers_per_subscription')::integer,
    'slots_used', (SELECT count(*) FROM pg_replication_slots)
));

-- This must succeed because the controller pins each endpoint to this value.
SELECT system_identifier::text FROM pg_control_system();
