WITH sequences AS (
    SELECT c.oid, n.nspname AS schemaname, c.relname AS sequencename,
           format_type(s.seqtypid, NULL) AS data_type,
           s.seqstart AS start_value, s.seqincrement AS increment_by,
           s.seqmin AS min_value, s.seqmax AS max_value,
           s.seqcache AS cache_size, s.seqcycle AS cycle,
           own_ns.nspname AS owned_schema, own_table.relname AS owned_table,
           own_col.attname AS owned_column,
           EXISTS (
               SELECT 1 FROM pg_depend ext
               WHERE ext.classid = 'pg_class'::regclass AND ext.objid = c.oid
                 AND ext.refclassid = 'pg_extension'::regclass AND ext.deptype = 'e'
           ) AS extension_owned
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_sequence AS s ON s.seqrelid = c.oid
    LEFT JOIN pg_depend AS own ON own.classid = 'pg_class'::regclass
        AND own.objid = c.oid AND own.refclassid = 'pg_class'::regclass
        AND own.deptype IN ('a', 'i')
    LEFT JOIN pg_class AS own_table ON own_table.oid = own.refobjid
    LEFT JOIN pg_namespace AS own_ns ON own_ns.oid = own_table.relnamespace
    LEFT JOIN pg_attribute AS own_col ON own_col.attrelid = own.refobjid
        AND own_col.attnum = own.refobjsubid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname !~ '^pg_toast'
)
SELECT COALESCE(json_agg(json_build_object(
    'schema', schemaname, 'name', sequencename, 'data_type', data_type,
    'start_value', start_value, 'increment_by', increment_by,
    'min_value', min_value, 'max_value', max_value, 'cache_size', cache_size,
    'cycle', cycle, 'owned_schema', owned_schema, 'owned_table', owned_table,
    'owned_column', owned_column, 'extension_owned', extension_owned
) ORDER BY schemaname, sequencename), '[]'::json)::text
FROM sequences;
