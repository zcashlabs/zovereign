# Small contract fixture. It does not contain Zakura or lightwalletd.
FROM python:3.13-alpine@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
ARG FIXTURE_REVISION=v1
ARG FIXTURE_DB_MAJOR=30
LABEL io.zovereign.test-fixture="1" \
      org.opencontainers.image.revision="${FIXTURE_REVISION}"
ENV DATA_DIR=/data \
    ZAKURA_VERSION=1.8.7 \
    ZAKURA_DB_MAJOR=${FIXTURE_DB_MAJOR} \
    LIGHTWALLETD_VERSION=v0.6.0 \
    FIXTURE_REVISION=${FIXTURE_REVISION} \
    PYTHONUNBUFFERED=1
COPY stack.py /opt/zovereign/stack.py
EXPOSE 9067
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=1s --timeout=3s --start-period=2s --retries=5 \
    CMD ["python3", "/opt/zovereign/stack.py", "health"]
ENTRYPOINT ["python3", "/opt/zovereign/stack.py"]
CMD ["run"]
