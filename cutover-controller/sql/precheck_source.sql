WITH publication AS (
    SELECT p.oid, p.pubname, p.puballtables, p.pubinsert, p.pubupdate,
           p.pubdelete, p.pubtruncate, p.pubviaroot,
           CASE WHEN current_setting('server_version_num')::integer >= 180000
                THEN to_jsonb(p)->>'pubgencols' ELSE NULL END AS pubgencols
    FROM pg_publication AS p
    WHERE p.pubname = {{publication}}
), publication_tables AS (
    SELECT pt.schemaname, pt.tablename, pt.attnames, pt.rowfilter
    FROM pg_publication_tables AS pt
    WHERE pt.pubname = {{publication}}
    ORDER BY pt.schemaname, pt.tablename
), slot AS (
    SELECT slot_name, plugin, slot_type, database, active, active_pid,
           restart_lsn::text, confirmed_flush_lsn::text, wal_status,
           safe_wal_size,
           CASE WHEN restart_lsn IS NULL THEN NULL
                ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint END AS retained_bytes,
           CASE WHEN confirmed_flush_lsn IS NULL THEN NULL
                ELSE greatest(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint, 0) END AS lag_bytes
    FROM pg_replication_slots
    WHERE slot_name = {{slot}}
)
SELECT json_build_object(
    'publication', (SELECT row_to_json(publication) FROM publication),
    'tables', COALESCE((SELECT json_agg(row_to_json(publication_tables)) FROM publication_tables), '[]'::json),
    'slot', (SELECT row_to_json(slot) FROM slot),
    'current_lsn', pg_current_wal_lsn()::text
)::text;
