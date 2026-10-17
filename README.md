# Cortex

Cortex is a Windows-first, cross-device personal computing assistant. A Python hub coordinates a desktop agent, a React/PyQt interface, voice providers, and enrolled devices.

The current runtime follows a model-driven path: user request → AgentTurn → capability selection → permission and privacy checks → execution → result interpretation → response. Long-running work uses background tasks. UI text entry uses observed editable controls and verification rather than app-specific typing recipes.

This is an actively developed prototype. Automated checks do not establish that every app, provider, or physical multi-device workflow works. Legacy compatibility routing remains available explicitly; A11 has not been started in this migration.

## Local setup

Run these commands from the repository root on Windows with Python and Node.js installed:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r hub/requirements.txt -r agent/requirements.txt
Copy-Item .env.example .env
```

Configure `.env` for the provider you intend to use. `CORTEX_LLM_PROVIDER=mock` is useful for offline development; it is not a real language model. Leave `OPENROUTER_API_KEY` unset to keep OpenRouter fallback disabled; supplying that key enables fallback in the current provider factory. Optional voice/integration providers require their own dependencies and credentials. Local Vosk voice additionally requires its model under `agent/voice/vosk-model/`; model files are not included here.

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

Install test dependencies and run the current migration regression set:

```powershell
python -m pip install pytest pytest-asyncio
$env:CORTEX_LLM_PROVIDER = "mock"
$env:CORTEX_LEGACY_COMPATIBILITY = "1"
python -m pytest tests/test_general_ui.py tests/test_task_recovery_context.py tests/test_reasoning_context.py tests/test_gui_controls.py tests/test_agent_turn_runtime.py tests/test_agent_voice_task_delivery.py tests/test_architecture_repair.py tests/test_phase_a5.py tests/test_phase_a6.py tests/test_phase_a7.py tests/test_phase_a8.py tests/test_phase_a9.py tests/test_phase_a10.py -q
Remove-Item Env:CORTEX_LEGACY_COMPATIBILITY
```

The compatibility flag above supports older regression contracts; individual model-first tests explicitly control their mode. Do not enable it for normal model-first operation. The full historical test suite is not claimed to pass. Physical app interaction and live provider acceptance remain separate checks.

## Repository contents

- `hub/`: runtime, routing, policy, storage, APIs, and integrations.
- `agent/`: Windows execution, observation, voice, and desktop UI.
- `tests/`: automated behavior and regression tests.
- `scenario_lab/`: simulated scenarios and evaluation code.
- `scripts/`: retained administration and manual verification tools.
- `cortex_architecture.md`: historical architecture notes.

Local secrets, screenshots, databases, logs, speech models, dependency caches, builds, and obsolete one-off utilities are excluded. See [the repository audit](docs/repository-audit.md) for the publication boundary. Local files were preserved; exclusion from Git is not deletion.
