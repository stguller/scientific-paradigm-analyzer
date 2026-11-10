#!/usr/bin/env python3
"""Shared skill, LLM, and formatting helpers for scientific-paradigm-analyzer tools."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SKILL_PATH = REPO_ROOT / "skills" / "scientific-paradigm-analyzer" / "SKILL.md"

START_MARKER = "<!-- scientific-paradigm-analyzer:start -->"
END_MARKER = "<!-- scientific-paradigm-analyzer:end -->"

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "cliproxy": {
        "base_url": "http://192.168.1.7:8317/v1",
        "model": "gemini-3.1-flash-lite",
        "env_key": "CLIPROXY_API_KEY",
        "hermes_key": "cliproxy",
    },
    "freellmapi": {
        "base_url": "http://192.168.1.7:3002/v1",
        "model": "gpt-oss-120b",
        "env_key": "FREELLMAPI_KEY",
        "hermes_key": "freellmapi",
    },
    "freetoken": {
        "base_url": "http://127.0.0.1:1919/v1",
        "model": "Qwen3.6-35B-A3B-NVFP4",
        "env_key": "FREETOKEN_API_KEY",
        "hermes_key": "freetoken",
    },
    "m365": {
        "base_url": "http://192.168.1.7:8230/v1",
        "model": "gpt-5.5",
        "env_key": "M365_API_KEY",
        "hermes_key": "m365",
    },
}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    timeout_sec: int = 120
    max_retries: int = 3


def load_skill_instructions(skill_path: Path = DEFAULT_SKILL_PATH) -> str:
    """Load SKILL.md body (stripping YAML frontmatter) for the system prompt."""
    if not skill_path.is_file():
        raise FileNotFoundError(f"Skill file not found: {skill_path}")
    text = skill_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def resolve_api_key(provider: str, explicit_key: str | None) -> str:
    """Resolve API key from CLI arg, env vars, or local Hermes key files."""
    if explicit_key:
        return explicit_key.strip()

    preset = PROVIDER_DEFAULTS.get(provider, {})
    for env_name in ("LLM_API_KEY", preset.get("env_key", ""), "OPENAI_API_KEY"):
        if env_name and os.getenv(env_name):
            return os.environ[env_name].strip()

    hermes_dir = Path.home() / ".hermes"
    key_file = hermes_dir / "api-keys" / "api_key.txt"
    hermes_key_name = preset.get("hermes_key", provider)
    if key_file.is_file():
        for line in key_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                k, v = line.split(":", 1)
                if k.strip().lower() == hermes_key_name.lower() and v.strip():
                    return v.strip()

    router_file = hermes_dir / "01_orkestrasyon" / "micro_router.py"
    if router_file.is_file():
        content = router_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(
            rf'"{re.escape(hermes_key_name.upper())}"\s*:\s*"([^"]+)"',
            content,
        )
        if m:
            return m.group(1).strip()

    if provider == "freetoken":
        return "freetoken-local"
    return ""


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Perform an HTTP JSON request and return the decoded JSON dict."""
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} on {method} {url}: {body}") from exc


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from an LLM response."""
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def evaluate_with_paradigm_skill(
    *,
    llm: LLMConfig,
    skill_instructions: str,
    user_prompt: str,
    language: str = "en",
) -> dict[str, Any]:
    """Run scientific-paradigm-analyzer prompt and parse validated JSON."""
    lang_note = (
        "Write all natural-language field values in clear academic Turkish."
        if language.lower().startswith("tr")
        else "Write all natural-language field values in clear academic English."
    )

    system_prompt = (
        "You are an expert science-methodology analyst applying the "
        "`scientific-paradigm-analyzer` skill.\n\n"
        "SKILL INSTRUCTIONS:\n"
        f"{skill_instructions}\n\n"
        "OUTPUT CONTRACT:\n"
        f"{lang_note}\n"
        "Return ONLY a single valid JSON object (no markdown fences, no extra prose) "
        "with these exact keys:\n"
        "- domain: concise scientific domain/subfield\n"
        "- core_breakthrough: 1-2 sentence synthesis of what this item establishes\n"
        "- epistemic_mode: one of 'Human', 'AI-Assisted', 'Automated Black-Box', 'Hybrid'\n"
        "- current_paradigm_stage: one of 'Phase 1 (Data/Tycho)', 'Phase 2 (Pattern/Kepler)', 'Phase 3 (Mechanism/Newton)'\n"
        "- phase_1_data: {findings, agent, reference} (Tycho Brahe raw observations/datasets/measurements)\n"
        "- phase_2_pattern: {findings, agent, reference} (Kepler empirical rules/correlations/models)\n"
        "- phase_3_mechanism: {findings, agent, reference} (Newton causal laws/theoretical or formal proofs; explicitly note if absent)\n"
        "- verification_vs_comprehension: analysis of whether empirical/formal validity outpaces human conceptual intuition\n"
        "- black_box_tension: concrete assessment of any 'cevap var ama fikir karanlıkta' (understanding gap) or correlation-vs-causation risk\n"
        "- methodological_takeaway: 1-2 sentence actionable takeaway\n"
        "- mechanistic_depth_score: integer 1-5 (1=raw observation, 3=empirical pattern, 5=causal mechanism or formal proof)\n"
        "Cite only the provided sources/identifiers and never invent numbers or citations."
    )

    url = f"{llm.base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": llm.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }

    last_err: Exception | None = None
    for attempt in range(1, llm.max_retries + 1):
        try:
            resp = http_json(
                url,
                method="POST",
                payload=payload,
                headers={"Authorization": f"Bearer {llm.api_key}"},
                timeout=llm.timeout_sec,
            )
            content = resp["choices"][0]["message"]["content"]
            parsed = extract_json_object(content)
            for req_key in (
                "domain",
                "core_breakthrough",
                "phase_1_data",
                "phase_2_pattern",
                "phase_3_mechanism",
            ):
                if req_key not in parsed:
                    raise ValueError(f"Missing key '{req_key}' in LLM JSON output")
            return parsed
        except Exception as exc:
            last_err = exc
            if attempt < llm.max_retries:
                time.sleep(min(2.0**attempt, 12.0))

    raise RuntimeError(f"LLM evaluation failed after {llm.max_retries} attempts: {last_err}")


def fmt_cell(val: Any) -> str:
    return str(val or "—").replace("|", "\\|").replace("\n", " ").strip()


def render_analysis_markdown(
    *,
    analysis: dict[str, Any],
    model_name: str,
    evaluated_at: str,
    include_markers: bool = True,
) -> str:
    """Render the evaluation block in standard Markdown."""
    p1 = analysis.get("phase_1_data") or {}
    p2 = analysis.get("phase_2_pattern") or {}
    p3 = analysis.get("phase_3_mechanism") or {}

    lines = [
        "## Scientific Paradigm Analysis",
        "",
        f"- **Domain:** {analysis.get('domain', '—')}",
        f"- **Core Breakthrough:** {analysis.get('core_breakthrough', '—')}",
        f"- **Current Paradigm Stage:** {analysis.get('current_paradigm_stage', '—')} "
        f"(Mechanistic Depth: {analysis.get('mechanistic_depth_score', '—')}/5)",
        f"- **Epistemic Mode:** {analysis.get('epistemic_mode', '—')}",
        f"- **Evaluated By:** `scientific-paradigm-analyzer` (`{model_name}`, {evaluated_at})",
        "",
        "| Phase | Historical Archetype | Findings | Agent | Reference |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| **1. Data & Observation** | Tycho Brahe | {fmt_cell(p1.get('findings'))} | {fmt_cell(p1.get('agent'))} | {fmt_cell(p1.get('reference'))} |",
        f"| **2. Pattern & Rule** | Kepler | {fmt_cell(p2.get('findings'))} | {fmt_cell(p2.get('agent'))} | {fmt_cell(p2.get('reference'))} |",
        f"| **3. Mechanism & Proof** | Newton | {fmt_cell(p3.get('findings'))} | {fmt_cell(p3.get('agent'))} | {fmt_cell(p3.get('reference'))} |",
        "",
        "### Epistemic Shift & Understanding Gap",
        f"- **Verification vs. Comprehension:** {analysis.get('verification_vs_comprehension', '—')}",
        f"- **Black-Box Tension:** {analysis.get('black_box_tension', '—')}",
        "",
        "### Methodological Takeaway",
        f"- {analysis.get('methodological_takeaway', '—')}",
    ]
    if include_markers:
        return "\n".join([START_MARKER, *lines, END_MARKER])
    return "\n".join(lines)
