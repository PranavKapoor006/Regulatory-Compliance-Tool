from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict

import requests


LOGGER = logging.getLogger(__name__)


def _extract_json(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def _gemini(system_prompt: str, user_prompt: str) -> Dict[str, Any] | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2400")),
            "responseMimeType": "application/json",
        },
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0].get("text", "")
        return _extract_json(text)
    except Exception as exc:
        LOGGER.warning("Gemini request failed: %s", exc)
        return None


def _ollama(system_prompt: str, user_prompt: str) -> Dict[str, Any] | None:
    if os.getenv("ENABLE_OLLAMA_FALLBACK", "false").lower() not in {"true", "1", "yes"}:
        return None
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    try:
        response = requests.post(f"{base}/api/chat", json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        return _extract_json(data.get("message", {}).get("content", ""))
    except Exception:
        return None


def chat_json(system_prompt: str, user_prompt: str) -> Dict[str, Any] | None:
    provider = os.getenv("LLM_PROVIDER", "gemini").lower().strip()
    if provider == "gemini":
        result = _gemini(system_prompt, user_prompt)
        if result is not None:
            return result
    result = _ollama(system_prompt, user_prompt)
    if result is not None:
        return result
    return None
