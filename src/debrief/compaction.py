"""Experimental model compaction for ``debrief squash --model`` (decision D5).

Off unless ``[experimental] compaction = true``. Two small standard-library
adapters speak the OpenAI chat-completions API and the Anthropic messages API,
which covers local model servers, hosted APIs and intranet deployments such
as Azure. The endpoint must be on the residency allowlist, the API key comes
from an environment variable, and the output is a separate file labeled as
model-written: it never replaces agent-written records.

Journals are compacted leg by leg and then combined, so a small local context
window is enough.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from . import config, paths, records, residency, util

PROMPT_VERSION = "compaction-v1"
MAX_CHUNK_CHARS = 24000
TIMEOUT = 180

SYSTEM_PROMPT = (
    "You summarize an AI coding agent's work log for a human reviewer. Be factual and specific. "
    "Never invent work that is not in the log. Keep file paths, symbols and test names exactly as written."
)


class CompactionError(RuntimeError):
    pass


def provider_settings(cfg: config.Config) -> Dict[str, str]:
    name = cfg.get("compaction", "provider")
    if not name:
        raise CompactionError(f"Set [compaction] provider in {cfg.path} to a [provider.<name>] section.")
    section = cfg.section(f"provider.{name}")
    if not section:
        raise CompactionError(f"{cfg.path} has no [provider.{name}] section.")
    missing = [key for key in ("api", "base_url", "model") if not section.get(key)]
    if missing:
        raise CompactionError(f"[provider.{name}] needs {', '.join(missing)}.")
    if section["api"] not in ("openai", "anthropic"):
        raise CompactionError(f"[provider.{name}] api must be openai or anthropic.")
    section["name"] = name
    return section


def _auth_headers(provider: Dict[str, str]) -> Dict[str, str]:
    env = provider.get("api_key_env")
    key = os.environ.get(env) if env else None
    if env and not key:
        raise CompactionError(f"The API key variable {env} is not set.")
    if not key:
        return {}
    style = (provider.get("auth_header") or ("x-api-key" if provider["api"] == "anthropic" else "authorization")).lower()
    if style == "authorization":
        return {"Authorization": f"Bearer {key}"}
    if style == "api-key":
        return {"api-key": key}
    if style == "x-api-key":
        return {"x-api-key": key}
    raise CompactionError("auth_header must be authorization, api-key or x-api-key.")


def _post(url: str, payload: dict, headers: Dict[str, str]) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise CompactionError(f"{url} answered {exc.code}: {body}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CompactionError(f"Could not reach {url}: {exc}") from None


def complete(provider: Dict[str, str], prompt: str, cfg: config.Config, max_tokens: int = 1200) -> str:
    base = provider["base_url"].rstrip("/")
    residency.check_url(base, "Compaction endpoint", cfg)
    headers = _auth_headers(provider)
    if provider["api"] == "openai":
        url = base + "/chat/completions"
        payload = {"model": provider["model"], "max_tokens": max_tokens, "temperature": 0.2,
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]}
        data = _post(url, payload, headers)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise CompactionError("Unexpected response shape from the OpenAI-compatible endpoint.") from None
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers["anthropic-version"] = provider.get("anthropic_version") or "2023-06-01"
    payload = {"model": provider["model"], "max_tokens": max_tokens, "system": SYSTEM_PROMPT,
               "messages": [{"role": "user", "content": prompt}]}
    data = _post(url, payload, headers)
    try:
        return "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text").strip()
    except (KeyError, TypeError, AttributeError):
        raise CompactionError("Unexpected response shape from the Anthropic endpoint.") from None


def _journal_text(feature: dict, leg_id: str) -> List[str]:
    chunks: List[str] = []
    current = ""
    for sess in feature["sessions"]:
        if sess["meta"].get("leg_id") != leg_id:
            continue
        for entry in sess["journal"]:
            text = f"[{entry['at']} {entry['kind']}] {entry['text']}\n"
            if len(current) + len(text) > MAX_CHUNK_CHARS and current:
                chunks.append(current)
                current = ""
            current += text
    if current:
        chunks.append(current)
    return chunks


def compact_feature(project_id: str, feature_id: str, root: Optional[Path] = None,
                    cfg: Optional[config.Config] = None) -> Path:
    cfg = cfg or config.load()
    if not cfg.get_bool("experimental", "compaction"):
        raise CompactionError(f"Model compaction is experimental and off. Set [experimental] compaction = true "
                              f"in {cfg.path} to enable it.")
    provider = provider_settings(cfg)
    feature_dir = paths.feature_dir(project_id, feature_id, root)
    feature = records.load_feature(feature_dir)
    leg_summaries = []
    for leg in feature["legs"]:
        chunks = _journal_text(feature, leg["leg_id"])
        if not chunks:
            continue
        partials = []
        for i, chunk in enumerate(chunks, 1):
            partials.append(complete(provider, (
                f"Summarize part {i} of {len(chunks)} of the journal for {leg['leg_id']} of feature {feature_id}. "
                "List what was built, decisions with their reasons, findings, test results and open problems, "
                f"as short bullet points.\n\n{chunk}"), cfg))
        summary = partials[0] if len(partials) == 1 else complete(provider, (
            f"Merge these partial summaries of {leg['leg_id']} into one list of short bullet points, "
            "removing repetition:\n\n" + "\n\n".join(partials)), cfg)
        leg_summaries.append((leg["leg_id"], summary))
    if not leg_summaries:
        raise CompactionError("No journal entries to compact.")
    brief = (feature.get("brief") or {}).get("body", "")
    combined = complete(provider, (
        "Write one consolidated account of this feature for a reviewer, in Markdown with the sections "
        "## Outcome, ## How it unfolded (one short paragraph per leg), ## Decisions, ## Open risks. "
        "Use the agent's final brief as the source of truth and the leg summaries for history.\n\n"
        f"Final brief:\n{brief[:MAX_CHUNK_CHARS]}\n\n" +
        "\n\n".join(f"{leg_id} summary:\n{summary}" for leg_id, summary in leg_summaries)), cfg, max_tokens=1800)
    stamp = util.now_iso()
    header = "\n".join([
        "---",
        f"feature_id: {feature_id}",
        "kind: model-compaction",
        "model_written: true",
        f"provider: {provider['name']}",
        f"api: {provider['api']}",
        f"model: {provider['model']}",
        f"prompt_version: {PROMPT_VERSION}",
        f"generated_at: {stamp}",
        "---",
        "",
        "> Model-written summary (experimental). The agent-written records remain authoritative.",
        "",
    ])
    legs_text = "\n\n".join(f"### {leg_id}\n\n{summary}" for leg_id, summary in leg_summaries)
    out = feature_dir / "squash" / "compaction.md"
    util.write_text(out, header + combined.strip() + "\n\n## Leg summaries\n\n" + legs_text + "\n")
    return out
