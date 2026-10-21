# Local context compression and quota diagnosis

The supplied hub log showed successful Groq HTTP 200 responses followed by local
quota_or_cooldown skips. Cortex incorrectly promoted an estimated token-budget
rejection into RATE_LIMIT_429. The continuation after a successful ui.observe was
blocked without another provider request.

The direct router no longer enforces guessed 8K-token/minute or daily caps.
Provider HTTP failures and Retry-After remain authoritative. Cooldown skips have
a distinct PROVIDER_COOLDOWN category and cannot claim a new HTTP 429. Bounded
usage history and allowlisted rate-limit headers remain visible for diagnostics;
they never imply that Cortex knows the account's remaining quota. This change
does not bypass real rate limits, enable billing, rotate keys or retry actions.

Current stabilization results: see [closure and live report](stabilization/close49-report.md). The catalog index now includes concise argument signatures; full schemas are available on demand. Groq AgentTurn proposals use a locally validated native function schema. The older measurements below describe the earlier packing implementation, not a current acceptance score.

## What is compressed

- The capability catalog uses rows, interned argument definitions and device
  sets. The initial index shortens purpose descriptions and carries argument
  names/types; on-demand inspection supplies full descriptions and constraints.
  The canonical registry remains authoritative for execution validation.
- Conversation history prioritizes the latest exchange, with a default 4,000
  character budget. Long messages become labelled head/tail excerpts. Omitted
  older messages are identified. Current user input is never shortened.
- Optional reasoning context has a 4,000-byte section budget with existing privacy
  checks and omission labels. Runtime code still holds the authoritative context.
- Tool context keeps full evidence while it fits within 6,000 bytes. When it
  exceeds that budget, older bulk payloads become outcome receipts and the newest
  evidence is prioritized. Large collections use complete prefix records plus an
  omission marker. Text/paths/IDs are not silently sliced into apparent facts.
- JSON whitespace and unnecessary Unicode escaping are removed in direct calls.

All packing runs locally without another model call. No model-generated summary
is saved as authoritative memory. Lossy excerpts cannot preserve every old detail;
the prompt requires clarification or fresh focused observation when necessary.
Action validation, permissions, device ownership, leases and full local UI
verification remain separate from compressed model context. Privacy routing sees
recent raw history before lossy compression, so omission cannot downgrade the
classifier's input. Privacy filtering precedes evidence packing.

## Configuration

Defaults work without changing .env. Optional overrides:

```dotenv
CORTEX_HISTORY_MAX_CHARS=4000
CORTEX_REASONING_CONTEXT_BYTES=4000
CORTEX_TOOL_CONTEXT_BYTES=6000
```

Values are resource budgets, not API quota promises. Extremely large current
requests, schemas or evidence can still be expensive; actual provider token
usage/limits remain authoritative. Cached-token accounting varies by provider.

## Historical measurement and validation

A reproducible synthetic 30-turn fixture with all 96 registry capabilities reduced
outgoing text from 39,051 to 21,422 UTF-8 bytes (45.1%). The catalog-only empty-state
prompt fell from 22,584 to 15,049 bytes (33.4%). Retained conversation text fell from
16,000 to at most 6,000 characters. These are byte/character measurements, not a
claim of an exact billed-token saving or demonstrated model-quality equivalence.

Offline regressions cover catalog round trips, latest follow-ups, omission labels,
privacy classification before compression, intact current input, source/error
metadata, actual 429 versus cooldown, and three questions followed by observation
and either interpretation or continued reasoning. They simulate HTTP/model/device
boundaries. No live provider quota was consumed during this change.

Validation: 254 focused offline regression checks passed, including privacy, approvals,
executor continuity, cancellation, general UI verification and compression. Live model
quality and actual account quotas were not tested.

Restart the hub to load the changes. Test an explanation followed by "make that
shorter", then "what app am I using rn?". Inspect model_prompt_budget and
model_route_failed logs. A real HTTP 429 can still occur; it must be reported as
such, while successful observations must not be re-executed to retry wording.


Stabilization update: AgentTurn sends the canonical capability index first and loads full
argument schemas through INSPECT_CAPABILITY on demand. Inspection never executes a tool.
The current default history and evidence budgets are 4000 characters and 6000 bytes.
Older percentage measurements above describe their original fixture/configuration;
they are not live acceptance measurements of this later discovery path.
