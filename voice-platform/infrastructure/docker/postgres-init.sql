-- Runs once on first container start (as the postgres superuser).
-- vp_owner owns the schema and runs migrations; vp_app is the runtime role (RLS enforced).
CREATE ROLE vp_owner LOGIN PASSWORD 'vp_owner_dev_password';
CREATE ROLE vp_app LOGIN PASSWORD 'vp_app_dev_password' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE voice_platform OWNER vp_owner;
\connect voice_platform
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
ALTER SCHEMA public OWNER TO vp_owner;
