# Direct model routing

Cortex connects directly to Groq and optional NVIDIA development endpoints.
There is no model broker or paid fallback. Existing Gemini and loopback Ollama
routes remain explicitly selectable. Models only propose decisions through the
existing AgentTurn schema; capabilities, approvals, privacy policy, leases and
execution verification remain authoritative.

## Configure locally

Put credentials in the repository's ignored `.env`, never in chat, source files,
screenshots or Git:

```dotenv
CORTEX_LLM_PROVIDER=direct
GROQ_API_KEY=your-groq-key
CORTEX_GROQ_MODEL=qwen/qwen3.8-27b
NVIDIA_API_KEY=your-nvidia-key
CORTEX_ENABLE_NVIDIA_TRIAL=0
```

The Groq key also serves existing voice transcription. No new Python dependency
is needed; direct inference uses the existing httpx dependency. Restart the hub
after changing configuration. Missing credentials produce a configuration error,
never mock success. Do not enable billing or automatic recharge if the intention
is to use only a provider's free plan: Cortex cannot verify an account's billing
status or guarantee that a provider continues offering a model for free.

Groq defaults to Qwen3.8-27B; GPT-OSS-120B is an eligible text-only fallback.
The model allowlist excludes arbitrary or unreviewed paid models. Model selection
is a local operation, not another model call. Requests use one bounded overall
deadline across attempts. Rate-limit errors trigger a Retry-After cooldown;
network, authentication, malformed and incomplete results are handled truthfully.
Cancellation stops the call. A model retry never dispatches an executor action.

## Routing and limits

The trusted caller can supply `routing_profile=general|reasoning|coding|vision`.
General and vision prefer Qwen; public reasoning prefers DeepSeek; public coding
prefers GLM. Planning/orchestration mode selects reasoning. These are initial,
configurable routing preferences, not proven quality rankings or natural-language
keyword handlers. Ordinary turns default to general; the router does not infer
"best" by scanning the wording of a user's prompt. Selection is pinned to a turn
and changes only on eligible failure/quota fallback, preserving the same history
and evidence. No benchmark-based learning or automatic quality optimizer is claimed.

Rate limits are enforced by actual provider responses, not guessed local daily or
per-minute quotas. HTTP 429 honors Retry-After; subsequent cooldown skips use a
separate PROVIDER_COOLDOWN category. A local estimate never fabricates a provider
rejection. Other failures also use bounded cooldowns to avoid retry storms. No
automatic execution retry or queue is added by model routing.

The router retains a bounded observational usage ledger and allowlisted quota
response headers. These are diagnostics, not a billing guarantee or an account
quota snapshot. model_prompt_budget logs text bytes and estimated input tokens,
never prompt bodies, screenshots or keys. The estimates are not exact tokenizer
counts and do not enforce quotas. [Local context compression](context-compression.md)
reduces repeated context without an additional model request.

## Privacy and screenshots

NVIDIA trial input/output is logged and its terms prohibit confidential/personal
data unless expressly permitted. `CORTEX_ENABLE_NVIDIA_TRIAL=1` enables only public
requests, with DeepSeek V4.1 Flash and GLM-5.3 candidates. Unclassified/internal,
personal, sensitive and screenshot payloads never route there. User cloud approval
does not override provider terms. Trial quotas and availability can change.

Personal/sensitive turns never change cloud provider on failure. Existing scoped
cloud approval and local Ollama privacy routing remain in effect. If an Ollama
model is configured, `CORTEX_REASONING_ROUTE=local` remains the default;
`CORTEX_REASONING_ROUTE=auto` privately classifies the request/history before
choosing a public cloud route. Invalid classification fails closed.

Screen analysis defaults to Groq for direct routing and uses the same existing
screen permission/privacy boundary. The image is resized using the existing
loader and sent as an inline image, with no public upload URL. Invalid observation
JSON fails instead of becoming a fabricated description. No vision-capable Groq
route means vision is unavailable. Direct NVIDIA screenshot analysis is disabled.

## Validate

`tests/test_direct_routing.py` uses mocked HTTP endpoints and scripted agent turns.
These checks prove protocol and routing behavior, not real model quality or live
provider availability. Once keys are entered, use the normal GUI to test general
conversation, a follow-up, system telemetry and screen analysis. Inspect
`model_route` / `model_route_failed` logs for the chosen model and reason. Logs
exclude keys, prompt bodies, image bytes and provider response bodies.

Provider references checked October 18, 2026:
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/model/qwen/qwen3.8-27b
- https://build.nvidia.com/deepseek-ai/deepseek-v4.1-flash?section=deploy
- https://build.nvidia.com/z-ai/glm-5-3?section=deploy

Run `py test_llm_failover.py` for an offline HTTP-level quota/fallback probe.
After adding keys, `py test_llm_failover.py --live` explicitly consumes provider
quota for a public arithmetic question. This is a connection probe, not desktop
control or model-quality validation.
