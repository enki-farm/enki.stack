"""Firecrawl-compatible /scrape API backed by a Crawl4AI server."""

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("firecrawl-shim")

DEFAULT_TIMEOUT_MS = 30_000
# Crawl4AI clamps untrusted page_timeout to 60s.
MAX_TIMEOUT_MS = 60_000
SUPPORTED_FORMATS = {"markdown", "html", "rawHtml", "links"}


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    formats: list[str] = ["markdown"]
    timeout: int | None = None
    waitFor: int | None = None
    includeTags: list[str] | None = None
    excludeTags: list[str] | None = None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": message})


def _markdown(result: dict[str, Any]) -> str:
    md = result.get("markdown")
    if isinstance(md, dict):
        return md.get("raw_markdown") or md.get("fit_markdown") or ""
    return md or ""


def _links(result: dict[str, Any]) -> list[str]:
    links = result.get("links") or {}
    return [
        link["href"]
        for group in ("internal", "external")
        for link in links.get(group, [])
        if isinstance(link, dict) and link.get("href")
    ]


def to_crawler_params(req: ScrapeRequest) -> dict[str, Any]:
    # Firecrawl's timeout covers the whole request; leave headroom for rendering and the response.
    timeout_ms = int(min(req.timeout or DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS) * 0.8)
    params: dict[str, Any] = {"cache_mode": "bypass", "page_timeout": timeout_ms}
    if req.waitFor:
        params["delay_before_return_html"] = req.waitFor / 1000
    if req.includeTags:
        params["css_selector"] = ", ".join(req.includeTags)
    if req.excludeTags:
        params["excluded_selector"] = ", ".join(req.excludeTags)
    return params


def to_firecrawl(req: ScrapeRequest, result: dict[str, Any]) -> dict[str, Any]:
    formats = set(req.formats) & SUPPORTED_FORMATS or {"markdown"}
    meta = result.get("metadata") or {}
    data: dict[str, Any] = {
        "metadata": {
            **{k: v for k, v in meta.items() if v is not None},
            "sourceURL": req.url,
            "url": result.get("redirected_url") or result.get("url") or req.url,
            "statusCode": result.get("status_code"),
        }
    }
    if "markdown" in formats:
        data["markdown"] = _markdown(result)
    if "html" in formats:
        data["html"] = result.get("cleaned_html") or ""
    if "rawHtml" in formats:
        data["rawHtml"] = result.get("html") or ""
    if "links" in formats:
        data["links"] = _links(result)
    return {"success": True, "data": data}


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    api_key = os.environ.get("FIRECRAWL_SHIM_API_KEY", "")
    crawl4ai_url = os.environ.get("CRAWL4AI_URL", "http://localhost:11235").rstrip("/")
    crawl4ai_token = os.environ.get("CRAWL4AI_API_TOKEN", "")
    if not api_key or not crawl4ai_token:
        raise RuntimeError("FIRECRAWL_SHIM_API_KEY and CRAWL4AI_API_TOKEN must be set")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=crawl4ai_url,
            headers={"Authorization": f"Bearer {crawl4ai_token}"},
            transport=transport,
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Firecrawl shim for Crawl4AI", lifespan=lifespan, docs_url=None, redoc_url=None)

    def require_key(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/scrape", dependencies=[Depends(require_key)])
    @app.post("/v2/scrape", dependencies=[Depends(require_key)])
    async def scrape(req: ScrapeRequest, request: Request) -> JSONResponse:
        parsed = urlparse(req.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return _error(400, "url must be an absolute http(s) URL")

        params = to_crawler_params(req)
        payload = {
            "urls": [req.url],
            "crawler_config": {"type": "CrawlerRunConfig", "params": params},
        }
        try:
            resp = await request.app.state.client.post(
                "/crawl", json=payload, timeout=params["page_timeout"] / 1000 + 15
            )
        except httpx.HTTPError as exc:
            logger.warning("crawl4ai request failed for %s: %s", req.url, exc)
            return _error(502, f"crawl4ai request failed: {exc.__class__.__name__}")

        if resp.status_code != 200:
            logger.warning("crawl4ai returned %s for %s", resp.status_code, req.url)
            # 400/422 are request rejections (e.g. SSRF guard); anything else is an upstream fault.
            if resp.status_code in (400, 422):
                try:
                    detail = resp.json().get("detail")
                except ValueError:
                    detail = None
                return _error(400, str(detail or "rejected by crawl4ai"))
            return _error(502, f"crawl4ai returned HTTP {resp.status_code}")

        results = resp.json().get("results") or []
        if not results or not results[0].get("success"):
            message = results[0].get("error_message") if results else None
            return _error(502, message or "crawl failed")

        return JSONResponse(to_firecrawl(req, results[0]))

    return app
