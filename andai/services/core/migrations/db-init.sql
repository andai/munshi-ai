-- db-init.sql — create the andai database and its two roles on the stack's Postgres. Idempotent.
--
-- Run by psql as the Postgres superuser inside the postgres container (`make -C andai db-init`)
-- and by the test fixtures. Passwords come from the environment, never from this file:
--   ANDAI_DB_PASSWORD_OWNER   andai_owner: owns the database and every table; runs migrations
--   ANDAI_DB_PASSWORD_APP     andai_app:   the application; no ownership, no BYPASSRLS
-- Re-running resets both passwords to the given values and re-asserts every attribute below.
\set ON_ERROR_STOP on
\getenv owner_password ANDAI_DB_PASSWORD_OWNER
\getenv app_password ANDAI_DB_PASSWORD_APP

SELECT 'CREATE ROLE andai_owner'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'andai_owner') \gexec
SELECT 'CREATE ROLE andai_app'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'andai_app') \gexec

ALTER ROLE andai_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'owner_password';
ALTER ROLE andai_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'app_password';

SELECT 'CREATE DATABASE andai OWNER andai_owner'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'andai') \gexec
ALTER DATABASE andai OWNER TO andai_owner;
REVOKE ALL ON DATABASE andai FROM PUBLIC;
GRANT CONNECT ON DATABASE andai TO andai_owner, andai_app;

\connect andai
-- The database owner owns schema public (pg_database_owner); nobody else may create in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO andai_app;
