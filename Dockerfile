# syntax=docker/dockerfile:1
FROM electriccoinco/lightwalletd:v0.6.0@sha256:abc8199dae800e050415a3459f71c7850aeefeb1ca6a681e9b9eda7249ce8613 AS lightwalletd
FROM fullstorydev/grpcurl:v1.9.3@sha256:085e183ca334eb4e81ca81ee12cbb2b2737505d1d77f5e33dabc5d066593d998 AS grpcurl
FROM zakuracore/zakura:1.8.7@sha256:6004f49d81fe07cce40d2e96d9f66fdff6fffcb5971e11070fb2f8007087933d

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 zstd tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=lightwalletd /usr/local/bin/lightwalletd /usr/local/bin/lightwalletd
COPY --from=grpcurl /bin/grpcurl /usr/local/bin/grpcurl
COPY docker/stack.py /opt/zovereign/stack.py
RUN zakurad --version && lightwalletd version && grpcurl --version

ENV DATA_DIR=/data \
    ZAKURA_VERSION=1.8.7 \
    ZAKURA_DB_MAJOR=30 \
    LIGHTWALLETD_VERSION=v0.6.0 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/zovereign
VOLUME ["/data"]
EXPOSE 8233 9067
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=30s --start-period=24h --retries=3 \
    CMD ["python3", "/opt/zovereign/stack.py", "health"]
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/zovereign/stack.py"]
CMD ["run"]
