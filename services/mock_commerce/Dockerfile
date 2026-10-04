# Build context is the repo root. Deliberately copies nothing but this service:
# the mocked upstream shares no code with the refund system.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" "psycopg[binary]"
COPY services/mock_commerce/main.py ./
EXPOSE 8010
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
