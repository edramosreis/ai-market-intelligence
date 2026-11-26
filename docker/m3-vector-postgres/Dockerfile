# Keep the server base and matching header pins in sync.
ARG TARGETARCH
FROM postgres:18.6-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650 AS server

FROM scratch AS headers-amd64
ADD --checksum=sha256:d6004e14b60c412992022046b41af8f388d44d357ea1183db598dd3a4af533ef \
    https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/postgresql-18/postgresql-server-dev-18_18.6-1.pgdg12%2B2_amd64.deb /headers.deb

FROM scratch AS headers-arm64
ADD --checksum=sha256:59c3799d46aa350aa47456a4e690851614b18ebad894d629292392dafd051572 \
    https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/postgresql-18/postgresql-server-dev-18_18.6-1.pgdg12%2B2_arm64.deb /headers.deb

FROM headers-${TARGETARCH} AS headers
FROM server AS builder
COPY --from=headers /headers.deb /tmp/headers.deb
ADD --checksum=sha256:83d673c3b287cddbf3879f86215f043f2c2b56c69d37b2a77504910db1ed6e56 \
    https://codeload.github.com/pgvector/pgvector/tar.gz/f37c13f68b57d2c3472b2214fbcff699d6d34876 /tmp/pgvector.tar.gz
RUN set -eux; \
    test "$(dpkg-deb --field /tmp/headers.deb Version)" = "$PG_VERSION"; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential /tmp/headers.deb; \
    dpkg -i /tmp/headers.deb; \
    test "$(dpkg-query -W -f='${Version}' "postgresql-$PG_MAJOR")" = "$PG_VERSION"; \
    mkdir /tmp/pgvector; \
    tar -xzf /tmp/pgvector.tar.gz --strip-components=1 -C /tmp/pgvector; \
    make -C /tmp/pgvector PG_CONFIG="/usr/lib/postgresql/$PG_MAJOR/bin/pg_config" OPTFLAGS=""; \
    make -C /tmp/pgvector PG_CONFIG="/usr/lib/postgresql/$PG_MAJOR/bin/pg_config" DESTDIR=/extension install; \
    install -D -m 644 /tmp/pgvector/LICENSE /extension/usr/share/doc/pgvector/LICENSE

FROM server AS vector
COPY --from=builder /extension/ /
