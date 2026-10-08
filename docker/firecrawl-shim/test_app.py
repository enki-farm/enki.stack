import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app as shim

KEY = "shim-key"
AUTH = {"Authorization": f"Bearer {KEY}"}

CRAWL_RESULT = {
    "url": "https://example.com",
    "success": True,
    "html": "<html><body><h1>Hi</h1></body></html>",
    "cleaned_html": "<h1>Hi</h1>",
    "markdown": {"raw_markdown": "# Hi", "fit_markdown": ""},
    "metadata": {"title": "Example", "description": None},
    "status_code": 200,
    "redirected_url": "https://example.com/",
    "links": {"internal": [{"href": "https://example.com/a"}], "external": []},
}


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_SHIM_API_KEY", KEY)
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "c4a-token")
    seen = {}

    def factory(status=200, body=None):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(status, json=body if body is not None else {"success": True, "results": [CRAWL_RESULT]})

        return TestClient(shim.create_app(transport=httpx.MockTransport(handler)))

    factory.seen = seen
    return factory


@pytest.mark.parametrize("path", ["/v1/scrape", "/v2/scrape"])
def test_scrape_maps_firecrawl_shape(make_client, path):
    with make_client() as client:
        resp = client.post(path, headers=AUTH, json={"url": "https://example.com", "formats": ["markdown", "rawHtml"], "timeout": 20000})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["markdown"] == "# Hi"
    assert data["rawHtml"] == CRAWL_RESULT["html"]
    assert "html" not in data
    assert data["metadata"] == {
        "title": "Example",
        "sourceURL": "https://example.com",
        "url": "https://example.com/",
        "statusCode": 200,
    }

    upstream = make_client.seen["request"]
    assert upstream.url.path == "/crawl"
    assert upstream.headers["authorization"] == "Bearer c4a-token"
    params = json.loads(upstream.content)["crawler_config"]["params"]
    assert params == {"cache_mode": "bypass", "page_timeout": 16000}


def test_options_are_translated():
    req = shim.ScrapeRequest(url="https://x.io", timeout=999_999, waitFor=1500, includeTags=["main"], excludeTags=["nav", ".ads"])
    assert shim.to_crawler_params(req) == {
        "cache_mode": "bypass",
        "page_timeout": 48_000,
        "delay_before_return_html": 1.5,
        "css_selector": "main",
        "excluded_selector": "nav, .ads",
    }


def test_links_and_html_formats(make_client):
    with make_client() as client:
        data = client.post("/v2/scrape", headers=AUTH, json={"url": "https://example.com", "formats": ["html", "links"]}).json()["data"]
    assert data["html"] == "<h1>Hi</h1>"
    assert data["links"] == ["https://example.com/a"]
    assert "markdown" not in data


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {KEY}"}])
def test_rejects_bad_auth(make_client, headers):
    with make_client() as client:
        resp = client.post("/v2/scrape", headers=headers, json={"url": "https://example.com"})
    assert resp.status_code == 401
    assert resp.json()["success"] is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com", "example.com"])
def test_rejects_non_http_urls(make_client, url):
    with make_client() as client:
        resp = client.post("/v2/scrape", headers=AUTH, json={"url": url})
    assert resp.status_code == 400


def test_upstream_failures(make_client):
    with make_client(status=500, body={}) as client:
        assert client.post("/v2/scrape", headers=AUTH, json={"url": "https://example.com"}).status_code == 502

    with make_client(status=400, body={"detail": "URL blocked (SSRF protection): URL blocked"}) as client:
        resp = client.post("/v2/scrape", headers=AUTH, json={"url": "http://kubernetes.default.svc/"})
    assert resp.status_code == 400
    assert "SSRF" in resp.json()["error"]

    failed = {"success": True, "results": [{"success": False, "error_message": "blocked: private address"}]}
    with make_client(body=failed) as client:
        resp = client.post("/v2/scrape", headers=AUTH, json={"url": "https://example.com"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "blocked: private address"


def test_health_is_public(make_client):
    with make_client() as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_requires_secrets(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_SHIM_API_KEY", raising=False)
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "x")
    with pytest.raises(RuntimeError):
        shim.create_app()
