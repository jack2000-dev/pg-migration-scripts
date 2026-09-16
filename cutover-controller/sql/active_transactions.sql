SELECT json_build_object(
    'active_transactions', COALESCE((
        SELECT json_agg(json_build_object(
            'pid', pid, 'user', usename, 'application', application_name,
            'state', state, 'xact_start', xact_start
        ) ORDER BY xact_start)
        FROM pg_stat_activity
        WHERE datname = current_database() AND pid <> pg_backend_pid()
          AND backend_type = 'client backend' AND xact_start IS NOT NULL
    ), '[]'::json),
    'prepared_transactions', COALESCE((
        SELECT json_agg(json_build_object('gid', gid, 'owner', owner, 'prepared', prepared))
        FROM pg_prepared_xacts WHERE database = current_database()
    ), '[]'::json)
)::text;
