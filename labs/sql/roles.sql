-- Run once per cluster as its provider/admin role. Set LOGIN passwords outside
-- this file using the provider UI or an interactive, non-logged session.
-- PostgreSQL has no CREATE ROLE IF NOT EXISTS, so review before rerunning.
CREATE ROLE lab_owner NOLOGIN;
CREATE ROLE lab_deployer LOGIN;
CREATE ROLE lab_app LOGIN;
GRANT lab_owner TO lab_deployer WITH SET TRUE;

-- On the DigitalOcean source only:
CREATE ROLE lab_forward_repl LOGIN REPLICATION;
GRANT lab_owner TO doadmin WITH SET TRUE;

-- On the OpenStack target only:
CREATE ROLE lab_reverse_repl LOGIN REPLICATION;
GRANT lab_owner TO migration_admin WITH SET TRUE;
