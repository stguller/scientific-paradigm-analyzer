# scientific-paradigm-analyzer

An AI Agent Skill that analyzes scientific videos, articles, and research breakthroughs through the historical three-phase discovery framework (**Data / Tycho Brahe → Pattern / Kepler → Mechanism / Newton**) and evaluates the epistemic shift introduced by AI-driven discoveries and formal proof assistants.

## Structure

```text
keplr/
├── README.md
└── skills/
    └── scientific-paradigm-analyzer/
        └── SKILL.md
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
