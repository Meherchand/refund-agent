# One image for refund-api, case-worker, executor and the three MCP servers.
# They share `packages/`, so six near-identical Dockerfiles would be
# duplication; compose supplies the `command`. mock-commerce keeps its own image
# on purpose — it is the mocked upstream and shares no code with the refund
# system.
#
# The MCP servers ride in this image rather than their own because `mcp` is
# needed on both sides of the protocol anyway: the servers register tools with
# it, case-worker's MCPToolProvider calls them with it.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir \
      fastapi "uvicorn[standard]" "psycopg[binary]" aio-pika "pydantic>=2.9" \
      httpx pyyaml mcp boto3
COPY config ./config
COPY packages ./packages
COPY services ./services
COPY mcp_servers ./mcp_servers
