---
name: scientific-paradigm-analyzer
description: >-
  Analyze scientific videos, articles, or breakthroughs by mapping them to the
  historical 400-year discovery framework (Data/Tycho Brahe -> Pattern/Kepler ->
  Mechanism/Newton) and evaluating the epistemic shift of AI-assisted discoveries.
  Use when analyzing scientific discussions, methodology changes, AI-generated
  proofs, or "black box" scientific results.
---

# Scientific Paradigm Analyzer

Deconstructs scientific discussions, documentaries, papers, or research
breakthroughs into the classic three-phase discovery pipeline and analyzes their
epistemic implications (e.g., machine-generated proofs vs. human intuition).

## When to Use

- When analyzing videos, articles, or discussions about scientific breakthroughs
  (such as AI in mathematics or science).
- When comparing data-driven pattern matching (**Kepler** phase) against
  mechanistic proof and explanatory laws (**Newton** phase).
- When evaluating the "black box" nature of AI-generated discoveries or formal
  proofs (e.g., automated theorem provers like Lean).

## Workflow Steps

### 1. Deconstruct the Source Material

- Identify the **core scientific domain** and the **breakthrough** being discussed.
- Map the material across the three historical/methodological tiers:
  1. **Phase 1 — Data / Observation (Tycho Brahe model):**
     What raw observations, datasets, benchmarks, or measurements were gathered?
  2. **Phase 2 — Pattern / Rule (Kepler model):**
     What empirical patterns, correlations, heuristics, or candidate mathematical
     rules were discovered from the data?
  3. **Phase 3 — Mechanism / Proof (Newton model):**
     What underlying causal laws, unifying principles, or formal proofs were
     established to explain *why* the pattern holds?

### 2. Evaluate the Epistemic Shift

- Determine whether each phase was driven by **human researchers**, **automated
  systems / AI models**, or a **hybrid** workflow.
- Assess the **"understanding gap"** (the *"answer exists, but the idea remains
  in the dark"* / *"cevap var ama fikir karanlıkta"* dilemma):
  - Is there a formally verified result (e.g., verified in Lean or another
    proof assistant) or high-accuracy prediction where human intuition and an
    explanatory narrative are missing?
  - Where does the system act as a black box versus producing transferable
    conceptual insight?

### 3. Format the Output

Present the analysis in a structured, scannable format (respond in the user's
language):

```markdown
## Scientific Paradigm Analysis: <Topic / Source>

- **Domain:** <Field / Subfield>
- **Core Breakthrough:** <1-2 sentence summary>
- **Epistemic Mode:** <Human | AI-Assisted | Automated Black-Box>

| Phase | Historical Archetype | Findings in Source | Agent (Human / AI) | Reference / Timestamp |
| :--- | :--- | :--- | :--- | :--- |
| **1. Data & Observation** | Tycho Brahe | ... | ... | ... |
| **2. Pattern & Rule** | Kepler | ... | ... | ... |
| **3. Mechanism & Proof** | Newton | ... | ... | ... |

### Epistemic Shift & Understanding Gap
- **Verification vs. Comprehension:** ...
- **Black-Box Tension:** ...

### Methodological Takeaway
- ...
```

## Gotchas

- **Do not confuse correlation or pattern matching with mechanistic proof.**
  High predictive accuracy or empirical fit belongs to the **Kepler** phase,
  not the **Newton** phase, unless an underlying causal mechanism or formal
  derivation is provided.
- **Cite source references or timestamps.** When analyzing video transcripts,
  lectures, or papers, include exact timestamps, sections, or quotes for claims.
- **Distinguish verification from understanding.** A machine-checked proof
  (e.g., in Lean) establishes formal validity (Phase 3 verification) even when
  conceptual human insight remains absent—explicitly highlight that tension.
