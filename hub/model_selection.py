"""Explainable scoring over structured requirements and observed health."""
import time


class ModelSelector:
    def __init__(self, health): self.health = health

    @staticmethod
    def privacy_reason(route, task, *, pending_approval=False, authorized=False):
        from .privacy import PrivacyPolicyEngine, TaskPrivacyContext, PRIVACY_ORDER
        from .device_handle import DeviceHandle
        provider = route.provider
        maximum = getattr(provider,"max_privacy_class","SENSITIVE")
        if task.privacy_class not in PRIVACY_ORDER or maximum not in PRIVACY_ORDER:
            return "privacy_ineligible"
        if PRIVACY_ORDER[task.privacy_class] > PRIVACY_ORDER[maximum]: return "provider_terms"
        local = getattr(provider,"locality","cloud_worker") in {"local_device","private_server"}
        candidate = DeviceHandle(device_id="agent_model", display_name="Reasoning model",
            device_type="desktop" if local else "cloud_worker", trust_level="trusted",
            locality=getattr(provider,"locality","cloud_worker"),private_compute_allowed=local,cloud_processing_allowed=not local)
        engine = PrivacyPolicyEngine.get_instance()
        approved = authorized and engine.policy.allow_cloud_approval_override
        privacy = TaskPrivacyContext(privacy_class=task.privacy_class,user_approved=approved,
            approved_executor_id="agent_model" if approved else None)
        decision = engine.evaluate_candidate(candidate,privacy,"agent.reason")
        if decision.is_allowed or pending_approval and decision.requires_approval: return None
        return "privacy_approval_required" if decision.requires_approval else "privacy_ineligible"

    def rank(self, routes, task, *, pending_approval=False, authorized_key=None, attempted=None,
             excluded_providers=None, minimum_window=0, ignore_health=False):
        eligible, rejected = [], []
        attempted, excluded_providers = attempted or set(), excluded_providers or set()
        for route in routes:
            p,m = route.provider,route.profile
            key=(p.name,p.model)
            reason=None
            if not getattr(p,"configured",True): reason="not_configured"
            elif key in attempted: reason="already_attempted"
            elif p.name in excluded_providers: reason="provider_failed_this_turn"
            elif task.requires_vision and ("image" not in m.modalities or not getattr(p,"supports_vision",False)): reason="unsupported_modality"
            elif task.requires_tools and not m.tool_calling: reason="unsupported_tool_use"
            elif task.requires_structured_output and not m.structured_output: reason="structured_output_required"
            elif (minimum_window or task.requires_long_context) and m.context_window is None: reason="context_window_unknown"
            elif m.context_window is not None and m.context_window < max(task.context_tokens,minimum_window): reason="context_too_large"
            else: reason=self.privacy_reason(route,task,pending_approval=pending_approval,authorized=key==authorized_key)
            blocked=self.health.blocked(p.name,p.model)
            if not reason and not ignore_health and (blocked or route.cooldown_until > time.monotonic()):
                reason="quota_exhausted" if blocked and blocked.state=="quota_exhausted" else "provider_cooldown"
            if reason:
                rejected.append({"provider":p.name,"model":p.model,"reason":reason})
                continue
            row=self.health.get(p.name,p.model)
            provider_health=self.health.get(p.name)
            latency=row.average_latency if row.average_latency is not None else m.expected_latency
            quality={"low":.4,"unknown":.5,"medium":2.,"high":5.}[task.complexity]
            n=row.successes+row.failures
            reliability=(row.successes+3)/(n+4)
            quota=row if row.known_limit is not None else provider_health
            pressure=max(0.,min(1.,1-quota.known_remaining/quota.known_limit)) if quota.known_limit and quota.known_remaining is not None else 0.
            components={
                "capability_fit":2. if task.requires_tools else 0.,
                "reasoning_fit":quality*m.reasoning,
                "coding_fit":5.*m.coding if task.requires_code_reasoning else 0.,
                "computer_use_fit":4.*m.computer_use+2.*m.tool_reliability if task.requires_computer_use else 0.,
                # Preserve the old neutral ranking penalty for unknown latency;
                # it is not promoted to a measured or configured time estimate.
                "expected_latency":-task.latency_priority*min(latency if latency is not None else 5.,120.)/5.,
                "recent_reliability":2.*reliability,
                "quota_pressure":-2.*pressure,
                "provider_health":-1. if provider_health.state=="degraded" or row.state=="degraded" else 0.,
                "configured_preference":2.*m.configured_priority,
                "interaction_fit":2.*(m.background_suitability if task.background_allowed else m.interactive_suitability)}
            components={k:round(v,4) for k,v in components.items()}
            eligible.append((route,{"provider":p.name,"model":p.model,"score":round(sum(components.values()),4),"components":components}))
        eligible.sort(key=lambda pair:-pair[1]["score"])
        return eligible,rejected
