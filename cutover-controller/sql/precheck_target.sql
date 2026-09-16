WITH subscription AS (
    SELECT s.oid, s.subname, s.subenabled, s.subslotname,
           s.subpublications, s.suborigin, s.subbinary, s.substream,
           s.subtwophasestate, s.subdisableonerr, s.subrunasowner, s.subfailover
    FROM pg_subscription AS s
    WHERE s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND s.subname = {{subscription}}
), workers AS (
    SELECT st.subname, st.worker_type, st.pid, st.relid,
           st.received_lsn::text, st.latest_end_lsn::text,
           st.last_msg_send_time, st.last_msg_receipt_time, st.latest_end_time
    FROM pg_stat_subscription AS st
    WHERE st.subname = {{subscription}}
), stats AS (
    SELECT ss.subname, ss.apply_error_count, ss.sync_error_count, ss.stats_reset
    FROM pg_stat_subscription_stats AS ss
    WHERE ss.subname = {{subscription}}
), relations AS (
    SELECT n.nspname AS schemaname, c.relname AS tablename,
           r.srsubstate, r.srsublsn::text
    FROM pg_subscription_rel AS r
    JOIN subscription AS s ON s.oid = r.srsubid
    JOIN pg_class AS c ON c.oid = r.srrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    ORDER BY n.nspname, c.relname
), origin AS (
    SELECT ro.roname AS external_id
    FROM pg_replication_origin AS ro
    JOIN subscription AS s ON ro.roname = 'pg_' || s.oid::text
)
SELECT json_build_object(
    'subscription', (SELECT row_to_json(subscription) FROM subscription),
    'workers', COALESCE((SELECT json_agg(row_to_json(workers)) FROM workers), '[]'::json),
    'stats', (SELECT row_to_json(stats) FROM stats),
    'relations', COALESCE((SELECT json_agg(row_to_json(relations)) FROM relations), '[]'::json),
    'not_ready_count', (SELECT count(*) FROM relations WHERE srsubstate <> 'r'),
    'origin', (SELECT row_to_json(origin) FROM origin)
)::text;
