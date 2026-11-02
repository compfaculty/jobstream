-- Run as an administrator in the existing application PostgreSQL database.
-- Provision a dedicated LOGIN role separately; give it only this schema's privileges.
CREATE SCHEMA IF NOT EXISTS infinity_runtime;
CREATE TABLE IF NOT EXISTS infinity_runtime.threads (
    id text PRIMARY KEY,
    conversation jsonb,
    state jsonb
);
CREATE TABLE IF NOT EXISTS infinity_runtime.executions (
    attempt text PRIMARY KEY,
    request jsonb NOT NULL,
    response jsonb NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    tool_calls integer NOT NULL DEFAULT 0,
    usage jsonb NOT NULL DEFAULT '{"input_tokens":0,"output_tokens":0}'::jsonb
);
-- For a separately provisioned role named jobstream_infinity:
-- GRANT USAGE ON SCHEMA infinity_runtime TO jobstream_infinity;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA infinity_runtime
--     TO jobstream_infinity;
-- Do not grant application table access or use the Django owner role for the runtime.
