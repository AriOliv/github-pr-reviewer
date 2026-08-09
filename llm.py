"""LLM routing: send model calls through a LiteLLM proxy for cost tracking.

Every model call the bots make can go through a self-hosted LiteLLM proxy so
spend is tracked per session. When `LITELLM_BASE_URL` is set, `generate_json`
calls the proxy's OpenAI-compatible `/chat/completions`; otherwise it falls back
to the google-genai client (`generate_content`). No new dependency — the proxy
call uses `requests`, already required by the project.

Cost attribution:
  * `user`  = a stable per-thread session id (see `session_id`), so all calls
    for one PR / issue / scan roll up together in LiteLLM.
  * header `x-litellm-tags: gh-pr-reviewer,<kind>` tags the traffic by bot.

Managed Agents note: the review's managed-agent path (`interactions.create`) is
a Google agent API that a model proxy cannot intercept — only model calls
(`generate_content` / `/chat/completions`) route through LiteLLM. So when the
proxy is configured, callers should prefer the model-only path to keep 100% of
spend tracked (review_pr.py does this unless USE_MANAGED_AGENTS=1).

Env:
  LITELLM_BASE_URL   proxy base, e.g. https://litellm.example.com (enables proxy)
  LITELLM_API_KEY    proxy key / virtual key (sent as Bearer)
  LLM_MODEL          model name to request (default: GEMINI_MODEL or gemini-3.6-flash)
  LLM_SESSION_ID     override the derived session id
  LITELLM_TIMEOUT    request timeout seconds (default 600)
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

TAG_PREFIX = "gh-pr-reviewer"


def proxy_enabled() -> bool:
    return bool(os.environ.get("LITELLM_BASE_URL"))


def model_name() -> str:
    return os.environ.get("LLM_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"


def session_id(kind: str, *, repo: str | None = None, ref: str | int | None = None) -> str:
    """Stable identifier sent as `user` so LiteLLM groups a thread's spend.

    `LLM_SESSION_ID` overrides everything. Otherwise it is derived from the repo
    and the PR/issue number (or 'scan'), e.g. `owner/repo#pr-12`, so every call
    in one review thread — first pass, follow-ups, fallback — shares one id.
    """
    override = os.environ.get("LLM_SESSION_ID")
    if override:
        return override
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "repo")
    if ref is None:
        return f"{repo}#{kind}"
    suffix = {"review": "pr", "issue": "issue", "scan": "scan"}.get(kind, kind)
    return f"{repo}#{suffix}-{ref}"


def _extract_openai_text(data: dict[str, Any]) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def generate_json(
    *,
    prompt: str,
    system_instruction: str,
    kind: str,
    session: str,
    genai_client: Any | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> str:
    """Return the model's text reply (expected to be JSON).

    Routes through LiteLLM when configured, else through the provided
    google-genai client. `kind` is one of review|issue|scan (used for tags);
    `session` is the id sent as `user` for cost roll-up.
    """
    mdl = model or model_name()
    if proxy_enabled():
        base = os.environ["LITELLM_BASE_URL"].rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "x-litellm-tags": f"{TAG_PREFIX},{kind}",
        }
        key = os.environ.get("LITELLM_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": mdl,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "user": session,
            "response_format": {"type": "json_object"},
        }
        to = timeout if timeout is not None else float(os.environ.get("LITELLM_TIMEOUT", "600"))
        resp = requests.post(
            f"{base}/chat/completions", headers=headers, json=payload, timeout=to
        )
        resp.raise_for_status()
        return _extract_openai_text(resp.json())

    # Fallback: direct google-genai model call.
    if genai_client is None:
        raise RuntimeError(
            "LiteLLM proxy not configured (set LITELLM_BASE_URL) and no genai "
            "client was provided for the fallback path."
        )
    response = genai_client.models.generate_content(
        model=mdl,
        contents=prompt,
        config={
            "system_instruction": system_instruction,
            "response_mime_type": "application/json",
        },
    )
    return response.text or ""
