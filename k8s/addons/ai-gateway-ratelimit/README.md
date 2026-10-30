# Usage Rate Limiter

This optional addon is outside the core CLI migration. Its former gateway
installer flag is retired; `enkictl component install ai-gateway` does not enable or
preserve a manually layered rate limiter. The manifests and values remain for
independently reviewed manual deployment, not as a supported component command.
Do not reconcile a CLI-owned gateway after manually enabling this layer without
reviewing the resulting values.

Manual setup must provision Redis and wait for readiness before layering
`helm/gateway/envoy-gateway-rate-limit-values.yaml` onto the gateway release,
then verify the rate-limit service before publishing protected routes. It
requires `local-path` storage. For AKS, adapt storage and validate separately.

Redis uses a single 1Gi persistent volume and AOF with `appendfsync everysec`.
Normal restarts retain counters, but a crash can lose about one second of
writes. There is no HA, disk-loss recovery, or strict spending guarantee.
No eviction policy is enabled: memory exhaustion causes errors rather than
silently evicting quota counters. Monitor memory and volume capacity.

Redis has no public Service or password. Its NetworkPolicy permits ingress only
from the trusted `envoy-gateway-system` namespace. This requires an enforcing
CNI; namespace administrators and cluster administrators remain trusted. Do not
use this unencrypted Redis configuration across untrusted networks.

`config.envoyGateway.rateLimit.failClosed: true` rejects requests covered by
global rate limits when the limiter cannot be reached (HTTP 500). Exhausted
token budgets return HTTP 429. Routes without global limits remain unaffected.
AgentRouter's `QuotaPolicy` configuration is a separate feature and is not used.

To disable, remove protected example routes first, then explicitly upgrade the
gateway with only its base values (no `--reuse-values`) and set
`config.envoyGateway.rateLimit=null`. Remove the
Redis workload and Service only after disabling the limiter. Retain
`data-redis-0` unless deliberately discarding state; deleting it resets counters.