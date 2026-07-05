# engram-lite — local agent memory in one container.
#
# Build:  docker build -t engram-lite .
#   docker run -i --rm -v engram-data:/data engram-lite
# Try the interactive demo:
#   docker run -it --rm -v engram-data:/data engram-lite demo
#
# The named volume keeps /data/memory.db across container restarts — that's the
# whole point: your agents restart, their memory does not.

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

FROM python:3.12-slim
RUN useradd -m -u 1000 engram
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/engram /usr/local/bin/engram

USER engram
VOLUME /data
ENV ENGRAM_DB_PATH=/data/memory.db \
    ENGRAM_AUTOCHECK=true \
    ENGRAM_EMBEDDER=hash

ENTRYPOINT ["engram"]
CMD ["demo"]
