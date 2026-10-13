-- Run once per deploy by the bootstrap Job, as the RDS master user, with
--   psql -v app_password=... -f db-bootstrap.sql
--
-- Idempotent: safe to re-run, and deploy.sh re-runs it on every deploy so a
-- rotated application password is re-applied to the role.
--
-- Creates a least-privilege `temporal` LOGIN role (no CREATEDB, no CREATEROLE)
-- and the two databases it owns. Temporal's schema tool then creates tables
-- as that role. The master credential is used for this and nothing else.

\set ON_ERROR_STOP on

SELECT 'CREATE ROLE temporal LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'temporal')
\gexec

-- Outside a DO block so psql can interpolate the variable; :'x' quotes it as a
-- literal, so the password never needs escaping.
ALTER ROLE temporal WITH LOGIN PASSWORD :'app_password';

-- On RDS the master user is not a superuser. To create a database OWNED BY
-- another role it must be a member of that role.
GRANT temporal TO CURRENT_USER;

SELECT 'CREATE DATABASE temporal OWNER temporal'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal')
\gexec

SELECT 'CREATE DATABASE temporal_visibility OWNER temporal'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'temporal_visibility')
\gexec

-- Nobody else connects to Temporal's databases.
REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
