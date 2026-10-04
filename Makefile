.PHONY: up down nuke migrate secrets seed embed-policy verify verify-chain replay replay-report demo-gif smoke logs test
# Works with either the `docker compose` plugin or the standalone v2 binary.
DC := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE := $(DC) --profile core
# The verifiers run on the host and need the same .env the containers get.
UV := uv run --env-file .env

# --- L0 ---------------------------------------------------------------------
up: secrets   ## infra + mock-commerce + the three MCP servers
	$(COMPOSE) up -d --build

down:
	$(DC) --profile "*" down

nuke:          ## also drops the volumes — the way to apply a schema change
	$(DC) --profile "*" down -v

migrate: secrets  ## apply schema.sql, the MCP roles, the dashboard accounts (all re-runnable)
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U refund -d refund \
	  < infra/local/schema.sql
	set -a; . ./.env; set +a; \
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U refund -d refund \
	  -v mcp_log_pw="$$MCP_LOG_PW" -v mcp_policy_pw="$$MCP_POLICY_PW" \
	  < infra/local/mcp-roles.sql
	set -a; . ./.env; set +a; \
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U refund -d refund \
	  -v reviewer_pw="$$REVIEWER_PW" -v admin_pw="$$ADMIN_PW" \
	  < infra/local/reviewers.sql

# Local-only passwords for the per-domain MCP roles (§4A) and the two dashboard
# accounts (L6). Generated into .env,
# which is gitignored, rather than committed or defaulted: a role with a blank
# password is a role anything on the Docker network can be.
secrets:
	@for k in MCP_LOG_PW MCP_POLICY_PW REVIEWER_PW ADMIN_PW; do \
	  grep -q "^$$k=." .env && continue; \
	  v=$$(openssl rand -hex 16); \
	  grep -q "^$$k=" .env && perl -pi -e "s|^$$k=.*|$$k=$$v|" .env || echo "$$k=$$v" >> .env; \
	  echo "generated $$k in .env"; \
	done

seed: embed-policy  ## generate the mocked upstream + labelled eval corpus + policy corpus
	$(UV) --with 'psycopg[binary]' --with faker scripts/seed_data.py

embed-policy:  ## chunk policy/refund-policy-v1.md by clause and embed it (L2.6)
	$(UV) --with httpx --with 'psycopg[binary]' --with pyyaml scripts/embed_policy_corpus.py

verify:        ## prove L0 through L8 are actually done
	$(UV) --with httpx --with 'psycopg[binary]' scripts/verify_l0.py
	$(UV) --with httpx --with 'psycopg[binary]' scripts/verify_l1.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp --with boto3 scripts/verify_l2.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp --with boto3 scripts/verify_l2_5.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp scripts/verify_l2_6.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp --with boto3 --with aio-pika scripts/verify_l3.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp --with boto3 --with aio-pika scripts/verify_l4.py
	$(COMPOSE) exec -T opa /opa test /policy
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp scripts/verify_l5.py
	$(UV) --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' scripts/verify_l6.py
	$(UV) $(REPLAY_DEPS) scripts/verify_l7.py
	$(UV) --with pillow scripts/verify_l8.py

# L7. The CI gate: exits 1 on any false approve, 3 while the corpus is not yet
# fully measured. Resumes from infra/local/replay/results.jsonl — re-run it
# until it exits 0; delete that file to start over after a prompt or Rego change.
REPLAY_DEPS := --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' --with pyyaml \
	  --with mcp --with boto3
replay:
	$(UV) $(REPLAY_DEPS) scripts/replay.py

replay-report:  ## report on what is recorded so far; runs nothing
	$(UV) $(REPLAY_DEPS) scripts/replay.py --report

demo-gif:  ## regenerate docs/demo.gif (L8 storyboard)
	$(UV) --with pillow scripts/make_demo_gif.py

verify-chain:  ## walk the audit hash chain (L6); prints the row count and head hash
	$(UV) --with 'psycopg[binary]' --with 'pydantic>=2.9' scripts/verify_chain.py

test:
	uv run --with pytest --with 'pydantic>=2.9' python -m pytest tests/ -q

# --- L0.5 -------------------------------------------------------------------
# Runs on the host: the capability probe precedes every container image existing.
smoke:
	$(UV) --with httpx scripts/smoke_llm.py

logs:
	$(DC) logs -f refund-api case-worker executor mcp-commerce
