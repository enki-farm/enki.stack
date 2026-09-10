# Custom k6 build with SSE support (core k6 can't time streamed chunks, which
# xk6-sse exposes as individual events with their own timestamps).
FROM grafana/xk6:latest AS build
# grafana/xk6 runs as a non-root user without write access to /, so build into
# the image's own workdir instead of an absolute path.
RUN xk6 build v1.2.1 --with github.com/phymbert/xk6-sse@v0.1.12 --output ./k6

FROM grafana/k6:latest
COPY --from=build /xk6/k6 /usr/bin/k6
ENTRYPOINT ["k6"]

