"""External peripheral knowledge. Shop facts remain in the authorized local tools."""

import asyncio
from hashlib import sha256

import httpx
from pydantic import BaseModel, Field, HttpUrl, StrictStr, ValidationError

from app.tools.schemas import WebSearchHit, WebSearchInput, WebSearchOutput

WEB_SEARCH_TIMEOUT_SECONDS = 8.0
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"


class WebSearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _DeepSeekHit(BaseModel):
    title: StrictStr
    url: HttpUrl
    snippet: StrictStr
    summary: StrictStr
    rerank_score: float = Field(ge=0, le=3, allow_inf_nan=False)


class _DeepSeekResponse(BaseModel):
    results: list[_DeepSeekHit]


class WebSearchToolService:
    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key.strip()
        self._transport = transport

    async def search(self, request: WebSearchInput) -> WebSearchOutput:
        if not self._api_key:
            raise WebSearchError(
                "web_search_not_configured", "Web search requires an official DeepSeek API key.",
            )
        try:
            # One total deadline, including client setup/connection/body reads. No automatic
            # transport or orchestration retries: an expired request may have spent credits.
            async with asyncio.timeout(WEB_SEARCH_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=WEB_SEARCH_TIMEOUT_SECONDS, transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        DEEPSEEK_SEARCH_URL,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={
                            "query": request.query, "count": request.limit,
                            "need_content": False,
                        },
                    )
        except (TimeoutError, httpx.TimeoutException) as from_error:
            raise WebSearchError(
                "web_search_timeout", "Web search timed out; no automatic retry was made.",
            ) from from_error
        except httpx.HTTPError as exc:
            raise WebSearchError(
                "web_search_unavailable", "Web search is temporarily unavailable.",
            ) from exc

        if response.status_code in {401, 403}:
            raise WebSearchError(
                "web_search_auth_failed", "Web search API credentials were rejected.",
            )
        if response.status_code in {402, 429}:
            raise WebSearchError(
                "web_search_quota_exceeded", "Web search rate or credit limit was reached.",
            )
        if response.status_code in {408, 504}:
            raise WebSearchError(
                "web_search_timeout", "Web search timed out; no automatic retry was made.",
            )
        if not response.is_success:
            raise WebSearchError(
                "web_search_unavailable", "Web search provider could not complete the request.",
            )
        try:
            data = _DeepSeekResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise WebSearchError(
                "web_search_invalid_response", "Web search provider returned an invalid response.",
            ) from exc
        hits = []
        seen = set()
        for item in data.results:
            url = str(item.url)
            if url in seen:
                continue
            seen.add(url)
            hits.append(WebSearchHit(
                # Stable, JS-safe ID supports existing evidence deduplication and memory.
                source_id=int(sha256(url.encode()).hexdigest()[:12], 16),
                url=url, title=item.title, snippet=item.summary.strip() or item.snippet,
                score=item.rerank_score,
                metadata={"provider": "deepseek"},
            ))
            if len(hits) == request.limit:
                break
        return WebSearchOutput(result_type="documents" if hits else "empty", documents=hits)
