from pathlib import Path

import pytest

from enkictl.config import Configuration, dump, private_write

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def profile_path(tmp_path):
    path = tmp_path / "profile.yaml"
    private_write(
        path,
        dump(
            {
                "platform": "dgx-spark",
                "context": "test-only-never-contact",
                "domain": "platform.internal",
                "gpu": "operator",
                "auth": {
                    "connector": "connector.yaml",
                    "hub_group": "test:core",
                    "grafana_viewer_group": "test:users",
                },
                "tls": {"mode": "self-signed"},
            }
        ),
    )
    private_write(
        tmp_path / "connector.yaml",
        dump(
            {
                "connectors": [
                    {
                        "id": "test",
                        "type": "oidc",
                        "config": {
                            "clientID": "$CLIENT_ID",
                            "clientSecret": "$CLIENT_SECRET",
                            "redirectURI": "@DEX_ISSUER@/callback",
                        },
                    }
                ]
            }
        ),
    )
    private_write(
        tmp_path / "secrets.yaml",
        dump(
            {
                "connector": {"CLIENT_ID": "id-test", "CLIENT_SECRET": "never-print-me"},
            }
        ),
    )
    return path


@pytest.fixture
def config(profile_path):
    return Configuration(profile_path)
