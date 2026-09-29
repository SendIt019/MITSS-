# Mermaid diagrams in the review panel

Jake's brief, 2026-09-29. A feature separate from the matrix runner, built on
`feat/mermaid-review` (branched from `feat/matrix-runner`).

## Goal

When Jake opens a run and scrolls down, any ```` ```mermaid ```` blocks in the
model's output are drawn as diagrams below the output text.

## Requirements

1. Front end only unless the back end is truly required. Mermaid is an npm
   dependency, bundled, not loaded from a CDN. Works offline.
2. Mermaid runs with `securityLevel: "strict"`. Model output is untrusted.
3. A block that fails to parse shows the parse error and the raw code in its
   place. Never a blank space, never a broken panel.
4. Several blocks render in order.
5. Outputs with no Mermaid look exactly as they do today.
6. A "Save PNG" button under each rendered diagram.

## Done when

- Backend gates pass (`cd backend && python -m unittest discover tests`) and
  the frontend production build passes.
- Tested against runs with valid, invalid and no Mermaid.
- `DECISIONS.md` has an entry; the README mentions the feature.
- Codex approves (review loop as in `docs/runner/REVIEW_LOOP.md`: up to 3
  rounds, reviews saved verbatim under `docs/mermaid/reviews/`).

## Codex prompt

```bash
codex exec --sandbox read-only "Read AGENTS.md for your role and report format; its runner-specific checklist does not apply here. Review the Mermaid review-panel feature: the changes on branch feat/mermaid-review since commit BASE. Check them against docs/mermaid/SPEC.md and the invariants in CLAUDE.md, with particular attention to untrusted model output reaching the DOM. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```
