FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
COPY docs ./docs

RUN pip install --no-cache-dir -e ".[api,planner]"

ENV SECURE_QUERY_AUTH_MODE=dev
ENV SECURE_QUERY_ENV=development
ENV SECURE_QUERY_PRINCIPAL_ID=demo-user
ENV SECURE_QUERY_TENANT_ID=chinook
EXPOSE 8000

CMD ["sh", "-c", "python -m secure_query.demo.load_chinook && uvicorn secure_query.api:app --host 0.0.0.0 --port 8000"]
