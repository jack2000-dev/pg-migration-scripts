WITH wanted(name) AS (
    VALUES ('max_replication_slots'), ('max_logical_replication_workers'),
           ('max_sync_workers_per_subscription'), ('max_worker_processes'),
           ('max_wal_senders'), ('max_active_replication_origins')
), settings AS (
    SELECT s.name, s.setting::bigint AS value
    FROM pg_settings AS s JOIN wanted AS w USING (name)
)
SELECT json_build_object(
    'settings', COALESCE((SELECT json_object_agg(name, value) FROM settings), '{}'::json),
    'replication_slots_used', (SELECT count(*) FROM pg_replication_slots),
    'logical_slots_used', (SELECT count(*) FROM pg_replication_slots WHERE slot_type = 'logical'),
    'wal_senders_used', (SELECT count(*) FROM pg_stat_replication),
    'logical_workers_visible', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'logical replication worker'),
    'worker_processes_visible', (SELECT count(*) FROM pg_stat_activity WHERE backend_type <> 'client backend'),
    'active_origins', (SELECT count(*) FROM pg_replication_origin_status)
)::text;
