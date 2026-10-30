"""Network-backed rendering smoke test; never contacts a Kubernetes cluster."""

from pathlib import Path

from enkictl.render import TRUST, Deployment
from enkictl.tools import Runner

ROOT = Path(__file__).resolve().parents[2]


def test_librechat_oidc_and_trust_render_with_pinned_chart(profile_path):
    from enkictl.addons.librechat import LibreChatAddon

    addon = LibreChatAddon(profile_path, ROOT)
    addon.runner.installation = "test-render-only"
    addon.deployment.render_charts("1.34.0")
    deployment = next(
        doc
        for doc in addon.deployment.templates["librechat"]
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "librechat-librechat"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = next(
        doc["data"]
        for doc in addon.deployment.templates["librechat"]
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "librechat-librechat-configenv"
    )
    assert env["ALLOW_EMAIL_LOGIN"] == "false"
    assert env["OPENID_ISSUER"] == "https://auth.platform.internal"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/enki-trust/ca.crt"
    assert any(
        source.get("secretRef", {}).get("name") == "librechat-credentials-env"
        for source in container["envFrom"]
    )
    assert any(
        volume.get("configMap", {}).get("name") == "enki-platform-trust"
        for volume in pod["volumes"]
    )
    assert any(mount["mountPath"] == "/etc/enki-trust" for mount in container["volumeMounts"])


def test_pinned_helm_charts_render(config):
    deployment = Deployment(config, ROOT, Runner(config.profile.context))
    deployment.runner.installation = "test-render-only"
    deployment.compose()
    deployment.render_charts("1.34.0")
    assert all(deployment.templates[release.name] for release in deployment.releases)
    gpu_policy = next(
        doc for doc in deployment.templates["gpu-operator"] if doc["kind"] == "ClusterPolicy"
    )
    assert gpu_policy["spec"]["dcgmExporter"]["serviceMonitor"]["enabled"] is False
    assert any(
        doc["kind"] == "ServiceMonitor" and doc["metadata"]["name"] == "gpu-operator-dcgm"
        for doc in deployment.resources["content"]
    )
    grafana = next(doc for doc in deployment.templates["grafana"] if doc["kind"] == "Deployment")
    assert grafana["spec"]["template"]["metadata"]["labels"]["enki.stack/component"] == "monitoring"
    assert grafana["metadata"]["annotations"]["configmap.reloader.stakater.com/reload"] == TRUST
    mounts = grafana["spec"]["template"]["spec"]["volumes"]
    assert any(volume.get("configMap", {}).get("name") == TRUST for volume in mounts)
    settings = next(
        doc
        for doc in deployment.templates["grafana"]
        if doc["kind"] == "ConfigMap" and "grafana.ini" in doc.get("data", {})
    )["data"]["grafana.ini"]
    assert "tls_client_ca = /etc/enki-trust/ca.crt" in settings
    crds = {
        doc["metadata"]["name"]: doc
        for doc in deployment.resources["gateway-controller"]
        if doc["kind"] == "CustomResourceDefinition"
    }

    def fields(name):
        schema = next(
            v["schema"]["openAPIV3Schema"] for v in crds[name]["spec"]["versions"] if v["storage"]
        )
        return schema["properties"]["spec"]["properties"]

    backend = fields("backends.gateway.envoyproxy.io")
    assert "caCertificateRefs" in backend["tls"]["properties"]
    policy = fields("securitypolicies.gateway.envoyproxy.io")
    assert "backendRefs" in policy["oidc"]["properties"]["provider"]["properties"]
    assert (
        "backendRefs"
        in policy["jwt"]["properties"]["providers"]["items"]["properties"]["remoteJWKS"][
            "properties"
        ]
    )


def test_kserve_crd_charts_have_unique_ownership(config):
    deployment = Deployment(config, ROOT, Runner(config.profile.context), ["kserve"])
    deployment.runner.installation = "test-render-only"
    deployment.acting = ["kserve"]
    deployment.releases = [
        release
        for release in deployment.releases
        if release.name in {"kserve-crd", "kserve-llmisvc-crd"}
    ]
    deployment.render_charts("1.34.0")
    owners = {}
    for release in deployment.releases:
        for doc in deployment.templates[release.name]:
            if doc["kind"] == "CustomResourceDefinition":
                name = doc["metadata"]["name"]
                assert name not in owners, (
                    f"{name} rendered by {owners.get(name)} and {release.name}"
                )
                owners[name] = release.name
    assert owners["clusterstoragecontainers.serving.kserve.io"] == "kserve-crd"
    assert owners["llminferenceserviceconfigs.serving.kserve.io"] == "kserve-llmisvc-crd"
    assert owners["llminferenceservices.serving.kserve.io"] == "kserve-llmisvc-crd"
