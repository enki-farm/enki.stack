# Activepieces

The CLI installs the official Activepieces chart **0.6.0** (app **0.92.2**).
`values.yaml` is an unchanged copy of the upstream defaults. Profile-specific
settings are supplied separately by the CLI.
The CLI keeps `imagePullPolicy: Always` for the custom `latest` image and
provisions the Enki OpenAI-compatible provider after Helm is ready; see the
[API key setup](../../docs/cli.md#addons-activepieces).

Upstream source:
<https://github.com/activepieces/activepieces/tree/1ea47497ba2c3bbeb112e891e3e6f7c4ebb2d104/deploy/activepieces-helm>

The official installation uses a local chart, not a published Helm repository.
`activepieces-0.6.0.tgz` packages that source and its locked dependencies so
installation does not need to clone the upstream repository or build dependencies.
The package includes the upstream chart and dependency license files.

To refresh, check out an immutable upstream revision, then run:

```bash
helm repo add mittwald https://helm.mittwald.de
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency build deploy/activepieces-helm
helm package deploy/activepieces-helm --destination /path/to/enki.stack/helm/activepieces
cp deploy/activepieces-helm/values.yaml /path/to/enki.stack/helm/activepieces/values.yaml
```

Update the CLI chart pin and this source reference together. Do not edit the
upstream defaults to configure a profile.

The upstream PostgreSQL/Redis image tags are amd64-only. CLI overrides pin
`bitnamilegacy/postgresql:14.9.0-debian-11-r61` and
`bitnamilegacy/redis:7.0.15-debian-12-r20`, both published for amd64 and arm64,
while retaining the upstream database major versions. These are archived
Bitnami images; review their maintenance/security status before production use.

See [CLI addon usage](../../docs/cli.md#addons-activepieces) and the
[official installation guide](https://www.activepieces.com/docs/install/options/helm).
