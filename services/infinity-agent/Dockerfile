FROM rust:1-bookworm AS builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --locked --release

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
COPY --from=builder /build/target/release/jobstream-infinity-agent /usr/local/bin/
RUN mkdir -p /var/log/jobstream && chown app:app /var/log/jobstream
USER app
EXPOSE 8091
CMD ["jobstream-infinity-agent"]
