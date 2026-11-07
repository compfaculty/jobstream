# Loki 3.7 uses a minimal image without wget. Reuse the pinned Prometheus
# image's static BusyBox for an actual HTTP readiness check; no package install.
FROM prom/prometheus:v3.13.5@sha256:aafa9d694e6a883c02db516d9f6e5b84e14195f49a2a6456a4cca815edda72b9 AS probe
FROM grafana/loki:3.7.9@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28
COPY --from=probe /bin/busybox /health/busybox
