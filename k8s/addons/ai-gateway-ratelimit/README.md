# Usage Rate Limiter

Install on DGX Spark with the gateway installer:

```bash
bash scripts/install-ai-gateway.sh --context spark --enable-usage-rate-limiting
```

This provisions Redis before enabling Envoy Gateway's managed rate-limit service.
The installer preserves this addon on subsequent runs, even without the flag.
It requires the `local-path` StorageClass; it is not automatically deployed by
either platform overlay. For AKS, adapt storage and validate the installation
separately before enabling it.

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
`config.envoyGateway.rateLimit=null`. Do not rerun the installer while the Redis
Service remains present unless you intend to enable the addon again. Remove the
Redis workload and Service only after disabling the limiter. Retain
`data-redis-0` unless deliberately discarding state; deleting it resets counters.