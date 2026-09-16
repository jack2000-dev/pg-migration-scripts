-- Run on both clusters before the cutover and keep frozen through the rollback
-- window. Provider/admin emergency accounts cannot be technically disabled.
ALTER ROLE lab_deployer NOLOGIN;

SELECT rolname, rolcanlogin
FROM pg_roles
WHERE rolname IN ('lab_owner', 'lab_deployer')
ORDER BY rolname;

-- Release only after the rollback window closes:
-- ALTER ROLE lab_deployer LOGIN;
