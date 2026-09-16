\set ON_ERROR_STOP on
\if :{?publication}
\else
\echo 'ERROR: pass -v publication=...'
\quit 3
\endif

SET ROLE lab_owner;
CREATE PUBLICATION :"publication" FOR TABLE
    public.accounts, public.transfers, public.event_log,
    cutover_control.replication_probe
WITH (publish = 'insert, update, delete, truncate');
RESET ROLE;
