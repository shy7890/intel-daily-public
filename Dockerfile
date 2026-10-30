# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     INTEL_DB_PATH=/data/intel.db

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /app app

COPY README.md pyproject.toml uv.lock ./
COPY src ./src

# 可复现构建：依赖安装完全由 uv.lock 约束（uv export 生成 pinned+hashes 清单，
# pip --require-hashes 校验安装），避免旧版 `pip install .` 自由解析漂移。
RUN pip install --no-cache-dir "uv==0.10.0" \
    && uv export --locked --no-dev --no-emit-project -o /tmp/requirements.lock \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && pip uninstall -y --no-cache-dir uv \
    && chown -R app:app /app

USER app

EXPOSE 8000

VOLUME /data

CMD ["python", "-m", "intel_daily.web"]
