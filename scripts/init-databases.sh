#!/bin/bash
# Runs once, on first boot of an empty pgdata volume. Only does what needs
# superuser at bootstrap; the application schema is applied by `make migrate`.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
    -- Langfuse v2 insists on its own database (PLANNING-LOCAL §11).
    SELECT 'CREATE DATABASE langfuse'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
EOSQL
