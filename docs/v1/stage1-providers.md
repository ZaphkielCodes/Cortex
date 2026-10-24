# Provider eligibility and compatibility â€” checked 2026-10-23

Session 2 update: see [session report](stage1-session2.md), [current accounting](stage1-accounting.md), and [executable runner](stage1-benchmark-runner.md). Session 1 inspection limitations below are historical; selection thresholds and task definitions are unchanged.
Eligibility here means a candidate for further assessment, not permission to purchase, send private data or run paid calls. No winner selected; no account quotas or prepaid/billing status verified. Existing allowlists/profile metadata do not establish current provider availability.

| Candidate | Official standard text input/output USD per million | Cortex compatibility | Eligibility/blocker |
|---|---|---|---|
| Groq openai/gpt-oss-120b | 0.15 / 0.60; cached input0.075 | Existing direct transport, native decision function, JSON validation; new opt-in budget transport | Strongest integration readiness, not a quality winner; account capacity, pricing coverage and real valid-tool-call rate need measurement |
| Google Gemini 3.5 Flash-Lite | 0.30 / 2.50 including thinking output | Existing Gemini adapter family; exact model eligibility/SDK schemas need verification | Candidate after metered SDK transport; currently blocked in budget mode to prevent unaccounted fallback |
| Google Gemini 3.1 Flash-Lite | 0.25 / 1.50 for text | Same SDK/accounting limitations | Lower price candidate, no measured reliability claim |
| DeepSeek direct deepseek-flash | Peak uncached0.30 /1.20; off-peak0.15 /0.60 | Official OpenAI-format API, JSON/tool calls; no Cortex direct DeepSeek provider adapter currently | Candidate only; onboarding, privacy terms, alias/version and adapter validation needed; not NVIDIA endpoint |
| Existing Groq Qwen / NVIDIA trial entries | Not independently priced/verified in this session | Existing configured development allowlists | Do not infer paid suitability from hardcoded profile names; NVIDIA trial remains PUBLIC-only and ineligible for private daily mail/calendar |

Official sources: [Groq model specification](https://console.groq.com/docs/model/openai/gpt-oss-120b), [Google pricing](https://ai.google.dev/gemini-api/docs/pricing), [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing/). Prices are a dated comparison, not an installed rate card. Reverify at activation. Use worst applicable uncached/peak prices for admission; cached discounts only reduce settled estimates when safely accounted. No built-in search, remote tools or extra billed services are authorized by this list.

Groq documents text input/output, function calling and context131072/output65536. DeepSeek documents JSON and tool calling, 1M context and max384K output; their thinking, cache and tool billing must be validated against actual usage contracts. Google paid versus free data-use terms differ: a free allowance is not automatic permission for sensitive email. Local privacy policy and explicit recipient approvals still apply regardless of vendor marketing.

All paid candidates remain blocked pending authorized funding/caps, verified account access, exact model contract checks, metered transport coverage and one-correction enforcement. No new keys, providers, billing settings, default model or automatic refill were installed.
