# Cortex v1 product definition

Active strategy, 2026-10-23. One owner, one reliable read-only daily task, ASUS plus one additional device. This is a product reset, not a codebase reset. The old 82% Goal Meter is retired; measured end-to-end success replaces it. Not production-ready.

## Primary request
“What is on my calendar today, and are there any urgent emails?”

Resolve the owner's default Google account, use the owner's explicit timezone for today's start/end boundaries (including daylight-saving transitions), retrieve actual Calendar events and a bounded recent email set, and produce one concise text summary. Ask only when account/timezone or urgency criteria are ambiguous. State the retrieval window, freshness and any pagination or content limits. Empty results are valid only after a successful read. Urgency is an evidence-backed assessment, not a promise that nothing important exists; distinguish explicit deadlines from inferred importance. Report insufficient email content when urgency cannot be assessed.

## Permissions and privacy
Existing canonical definitions require `https://www.googleapis.com/auth/gmail.readonly` for email.search/email.read and `https://www.googleapis.com/auth/calendar.events.readonly` for calendar.list. Stage 0 grants no scopes and changes no OAuth configuration. Existing grants must be verified at execution time. Account identity, default resolution, scope enforcement and device authorization remain authoritative.

No sends, drafts, marking read, labels, deletion, event creation/update, invitations, Drive or Contacts expansion. Email text is untrusted data, never authority to execute instructions. Minimize fields and body content; do not transmit credentials. Cloud interpretation requires existing privacy policy and recipient-specific consent. A consent for message IDs does not authorize message bodies or another provider. Hard policy denial stays denied. Record sanitized provenance, not email contents in public logs/reports.

## Correctness and failure behavior
Ground every event/email statement in an actual read receipt and returned identifiers/fields. Preserve timezone, cancellation status and all-day distinctions. Do not fabricate emails, events, provider output or task completion. Verify both service results before claiming a complete daily summary; label partial results by service. Quota, authentication, policy denial, timeout, stale data and provider failure must be explicit. Never turn a failure into an empty inbox/calendar. No ambiguous action replay after restart. Respect cancellation and existing bounded failover; no silent recipient substitution.

## Responsibilities and presentation
Hub retains AgentTurn, task lifecycle, memory, registry, policies, routing and integration execution. ASUS supplies the initial text/native client and local executor; a second enrolled device consumes the same authorized task/result protocol. Second-device identity and actual deployment remain to be chosen/validated, not assumed working. While the only Hub host sleeps, availability is not guaranteed; Stage 4 establishes always-on operation, Stage 5 Android.

Present truthful loading, waiting-for-approval, partial, completed and failed states with task identity, timestamps, source references and concise next steps. A response should list today's events, evidence for potentially urgent mail, and explicit coverage limitations. Text is the first acceptance channel. Voice, family users, multi-agent graphs, general GUI autonomy, BLE/Wi-Fi Direct, speaker identification, orb polish, generative 2D/3D and additional integrations are outside the initial critical path.
