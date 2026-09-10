# Custom k6 build with SSE support (core k6 can't time streamed chunks, which
# xk6-sse exposes as individual events with their own timestamps).
FROM grafana/xk6:latest AS build
RUN xk6 build --with github.com/phymbert/xk6-sse@latest --output /k6

FROM grafana/k6:latest
COPY --from=build /k6 /usr/bin/k6
ENTRYPOINT ["k6"]
