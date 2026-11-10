# scientific-paradigm-analyzer

An AI Agent Skill that analyzes scientific videos, articles, and research breakthroughs through the historical three-phase discovery framework (**Data / Tycho Brahe → Pattern / Kepler → Mechanism / Newton**) and evaluates the epistemic shift introduced by AI-driven discoveries and formal proof assistants.

## Structure

```text
keplr/
├── README.md
├── .gitignore
├── paradigm_core.py
├── openkb_paradigm_analyzer.py
├── academic_paradigm_analyzer.py
└── skills/
    └── scientific-paradigm-analyzer/
        └── SKILL.md
```

## Tools

### 1. OpenKB Batch Analyzer (`openkb_paradigm_analyzer.py`)

Fetches all Concept pages from an OpenKB Studio instance (e.g., 954 concepts in `openkb-kb`), evaluates each concept using the `scientific-paradigm-analyzer` framework, checkpoints results to a local SQLite database (`data/paradigm_evaluations.db`), writes the `## Scientific Paradigm Analysis` section back into each concept page via `PUT /api/v1/page` (preserving OKF frontmatter), and builds a consolidated report (`data/scientific_paradigm_analysis_report.md`).

```bash
# Dry-run on 5 concepts first
python openkb_paradigm_analyzer.py --dry-run --limit 5

# Run full evaluation & write back to OpenKB (resumes automatically from SQLite checkpoint)
python openkb_paradigm_analyzer.py --workers 4
```

### 2. Academic Pipeline Analyzer (`academic_paradigm_analyzer.py`)

Reads research articles (`makaleler`, `ozetler`, `yontemler`, `sayisal_sonuclar`) from the `academic_pipeline` PostgreSQL database, evaluates them with `scientific-paradigm-analyzer`, and writes the results back to the same PostgreSQL database (`paradigma_analizleri` table + idempotent notes in `notlarim` for the live dashboard).

```bash
# Dry-run on 5 articles
python academic_paradigm_analyzer.py --dry-run --limit 5

# Evaluate all pending articles and write back to PostgreSQL
python academic_paradigm_analyzer.py --workers 4
```

## Installation

Copy `skills/scientific-paradigm-analyzer/` into your agent's skills directory (e.g., `.agents/skills/`, `~/.gemini/config/skills/`, or `~/.claude/skills/`), or install via:

```bash
npx skills add stguller/scientific-paradigm-analyzer
```

## What It Evaluates

1. **Data / Observation (Tycho Brahe):** Raw datasets, measurements, and empirical inputs.
2. **Pattern / Rule (Kepler):** Discovered correlations, candidate equations, and empirical regularities.
3. **Mechanism / Proof (Newton):** Underlying laws, causal explanations, and formal verification (e.g., Lean).
4. **Epistemic Shift:** Human vs. AI agency and the "understanding gap" where a valid answer or formal proof exists without human conceptual intuition.
