-- Per-domain credentials for the MCP servers (PLANNING-LOCAL.md §4A).
--
-- Applied by `make migrate` as
--   psql -v mcp_log_pw=... -v mcp_policy_pw=... -f infra/local/mcp-roles.sql
-- with both values read from .env, which is gitignored. No password is written
-- in this file and none is defaulted — a role with a blank password is a role
-- anything on the network can be.
--
-- Two roles, not three. `mcp-commerce` appears nowhere here because it holds no
-- database credential at all: it reads through mock-commerce, which is the trust
-- boundary L0 established, and a read-only role on the commerce tables would
-- have quietly bypassed it. `mcp-evidence` likewise holds only MinIO keys.
--
-- What every domain does need is somewhere to write its access log, and that is
-- the interesting grant: INSERT and nothing else. mcp_logger cannot UPDATE,
-- DELETE or even SELECT the table it appends to, so a compromised MCP server
-- cannot edit the record of what it did. `citations_verified` is only worth
-- checking because of this line.

SELECT 'CREATE ROLE mcp_logger LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_logger')\gexec
ALTER ROLE mcp_logger PASSWORD :'mcp_log_pw';

SELECT 'CREATE ROLE mcp_policy_ro LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_policy_ro')\gexec
ALTER ROLE mcp_policy_ro PASSWORD :'mcp_policy_pw';

GRANT CONNECT ON DATABASE refund TO mcp_logger, mcp_policy_ro;
GRANT USAGE ON SCHEMA public TO mcp_logger, mcp_policy_ro;

-- Append-only. The sequence grant is what INSERT actually needs from BIGSERIAL.
GRANT INSERT ON mcp_access_log TO mcp_logger;
GRANT USAGE, SELECT ON SEQUENCE mcp_access_log_id_seq TO mcp_logger;

GRANT SELECT ON policy_clauses TO mcp_policy_ro;

-- Nothing grants USAGE ON SCHEMA commerce to either role, so neither can reach
-- an order or a payment even by name. verify_l2_5.py asserts exactly that.
