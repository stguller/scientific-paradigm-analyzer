#!/usr/bin/env python3
"""Academic Pipeline (8085) Scientific Paradigm Analyzer.

Reads research articles (`makaleler`, `ozetler`, `yontemler`, `sayisal_sonuclar`)
from the SU+AI Academic Pipeline PostgreSQL database (`academic_pipeline`),
evaluates each paper with the `scientific-paradigm-analyzer` skill framework,
and writes structured + dashboard-visible results back to the SAME database:

1. `paradigma_analizleri` table (structured columns + JSONB per `makale_id`)
2. `notlarim` table (idempotent upsert tagged with
   `{"source": "scientific-paradigm-analyzer", ...}` so the analysis appears
   immediately in the existing http://192.168.1.19:8085/ Live Dashboard).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from paradigm_core import (
    DEFAULT_SKILL_PATH,
    PROVIDER_DEFAULTS,
    REPO_ROOT,
    LLMConfig,
    evaluate_with_paradigm_skill,
    fmt_cell,
    load_skill_instructions,
    render_analysis_markdown,
    resolve_api_key,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_REPORT_PATH = REPO_ROOT / "data" / "academic_paradigm_report.md"

PARADIGM_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS paradigma_analizleri (
    makale_id INTEGER PRIMARY KEY REFERENCES makaleler(id) ON DELETE CASCADE,
    domain TEXT,
    core_breakthrough TEXT,
    epistemic_mode TEXT,
    current_paradigm_stage TEXT,
    mechanistic_depth_score INTEGER,
    phase_1_data JSONB,
    phase_2_pattern JSONB,
    phase_3_mechanism JSONB,
    verification_vs_comprehension TEXT,
    black_box_tension TEXT,
    methodological_takeaway TEXT,
    analysis_json JSONB NOT NULL,
    model TEXT,
    evaluated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
"""


def resolve_pg_config(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve PostgreSQL connection parameters without hardcoding secrets."""
    cfg: dict[str, Any] = {
        "host": args.pg_host or os.getenv("POSTGRES_HOST") or os.getenv("PGHOST") or "192.168.1.19",
        "port": int(args.pg_port or os.getenv("POSTGRES_PORT") or os.getenv("PGPORT") or 5432),
        "dbname": args.pg_db or os.getenv("POSTGRES_DB") or os.getenv("PGDATABASE") or "academic_pipeline",
        "user": args.pg_user or os.getenv("POSTGRES_USER") or os.getenv("PGUSER") or "pipeline",
        "password": args.pg_password or os.getenv("POSTGRES_PASSWORD") or os.getenv("PGPASSWORD") or "",
    }

    if not cfg["password"]:
        hermes_dir = Path.home() / ".hermes"
        env_candidates = [
            REPO_ROOT / ".env",
            hermes_dir / "90_projeler" / "academic-pipeline-v2" / ".env",
            hermes_dir / ".env",
        ]
        for env_file in env_candidates:
            if env_file.is_file():
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("POSTGRES_PASSWORD=") or line.startswith("PGPASSWORD="):
                        cfg["password"] = line.split("=", 1)[1].strip().strip("'\"")
                        break
            if cfg["password"]:
                break

    if not cfg["password"]:
        mem_file = Path.home() / ".hermes" / "MEMORY.md"
        if mem_file.is_file():
            text = mem_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"dbname=academic_pipeline.*?password=([^\s`)\]]+)", text)
            if m:
                cfg["password"] = m.group(1).strip()

    return cfg


def connect_pg(pg_cfg: dict[str, Any]):
    return psycopg2.connect(
        host=pg_cfg["host"],
        port=pg_cfg["port"],
        dbname=pg_cfg["dbname"],
        user=pg_cfg["user"],
        password=pg_cfg["password"],
        connect_timeout=5,
    )


def ensure_schema(conn) -> None:
    """Create `paradigma_analizleri` table if it does not exist yet."""
    with conn.cursor() as cur:
        cur.execute(PARADIGM_TABLE_DDL)
    conn.commit()


def fetch_articles(
    conn,
    *,
    only_pending: bool,
    min_judge: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch articles with their summaries, methods, and numeric results."""
    where_clauses = ["1=1"]
    params: list[Any] = []
    if only_pending:
        where_clauses.append("pa.makale_id IS NULL")
    if min_judge is not None:
        where_clauses.append("COALESCE(m.judge_overall, 0) >= %s")
        params.append(min_judge)

    limit_sql = f"LIMIT {int(limit)}" if limit and limit > 0 else ""

    join_pa = "LEFT JOIN paradigma_analizleri pa ON pa.makale_id = m.id" if only_pending else ""
    query = f"""
        SELECT
            m.id,
            m.baslik,
            m.doi,
            m.arxiv_id,
            m.dergi_konferans,
            m.yayin_tarihi,
            m.kesif_kaynak,
            m.judge_overall,
            m.judge_strengths,
            m.judge_weaknesses,
            o.ozet_metni,
            o.amac,
            o.kapsam,
            o.veri_kaynaklari,
            o.katkilar,
            o.sonuclar,
            o.kullanilan_modeller,
            o.sonuc_metrikleri,
            o.sinirlar_ve_kisitlar,
            o.gelecek_calisma_onerileri
        FROM makaleler m
        LEFT JOIN ozetler o ON o.makale_id = m.id
        {join_pa}
        WHERE {' AND '.join(where_clauses)}
        ORDER BY m.id DESC
        {limit_sql}
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return []

        ids = [r["id"] for r in rows]
        cur.execute(
            """
            SELECT makale_id, yontem_adi, kullanilan_arac_yazilim, aciklama
            FROM yontemler
            WHERE makale_id = ANY(%s)
            ORDER BY id ASC
            """,
            (ids,),
        )
        methods_by_id: dict[int, list[str]] = {}
        for r in cur.fetchall():
            lst = methods_by_id.setdefault(r["makale_id"], [])
            if len(lst) < 12:
                parts = [p for p in (r["yontem_adi"], r["kullanilan_arac_yazilim"], r["aciklama"]) if p]
                lst.append(" — ".join(parts)[:220])

        cur.execute(
            """
            SELECT makale_id, degisken, deger, birim, istatistik_tipi
            FROM sayisal_sonuclar
            WHERE makale_id = ANY(%s)
            ORDER BY id ASC
            """,
            (ids,),
        )
        results_by_id: dict[int, list[str]] = {}
        for r in cur.fetchall():
            lst = results_by_id.setdefault(r["makale_id"], [])
            if len(lst) < 12:
                val = f"{r['degisken'] or ''}: {r['deger'] or ''} {r['birim'] or ''}".strip()
                if r["istatistik_tipi"]:
                    val += f" ({r['istatistik_tipi']})"
                lst.append(val[:180])

    for r in rows:
        r["yontemler"] = methods_by_id.get(r["id"], [])
        r["sayisal_sonuclar"] = results_by_id.get(r["id"], [])
    return rows


def build_article_prompt(article: dict[str, Any]) -> str:
    """Assemble structured DB context for one article."""
    ref = article.get("doi") or article.get("arxiv_id") or f"makale_id:{article['id']}"
    parts = [
        f"Article ID: {article['id']} (Ref: {ref})",
        f"Title: {article.get('baslik') or '—'}",
        f"Venue/Date: {article.get('dergi_konferans') or '—'} | {article.get('yayin_tarihi') or '—'}",
        f"Discovery Source: {article.get('kesif_kaynak') or '—'} | Judge Score: {article.get('judge_overall')}",
    ]
    for label, key in (
        ("Abstract/Summary", "ozet_metni"),
        ("Objective (Amaç)", "amac"),
        ("Scope (Kapsam)", "kapsam"),
        ("Data Sources (Veri Kaynakları)", "veri_kaynaklari"),
        ("Models Used (Kullanılan Modeller)", "kullanilan_modeller"),
        ("Metrics (Sonuç Metrikleri)", "sonuc_metrikleri"),
        ("Contributions (Katkılar)", "katkilar"),
        ("Results (Sonuçlar)", "sonuclar"),
        ("Limitations (Sınırlar/Kısıtlar)", "sinirlar_ve_kisitlar"),
        ("Future Work", "gelecek_calisma_onerileri"),
        ("Judge Strengths", "judge_strengths"),
        ("Judge Weaknesses", "judge_weaknesses"),
    ):
        val = article.get(key)
        if val:
            parts.append(f"{label}: {str(val)[:1200]}")

    if article.get("yontemler"):
        parts.append("Methods:\n- " + "\n- ".join(article["yontemler"]))
    if article.get("sayisal_sonuclar"):
        parts.append("Numeric Results:\n- " + "\n- ".join(article["sayisal_sonuclar"]))

    return "\n\n".join(parts)[:9000]


def save_article_evaluation(
    conn,
    db_lock: threading.Lock,
    *,
    makale_id: int,
    analysis: dict[str, Any],
    model_name: str,
    note_markdown: str,
    write_note: bool,
) -> None:
    """Persist evaluation into `paradigma_analizleri` and `notlarim` in one transaction."""
    tags = {
        "source": "scientific-paradigm-analyzer",
        "stage": analysis.get("current_paradigm_stage"),
        "epistemic_mode": analysis.get("epistemic_mode"),
        "depth_score": analysis.get("mechanistic_depth_score"),
        "model": model_name,
    }
    with db_lock:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO paradigma_analizleri (
                    makale_id, domain, core_breakthrough, epistemic_mode,
                    current_paradigm_stage, mechanistic_depth_score,
                    phase_1_data, phase_2_pattern, phase_3_mechanism,
                    verification_vs_comprehension, black_box_tension,
                    methodological_takeaway, analysis_json, model, evaluated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s, now())
                ON CONFLICT (makale_id) DO UPDATE SET
                    domain = EXCLUDED.domain,
                    core_breakthrough = EXCLUDED.core_breakthrough,
                    epistemic_mode = EXCLUDED.epistemic_mode,
                    current_paradigm_stage = EXCLUDED.current_paradigm_stage,
                    mechanistic_depth_score = EXCLUDED.mechanistic_depth_score,
                    phase_1_data = EXCLUDED.phase_1_data,
                    phase_2_pattern = EXCLUDED.phase_2_pattern,
                    phase_3_mechanism = EXCLUDED.phase_3_mechanism,
                    verification_vs_comprehension = EXCLUDED.verification_vs_comprehension,
                    black_box_tension = EXCLUDED.black_box_tension,
                    methodological_takeaway = EXCLUDED.methodological_takeaway,
                    analysis_json = EXCLUDED.analysis_json,
                    model = EXCLUDED.model,
                    evaluated_at = now()
                """,
                (
                    makale_id,
                    analysis.get("domain"),
                    analysis.get("core_breakthrough"),
                    analysis.get("epistemic_mode"),
                    analysis.get("current_paradigm_stage"),
                    analysis.get("mechanistic_depth_score"),
                    json.dumps(analysis.get("phase_1_data") or {}, ensure_ascii=False),
                    json.dumps(analysis.get("phase_2_pattern") or {}, ensure_ascii=False),
                    json.dumps(analysis.get("phase_3_mechanism") or {}, ensure_ascii=False),
                    analysis.get("verification_vs_comprehension"),
                    analysis.get("black_box_tension"),
                    analysis.get("methodological_takeaway"),
                    json.dumps(analysis, ensure_ascii=False),
                    model_name,
                ),
            )

            if write_note:
                cur.execute(
                    """
                    SELECT id FROM notlarim
                    WHERE makale_id = %s
                      AND etiketler->>'source' = 'scientific-paradigm-analyzer'
                    LIMIT 1
                    """,
                    (makale_id,),
                )
                existing_note = cur.fetchone()
                if existing_note:
                    cur.execute(
                        """
                        UPDATE notlarim
                        SET not_metni = %s, etiketler = %s::jsonb, olusturulma_tarihi = now()
                        WHERE id = %s
                        """,
                        (note_markdown, json.dumps(tags, ensure_ascii=False), existing_note[0]),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO notlarim (makale_id, not_metni, etiketler)
                        VALUES (%s, %s, %s::jsonb)
                        """,
                        (makale_id, note_markdown, json.dumps(tags, ensure_ascii=False)),
                    )
        conn.commit()


def generate_pg_report(conn, report_path: Path) -> None:
    """Export a summary Markdown report directly from `paradigma_analizleri`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pa.makale_id, m.baslik, pa.domain, pa.current_paradigm_stage,
                   pa.mechanistic_depth_score, pa.epistemic_mode, pa.black_box_tension
            FROM paradigma_analizleri pa
            JOIN makaleler m ON m.id = pa.makale_id
            ORDER BY pa.mechanistic_depth_score DESC NULLS LAST, pa.makale_id DESC
            """
        )
        rows = cur.fetchall()

    total = len(rows)
    stage_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    for _, _, _, stage, _, mode, _ in rows:
        stage_counts[stage or "Unknown"] = stage_counts.get(stage or "Unknown", 0) + 1
        mode_counts[mode or "Unknown"] = mode_counts.get(mode or "Unknown", 0) + 1

    lines = [
        "# SU+AI Academic Pipeline — Scientific Paradigm Analysis Report",
        "",
        f"> **Database:** `academic_pipeline` | **Evaluated Articles:** {total} | **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. Paradigm Stage Distribution (Tycho → Kepler → Newton)",
        "",
        "| Stage | Count | Share |",
        "| :--- | :---: | :---: |",
    ]
    for stage, cnt in sorted(stage_counts.items()):
        pct = (cnt * 100 // total) if total else 0
        lines.append(f"| {stage} | {cnt} | {pct}% |")

    lines.extend([
        "",
        "## 2. Epistemic Mode Distribution",
        "",
        "| Mode | Count | Share |",
        "| :--- | :---: | :---: |",
    ])
    for mode, cnt in sorted(mode_counts.items()):
        pct = (cnt * 100 // total) if total else 0
        lines.append(f"| {mode} | {cnt} | {pct}% |")

    lines.extend([
        "",
        "## 3. Article Paradigm Matrix",
        "",
        "| ID | Title | Domain | Stage | Depth | Mode | Black-Box / Gap |",
        "| :---: | :--- | :--- | :--- | :---: | :--- | :--- |",
    ])
    for mid, title, domain, stage, depth, mode, gap in rows:
        lines.append(
            f"| {mid} | {fmt_cell(str(title)[:80])} | {fmt_cell(domain)} | {fmt_cell(stage)} | {depth}/5 | {fmt_cell(mode)} | {fmt_cell(str(gap or '')[:120])} |"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze academic_pipeline articles with scientific-paradigm-analyzer and write back to PostgreSQL."
    )
    parser.add_argument("--pg-host", default=None)
    parser.add_argument("--pg-port", type=int, default=None)
    parser.add_argument("--pg-db", default=None)
    parser.add_argument("--pg-user", default=None)
    parser.add_argument("--pg-password", default=None)
    parser.add_argument("--provider", choices=sorted(PROVIDER_DEFAULTS.keys()), default="cliproxy")
    parser.add_argument("--llm-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--lang", default="tr", choices=["tr", "en"], help="Analysis language (default: tr)")
    parser.add_argument("--skill-path", type=Path, default=DEFAULT_SKILL_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N articles (0 = all)")
    parser.add_argument("--min-judge", type=int, default=None, help="Minimum makaleler.judge_overall filter")
    parser.add_argument("--no-note", action="store_true", help="Do not upsert formatted notes into `notlarim`")
    parser.add_argument("--dry-run", action="store_true", help="Run LLM analysis without writing to PostgreSQL")
    parser.add_argument("--force", action="store_true", help="Re-evaluate articles already in `paradigma_analizleri`")
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

    pg_cfg = resolve_pg_config(args)
    if not pg_cfg["password"]:
        print("❌ PostgreSQL password not found. Set PGPASSWORD or pass --pg-password.", file=sys.stderr)
        return 2

    skill_instructions = load_skill_instructions(args.skill_path)
    llm = LLMConfig(provider=args.provider, base_url=base_url, model=model, api_key=api_key)

    conn = connect_pg(pg_cfg)
    try:
        if not args.dry_run:
            ensure_schema(conn)
            only_pending = not args.force
        else:
            # Check if table exists before filtering by it during dry-run
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.paradigma_analizleri');")
                table_exists = cur.fetchone()[0] is not None
            only_pending = table_exists and (not args.force)

        articles = fetch_articles(
            conn,
            only_pending=only_pending,
            min_judge=args.min_judge,
            limit=args.limit,
        )
        print(f"📚 Selected {len(articles)} articles from {pg_cfg['host']}/{pg_cfg['dbname']}.")

        if not articles:
            if not args.dry_run:
                generate_pg_report(conn, args.report_path)
            print("✅ Nothing pending.")
            return 0

        db_lock = threading.Lock()
        total = len(articles)
        completed = 0
        errors = 0
        t0 = time.time()

        def process_one(article: dict[str, Any]) -> tuple[int, str, bool, str | None]:
            mid = int(article["id"])
            title = str(article.get("baslik") or "")[:60]
            evaluated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                user_prompt = build_article_prompt(article)
                analysis = evaluate_with_paradigm_skill(
                    llm=llm,
                    skill_instructions=skill_instructions,
                    user_prompt=user_prompt,
                    language=args.lang,
                )
                if not args.dry_run:
                    note_md = render_analysis_markdown(
                        analysis=analysis,
                        model_name=llm.model,
                        evaluated_at=evaluated_at,
                        include_markers=False,
                        language=args.lang,
                    )
                    save_article_evaluation(
                        conn,
                        db_lock,
                        makale_id=mid,
                        analysis=analysis,
                        model_name=llm.model,
                        note_markdown=note_md,
                        write_note=not args.no_note,
                    )
                return mid, title, True, None
            except Exception as exc:
                return mid, title, False, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(process_one, art) for art in articles]
            for fut in as_completed(futures):
                mid, title, ok, err = fut.result()
                completed += 1
                if not ok:
                    errors += 1
                    print(f"  ❌ [{completed}/{total}] id={mid} ({title}): {err}", file=sys.stderr)
                elif completed % 10 == 0 or completed == total or total <= 10:
                    elapsed = max(time.time() - t0, 0.1)
                    rate = completed / elapsed
                    eta = (total - completed) / max(rate, 1e-6)
                    tag = "DRY" if args.dry_run else "SAVED"
                    print(f"  [{completed}/{total}] {tag} id={mid} {title} ({rate:.2f} art/s, ETA {eta:.0f}s)")

        if not args.dry_run:
            generate_pg_report(conn, args.report_path)
            print(f"\n📄 Report written: {args.report_path}")
        print(f"🏁 Done: {completed - errors}/{total} succeeded, {errors} errors in {time.time() - t0:.1f}s.")
        return 0 if errors == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
