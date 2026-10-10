"""Google AI Studio (Gemini) goes through its OpenAI-compatible endpoint with the Google key."""

import asyncio
import dataclasses
import json

import httpx

from app.config import Settings
from app.llm import LLMClient


def test_google_uses_the_compatible_endpoint_and_key():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    s = dataclasses.replace(Settings(), llm_provider="google", google_api_key="AIza-test", google_model="gemini-x")
    llm = LLMClient(s)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert llm.provider == "google" and llm.model == "gemini-x"
    assert asyncio.run(llm._text("sys", "hi")) == "hello"
    url, auth, body = seen[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert auth == "Bearer AIza-test" and body["model"] == "gemini-x"


def test_google_without_a_key_is_no_provider():
    assert LLMClient(dataclasses.replace(Settings(), llm_provider="google", google_api_key="")).provider == "none"
