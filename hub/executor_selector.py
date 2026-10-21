"""
hub/executor_selector.py
------------------------
Deterministic executor selection engine for Cortex Phase A5.

Deterministic Priority:
1. Explicit user-requested device if valid (STRICT: never reroute if offline or incapable)
2. Device owning target/resource (e.g. file path on ASUS -> owner=asus)
3. Source/current device if capable, online, and authorized
4. Another authorized online capable executor
5. No eligible executor

Enforces trust constraints:
- Restricted devices cannot execute destructive operations (tier >= 3 or classification='destructive').
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional

from .executor_registry import ExecutorRegistry
from .device_handle import DeviceHandle
from .privacy import (
    PrivacyPolicyEngine,
    PrivacyDecision,
    PrivacyClass,
    TaskPrivacyContext,
    classify_task_privacy,
)

log = logging.getLogger("cortex.executor_selector")


@dataclass
class SelectionResult:
    selected_device: Optional[str]
    status: str                         # "success", "device_offline", "capability_mismatch", "trust_denied", "no_eligible_executor", "owning_device_unavailable", "requires_approval", "wait_for_private_executor", "privacy_denied"
    reason: str
    alternatives: List[str] = field(default_factory=list)
    privacy_decision: Optional[str] = None
    approval_details: Optional[Dict[str, Any]] = None

    @property
    def is_success(self) -> bool:
        return self.status == "success" and self.selected_device is not None


class ExecutorSelector:
    """Selects the deterministic execution node for a given capability, context, and privacy policy."""

    def __init__(self, registry: Optional[ExecutorRegistry] = None, privacy_engine: Optional[PrivacyPolicyEngine] = None):
        self.registry = registry or ExecutorRegistry.get_instance()
        self.privacy_engine = privacy_engine or PrivacyPolicyEngine.get_instance()

    def select_executor(
        self,
        required_capability: str,
        requested_device: Optional[str] = None,
        source_device: str = "",
        owning_device: Optional[str] = None,
        trust_requirement: Optional[str] = None,
        permission_tier: int = 1,
        classification: str = "read",
        arguments: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        privacy_class: Optional[str] = None,
        data_categories: Optional[List[str]] = None,
        task_context: Optional[TaskPrivacyContext] = None,
    ) -> SelectionResult:
        """
        Deterministically select an execution node adhering to capability, locality,
        and privacy policy. Enforces: 'Capable != Allowed'.
        """
        registry = self.registry
        privacy_engine = self.privacy_engine

        # Phase A7: Task Privacy Classification
        merged_ctx = dict(context or {})
        if privacy_class:
            merged_ctx["privacy_class"] = privacy_class
        if data_categories:
            merged_ctx["data_categories"] = data_categories

        p_ctx = task_context or classify_task_privacy(
            capability=required_capability,
            arguments=arguments,
            context=merged_ctx,
        )
        if owning_device:
            p_ctx.resource_localities.setdefault("target_resource", owning_device)

        # -------------------------------------------------------------------
        # 1. Explicit user-requested device
        # -------------------------------------------------------------------
        if requested_device:
            req_id = requested_device.strip()
            handle = registry.get_device_handle(req_id)
            if not handle:
                # Search by display name
                for h in registry.list_device_handles():
                    if h.display_name.lower() == req_id.lower() or h.device_id.lower() == req_id.lower():
                        handle = h
                        break

            if not handle:
                return SelectionResult(
                    selected_device=None,
                    status="device_not_found",
                    reason=f"Requested device '{requested_device}' is not enrolled in Cortex.",
                )

            # Check liveness
            if not registry.is_device_online(handle.device_id):
                # NEVER reroute to ASUS if user explicitly asked for another device
                return SelectionResult(
                    selected_device=None,
                    status="device_offline",
                    reason=f"Target device '{handle.display_name}' ({handle.device_id}) is offline or heartbeat expired.",
                )

            # Check technical capability
            if not handle.supports_capability(required_capability):
                # NEVER reroute to ASUS if capability is missing on target device
                return SelectionResult(
                    selected_device=None,
                    status="capability_mismatch",
                    reason=f"Target device '{handle.display_name}' does not support required capability '{required_capability}'.",
                )

            # Check trust and permission authorization
            if not handle.is_authorized_for(required_capability, tier=permission_tier, classification=classification):
                return SelectionResult(
                    selected_device=None,
                    status="trust_denied",
                    reason=f"Target device '{handle.display_name}' has trust level '{handle.trust_level}' and is not authorized to execute '{required_capability}' (tier={permission_tier}, classification={classification}).",
                )

            # Evaluate Privacy Policy
            priv_eval = privacy_engine.evaluate_candidate(handle, p_ctx, required_capability)
            if not priv_eval.is_allowed:
                if priv_eval.requires_approval:
                    return SelectionResult(
                        selected_device=None,
                        status="requires_approval",
                        reason=priv_eval.reason,
                        privacy_decision=priv_eval.decision,
                        approval_details=priv_eval.approval_details,
                    )
                status_code = "trust_denied" if handle.trust_level == "restricted" else "privacy_denied"
                return SelectionResult(
                    selected_device=None,
                    status=status_code,
                    reason=priv_eval.reason,
                    privacy_decision=priv_eval.decision,
                )

            return SelectionResult(
                selected_device=handle.device_id,
                status="success",
                reason=f"Explicitly requested target device '{handle.device_id}'.",
                privacy_decision=PrivacyDecision.ALLOW,
            )

        # -------------------------------------------------------------------
        # 2. Target ownership (e.g. file located on specific machine)
        # -------------------------------------------------------------------
        if owning_device:
            own_id = owning_device.strip()
            handle = registry.get_device_handle(own_id)
            if handle:
                is_online = registry.is_device_online(handle.device_id)
                supports_cap = handle.supports_capability(required_capability)
                is_auth = handle.is_authorized_for(required_capability, tier=permission_tier, classification=classification)
                priv_eval = privacy_engine.evaluate_candidate(handle, p_ctx, required_capability) if (is_online and supports_cap and is_auth) else None

                if is_online and supports_cap and is_auth and priv_eval and priv_eval.is_allowed:
                    return SelectionResult(
                        selected_device=handle.device_id,
                        status="success",
                        reason=f"Target resource is owned by '{handle.device_id}'.",
                        privacy_decision=PrivacyDecision.ALLOW,
                    )
                else:
                    reason = f"Target resource is owned by '{own_id}', which is currently unavailable, incapable of '{required_capability}', or unauthorized."
                    if priv_eval and not priv_eval.is_allowed:
                        reason = f"Target resource owner '{own_id}' privacy check failed: {priv_eval.reason}"
                    return SelectionResult(
                        selected_device=None,
                        status="owning_device_unavailable",
                        reason=reason,
                        privacy_decision=priv_eval.decision if priv_eval else None,
                    )

        # -------------------------------------------------------------------
        # 3. Hub-Resident Capabilities (Local execution on hub / source node)
        # -------------------------------------------------------------------
        from .capability_registry import CanonicalCapabilityRegistry
        cap_def = CanonicalCapabilityRegistry.get_instance().get_capability(required_capability)
        is_hub_cap = bool(cap_def and cap_def.target_type == "hub")
        # Unavailable integrations do not override real worker capability/privacy checks.
        if is_hub_cap and cap_def.provider_status.get("integration") and not cap_def.availability:
            is_hub_cap = False
        if is_hub_cap:
            from .config import get_hub_device_id
            exec_dev = get_hub_device_id()
            return SelectionResult(
                selected_device=exec_dev,
                status="success",
                reason=f"Hub-resident capability '{required_capability}' executes on local node '{exec_dev}'.",
                privacy_decision=PrivacyDecision.ALLOW,
            )

        # -------------------------------------------------------------------
        # 4. Candidate Pool & Privacy-Aware Ranking (Local-First)
        # -------------------------------------------------------------------
        # Destructive actions should NOT silently route to random external machines
        if classification == "destructive" or permission_tier >= 3:
            return SelectionResult(
                selected_device=None,
                status="no_eligible_executor",
                reason=f"Destructive action '{required_capability}' cannot be silently routed to an alternate executor.",
            )

        # Gather all capable and authorized online candidates
        capable_handles = registry.get_capable_devices(required_capability, online_only=True)
        authorized_handles = [
            h for h in capable_handles
            if h.is_authorized_for(required_capability, tier=permission_tier, classification=classification)
        ]

        if not authorized_handles:
            # Check if an unauthorized/restricted device exists for clear error reporting
            restricted_handles = [
                h for h in capable_handles
                if not h.is_authorized_for(required_capability, tier=permission_tier, classification=classification)
            ]
            if restricted_handles:
                first_r = restricted_handles[0]
                return SelectionResult(
                    selected_device=None,
                    status="trust_denied",
                    reason=f"Device '{first_r.device_id}' has trust level '{first_r.trust_level}' and is rejected for '{required_capability}'.",
                    privacy_decision=PrivacyDecision.DENY,
                )
            return SelectionResult(
                selected_device=None,
                status="no_eligible_executor",
                reason=f"No online capable executor found for capability '{required_capability}'.",
            )

        # Filter and rank candidates through PrivacyPolicyEngine
        allowed_handles, rejection = privacy_engine.rank_and_filter_candidates(
            candidates=authorized_handles,
            task_context=p_ctx,
            required_capability=required_capability,
        )

        if allowed_handles:
            # Local-first prioritization:
            # If source_device is capable and allowed, prefer it if privacy prefers local execution
            chosen = allowed_handles[0]
            if source_device:
                src_h = next((h for h in allowed_handles if h.device_id == source_device), None)
                if src_h and privacy_engine.policy.prefer_local_execution and p_ctx.privacy_class != PrivacyClass.PUBLIC:
                    chosen = src_h

            alts = [h.device_id for h in allowed_handles if h.device_id != chosen.device_id]
            return SelectionResult(
                selected_device=chosen.device_id,
                status="success",
                reason=f"Selected capable, authorized, and privacy-compliant executor '{chosen.device_id}' ({getattr(chosen, 'locality', 'local_device')}).",
                alternatives=alts,
                privacy_decision=PrivacyDecision.ALLOW,
            )

        # If no candidates passed privacy evaluation:
        if rejection:
            if rejection.requires_approval:
                return SelectionResult(
                    selected_device=None,
                    status="requires_approval",
                    reason=rejection.reason,
                    privacy_decision=PrivacyDecision.REQUIRE_APPROVAL,
                    approval_details=rejection.approval_details,
                )
            if rejection.decision in (PrivacyDecision.WAIT_FOR_PRIVATE_EXECUTOR, PrivacyDecision.LOCAL_ONLY):
                return SelectionResult(
                    selected_device=None,
                    status="wait_for_private_executor",
                    reason=rejection.reason,
                    privacy_decision=rejection.decision,
                )
            if rejection.decision == PrivacyDecision.DENY:
                return SelectionResult(
                    selected_device=None,
                    status="privacy_denied",
                    reason=rejection.reason,
                    privacy_decision=PrivacyDecision.DENY,
                )

        return SelectionResult(
            selected_device=None,
            status="no_eligible_executor",
            reason=f"No eligible executor permitted under privacy policy for '{required_capability}'.",
            privacy_decision=PrivacyDecision.DENY,
        )
