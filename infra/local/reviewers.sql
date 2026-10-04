-- The two dashboard accounts (L6). Applied by `make migrate` as
--   psql -v reviewer_pw=... -v admin_pw=... -f infra/local/reviewers.sql
-- with both values generated into .env by `make secrets` (gitignored). No
-- password is written here and none is defaulted.
--
-- Hashed by Postgres itself (pgcrypto, bcrypt), and checked the same way:
-- `password_hash = crypt(<given>, password_hash)`. So neither refund-api nor
-- this file ever holds a hash function of its own to get wrong.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO reviewers (email, role, password_hash) VALUES
    ('reviewer@example.com', 'reviewer', crypt(:'reviewer_pw', gen_salt('bf'))),
    ('admin@example.com',    'admin',    crypt(:'admin_pw',    gen_salt('bf')))
ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, password_hash = EXCLUDED.password_hash;
