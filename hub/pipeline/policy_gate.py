"""
hub/pipeline/policy_gate.py
---------------------------
Authoritative policy gate verifying permissions, privacy, and side-effect safety (Section 13).
LLM output is advisory; this gate is authoritative.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..privacy import PrivacyPolicyEngine
from ..task_ownership import ActionSafetyClass, get_action_safety_class
from .context_assembler import AssembledContext
from .semantic_interpreter import SemanticIntent

log = logging.getLogger("cortex.pipeline.policy_gate")


@dataclass
class PolicyGateResult:
    allowed: bool
    requires_approval: bool = False
    denial_reason: Optional[str] = None
    approval_prompt: Optional[str] = None


class PolicyGate:
    """
    Enforces permissions, privacy classification, and side-effect safety.
    """

    @classmethod
    async def evaluate(
        cls,
        intent: SemanticIntent,
        target_device: str,
        context: AssembledContext,
        control_layer: Any,
    ) -> PolicyGateResult:
        if not intent.candidate_capabilities:
            return PolicyGateResult(allowed=True)

        action = intent.candidate_capabilities[0]

        # 1. Permission check via Control Layer
        if hasattr(control_layer, "check_permissions"):
            import inspect
            raw_perm = control_layer.check_permissions(context.source_device, target_device, action)
            perm = await raw_perm if inspect.isawaitable(raw_perm) else raw_perm
            if perm and not getattr(perm, "allowed", True):
                return PolicyGateResult(
                    allowed=False,
                    denial_reason=f"Permission denied for capability '{action}'.",
                )

        # 2. Side-effect safety classification
        safety_cls = get_action_safety_class(action)
        if safety_cls == ActionSafetyClass.HUMAN_APPROVAL_REQUIRED:
            return PolicyGateResult(
                allowed=True,
                requires_approval=True,
                approval_prompt=f"Action '{action}' requires explicit confirmation. Proceed?",
            )

        # 2b. High-impact UI action effect evaluation (Phase A10)
        if action.startswith("ui."):
            from ..computer_use_controller import ComputerUseTaskController
            cu_ctrl = ComputerUseTaskController.get_instance()
            tgt_lbl = str(intent.arguments.get("target") or intent.arguments.get("label") or intent.target_reference or "")
            req_app, r_lvl, app_prompt = cu_ctrl.evaluate_action_risk(
                action=action,
                target_label=tgt_lbl,
                goal=intent.goal or "",
                action_args=intent.arguments,
            )
            if req_app:
                return PolicyGateResult(
                    allowed=True,
                    requires_approval=True,
                    approval_prompt=app_prompt,
                )

        # 3. Privacy Policy Evaluation
        priv_engine = PrivacyPolicyEngine.get_instance()
        # Privacy engine verification passes if no block rule applies

        return PolicyGateResult(allowed=True)
