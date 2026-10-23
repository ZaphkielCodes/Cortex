# Frozen-system findings — proposed follow-up only

No A10/A10.5 or A14 implementation was changed in the usability review.

## A14 false conflict classification: demonstrated blocker

Real request `request_683161554ae731fe2a364218`, graph
`graph_b262e2031f84e38b4026e19a` admitted a model-generated four-worker graph.
All workers completed using real Groq Qwen/GPT-OSS responses. Independent worker
lifetimes overlapped 1.318527 seconds; dependent starts followed prerequisite
completion. No manually supplied graph or worker output was used.

The retrieval-quiz worker and explaining-aloud worker both used claim keys
`evidence`, `limitations`, and `assumptions`, with different text about their
different subjects. Those differences are not demonstrated contradictions.
`Orchestrator.conflicts` treats any unequal values under a shared key as a
conflict, without a subject/predicate/scope contract. It rejected the graph before
synthesis with `verified=false` and those three keys. No synthesis route ran.

There are two boundary failures: models produced generic section labels as claim
identifiers, and deterministic conflict checking treated them as identity of a
fact. This failure is independent of the intermittent provider quota failures.
The workers also called remembered literature “Sourced” without live retrieval;
those citations are unverified model analysis, not externally validated evidence.

Proposed repair for explicit review: require comparable fact identity (subject,
predicate and scope), distinguish unrelated claims from contradictions, and retain
conservative rejection for actual incompatible facts. Validate paraphrases,
different subjects, actual contradictions, adversarial keys and end-to-end
synthesis. Simply removing conflict checking, prefixing every key per worker, or
forcing a successful graph would erase the safety property and is not proposed.

## Frozen AgentTurn execution-approval lifecycle

Cancelling the real synthetic `fs.copy` request removed its in-memory approval
future and cancelled both owning tasks. No physical copy ran. Its confirmation DB
row nevertheless remained pending: the execution approval `finally` removes the
future but does not resolve the confirmation or update owning progress to WAITING.
The separate model-sharing approval path does perform expiry cleanup.

Proposed narrowly scoped review: bind execution approvals to task ownership, mark
owning task WAITING, and resolve only that task's outstanding approval on timeout,
cancellation or shutdown. Test late approval cannot dispatch, unrelated pending
approvals remain untouched, cancellation reaches parent/child, and restart does
not replay side effects. Do not blanket-expire a device's approvals. The review
denied only its own orphan fixture approval through the existing authenticated API.

## Frozen inference payload/capacity

Measured regular decision payloads reached 30,943 wire bytes and a byte-based
estimate of 7,736 tokens plus 1,024 output allowance. Real provider admission
failures included 413 `rate_limit_exceeded` and 429, separate from verified model
context windows. The byte estimate is not an exact tokenizer or quota measurement.

Proposed follow-up: preserve complete user requirements, privacy and capability
constraints while reducing unnecessary catalog/session serialization, and use
observed provider scope evidence rather than status alone when classifying limits.
Do not widen retries, clear cooldowns, invent capacity metadata or change the frozen
router under this review. Existing 11 frozen hashes remain identical.
