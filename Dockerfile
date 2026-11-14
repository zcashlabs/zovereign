# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM golang:1.27.3-bookworm@sha256:dc0963f32053e4bc451d24d87e3892d42d01021b61f093e98eb95c0352bb84b0 AS lightwalletd
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
# v0.6.0; build the same source for both architectures. The upstream image is amd64-only.
ADD --checksum=sha256:96e6e788a4edd618e1b17cc2de8c5ce614a1fea498e7889e86b87d354eb89f7d https://codeload.github.com/zcash/lightwalletd/tar.gz/e7436d2adb9ab5828eb8a27de534e16218841fc2 /tmp/lightwalletd.tar.gz
RUN tar -xzf /tmp/lightwalletd.tar.gz --strip-components=1 -C /src \
    && rm /tmp/lightwalletd.tar.gz
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOTOOLCHAIN=local \
    go build -mod=readonly -trimpath -buildvcs=false \
      -ldflags="-X github.com/zcash/lightwalletd/common.Version=v0.6.0 -X github.com/zcash/lightwalletd/common.GitCommit=e7436d2adb9ab5828eb8a27de534e16218841fc2" \
      -o /usr/local/bin/lightwalletd .
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
