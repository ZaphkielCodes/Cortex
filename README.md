# Cortex

Cortex is a Windows-first, cross-device personal computing assistant. A Python hub coordinates a desktop agent, a React/PyQt interface, voice providers, and enrolled devices.

The current runtime follows a model-driven path: user request → AgentTurn → capability selection → permission and privacy checks → execution → result interpretation → response. Long-running work uses background tasks. UI text entry uses observed editable controls and verification rather than app-specific typing recipes.

This is an actively developed prototype. Automated checks do not establish that every app, provider, or physical multi-device workflow works. Ordinary requests require model reasoning; the old phrase-routing compatibility flag no longer enables a second path. A11 has not been started in this migration.

## Local setup

Run these commands from the repository root on Windows with Python and Node.js installed:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r hub/requirements.txt -r agent/requirements.txt
Copy-Item .env.example .env
```

Configure `.env` for the provider you intend to use. `CORTEX_LLM_PROVIDER=mock` is useful for offline development; it is not a real language model. Direct Groq/NVIDIA configuration is described in [docs/direct-model-routing.md](docs/direct-model-routing.md). Sensitive data approved for the primary model never fails over to another provider. Missing or invalid provider configuration reports an error instead of silently selecting mock mode. Optional voice/integration providers require their own dependencies and credentials. Local Vosk voice additionally requires its model under `agent/voice/vosk-model/`; model files are not included here.

Structured Gemini decisions and result interpretation have a shared 30-second request budget by default (`CORTEX_DEADLINE_STRUCTURED_TOTAL`). One explicit HTTP 503 can retry after a short delay within that same budget. Timeouts, quota/auth failures, and capability execution are never retried by this recovery path. Background task acknowledgement still arrives before model completion when needed.

The wake listener detects the wake phrase during speech and hands up to three seconds of recent audio to transcription in memory. Groq transcription requests segment confidence metadata; unreliable segments ask the user to repeat before a request is dispatched. Audio capture, cloud transcription, and physical microphone behavior still require testing on the target device.

Build the active desktop frontend using the committed pnpm lockfile:

```powershell
Push-Location agent/ui
pnpm install --frozen-lockfile
pnpm run build
Pop-Location
python cortex.py
```

The desktop loads `agent/ui/dist` first. `agent/ui/cortex_core` is retained as a fallback frontend and original orb source, and has its own dependency manifest. Neither generated build directory is committed.

For runtime lifecycle commands, run `python cortex.py --help`. For manual hub/agent setup, run `python -m hub.cli --help` and `python -m agent.agent --help`; enroll each device and provision its own credential before connecting it. Historical scripts in `scripts/` include machine-specific deployment/physical checks; inspect their configuration before running them.

## Focused offline regression checks

Run the focused offline set with simulated model/device boundaries:

```powershell
$env:CORTEX_LLM_PROVIDER = "mock"
$env:GROQ_API_KEY = ""
$env:NVIDIA_API_KEY = ""
$env:GEMINI_API_KEY = ""
python -m pytest tests/test_request_lifecycle.py tests/test_agent_turn_runtime.py tests/test_direct_routing.py tests/test_gui_controls.py tests/test_privacy_approval_runtime.py tests/test_private_local_reasoning.py tests/test_agent_voice_task_delivery.py tests/test_provider_speech_reliability.py tests/test_executor_wait_continuity.py tests/test_reasoning_context.py tests/test_reference_resolution.py tests/test_task_recovery_context.py tests/test_general_ui.py tests/test_control_auth_boundary.py -q
```

Context compression and provider cooldown diagnosis are described in [docs/context-compression.md](docs/context-compression.md).

Mock mode supports component tests; it cannot understand arbitrary user requests. The historical suite includes tests that require removed keyword routing, template plans, and device-name privilege inference and is not fully passing. See [runtime changes and validation limits](docs/runtime-polish.md). Physical app interaction and live provider acceptance remain separate checks.

## Repository contents

- `hub/`: runtime, routing, policy, storage, APIs, and integrations.
- `agent/`: Windows execution, observation, voice, and desktop UI.
- `tests/`: automated behavior and regression tests.
- `scenario_lab/`: simulated scenarios and evaluation code.
- `scripts/`: retained administration and manual verification tools.
- `cortex_architecture.md`: historical architecture notes.

Local secrets, screenshots, databases, logs, speech models, dependency caches, builds, and obsolete one-off utilities are excluded. See [the repository audit](docs/repository-audit.md) for the publication boundary. Local files were preserved; exclusion from Git is not deletion.
