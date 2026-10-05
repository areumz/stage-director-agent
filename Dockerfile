FROM python:3.12-slim

# ponytail: uv 버전은 개발 환경(uv --version)에 맞춘다. 올릴 때는 uv.lock 이 같은 의존성으로 풀리는지 한 번 확인할 것
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# 의존성 레이어: pyproject·uv.lock 이 바뀔 때만 다시 만든다
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY src ./src
COPY contracts ./contracts
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080

# 프로세스 하나(--workers 1)여야 한다: RPM 제한·resume 락이 프로세스 단위다.
# Cloud Run 이 정해 주는 $PORT(기본 8080)로 뜬다. 시크릿은 이미지에 넣지 않고 실행 시 환경변수로 받는다
CMD ["sh", "-c", "exec uvicorn --factory stage_director.api:create_app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
