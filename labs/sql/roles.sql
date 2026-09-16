\set ON_ERROR_STOP on
\if :{?admin_role}
\else
\echo 'ERROR: pass -v admin_role=...'
\quit 3
\endif
\if :{?replication_role}
\else
\echo 'ERROR: pass -v replication_role=...'
\quit 3
\endif
\if :{?create_replication_role}
\else
\set create_replication_role true
\endif

-- Run once on each cluster. Set LOGIN passwords outside this file.
-- PostgreSQL has no CREATE ROLE IF NOT EXISTS, so review before rerunning.
CREATE ROLE lab_owner NOLOGIN;
CREATE ROLE lab_deployer LOGIN;
CREATE ROLE lab_app LOGIN;
GRANT lab_owner TO lab_deployer WITH SET TRUE;

\if :create_replication_role
CREATE ROLE :"replication_role" LOGIN REPLICATION;
\endif
GRANT lab_owner TO :"admin_role" WITH SET TRUE;
