# A14.3: separate review of frozen HTTP 413 handling

**Proposal only; not implemented. All 11 frozen source hashes still match.**

`hub/provider_failover.py` currently maps every HTTP 413 to CONTEXT_TOO_LARGE,
including `rate_limit_exceeded`. `hub/direct_router.py` then requires a context
window larger than the failed model's window. This is conservative for an actual
context overflow, but it does not distinguish a per-request TPM admission ceiling,
wire-body size or other payload restrictions.

Groq's [error documentation](https://console.groq.com/docs/errors) describes 413
as request-body size, while its [rate-limit documentation](https://console.groq.com/docs/rate-limits)
describes TPM and 429 separately. The historical Cortex 413 lacked retained numeric
metadata. A14.3 did not reproduce 413, so there is insufficient evidence to
retrospectively classify that exact response as TPM exhaustion.

If a future sanitized response explicitly identifies TPM and records a requested
token count exceeding its known limit, a narrowly reviewed change could distinguish
that observed admission limit from model context overflow. It must retain the
original HTTP status/code, preserve quota/cooldown/retry bounds, avoid immediate
retry of an oversized request, and keep actual context overflow/ambiguous 413
conservative. A rate-limit code alone is insufficient evidence.

There is no need to alter frozen routing to load verified model capacities through
the existing profile file. That compatible change is implemented in A14.3.

No frozen-file modification is proposed for immediate application without the
missing discriminating evidence. No classification or status was rewritten at
the provider boundary to trick the router into selecting an alternate.
