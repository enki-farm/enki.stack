import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from enkictl.checks import certificate, quantity, requests, version
from enkictl.config import StackError


@pytest.mark.parametrize(
    ("value", "expected"),
    [("500m", ".5"), ("1Gi", "1073741824"), ("2e3", "2000"), ("50Mi", "52428800")],
)
def test_quantity(value, expected):
    assert quantity(value) == quantity(expected)


def test_requests_include_init_sidecars():
    assert (
        requests(
            {
                "containers": [{"resources": {"requests": {"cpu": "500m"}}}],
                "initContainers": [
                    {"resources": {"requests": {"cpu": "1"}}, "restartPolicy": "Always"},
                    {"resources": {"requests": {"cpu": "2"}}},
                ],
            }
        )["cpu"]
        == 3
    )


def test_version():
    assert version("v1.34.2+k3s1") == (1, 34)
    with pytest.raises(StackError):
        version("unknown")


def test_certificate_validation():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "platform.internal")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("platform.internal"),
                    x509.DNSName("*.platform.internal"),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    secret = {
        "type": "kubernetes.io/tls",
        "data": {
            "tls.crt": base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode(),
            "tls.key": base64.b64encode(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            ).decode(),
        },
    }
    assert certificate(secret, "platform.internal").startswith("-----BEGIN CERTIFICATE")
    with pytest.raises(StackError):
        certificate(secret, "wrong.internal")
