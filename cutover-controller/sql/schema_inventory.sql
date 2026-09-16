WITH user_tables AS (
    SELECT c.oid, n.nspname AS schemaname, c.relname AS tablename,
           c.relkind, c.relreplident
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_toast'
), table_data AS (
    SELECT t.*,
           COALESCE((
             SELECT json_agg(json_build_object(
                 'name', a.attname,
                 'type', format_type(a.atttypid, a.atttypmod),
                 'type_oid', a.atttypid,
                 'typmod', a.atttypmod,
                 'not_null', a.attnotnull,
                 'generated', a.attgenerated,
                 'identity', a.attidentity,
                 'has_default', d.adbin IS NOT NULL,
                 'default_expression', CASE WHEN d.adbin IS NULL THEN NULL
                                            ELSE pg_get_expr(d.adbin, d.adrelid) END
             ) ORDER BY a.attnum)
             FROM pg_attribute AS a
             LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
           ), '[]'::json) AS columns,
           COALESCE((
             SELECT json_agg(a.attname ORDER BY k.ordinality)
             FROM pg_index AS i
             CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
             JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             WHERE i.indrelid = t.oid
               AND ((t.relreplident = 'i' AND i.indisreplident)
                    OR (t.relreplident = 'd' AND i.indisprimary))
           ), '[]'::json) AS identity_columns
    FROM user_tables AS t
)
SELECT COALESCE(json_agg(json_build_object(
    'schema', schemaname, 'table', tablename, 'kind', relkind,
    'replica_identity', relreplident, 'identity_columns', identity_columns,
    'columns', columns
) ORDER BY schemaname, tablename), '[]'::json)::text
FROM table_data;
