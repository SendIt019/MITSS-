# Mermaid review panel — round 1

Codex, run by Claude Code on 2026-09-29 with:

```
codex exec --sandbox read-only "Read AGENTS.md for your role and report format; its runner-specific checklist does not apply here. Review the Mermaid review-panel feature: the changes on branch feat/mermaid-review since commit c7d51c3. Check them against docs/mermaid/SPEC.md and the invariants in CLAUDE.md, with particular attention to untrusted model output reaching the DOM. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```

Reviewed at `cbf8628`. Its final message, verbatim:

---

Verdict: request changes

Findings (most serious first):
- [major] frontend/src/mermaidRender.js:96 — `mermaid.render()` creates SVG nodes in the live DOM before producing the inert image. Strict mode accepts `flowchart TD` with `A@{ img: "https://example.invalid/beacon", h: 60 }`; Mermaid’s image renderer assigns that URL to `new Image().src`, triggering a request before sanitization or cleanup. Untrusted output can therefore contact external or local endpoints, contradicting the offline/security claims. Block external resources before rendering, isolate rendering with enforced network restrictions, and add a browser regression test asserting zero requests for hostile image blocks.

Verification: all 11 frontend tests passed. Backend tests and the production build were blocked by the read-only sandbox’s temporary-file restrictions.