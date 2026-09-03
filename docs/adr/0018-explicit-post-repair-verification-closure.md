# ADR-0018: Explicit Post-Repair Verification Closure

**Status:** Accepted
**Date:** 2026-09-03
**Deciders:** KIHACHI Music AI project

## Context

VS9/VS10 write an immutable repair execution receipt and stop at unverified states:

- `repair_applied_unverified`
- `repair_satisfied_unverified`
- `repair_attempted_unverified`

A successful AbletonGPT repair call is not proof that the selected Live postcondition now exists. `ableton-verify` can take a new snapshot, but nothing durable linked:

```text
the exact repair execution receipt
        ↓
the exact newly observed verification
        ↓
the selected repaired check
        ↓
pass / fail / not observable
```

Automatic verify-after-apply would hide that gap, retry a mutation, or pretend the old repair plan was generated from the new snapshot. Fresh `ableton_verification.json` intentionally makes the previous repair plan historical relative to the newly observed Live state.

## Decision

VS11 adds `kihachi ableton-repair-verify PROJECT` as an explicit, read-only closure step.

- Repair execution receipts remain immutable. VS11 never rewrites `ableton_repair_execution.json`.
- Post-repair verification is a separate provenance artifact: `ableton_repair_verification.json`.
- Check identity comes from the current receipt. There is no `--check-id`.
- Eligible receipts are applied, satisfied, or attempted unverified states. Prepare-only receipts are refused before any Live read.
- Fresh evidence is collected only through the existing VS7 `verify_ableton_execution(...)` path. KIHACHI does not open the Live socket, copy the VS7 collector, call `repair_live_device`, or run a JobPlan.
- Closure maps only the selected check: pass → `repair_check_verified` (exit 0), fail → `repair_check_failed` (exit 1), not_observable → `repair_check_not_observable` (exit 2). AbletonGPT/Live unavailability is `repair_verification_not_run` (exit 2).
- Selected-check success does not claim the full Live Set is verified.
- `causality_claimed` is always false. VS11 observes a postcondition; it does not prove the repair caused it.
- `ableton-repair-apply` never invokes VS11. There is no automatic retry, replan, or adoption.
- The old repair plan is left on disk. Further `ableton-repair-plan --overwrite` remains a human action.

## Consequences

- A human can close one authorized repair check with a durable receipt-to-observation link.
- Remaining arrangement failures stay visible on the full VS7 artifact.
- After closure the previous repair plan is stale relative to the new verification. That is expected and not silently repaired.

## Options Considered

### A. Auto-verify inside `ableton-repair-apply`

Collapses mutation and observation. A successful job would be easy to misread as a verified Live Set, and retries would hide partial mutations.

### B. Reuse the latest `ableton_verification.json` without a fresh read

A user may already have re-verified, and VS11 itself replaces that snapshot. Closing against a possibly pre-repair file would not prove a fresh observation after the receipt.

### C. Separate read-only closure artifact (adopted)

Keeps the VS10 receipt immutable and makes the selected-check / full-Set distinction explicit.
