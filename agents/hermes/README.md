# Hermes Agent

This package deploys one persistent Hermes/Herdr environment with a private SSH Service.
Warpgate is expected to connect to `hermes-ssh.agents.svc.cluster.local:22` from
the `warpgate` namespace.

## Install

The upstream Warpgate operator image is amd64-only. Publish the pinned multi-arch
build before installing its chart:

```sh
docker login ghcr.io
PLATFORMS=linux/arm64 IMAGE_TAG=v0.5.0-enki2-arm64 \
  bash scripts/build-warpgate-operator-image.sh
enkictl component install warpgate -p profiles/local/profile.yaml
```

The Warpgate component installs the operator and CRDs, a persistent Warpgate
instance, and a local `hermes-access` user restricted to the Hermes SSH target.
The instance admin password is stored in the profile's mode-0600 `secrets.yaml`.
The generated `hermes-access` password is stored in the `warpgate` namespace as
the `hermes-access-password` Secret.

Before applying this package, copy a Warpgate client public key into the
`agents` namespace. The key is available from the Warpgate pod:

```sh
kubectl -n warpgate exec deploy/warpgate -- warpgate --config /data/warpgate.yaml client-keys
kubectl create namespace agents
kubectl -n agents create secret generic hermes-ssh-authorized-keys \
  --from-file=hermes=/path/to/warpgate-agent.pub
kubectl apply -k agents/hermes
```

Retrieve the LoadBalancer address with `kubectl -n warpgate get svc warpgate-ssh`.
Add it to the local SSH config; OpenSSH will prompt for the generated
`hermes-access` password and the first-time Warpgate host-key confirmation:

```sshconfig
Host spark-hermes
  HostName <warpgate-ssh-address>
  Port 2222
  User hermes-access:hermes
  StrictHostKeyChecking yes
```

The container runs headless Herdr Core (`hc`), not the Herdr TUI. Install `hc`
on the local machine, then add and use the remote core:

```sh
hc machine add spark-hermes
hc run --on spark-hermes hermes
```

Use the terminal address printed by `hc run` with `hc attach` to take over the
agent session.

The 20Gi PVC stores `/opt/data`, including Hermes configuration, credentials,
sessions, workspace state, Herdr Core state, and the SSH host key. Back it up as
sensitive data. A pod restart terminates live processes; Herdr Core can restore
terminal metadata but does not preserve those processes.

## Model configuration

The image bundles [config.yml](../../docker/hermes-agent/config.yml). On first
boot, upstream Hermes copies it to `$HERMES_HOME/config.yaml` (`/opt/data/config.yaml`).
It defaults to the Enki AI Gateway at `https://ai.zer0.garden/v1`, model `fast`.
The provider is `custom` (OpenAI-compatible), not Hermes's `ai-gateway` provider,
which targets Vercel.

Before starting Hermes, place `ENKI_STACK_AI_API_KEY=<gateway API key>` in
`/opt/data/.env` on the data volume, owned by the `hermes` user with mode `0600`.
Hermes loads this file for both container and SSH-launched sessions; credentials
are not included in the image. For local Docker use, prepare a data directory:

```sh
mkdir -p hermes-data
cp docker/hermes-agent/config.yml hermes-data/config.yaml
# Edit hermes-data/config.yaml and create hermes-data/.env with your API key.
chmod 0600 hermes-data/.env
docker run --rm -it \
  --mount type=bind,src="$(pwd)/hermes-data",dst=/opt/data \
  ghcr.io/enki-farm/hermes-agent:v0.23.1-herdr-core-v0.1.20-aigateway1 hermes
```

To select another endpoint, model, or credential environment variable before
startup, edit the prepared `config.yaml` or the bundled template before building.
Existing volume configurations are preserved across restarts and image upgrades;
update their `model` section explicitly to opt into the gateway default.

Publish with `bash scripts/build-hermes-agent-image.sh`, then update the pinned
image tag and digest in [deployment.yaml](deployment.yaml) before applying the
package. The existing deployment pin does not pick up a newly built image.