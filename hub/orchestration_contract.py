"""A14 catalog contract at the existing AgentTurn inference boundary.

No request parsing, decision substitution or routing policy. The real model must
select the orchestration capability; A10.5 still chooses and calls providers.
"""
import json
import logging

log = logging.getLogger('cortex.orchestration_contract')

CONTRACT = (
    '\nA14 capability contract: agent.orchestrate is a registered HUB capability, not a device tool. '
    'It is available unless STATE.unavailable_capabilities says otherwise. Its empty device list '
    'does not mean it needs a device executor; existing authorization is checked at dispatch. '
    'Assess the WHOLE request before choosing a constituent action. '
    'agent.orchestrate runs a real multi-worker dependency graph. '
    'When the user requests independent agents, parallel delegated analyses or a team of workers, '
    'delegate the entire request through this capability before executing constituent actions. '
    'A direct response or one constituent tool call does not create workers or satisfy delegation. '
    'Select CALL_CAPABILITY, capability agent.orchestrate, arguments {}. Its owned parent request '
    'supplies the objective; do not invent arguments or prebuild a graph in plan_steps. '
    'This is a single hub capability, not a UI PLAN: omit success_condition and expected_effect '
    'unless an actual structured predicate is needed. A capability predicate must use source '
    'capability and a path starting with data, never prose or dispatch status. '
    'The graph verifies its own workers and returns data.verified. Simple requests continue '
    'through normal responses or direct capabilities. All existing privacy and permission rules apply.'
    ' Omit unused optional output fields. Never put natural-language goals in success_condition or expected_effect. '
    'Do not claim a multi-worker request is complete from a single constituent receipt.'
)


def admission_prompt(prompt):
    # Place the contract adjacent to the catalog, after the general single-action
    # instructions. Keep every original instruction and both JSON data sections.
    marker = '\nCAPABILITIES:\n'
    if marker in prompt:
        head, tail = prompt.split(marker, 1)
        return head + '\n' + CONTRACT + marker + tail
    return CONTRACT + '\n' + prompt


class OrchestrationContract:
    def __init__(self, provider):
        self.provider = provider

    def __getattr__(self, key):
        return getattr(self.provider, key)

    @property
    def local(self):
        return getattr(self.provider, 'local', None)

    async def select_for_turn(self, utterance, context, history):
        from .orchestration_workers import unwrap
        selector = unwrap(self.provider)
        if hasattr(selector, 'select_for_turn'):
            selected, context = await selector.select_for_turn(utterance, context, history)
            return OrchestrationContract(selected), context
        return self, context

    async def generate_result(self, text, context, on_chunk=None):
        schema = context.get('decision_schema') or {}
        names = schema.get('properties', {}).get('capability', {}).get('enum', [])
        if context.get('mode') == 'agent_decision' and 'agent.orchestrate' in names:
            # The frozen prompt ends with machine-readable CATALOG / STATE
            # sections. Keep those complete JSON documents at their boundaries.
            context = dict(context, a14_inference_contract=True,
                           system_prompt=admission_prompt(context.get('system_prompt', '')))
            log.info('a14_admission_contract task_id=%s catalog_count=%s prompt_bytes=%s schema_bytes=%s',
                context.get('operational_task_id'), len(names)-1,
                len(context['system_prompt'].encode('utf-8')), len(json.dumps(schema).encode('utf-8')))
        # Preserve the actual model result, including invalid output/failure.
        if on_chunk is None:
            result = await self.provider.generate_result(text, context)
        else:
            result = await self.provider.generate_result(text, context, on_chunk=on_chunk)
        if context.get('mode') == 'agent_decision' and 'agent.orchestrate' in names:
            # Shape diagnostics only: never persist prompts, arguments or model reasoning.
            from .agent_turn import AgentDecision
            raw = result if isinstance(result, str) else getattr(result, 'response_text', None)
            try:
                parsed = AgentDecision.model_validate_json(raw)
            except (ValueError, TypeError):
                log.info('a14_admission_proposal task_id=%s valid=false', context.get('operational_task_id'))
            else:
                log.info('a14_admission_proposal task_id=%s valid=true decision=%s capability=%s',
                    context.get('operational_task_id'), parsed.decision,
                    parsed.capability if parsed.capability in names else 'unregistered')
        return result
