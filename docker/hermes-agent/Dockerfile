# syntax=docker/dockerfile:1
# Image: ghcr.io/enki-farm/hermes-agent
# Build: scripts/build-hermes-agent-image.sh

ARG HERMES_IMAGE=nousresearch/hermes-agent:v0.23.1@sha256:276d44685c65c0cb2a25f190b63c65c8906f9dc76486fb1c05ae71b5826bda08
FROM ${HERMES_IMAGE}

USER root

ARG HERDR_CORE_VERSION=0.1.20

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssh-server passwd tar \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /etc/ssh/authorized_keys /run/sshd \
    && install -d -o hermes -g hermes -m 0700 /tmp/hermes-runtime

  ENV XDG_RUNTIME_DIR=/tmp/hermes-runtime

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      arm64) \
        core_arch=aarch64; \
        core_sha256=5f3037379d15cc76cfc72d2fd1a4262ea1c5b24df278bf7cfd8adcff7b914862 ;; \
      amd64) \
        core_arch=x86_64; \
        core_sha256=ac6b86890c451a37584620aaa438b71bc0d0bd430a2e7c1d7e095f052c414e74 ;; \
      *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    archive="/tmp/herdr-core-linux-${core_arch}.tar.gz"; \
    curl -fsSL --retry 3 \
      "https://releases.herdr.dev/core/${HERDR_CORE_VERSION}/herdr-core-linux-${core_arch}.tar.gz" \
      -o "$archive"; \
    printf '%s  %s\n' "$core_sha256" "$archive" | sha256sum --check -; \
    tar -xzf "$archive" -C /tmp herdr-core; \
    install -m 0755 /tmp/herdr-core /usr/local/bin/herdr-core; \
    ln -s herdr-core /usr/local/bin/hc; \
    /usr/local/bin/hc --version; \
    rm -f "$archive" /tmp/herdr-core

# Upstream seeds $HERMES_HOME/config.yaml from this template only when absent.
COPY config.yml /opt/hermes/cli-config.yaml.example
COPY sshd_config /etc/ssh/sshd_config
COPY s6-rc.d/sshd/type /etc/s6-overlay/s6-rc.d/sshd/type
COPY s6-rc.d/sshd/dependencies.d/base /etc/s6-overlay/s6-rc.d/sshd/dependencies.d/base
COPY s6-rc.d/user/contents.d/sshd /etc/s6-overlay/s6-rc.d/user/contents.d/sshd
COPY --chmod=0755 s6-rc.d/sshd/run /etc/s6-overlay/s6-rc.d/sshd/run

EXPOSE 22