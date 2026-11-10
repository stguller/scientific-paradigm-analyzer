#!/usr/bin/env python3
"""Publish evaluated OpenKB concepts (954) + Academic Pipeline articles (625)
into the target OpenKB Studio Knowledge Base (`/home/msi/keplr/wiki`).

Builds:
  - wiki/concepts/<slug>.md    (from SQLite checkpoint DB)
  - wiki/summaries/makale_<id>.md (from PostgreSQL `academic_pipeline`)
  - wiki/entities/phase-*.md   (3 Paradigm Phase hub pages for Knowledge Graph)
  - wiki/reports/*.md          (consolidated reports for both sources)
  - wiki/index.md & wiki/log.md

Then packages wiki/ as a tarball and deploys it to MSI over SSH/SFTP.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import paramiko

from academic_paradigm_analyzer import connect_pg, resolve_pg_config
from openkb_paradigm_analyzer import DEFAULT_DB_PATH
from paradigm_core import REPO_ROOT, fmt_cell, render_analysis_markdown

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_STAGE_DIR = REPO_ROOT / "data" / "keplr_wiki"
DEFAULT_TAR_PATH = REPO_ROOT / "data" / "keplr_wiki.tar.gz"

PHASE_HUBS: dict[str, tuple[str, str, str]] = {
    "Phase 1": (
        "entities/phase-1-data-tycho",
        "Phase 1: Data & Observation (Tycho Brahe)",
        "Empirical data collection, raw measurements, sensor datasets, and benchmark observations without inferred rules or causal laws.",
    ),
    "Phase 2": (
        "entities/phase-2-pattern-kepler",
        "Phase 2: Pattern & Rule (Kepler)",
        "Empirical regularities, correlations, statistical fits, and predictive AI/ML surrogates extracted from data without first-principles causal explanation.",
    ),
    "Phase 3": (
        "entities/phase-3-mechanism-newton",
        "Phase 3: Mechanism & Proof (Newton)",
        "Causal mechanisms, unifying theoretical laws, and formally verified proofs (e.g., Lean theorem proving).",
    ),
}


def resolve_ssh_config(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve MSI SSH credentials from CLI/env or local ~/.hermes config."""
    host = args.ssh_host or os.getenv("MSI_SSH_HOST") or "192.168.1.19"
    user = args.ssh_user or os.getenv("MSI_SSH_USER") or "msi"
    pwd = args.ssh_password or os.getenv("MSI_SSH_PASSWORD") or ""
    if not pwd:
        mem_file = Path.home() / ".hermes" / "MEMORY.md"
        if mem_file.is_file():
            text = mem_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"Hedef Sunucu:.*?`192\.168\.1\.19`.*?Şifre:\s*`([^`]+)`", text)
            if m:
                pwd = m.group(1).strip()
    return {"host": host, "port": args.ssh_port, "user": user, "password": pwd}


def stage_to_entity_ref(stage: str | None) -> str:
    s = stage or ""
    if "Phase 1" in s:
        return PHASE_HUBS["Phase 1"][0]
    if "Phase 3" in s:
        return PHASE_HUBS["Phase 3"][0]
    return PHASE_HUBS["Phase 2"][0]


def yaml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ").strip()


def build_phase_entities(wiki_dir: Path) -> None:
    ent_dir = wiki_dir / "entities"
    ent_dir.mkdir(parents=True, exist_ok=True)
    for _, (ref, title, desc) in PHASE_HUBS.items():
        slug = ref.split("/", 1)[1]
        content = (
            "---\n"
            'type: "Entity"\n'
            f'description: "{yaml_escape(desc)}"\n'
            "---\n\n"
            f"# {title}\n\n"
            f"{desc}\n\n"
            "All concepts and research papers classified at this paradigm tier link to this hub.\n"
        )
        (ent_dir / f"{slug}.md").write_text(content, encoding="utf-8")


def export_concepts(db_path: Path, wiki_dir: Path) -> list[dict[str, Any]]:
    concepts_dir = wiki_dir / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.is_file():
        return []

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """
        SELECT concept_id, description, domain, epistemic_mode,
               current_paradigm_stage, mechanistic_depth_score,
               analysis_json, rendered_page
        FROM concept_evaluations
        WHERE error IS NULL AND rendered_page IS NOT NULL
        ORDER BY concept_id ASC
        """
    ).fetchall()
    conn.close()

    exported: list[dict[str, Any]] = []
    for cid, desc, domain, mode, stage, depth, raw_json, rendered in rows:
        slug = cid.split("/", 1)[1] if "/" in cid else cid
        phase_ref = stage_to_entity_ref(stage)
        # Add a single Paradigm Hub wikilink cleanly if not already present
        page_text = rendered.rstrip()
        hub_line = f"- **Paradigm Hub:** [[{phase_ref}]]"
        if phase_ref not in page_text:
            page_text = f"{page_text}\n\n{hub_line}\n"
        else:
            page_text += "\n"
        (concepts_dir / f"{slug}.md").write_text(page_text, encoding="utf-8")
        exported.append(
            {
                "id": cid,
                "slug": slug,
                "description": desc or "",
                "domain": domain or "",
                "mode": mode or "",
                "stage": stage or "",
                "depth": depth or 0,
                "analysis": json.loads(raw_json),
            }
        )
    return exported


def export_articles(pg_cfg: dict[str, Any], wiki_dir: Path) -> list[dict[str, Any]]:
    summaries_dir = wiki_dir / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_pg(pg_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.paradigma_analizleri');")
            if cur.fetchone()[0] is None:
                return []
            cur.execute(
                """
                SELECT
                    m.id, m.baslik, m.doi, m.arxiv_id, m.dergi_konferans,
                    m.yayin_tarihi, m.kesif_kaynak, m.judge_overall,
                    o.ozet_metni, o.amac, o.kullanilan_modeller, o.sonuclar,
                    pa.domain, pa.core_breakthrough, pa.epistemic_mode,
                    pa.current_paradigm_stage, pa.mechanistic_depth_score,
                    pa.analysis_json, pa.model, to_char(pa.evaluated_at, 'YYYY-MM-DD HH24:MI:SS')
                FROM paradigma_analizleri pa
                JOIN makaleler m ON m.id = pa.makale_id
                LEFT JOIN ozetler o ON o.makale_id = m.id
                ORDER BY m.id ASC
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    exported: list[dict[str, Any]] = []
    for row in rows:
        (
            mid,
            baslik,
            doi,
            arxiv_id,
            venue,
            pub_date,
            kesif,
            judge,
            ozet,
            amac,
            modeller,
            sonuclar,
            domain,
            breakthrough,
            mode,
            stage,
            depth,
            analysis_obj,
            model_name,
            eval_at,
        ) = row
        analysis = analysis_obj if isinstance(analysis_obj, dict) else json.loads(analysis_obj)
        slug = f"makale_{mid}"
        sid = f"summaries/{slug}"
        phase_ref = stage_to_entity_ref(stage)
        desc = breakthrough or baslik or f"Article {mid}"

        analysis_md = render_analysis_markdown(
            analysis=analysis,
            model_name=model_name or "gemini-3-flash",
            evaluated_at=eval_at or "",
            include_markers=True,
        )

        body_parts = [
            "---",
            'type: "Summary"',
            f'description: "{yaml_escape(str(desc)[:240])}"',
            f"makale_id: {mid}",
            f'paradigm_stage: "{yaml_escape(str(stage or ""))}"',
            f'epistemic_mode: "{yaml_escape(str(mode or ""))}"',
            f"mechanistic_depth: {int(depth or 0)}",
            "---",
            "",
            f"# {baslik or slug}",
            "",
            "## Metadata",
            f"- **Makale ID:** `{mid}`",
            f"- **DOI:** {doi or '—'} | **arXiv:** {arxiv_id or '—'}",
            f"- **Dergi / Konferans:** {venue or '—'} ({pub_date or '—'})",
            f"- **Keşif Kaynağı:** {kesif or '—'} | **Judge Skoru:** {judge if judge is not None else '—'}",
            f"- **Paradigm Hub:** [[{phase_ref}]]",
            "",
        ]
        if ozet:
            body_parts.extend(["## Özet", str(ozet).strip(), ""])
        if amac:
            body_parts.extend(["## Amaç", str(amac).strip(), ""])
        if modeller:
            body_parts.extend(["## Kullanılan Modeller", str(modeller).strip(), ""])
        if sonuclar:
            body_parts.extend(["## Sonuçlar", str(sonuclar).strip(), ""])

        body_parts.extend([analysis_md, ""])
        (summaries_dir / f"{slug}.md").write_text("\n".join(body_parts), encoding="utf-8")

        exported.append(
            {
                "id": sid,
                "makale_id": mid,
                "title": baslik or slug,
                "domain": domain or "",
                "mode": mode or "",
                "stage": stage or "",
                "depth": depth or 0,
                "analysis": analysis,
            }
        )
    return exported


def write_reports_and_index(
    wiki_dir: Path,
    concepts: list[dict[str, Any]],
    articles: list[dict[str, Any]],
) -> None:
    reports_dir = wiki_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1. OpenKB Concepts Report
    c_lines = [
        "# OpenKB Concepts — Scientific Paradigm Analysis Report",
        "",
        f"> **Total Evaluated Concepts:** {len(concepts)} | **Generated:** {now_str}",
        "",
        "| Concept | Domain | Stage | Depth | Epistemic Mode | Black-Box / Gap |",
        "| :--- | :--- | :--- | :---: | :--- | :--- |",
    ]
    for c in sorted(concepts, key=lambda x: (-int(x["depth"]), x["id"])):
        gap = fmt_cell(c["analysis"].get("black_box_tension", ""))[:120]
        c_lines.append(
            f"| [[{c['id']}]] | {fmt_cell(c['domain'])} | {fmt_cell(c['stage'])} | {c['depth']}/5 | {fmt_cell(c['mode'])} | {gap} |"
        )
    (reports_dir / "openkb_concepts_paradigm_report.md").write_text(
        "\n".join(c_lines) + "\n", encoding="utf-8"
    )

    # 2. Academic Articles Report
    a_lines = [
        "# SU+AI Academic Pipeline — Scientific Paradigm Analysis Report",
        "",
        f"> **Total Evaluated Articles:** {len(articles)} | **Generated:** {now_str}",
        "",
        "| Article | Title | Domain | Stage | Depth | Epistemic Mode | Black-Box / Gap |",
        "| :--- | :--- | :--- | :--- | :---: | :--- | :--- |",
    ]
    for a in sorted(articles, key=lambda x: (-int(x["depth"]), -int(x["makale_id"]))):
        gap = fmt_cell(a["analysis"].get("black_box_tension", ""))[:120]
        a_lines.append(
            f"| [[{a['id']}]] | {fmt_cell(str(a['title'])[:70])} | {fmt_cell(a['domain'])} | {fmt_cell(a['stage'])} | {a['depth']}/5 | {fmt_cell(a['mode'])} | {gap} |"
        )
    (reports_dir / "academic_articles_paradigm_report.md").write_text(
        "\n".join(a_lines) + "\n", encoding="utf-8"
    )

    # 3. index.md
    idx_lines = [
        "# Knowledge Base Index: Keplr Scientific Paradigm Analyzer",
        "",
        f"> **Evaluated Concepts:** {len(concepts)} | **Evaluated Academic Articles:** {len(articles)} | **Updated:** {now_str}",
        "",
        "## Explorations",
        "- [[reports/openkb_concepts_paradigm_report]] — 954 OpenKB Concepts Paradigm Matrix",
        "- [[reports/academic_articles_paradigm_report]] — 625 Academic Pipeline Articles Paradigm Matrix",
        "",
        "## Entities",
    ]
    for _, (ref, title, desc) in PHASE_HUBS.items():
        idx_lines.append(f"- [[{ref}]] — {desc}")

    idx_lines.extend(["", f"## Documents ({len(articles)} Academic Pipeline Articles)", ""])
    for a in articles:
        idx_lines.append(
            f"- [[{a['id']}]] (`{a['stage']}`, {a['depth']}/5) — {str(a['title'])[:110]}"
        )

    idx_lines.extend(["", f"## Concepts ({len(concepts)} OpenKB Concepts)", ""])
    for c in concepts:
        idx_lines.append(
            f"- [[{c['id']}]] (`{c['stage']}`, {c['depth']}/5) — {str(c['description'])[:110]}"
        )

    (wiki_dir / "index.md").write_text("\n".join(idx_lines) + "\n", encoding="utf-8")

    # 4. log.md
    log_entry = (
        f"# Keplr KB Log\n\n"
        f"- **{now_str}**: Published {len(concepts)} concepts + {len(articles)} academic articles "
        f"evaluated via `scientific-paradigm-analyzer` (`gemini-3-flash` via CLIProxyAPI).\n"
    )
    (wiki_dir / "log.md").write_text(log_entry, encoding="utf-8")


def deploy_tar_to_msi(
    *,
    stage_dir: Path,
    tar_path: Path,
    ssh_cfg: dict[str, Any],
    remote_wiki_dir: str,
) -> None:
    if not ssh_cfg["password"]:
        raise RuntimeError("MSI SSH password not found. Set MSI_SSH_PASSWORD or pass --ssh-password.")

    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        for child in stage_dir.iterdir():
            tf.add(child, arcname=child.name)

    remote_tar = "/tmp/keplr_wiki_deploy.tar.gz"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ssh_cfg["host"],
        port=ssh_cfg["port"],
        username=ssh_cfg["user"],
        password=ssh_cfg["password"],
        timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        sftp = ssh.open_sftp()
        sftp.put(str(tar_path), remote_tar)
        sftp.close()

        cmd = (
            f"mkdir -p {remote_wiki_dir} && "
            f"tar -xzf {remote_tar} -C {remote_wiki_dir} && "
            f"rm -f {remote_tar} && "
            f"sed -i 's/^model:.*/model: gemini-3-flash/' /home/msi/keplr/.openkb/config.yaml || true"
        )
        _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            raise RuntimeError(f"Remote extract failed (rc={rc}): {stderr.read().decode('utf-8', 'replace')}")
    finally:
        ssh.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build & deploy evaluated data into /home/msi/keplr/wiki.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    parser.add_argument("--tar-path", type=Path, default=DEFAULT_TAR_PATH)
    parser.add_argument("--remote-wiki-dir", default="/home/msi/keplr/wiki")
    parser.add_argument("--pg-host", default=None)
    parser.add_argument("--pg-port", type=int, default=None)
    parser.add_argument("--pg-db", default=None)
    parser.add_argument("--pg-user", default=None)
    parser.add_argument("--pg-password", default=None)
    parser.add_argument("--ssh-host", default=None)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--ssh-user", default=None)
    parser.add_argument("--ssh-password", default=None)
    parser.add_argument("--no-deploy", action="store_true", help="Only build local staged wiki directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.stage_dir.exists():
        shutil.rmtree(args.stage_dir)
    args.stage_dir.mkdir(parents=True, exist_ok=True)

    build_phase_entities(args.stage_dir)
    concepts = export_concepts(args.db_path, args.stage_dir)
    pg_cfg = resolve_pg_config(args)
    articles = export_articles(pg_cfg, args.stage_dir)
    write_reports_and_index(args.stage_dir, concepts, articles)

    print(f"📦 Staged keplr wiki: {len(concepts)} concepts, {len(articles)} articles -> {args.stage_dir}")

    if not args.no_deploy:
        ssh_cfg = resolve_ssh_config(args)
        deploy_tar_to_msi(
            stage_dir=args.stage_dir,
            tar_path=args.tar_path,
            ssh_cfg=ssh_cfg,
            remote_wiki_dir=args.remote_wiki_dir,
        )
        print(f"🚀 Deployed to {ssh_cfg['host']}:{args.remote_wiki_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
