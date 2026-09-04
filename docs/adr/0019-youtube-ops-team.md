# ADR-0019: YouTube monetization ops team boundary

**Status:** Accepted  
**Date:** 2026-09-04  
**Deciders:** KIHACHI Music AI project

## Context

KIHACHI can compose, review, and hand off music. Turning that into a YouTube
channel that can later join the YouTube Partner Program needs standing roles,
a 24-hour duty roster, release packaging, and a clear line between
**preparing** content and **publishing** it. The core must stay upload-free and
stdlib-only (ADR-0001). Listening decisions elsewhere already refuse to
automate adoption (see `decision.py`); publish must do the same.

## Decision

Add a `youtube-ops` command family and `youtube_ops` module that:

1. Defines six agent roles and a fixed 4-hour UTC rotation covering the day.
2. Maintains an ops workspace (`ops/youtube/`) with queue, packages, authorize
   records, checklist, and shift log.
3. Builds release packages (title, description, tags, chapters, thumbnail brief)
   from finished projects without talking to YouTube.
4. Requires an explicit human `authorize` record before a package is treated as
   publish-ready, and never performs the upload.
5. Tracks monetization checklist evidence without claiming YPP eligibility.

Continuous operation is expected via Cloud Agent timer shifts that call
`youtube-ops shift` / package / checklist tooling; the timer is an operator
concern, not a core library dependency.

## Options Considered

### A. Ops boundary in-repo, human publish gate (adopted)

| Dimension | Assessment |
|---|---|
| Complexity | Low–medium |
| Existing-system risk | Low |
| Testability | High (stdlib, tempfile) |
| Policy fit | Matches adopt/decide human gates |

### B. Direct YouTube Data API uploads from KIHACHI

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Existing-system risk | High (credentials, quotas, policy) |
| Testability | Low without network mocks |
| Policy fit | Violates ADR-0001 standalone core |

### C. Docs-only playbook with no durable artifacts

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Existing-system risk | None |
| Testability | None |
| Policy fit | Cannot run a real 24h ops loop |

## Consequences

- Agents can staff strategy → produce → package → gate → analyze → community
  around the clock without implying an upload happened.
- Monetization readiness is an evidence checklist, not a green light from code.
- Future YouTube API adapters, if any, belong beside other network adapters and
  must still require the authorize receipt.
