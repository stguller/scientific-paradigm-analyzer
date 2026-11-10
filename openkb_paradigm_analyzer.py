#!/usr/bin/env python3
"""OpenKB Scientific Paradigm Analyzer.

Evaluates OpenKB concept pages (e.g., all 954 concepts in ``openkb-kb``) using
the ``scientific-paradigm-analyzer`` skill framework:
  Phase 1: Data / Observation (Tycho Brahe)
  Phase 2: Pattern / Rule (Kepler)
  Phase 3: Mechanism / Proof (Newton)
  + Epistemic Shift & Understanding Gap ("cevap var ama fikir karanlıkta")

Reads concept pages from OpenKB Studio's REST API, checkpoints structured
evaluations into a local SQLite database, writes the analysis block back into
each concept page via ``PUT /api/v1/page`` (preserving OKF frontmatter), and
generates a consolidated Markdown report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SKILL_PATH = REPO_ROOT / "skills" / "scientific-paradigm-analyzer" / "SKILL.md"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "paradigm_evaluations.db"
DEFAULT_REPORT_PATH = REPO_ROOT / "data" / "scientific_paradigm_analysis_report.md"

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


def load_skill_instructions(skill_path: Path) -> str:
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
    """Resolve API key from CLI arg, environment variables, or local Hermes key files."""
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

    # Fallback: read KEYS dict from local micro_router.py if present on this machine.
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


def fetch_concepts(openkb_url: str, kb: str, bearer_token: str | None = None) -> list[dict[str, Any]]:
    """Fetch all Concept nodes from OpenKB Studio's /api/v1/graph endpoint."""
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    graph = http_json(
        f"{openkb_url.rstrip('/')}/api/v1/graph",
        method="POST",
        payload={"kb": kb},
        headers=headers,
        timeout=60,
    )
    concepts = [
        node
        for node in graph.get("nodes", [])
        if str(node.get("id", "")).startswith("concepts/")
        or str(node.get("type", "")).lower() == "concept"
    ]
    concepts.sort(key=lambda n: str(n.get("id", "")))
    return concepts


def fetch_page(openkb_url: str, kb: str, path: str, bearer_token: str | None = None) -> str:
    """Fetch full markdown content of an OpenKB wiki page."""
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    resp = http_json(
        f"{openkb_url.rstrip('/')}/api/v1/page",
        method="POST",
        payload={"kb": kb, "path": path},
        headers=headers,
        timeout=30,
    )
    return str(resp.get("content", ""))


def put_page(
    openkb_url: str,
    kb: str,
    path: str,
    body_without_frontmatter: str,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Update an OpenKB wiki page body via PUT /api/v1/page (frontmatter preserved server-side)."""
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    return http_json(
        f"{openkb_url.rstrip('/')}/api/v1/page",
        method="PUT",
        payload={"kb": kb, "path": path, "content": body_without_frontmatter},
        headers=headers,
        timeout=60,
    )


def split_frontmatter(markdown_text: str) -> tuple[str, str]:
    """Split leading YAML frontmatter block from markdown body.

    OpenKB's edit_wiki_page expects only the body (it re-attaches the stored
    OKF frontmatter verbatim and never strips incoming frontmatter).
    """
    m = re.match(r"\A---\r?\n.*?\r?\n---\r?\n?", markdown_text, flags=re.DOTALL)
    if not m:
        return "", markdown_text
    return m.group(0), markdown_text[m.end() :]


def strip_existing_analysis_block(body: str) -> str:
    """Remove any previous scientific-paradigm-analyzer block for idempotent updates."""
    pattern = re.compile(
        rf"\n*{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n*",
        flags=re.DOTALL,
    )
    cleaned = pattern.sub("\n\n", body)
    return cleaned.rstrip() + "\n"


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract the first valid JSON object from an LLM response."""
    text = raw_text.strip()
    # Strip markdown code fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def evaluate_concept_with_llm(
    *,
    llm: LLMConfig,
    skill_instructions: str,
    concept_id: str,
    description: str,
    sources: list[str],
    body_text: str,
    source_snippets: str = "",
) -> dict[str, Any]:
    """Evaluate a concept using the scientific-paradigm-analyzer schema."""
    system_prompt = (
        "You are an expert science-methodology analyst applying the "
        "`scientific-paradigm-analyzer` skill.\n\n"
        "SKILL INSTRUCTIONS:\n"
        f"{skill_instructions}\n\n"
        "OUTPUT CONTRACT:\n"
        "Return ONLY a single valid JSON object (no markdown fences, no extra prose) "
        "with these exact string/number keys:\n"
        "- domain: concise scientific domain/subfield\n"
        "- core_breakthrough: 1-2 sentence synthesis of what this concept/work establishes\n"
        "- epistemic_mode: one of 'Human', 'AI-Assisted', 'Automated Black-Box', 'Hybrid'\n"
        "- current_paradigm_stage: one of 'Phase 1 (Data/Tycho)', 'Phase 2 (Pattern/Kepler)', 'Phase 3 (Mechanism/Newton)'\n"
        "- phase_1_data: {findings, agent, reference} (Tycho Brahe raw observations/datasets/measurements)\n"
        "- phase_2_pattern: {findings, agent, reference} (Kepler empirical rules/correlations/models)\n"
        "- phase_3_mechanism: {findings, agent, reference} (Newton causal laws/theoretical or formal Lean proofs; explicitly note if absent)\n"
        "- verification_vs_comprehension: analysis of whether empirical/formal validity outpaces human conceptual intuition\n"
        "- black_box_tension: concrete assessment of any 'cevap var ama fikir karanlıkta' (understanding gap) or correlation-vs-causation risk\n"
        "- methodological_takeaway: 1-2 sentence actionable takeaway\n"
        "- mechanistic_depth_score: integer 1-5 (1=raw observation, 3=empirical pattern, 5=causal mechanism or formal proof)\n"
        "Cite the provided OpenKB sources (e.g., [[summaries/...]]) when referencing evidence, and never invent citations."
    )

    clean_body = strip_existing_analysis_block(body_text)
    sources_str = ", ".join(sources) if sources else "None listed"
    user_prompt = (
        f"Concept ID: {concept_id}\n"
        f"Description: {description}\n"
        f"Linked Sources: {sources_str}\n\n"
        f"Concept Content:\n{clean_body[:6000]}"
    )
    if source_snippets:
        user_prompt += f"\n\nSource Excerpts:\n{source_snippets[:2500]}"

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
            # Validate minimal structure
            for req_key in ("domain", "core_breakthrough", "phase_1_data", "phase_2_pattern", "phase_3_mechanism"):
                if req_key not in parsed:
                    raise ValueError(f"Missing key '{req_key}' in LLM JSON output")
            return parsed
        except Exception as exc:
            last_err = exc
            if attempt < llm.max_retries:
                time.sleep(min(2.0**attempt, 12.0))

    raise RuntimeError(f"LLM evaluation failed after {llm.max_retries} attempts: {last_err}")


def _fmt_cell(val: Any) -> str:
    return str(val or "—").replace("|", "\\|").replace("\n", " ").strip()


def render_analysis_markdown(
    *,
    concept_id: str,
    analysis: dict[str, Any],
    model_name: str,
    evaluated_at: str,
) -> str:
    """Render the evaluation block formatted for OpenKB Studio concept pages."""
    p1 = analysis.get("phase_1_data") or {}
    p2 = analysis.get("phase_2_pattern") or {}
    p3 = analysis.get("phase_3_mechanism") or {}

    lines = [
        START_MARKER,
        "## Scientific Paradigm Analysis",
        "",
        f"- **Domain:** {analysis.get('domain', '—')}",
        f"- **Core Breakthrough:** {analysis.get('core_breakthrough', '—')}",
        f"- **Current Paradigm Stage:** {analysis.get('current_paradigm_stage', '—')} "
        f"(Mechanistic Depth: {analysis.get('mechanistic_depth_score', '—')}/5)",
        f"- **Epistemic Mode:** {analysis.get('epistemic_mode', '—')}",
        f"- **Evaluated By:** `scientific-paradigm-analyzer` (`{model_name}`, {evaluated_at})",
        "",
        "| Phase | Historical Archetype | Findings in Concept & Sources | Agent | Reference |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| **1. Data & Observation** | Tycho Brahe | {_fmt_cell(p1.get('findings'))} | {_fmt_cell(p1.get('agent'))} | {_fmt_cell(p1.get('reference'))} |",
        f"| **2. Pattern & Rule** | Kepler | {_fmt_cell(p2.get('findings'))} | {_fmt_cell(p2.get('agent'))} | {_fmt_cell(p2.get('reference'))} |",
        f"| **3. Mechanism & Proof** | Newton | {_fmt_cell(p3.get('findings'))} | {_fmt_cell(p3.get('agent'))} | {_fmt_cell(p3.get('reference'))} |",
        "",
        "### Epistemic Shift & Understanding Gap",
        f"- **Verification vs. Comprehension:** {analysis.get('verification_vs_comprehension', '—')}",
        f"- **Black-Box Tension:** {analysis.get('black_box_tension', '—')}",
        "",
        "### Methodological Takeaway",
        f"- {analysis.get('methodological_takeaway', '—')}",
        END_MARKER,
    ]
    return "\n".join(lines)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create/open local SQLite checkpoint database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_evaluations (
            concept_id TEXT PRIMARY KEY,
            kb TEXT NOT NULL,
            description TEXT,
            sources_json TEXT,
            domain TEXT,
            epistemic_mode TEXT,
            current_paradigm_stage TEXT,
            mechanistic_depth_score INTEGER,
            analysis_json TEXT NOT NULL,
            written_to_openkb INTEGER NOT NULL DEFAULT 0,
            model TEXT,
            evaluated_at TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_completed_ids(conn: sqlite3.Connection, *, require_written: bool) -> set[str]:
    """Return concept_ids already processed in the checkpoint database."""
    if require_written:
        rows = conn.execute(
            "SELECT concept_id FROM concept_evaluations WHERE written_to_openkb = 1 AND error IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT concept_id FROM concept_evaluations WHERE error IS NULL"
        ).fetchall()
    return {r[0] for r in rows}


def save_checkpoint(
    conn: sqlite3.Connection,
    db_lock: threading.Lock,
    *,
    concept_id: str,
    kb: str,
    description: str,
    sources: list[str],
    analysis: dict[str, Any] | None,
    written: bool,
    model: str,
    evaluated_at: str,
    error: str | None = None,
) -> None:
    """Persist one concept evaluation into local SQLite."""
    a = analysis or {}
    with db_lock:
        conn.execute(
            """
            INSERT INTO concept_evaluations (
                concept_id, kb, description, sources_json, domain, epistemic_mode,
                current_paradigm_stage, mechanistic_depth_score, analysis_json,
                written_to_openkb, model, evaluated_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(concept_id) DO UPDATE SET
                kb=excluded.kb,
                description=excluded.description,
                sources_json=excluded.sources_json,
                domain=excluded.domain,
                epistemic_mode=excluded.epistemic_mode,
                current_paradigm_stage=excluded.current_paradigm_stage,
                mechanistic_depth_score=excluded.mechanistic_depth_score,
                analysis_json=excluded.analysis_json,
                written_to_openkb=excluded.written_to_openkb,
                model=excluded.model,
                evaluated_at=excluded.evaluated_at,
                error=excluded.error
            """,
            (
                concept_id,
                kb,
                description,
                json.dumps(sources, ensure_ascii=False),
                a.get("domain"),
                a.get("epistemic_mode"),
                a.get("current_paradigm_stage"),
                a.get("mechanistic_depth_score"),
                json.dumps(a, ensure_ascii=False),
                1 if written else 0,
                model,
                evaluated_at,
                error,
            ),
        )
        conn.commit()


def generate_master_report(conn: sqlite3.Connection, report_path: Path, kb: str) -> None:
    """Build a consolidated Markdown report across all evaluated concepts."""
    rows = conn.execute(
        """
        SELECT concept_id, domain, epistemic_mode, current_paradigm_stage,
               mechanistic_depth_score, analysis_json, evaluated_at
        FROM concept_evaluations
        WHERE error IS NULL
        ORDER BY mechanistic_depth_score DESC, concept_id ASC
        """
    ).fetchall()

    total = len(rows)
    stage_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    for _, _, mode, stage, _, _, _ in rows:
        stage_counts[stage or "Unknown"] = stage_counts.get(stage or "Unknown", 0) + 1
        mode_counts[mode or "Unknown"] = mode_counts.get(mode or "Unknown", 0) + 1

    lines = [
        "# OpenKB Scientific Paradigm Analysis Report",
        "",
        f"> **Knowledge Base:** `{kb}` | **Evaluated Concepts:** {total} | **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. Paradigm Stage Distribution (Tycho → Kepler → Newton)",
        "",
        "| Paradigm Stage | Concept Count | Share |",
        "| :--- | :---: | :---: |",
    ]
    for stage, cnt in sorted(stage_counts.items()):
        pct = (cnt * 100 // total) if total else 0
        lines.append(f"| {stage} | {cnt} | {pct}% |")

    lines.extend([
        "",
        "## 2. Epistemic Mode Distribution",
        "",
        "| Epistemic Mode | Concept Count | Share |",
        "| :--- | :---: | :---: |",
    ])
    for mode, cnt in sorted(mode_counts.items()):
        pct = (cnt * 100 // total) if total else 0
        lines.append(f"| {mode} | {cnt} | {pct}% |")

    lines.extend([
        "",
        "## 3. Concept Matrix (Sample / Full Index)",
        "",
        "| Concept | Domain | Stage | Depth | Epistemic Mode | Black-Box / Gap Note |",
        "| :--- | :--- | :--- | :---: | :--- | :--- |",
    ])
    for cid, domain, mode, stage, score, raw_json, _ in rows:
        a = json.loads(raw_json)
        gap = _fmt_cell(a.get("black_box_tension", ""))[:140]
        lines.append(
            f"| [[{cid}]] | {_fmt_cell(domain)} | {_fmt_cell(stage)} | {score}/5 | {_fmt_cell(mode)} | {gap} |"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze OpenKB concepts with scientific-paradigm-analyzer and write back to OpenKB."
    )
    parser.add_argument("--openkb-url", default="http://192.168.1.19:7566", help="OpenKB Studio base URL")
    parser.add_argument("--kb", default="openkb-kb", help="OpenKB knowledge base ID")
    parser.add_argument("--openkb-token", default=os.getenv("OPENKB_TOKEN"), help="Optional OpenKB bearer token")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_DEFAULTS.keys()),
        default="cliproxy",
        help="LLM provider preset",
    )
    parser.add_argument("--llm-url", default=None, help="Override OpenAI-compatible base URL")
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument("--api-key", default=None, help="Override LLM API key")
    parser.add_argument("--skill-path", type=Path, default=DEFAULT_SKILL_PATH, help="Path to SKILL.md")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="Local SQLite checkpoint path")
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH, help="Output master report path")
    parser.add_argument("--workers", type=int, default=4, help="Parallel worker threads")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N concepts (0 = all)")
    parser.add_argument("--filter", default="", help="Only process concepts whose ID contains this substring")
    parser.add_argument("--include-sources", action="store_true", help="Also fetch linked summary excerpts (slower)")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate and checkpoint locally without PUT to OpenKB")
    parser.add_argument("--force", action="store_true", help="Re-evaluate concepts already in checkpoint DB")
    parser.add_argument("--only-missing", action="store_true", help="Skip pages that already have the analysis marker block")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    preset = PROVIDER_DEFAULTS[args.provider]
    base_url = args.llm_url or preset["base_url"]
    model = args.model or preset["model"]
    api_key = resolve_api_key(args.provider, args.api_key)
    if not api_key:
        print("❌ LLM API key not found. Set LLM_API_KEY or pass --api-key.", file=sys.stderr)
        return 2

    if args.provider == "m365" and args.workers > 1:
        print("⚠️ M365 Bridge rate-limits bursts; capping --workers to 1.")
        args.workers = 1

    skill_instructions = load_skill_instructions(args.skill_path)
    llm = LLMConfig(provider=args.provider, base_url=base_url, model=model, api_key=api_key)

    print(f"📡 Fetching concepts from {args.openkb_url} (kb={args.kb})...")
    concepts = fetch_concepts(args.openkb_url, args.kb, args.openkb_token)
    print(f"📚 Found {len(concepts)} concept nodes.")

    if args.filter:
        concepts = [c for c in concepts if args.filter.lower() in str(c.get("id", "")).lower()]
        print(f"🔎 Filtered to {len(concepts)} concepts matching '{args.filter}'.")

    conn = init_db(args.db_path)
    db_lock = threading.Lock()
    if not args.force:
        done = get_completed_ids(conn, require_written=not args.dry_run)
        concepts = [c for c in concepts if c["id"] not in done]
        print(f"⏭️ Remaining after checkpoint resume: {len(concepts)} concepts.")

    if args.limit and args.limit > 0:
        concepts = concepts[: args.limit]
        print(f"🧪 Limited run to {len(concepts)} concepts.")

    if not concepts:
        generate_master_report(conn, args.report_path, args.kb)
        print(f"✅ Nothing pending. Report refreshed: {args.report_path}")
        return 0

    total = len(concepts)
    completed = 0
    errors = 0
    t0 = time.time()

    def process_one(node: dict[str, Any]) -> tuple[str, bool, str | None]:
        cid = str(node["id"])
        desc = str(node.get("description", ""))
        sources = [str(s) for s in (node.get("sources") or [])]
        evaluated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            raw_md = fetch_page(args.openkb_url, args.kb, cid, args.openkb_token)
            if args.only_missing and START_MARKER in raw_md:
                return cid, False, "skipped_marker_exists"

            _, body = split_frontmatter(raw_md)
            source_snippets = ""
            if args.include_sources and sources:
                snippets: list[str] = []
                for src in sources[:2]:
                    src_ref = src[:-3] if src.endswith(".md") else src
                    try:
                        src_md = fetch_page(args.openkb_url, args.kb, src_ref, args.openkb_token)
                        _, src_body = split_frontmatter(src_md)
                        snippets.append(f"### {src_ref}\n{src_body[:1200]}")
                    except Exception:
                        pass
                source_snippets = "\n\n".join(snippets)

            analysis = evaluate_concept_with_llm(
                llm=llm,
                skill_instructions=skill_instructions,
                concept_id=cid,
                description=desc,
                sources=sources,
                body_text=body,
                source_snippets=source_snippets,
            )
            block = render_analysis_markdown(
                concept_id=cid,
                analysis=analysis,
                model_name=llm.model,
                evaluated_at=evaluated_at,
            )
            clean_body = strip_existing_analysis_block(body)
            new_body = f"{clean_body.rstrip()}\n\n{block}\n"

            written = False
            if not args.dry_run:
                put_page(args.openkb_url, args.kb, cid, new_body, args.openkb_token)
                written = True

            save_checkpoint(
                conn,
                db_lock,
                concept_id=cid,
                kb=args.kb,
                description=desc,
                sources=sources,
                analysis=analysis,
                written=written,
                model=llm.model,
                evaluated_at=evaluated_at,
            )
            return cid, True, None
        except Exception as exc:
            save_checkpoint(
                conn,
                db_lock,
                concept_id=cid,
                kb=args.kb,
                description=desc,
                sources=sources,
                analysis=None,
                written=False,
                model=llm.model,
                evaluated_at=evaluated_at,
                error=str(exc),
            )
            return cid, False, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(process_one, node) for node in concepts]
        for fut in as_completed(futures):
            cid, ok, err = fut.result()
            completed += 1
            if not ok and err != "skipped_marker_exists":
                errors += 1
                print(f"  ❌ [{completed}/{total}] {cid}: {err}", file=sys.stderr)
            elif completed % 10 == 0 or completed == total or total <= 10:
                elapsed = max(time.time() - t0, 0.1)
                rate = completed / elapsed
                eta = (total - completed) / max(rate, 1e-6)
                status = "DRY" if args.dry_run else "SAVED"
                print(
                    f"  [{completed}/{total}] {status} {cid} "
                    f"({rate:.2f} concepts/s, ETA {eta:.0f}s)"
                )

    generate_master_report(conn, args.report_path, args.kb)
    print(f"\n📄 Report written: {args.report_path}")
    print(f"💾 Checkpoint DB: {args.db_path}")
    print(f"🏁 Done: {completed - errors}/{total} succeeded, {errors} errors in {time.time() - t0:.1f}s.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
