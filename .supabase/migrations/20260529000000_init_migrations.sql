-- Initialize Supabase migrations table
CREATE SCHEMA IF NOT EXISTS supabase_migrations;

CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (
    version BIGINT PRIMARY KEY,
    name VARCHAR(255) UNIQUE,
    statements TEXT[],
    execution_time INTERVAL
);
