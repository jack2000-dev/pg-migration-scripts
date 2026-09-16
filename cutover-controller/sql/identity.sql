SELECT json_build_object(
    'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
    'server_version', current_setting('server_version'),
    'server_version_num', current_setting('server_version_num')::integer,
    'database', current_database(),
    'user', current_user,
    'server_addr', COALESCE(inet_server_addr()::text, 'local-socket'),
    'server_port', inet_server_port(),
    'in_recovery', pg_is_in_recovery()
)::text;
