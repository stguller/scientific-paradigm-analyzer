#!/usr/bin/env python3
"""OpenKB Scientific Paradigm Analyzer.

Evaluates OpenKB concept pages (e.g., all 954 concepts in ``openkb-kb``) using
the ``scientific-paradigm-analyzer`` skill framework, checkpoints structured
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from paradigm_core import (
    DEFAULT_SKILL_PATH,
    END_MARKER,
    PROVIDER_DEFAULTS,
    REPO_ROOT,
    START_MARKER,
    LLMConfig,
    evaluate_with_paradigm_skill,
    fmt_cell,
    http_json,
    load_skill_instructions,
    render_analysis_markdown,
    resolve_api_key,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_DB_PATH = REPO_ROOT / "data" / "paradigm_evaluations.db"
DEFAULT_REPORT_PATH = REPO_ROOT / "data" / "scientific_paradigm_analysis_report.md"


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
    """Split leading YAML frontmatter block from markdown body."""
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
            rendered_page TEXT,
            written_to_openkb INTEGER NOT NULL DEFAULT 0,
            model TEXT,
            evaluated_at TEXT NOT NULL,
            error TEXT
        )
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(concept_evaluations)").fetchall()}
    if "rendered_page" not in cols:
        conn.execute("ALTER TABLE concept_evaluations ADD COLUMN rendered_page TEXT")
    conn.commit()
    return conn


def get_completed_ids(conn: sqlite3.Connection, *, require_written: bool) -> set[str]:
    """Return concept_ids already processed in the checkpoint database."""
    sql = (
        "SELECT concept_id FROM concept_evaluations WHERE written_to_openkb = 1 AND error IS NULL"
        if require_written
        else "SELECT concept_id FROM concept_evaluations WHERE error IS NULL"
    )
    return {r[0] for r in conn.execute(sql).fetchall()}


def save_checkpoint(
    conn: sqlite3.Connection,
    db_lock: threading.Lock,
    *,
    concept_id: str,
    kb: str,
    description: str,
    sources: list[str],
    analysis: dict[str, Any] | None,
    rendered_page: str | None,
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
                rendered_page, written_to_openkb, model, evaluated_at, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(concept_id) DO UPDATE SET
                kb=excluded.kb,
                description=excluded.description,
                sources_json=excluded.sources_json,
                domain=excluded.domain,
                epistemic_mode=excluded.epistemic_mode,
                current_paradigm_stage=excluded.current_paradigm_stage,
                mechanistic_depth_score=excluded.mechanistic_depth_score,
                analysis_json=excluded.analysis_json,
                rendered_page=excluded.rendered_page,
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
                rendered_page,
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
               mechanistic_depth_score, analysis_json
        FROM concept_evaluations
        WHERE error IS NULL
        ORDER BY mechanistic_depth_score DESC, concept_id ASC
        """
    ).fetchall()

    total = len(rows)
    stage_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    for _, _, mode, stage, _, _ in rows:
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
        "## 3. Concept Matrix",
        "",
        "| Concept | Domain | Stage | Depth | Epistemic Mode | Black-Box / Gap Note |",
        "| :--- | :--- | :--- | :---: | :--- | :--- |",
    ])
    for cid, domain, mode, stage, score, raw_json in rows:
        a = json.loads(raw_json)
        gap = fmt_cell(a.get("black_box_tension", ""))[:140]
        lines.append(
            f"| [[{cid}]] | {fmt_cell(domain)} | {fmt_cell(stage)} | {score}/5 | {fmt_cell(mode)} | {gap} |"
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
    parser.add_argument("--provider", choices=sorted(PROVIDER_DEFAULTS.keys()), default="cliproxy")
    parser.add_argument("--llm-url", default=None, help="Override OpenAI-compatible base URL")
    parser.add_argument("--model", default=None, help="Override LLM model name")
    parser.add_argument("--api-key", default=None, help="Override LLM API key")
    parser.add_argument("--lang", default="en", choices=["en", "tr"], help="Output language")
    parser.add_argument("--skill-path", type=Path, default=DEFAULT_SKILL_PATH)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N concepts (0 = all)")
    parser.add_argument("--filter", default="", help="Only process concepts matching substring")
    parser.add_argument("--include-sources", action="store_true", help="Also fetch linked summary excerpts")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate locally without PUT to OpenKB")
    parser.add_argument("--force", action="store_true", help="Re-evaluate concepts already in checkpoint DB")
    parser.add_argument("--only-missing", action="store_true", help="Skip pages that already have marker block")
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

            fm, body = split_frontmatter(raw_md)
            clean_body = strip_existing_analysis_block(body)
            sources_str = ", ".join(sources) if sources else "None listed"
            user_prompt = (
                f"Concept ID: {cid}\n"
                f"Description: {desc}\n"
                f"Linked Sources: {sources_str}\n\n"
                f"Concept Content:\n{clean_body[:6000]}"
            )
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
                if snippets:
                    user_prompt += "\n\nSource Excerpts:\n" + "\n\n".join(snippets)[:2500]

            analysis = evaluate_with_paradigm_skill(
                llm=llm,
                skill_instructions=skill_instructions,
                user_prompt=user_prompt,
                language=args.lang,
            )
            block = render_analysis_markdown(
                analysis=analysis,
                model_name=llm.model,
                evaluated_at=evaluated_at,
                include_markers=True,
            )
            new_body = f"{clean_body.rstrip()}\n\n{block}\n"
            rendered_page = f"{fm}{new_body}"

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
                rendered_page=rendered_page,
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
                rendered_page=None,
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
