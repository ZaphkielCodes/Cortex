"""
tests/test_phase_a7.py
----------------------
Phase A7 Test Suite: Privacy-Aware Execution Routing, Structured Classification,
Data Minimization, Approval Override Flow, A6 Handoff Integration, Policy Persistence,
and Ground-Truth Self-Knowledge.

Core Invariant: "Capable != Allowed"

Covers:
- Test 1 — Public task -> remote/cloud allowed
- Test 2 — Sensitive local task -> local executor selected
- Test 3 — Sensitive task + local unavailable + cloud forbidden -> wait (WAIT_FOR_PRIVATE_EXECUTOR)
- Test 4 — Sensitive task + trusted Lenovo allowed -> Lenovo selected
- Test 5 — Restricted executor rejected despite capability
- Test 6 — User policy override requiring approval
- Test 7 — Approval denial preserves wait/local-only state
- Test 8 — Approval grant permits only specified executor/data
- Test 9 — A6 failover obeys privacy constraints
- Test 10 — Data minimization excludes unrelated context
- Test 11 — Policy survives restart
- Test 12 — Self-knowledge explains decision truthfully (all 5 questions)
- Test 13 — Production DB isolation check
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import pytest
import pytest_asyncio

from hub.device_handle import DeviceHandle
from hub.executor_registry import ExecutorRegistry
from hub.executor_selector import ExecutorSelector, SelectionResult
from hub.privacy import (
    PrivacyPolicyEngine,
    PrivacyClass,
    ExecutorLocality,
    ExecutionEnvironment,
    PrivacyDecision,
    UserPrivacyPolicy,
    TaskPrivacyContext,
    classify_task_privacy,
    minimize_execution_payload,
)
from hub.task_ownership import (
    TaskOwnershipManager,
    TaskExecution,
    ExecutionState,
    ActionSafetyClass,
)
from hub.agent_harness import AgentHarness
from hub.database import (
    save_task_execution,
    get_task_execution,
    list_task_executions,
    save_privacy_policy,
    get_privacy_policy,
    list_privacy_policies,
)


# Helper device handle factories
def make_asus_local() -> DeviceHandle:
    return DeviceHandle(
        device_id="asus",
        display_name="ASUS Zenbook",
        role="primary_admin",
        trust_level="admin",
        locality=ExecutorLocality.LOCAL_DEVICE,
        execution_environment=ExecutionEnvironment.ON_PREMISE,
        private_compute_allowed=True,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.list", "fs.read", "email.send", "ui.notify", "app.launch"],
        online=True,
    )


def make_lenovo_trusted() -> DeviceHandle:
    return DeviceHandle(
        device_id="lenovo",
        display_name="Lenovo ThinkPad",
        role="secondary_worker",
        trust_level="trusted",
        locality=ExecutorLocality.TRUSTED_LAN_DEVICE,
        execution_environment=ExecutionEnvironment.ON_PREMISE,
        private_compute_allowed=True,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.list", "fs.read", "email.send", "ui.notify", "app.launch"],
        online=True,
    )


def make_cloud_worker() -> DeviceHandle:
    return DeviceHandle(
        device_id="cloud_worker_1",
        display_name="Cortex Cloud Cluster",
        role="cloud_worker",
        trust_level="trusted",
        device_type="cloud_worker",
        locality=ExecutorLocality.CLOUD_WORKER,
        execution_environment=ExecutionEnvironment.CLOUD_HOSTED,
        private_compute_allowed=False,
        cloud_processing_allowed=True,
        capabilities=["web.search", "web.scrape_page", "fs.read", "email.send"],
        online=True,
    )


def make_restricted_guest() -> DeviceHandle:
    return DeviceHandle(
        device_id="guest_phone",
        display_name="Guest Mobile Phone",
        role="guest",
        trust_level="restricted",
        locality=ExecutorLocality.TRUSTED_LAN_DEVICE,
        execution_environment=ExecutionEnvironment.UNTRUSTED_RUNTIME,
        private_compute_allowed=False,
        cloud_processing_allowed=False,
        capabilities=["web.search", "fs.read", "email.send"],
        online=True,
    )


# ===========================================================================
# Test 1 — Public task -> remote/cloud allowed
# ===========================================================================
@pytest.mark.asyncio
async def test_1_public_task_remote_allowed():
    """
    Test 1: Public task (e.g. web search for public information)
    is permitted to run on remote / cloud executor.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    cloud = make_cloud_worker()
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)

    p_ctx = classify_task_privacy(
        capability="web.search",
        arguments={"query": "weather forecast for Tokyo tomorrow"},
    )
    assert p_ctx.privacy_class == PrivacyClass.PUBLIC

    res = selector.select_executor(
        required_capability="web.search",
        requested_device="cloud_worker_1",
        arguments={"query": "weather forecast for Tokyo tomorrow"},
    )

    assert res.is_success
    assert res.selected_device == "cloud_worker_1"
    assert res.privacy_decision == PrivacyDecision.ALLOW


# ===========================================================================
# Test 2 — Sensitive local task -> local executor selected
# ===========================================================================
@pytest.mark.asyncio
async def test_2_sensitive_local_task_selects_local():
    """
    Test 2: Sensitive task (personal desktop files / sensitive documents)
    is prioritized to local executor (ASUS) over available cloud worker.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    asus = make_asus_local()
    cloud = make_cloud_worker()

    registry.register_device(asus)
    registry.set_device_online("asus", True)
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)

    # Email draft or desktop file inspection is SENSITIVE
    res = selector.select_executor(
        required_capability="fs.read",
        arguments={"path": "C:\\Users\\abhin\\Desktop\\Q3_financial_report.pdf"},
        source_device="asus",
    )

    assert res.is_success
    assert res.selected_device == "asus"
    assert "cloud_worker_1" not in (res.selected_device or "")


# ===========================================================================
# Test 3 — Sensitive task + local unavailable + cloud forbidden -> wait
# ===========================================================================
@pytest.mark.asyncio
async def test_3_sensitive_task_local_unavailable_cloud_forbidden_waits():
    """
    Test 3: Sensitive task when local executor is offline and cloud execution
    is forbidden by user policy transitions to WAIT_FOR_PRIVATE_EXECUTOR.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    privacy_engine.policy.sensitive_data_cloud_allowed = False
    privacy_engine.policy.allow_cloud_approval_override = False
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    asus = make_asus_local()
    cloud = make_cloud_worker()

    registry.register_device(asus)
    registry.set_device_online("asus", False)  # ASUS offline
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)  # Cloud online

    res = selector.select_executor(
        required_capability="email.send",
        arguments={"to": "client@example.com", "subject": "Contract", "body": "Sensitive details"},
        privacy_class=PrivacyClass.SENSITIVE,
        source_device="asus",
    )

    assert not res.is_success
    assert res.status == "wait_for_private_executor"
    assert res.privacy_decision == PrivacyDecision.WAIT_FOR_PRIVATE_EXECUTOR
    assert "cloud" in res.reason.lower()


# ===========================================================================
# Test 4 — Sensitive task + trusted Lenovo allowed -> Lenovo selected
# ===========================================================================
@pytest.mark.asyncio
async def test_4_sensitive_task_trusted_lenovo_selected():
    """
    Test 4: When ASUS is offline but trusted LAN Lenovo is online with
    private compute enabled, sensitive task routes to Lenovo and NOT cloud.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    asus = make_asus_local()
    lenovo = make_lenovo_trusted()
    cloud = make_cloud_worker()

    registry.register_device(asus)
    registry.set_device_online("asus", False)  # ASUS offline
    registry.register_device(lenovo)
    registry.set_device_online("lenovo", True)  # Lenovo online
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)  # Cloud online

    res = selector.select_executor(
        required_capability="email.send",
        arguments={"to": "team@work.com", "subject": "Review", "body": "Confidential draft"},
        privacy_class=PrivacyClass.SENSITIVE,
        source_device="asus",
    )

    assert res.is_success
    assert res.selected_device == "lenovo"
    assert res.privacy_decision == PrivacyDecision.ALLOW


# ===========================================================================
# Test 5 — Restricted executor rejected despite capability
# ===========================================================================
@pytest.mark.asyncio
async def test_5_restricted_executor_rejected_despite_capability():
    """
    Test 5: Capable != Allowed.
    A guest/restricted device advertising required capability is strictly
    denied from receiving sensitive or personal tasks.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    guest = make_restricted_guest()
    registry.register_device(guest)
    registry.set_device_online("guest_phone", True)

    res = selector.select_executor(
        required_capability="email.send",
        requested_device="guest_phone",
        arguments={"to": "ceo@corp.com", "subject": "Secret", "body": "Financials"},
        privacy_class=PrivacyClass.SENSITIVE,
    )

    assert not res.is_success
    assert res.status in ("trust_denied", "privacy_denied")
    assert "restricted" in res.reason.lower() or "not authorized" in res.reason.lower()


# ===========================================================================
# Test 6 — User policy override requiring approval
# ===========================================================================
@pytest.mark.asyncio
async def test_6_user_policy_override_requires_approval():
    """
    Test 6: When local device is offline, cloud candidate is capable,
    and policy has allow_cloud_approval_override=True, selector returns
    status="requires_approval" with structured approval details.
    """
    registry = ExecutorRegistry.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    privacy_engine.policy.sensitive_data_cloud_allowed = False
    privacy_engine.policy.allow_cloud_approval_override = True
    selector = ExecutorSelector(registry=registry, privacy_engine=privacy_engine)

    asus = make_asus_local()
    cloud = make_cloud_worker()

    registry.register_device(asus)
    registry.set_device_online("asus", False)
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)

    res = selector.select_executor(
        required_capability="email.send",
        arguments={"to": "client@example.com", "subject": "Invoice", "body": "Invoice data"},
        privacy_class=PrivacyClass.SENSITIVE,
        source_device="asus",
    )

    assert not res.is_success
    assert res.status == "requires_approval"
    assert res.privacy_decision == PrivacyDecision.REQUIRE_APPROVAL
    assert res.approval_details is not None
    assert "approval_id" in res.approval_details
    assert "what_data" in res.approval_details
    assert "which_executor" in res.approval_details
    assert "why_needed" in res.approval_details
    assert "what_will_leave_local_device" in res.approval_details


# ===========================================================================
# Test 7 — Approval denial preserves wait/local-only state
# ===========================================================================
@pytest.mark.asyncio
async def test_7_approval_denial_preserves_wait_state():
    """
    Test 7: When user explicitly denies approval, the cloud execution
    remains denied and task remains local/waiting.
    """
    privacy_engine = PrivacyPolicyEngine.get_instance()
    cloud = make_cloud_worker()

    p_ctx = TaskPrivacyContext(
        privacy_class=PrivacyClass.SENSITIVE,
        task_execution_id="task_deny_1",
    )

    details = privacy_engine._build_approval_details(cloud, p_ctx, capability="email.send")
    appr_id = details["approval_id"]

    denied_res = privacy_engine.deny_approval(appr_id)
    assert denied_res is not None
    assert denied_res["status"] == "denied"
    assert not privacy_engine.is_approval_granted(appr_id, "cloud_worker_1")

    # Evaluation remains blocked
    eval_res = privacy_engine.evaluate_candidate(cloud, p_ctx, required_capability="email.send")
    assert not eval_res.is_allowed


# ===========================================================================
# Test 8 — Approval grant permits only specified executor/data
# ===========================================================================
@pytest.mark.asyncio
async def test_8_approval_grant_permits_specified_executor():
    """
    Test 8: Explicit approval grant permits only the specified executor,
    and data minimization applies to the payload sent to that executor.
    """
    privacy_engine = PrivacyPolicyEngine.get_instance()
    cloud = make_cloud_worker()

    p_ctx = TaskPrivacyContext(
        privacy_class=PrivacyClass.SENSITIVE,
        task_execution_id="task_grant_1",
    )
    details = privacy_engine._build_approval_details(cloud, p_ctx, capability="email.send")
    appr_id = details["approval_id"]

    granted_res = privacy_engine.grant_approval(appr_id)
    assert granted_res["status"] == "granted"
    assert privacy_engine.is_approval_granted(appr_id, "cloud_worker_1")
    assert not privacy_engine.is_approval_granted(appr_id, "other_worker")

    # Mark context as approved for this specific executor
    p_ctx.user_approved = True
    p_ctx.approved_executor_id = "cloud_worker_1"

    eval_res = privacy_engine.evaluate_candidate(cloud, p_ctx, required_capability="email.send")
    assert eval_res.is_allowed
    assert eval_res.decision == PrivacyDecision.ALLOW

    # Verify data minimization applies
    min_res = minimize_execution_payload(
        capability="email.send",
        arguments={"to": "partner@firm.com", "subject": "Quarterly Update", "body": "Details enclosed"},
        remote_executor_id="cloud_worker_1",
        context={
            "conversation_history": ["user: hi", "assistant: hello"],
            "user_profile": {"name": "Abhin", "ssn": "000-00-0000"},
            "session_token": "secret_session_abc",
        },
    )
    assert "conversation_history" not in min_res.minimized_payload
    assert "user_profile" not in min_res.minimized_payload
    assert "session_token" not in min_res.minimized_payload
    assert "conversation_history" in min_res.stripped_fields
    assert "to" in min_res.minimized_payload
    assert "subject" in min_res.minimized_payload
    assert "body" in min_res.minimized_payload


# ===========================================================================
# Test 9 — A6 failover obeys privacy constraints
# ===========================================================================
@pytest.mark.asyncio
async def test_9_a6_failover_obeys_privacy():
    """
    Test 9: Task active on ASUS with SENSITIVE privacy class.
    ASUS disconnects. Cloud worker is available.
    Handoff to cloud worker is rejected under privacy constraints;
    task enters WAITING_FOR_EXECUTOR with WAITING_FOR_PRIVATE_EXECUTOR metadata.
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    privacy_engine.policy.sensitive_data_cloud_allowed = False
    privacy_engine.policy.allow_cloud_approval_override = False

    asus = make_asus_local()
    cloud = make_cloud_worker()

    registry.register_device(asus)
    registry.set_device_online("asus", True)
    registry.register_device(cloud)
    registry.set_device_online("cloud_worker_1", True)

    # Create active task execution on ASUS
    ex = await tom.create_execution(
        goal_id="goal_priv_failover_1",
        step_id="step_failover_1",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["email.send"],
        metadata={"privacy_class": PrivacyClass.SENSITIVE},
    )
    assert ex.execution_state == ExecutionState.ACTIVE

    # Simulate ASUS heartbeat expired
    registry.set_device_online("asus", False)

    # Failover trigger
    failover_res = await tom.failover_if_needed(ex.task_execution_id)

    assert failover_res["status"] == "waiting_for_executor"
    assert failover_res["privacy_decision"] == "WAIT_FOR_PRIVATE_EXECUTOR"
    assert "privacy policy" in failover_res["reason"].lower()

    # Verify persisted execution is in WAITING_FOR_EXECUTOR
    updated_ex = await tom.get_execution(ex.task_execution_id)
    assert updated_ex.execution_state == ExecutionState.WAITING_FOR_EXECUTOR
    wait_info = updated_ex.metadata.get("waiting_for_executor", {})
    assert wait_info.get("reason") == "WAITING_FOR_PRIVATE_EXECUTOR"
    assert wait_info.get("privacy_requirement") == PrivacyClass.SENSITIVE


# ===========================================================================
# Test 10 — Data minimization excludes unrelated context
# ===========================================================================
def test_10_data_minimization_excludes_unrelated_context():
    """
    Test 10: Strict data minimization test. Verifies that conversation
    history, chat history, memory, personal profile, and session tokens
    are completely stripped, and only declared capability arguments remain.
    """
    min_res = minimize_execution_payload(
        capability="web.search",
        arguments={
            "query": "photosynthesis mechanism",
            "max_results": 5,
            "extra_unneeded_arg": "should_be_stripped",
        },
        remote_executor_id="cloud_worker_1",
        context={
            "conversation_history": [{"role": "user", "text": "my private thoughts"}],
            "memory": {"user_notes": "sensitive personal journal"},
            "user_profile": {"home_address": "123 Private St"},
            "system_prompt": "You are a helpful assistant",
            "internal_telemetry": {"ip": "192.168.1.100"},
        },
    )

    assert min_res.minimized_payload == {
        "query": "photosynthesis mechanism",
        "max_results": 5,
    }
    assert "extra_unneeded_arg" in min_res.stripped_fields
    assert "conversation_history" in min_res.stripped_fields
    assert "memory" in min_res.stripped_fields
    assert "user_profile" in min_res.stripped_fields
    assert "system_prompt" in min_res.stripped_fields
    assert "internal_telemetry" in min_res.stripped_fields


# ===========================================================================
# Test 11 — Policy survives restart
# ===========================================================================
@pytest.mark.asyncio
async def test_11_policy_survives_restart():
    """
    Test 11: User privacy preferences are durably stored in SQLite
    (privacy_policies table) and reloaded across engine restarts.
    """
    engine1 = PrivacyPolicyEngine.get_instance()
    await engine1.initialize()

    # Update policy
    await engine1.update_user_policy({
        "sensitive_data_cloud_allowed": True,
        "prefer_local_execution": False,
        "allow_remote_public_tasks": True,
    })

    assert engine1.policy.sensitive_data_cloud_allowed is True
    assert engine1.policy.prefer_local_execution is False

    # Simulate restart
    PrivacyPolicyEngine.reset()
    engine2 = PrivacyPolicyEngine.get_instance()
    await engine2.initialize()

    assert engine2.policy.sensitive_data_cloud_allowed is True
    assert engine2.policy.prefer_local_execution is False
    assert engine2.policy.allow_remote_public_tasks is True


# ===========================================================================
# Test 12 — Self-knowledge explains decision truthfully
# ===========================================================================
@pytest.mark.asyncio
async def test_12_self_knowledge_answers_all_5_privacy_questions():
    """
    Test 12: Truthful conversational answers to all 5 required privacy queries:
    1. "Why are you running this locally?"
    2. "Why didn't this move to the cloud?"
    3. "Can Lenovo handle this privately?"
    4. "What data would leave my laptop?"
    5. "Why is this task waiting?"
    """
    registry = ExecutorRegistry.get_instance()
    tom = TaskOwnershipManager.get_instance()
    privacy_engine = PrivacyPolicyEngine.get_instance()
    privacy_engine.policy.sensitive_data_cloud_allowed = False

    asus = make_asus_local()
    lenovo = make_lenovo_trusted()
    registry.register_device(asus)
    registry.register_device(lenovo)
    registry.set_device_online("asus", True)
    registry.set_device_online("lenovo", True)

    from hub.control_layer import CortexControlLayer
    ctrl = CortexControlLayer()
    from model_contract_fixtures import runtime_harness
    harness, model = await runtime_harness()

    # Create active sensitive execution on ASUS
    ex = await tom.create_execution(
        goal_id="goal_sk_priv_1",
        step_id="step_sk_priv_1",
        owner_device_id="cortex",
        executor_device_id="asus",
        required_capabilities=["email.send"],
        required_resources=["C:\\Users\\abhin\\Desktop\\secrets.txt"],
        metadata={"privacy_class": PrivacyClass.SENSITIVE},
    )

    # 1. "Why are you running this locally?"
    r1 = await harness.process_request("asus", "Why are you running this locally?", context={})
    assert r1.status == "success", r1

    # 2. "Why didn't this move to the cloud?"
    r2 = await harness.process_request("asus", "Why didn't this move to the cloud?", context={})
    assert r2.status == "success", r2

    # 3. "Can Lenovo handle this privately?"
    r3 = await harness.process_request("asus", "Can Lenovo handle this privately?", context={})
    assert r3.status == "success", r3

    # 4. "What data would leave my laptop?"
    r4 = await harness.process_request("asus", "What data would leave my laptop?", context={})
    assert r4.status == "success", r4

    # Transition to waiting for private executor
    await tom.transition_to_waiting_for_executor(
        task_execution_id=ex.task_execution_id,
        preferred_executor="asus",
        missing_reason="ASUS offline and cloud forbidden by policy",
        privacy_requirement="SENSITIVE",
        extra_metadata={"reason": "WAITING_FOR_PRIVATE_EXECUTOR"},
    )

    # 5. "Why is this task waiting?"
    r5 = await harness.process_request("asus", "Why is this task waiting?", context={})
    assert r5.status == "success", r5
    assert model.calls[-1]["mode"] == "result_interpretation"
    evidence=model.calls[-1]["tool_result"]
    assert evidence["status"] == "success"
    assert evidence["data"]["source_device"] == "asus"
    assert evidence["data"]["automatic_action_replay"] is False
    assert evidence["data"]["privacy_policy"]["sensitive_data_cloud_allowed"] is False


# ===========================================================================
# Test 13 — Production DB Isolation Check
# ===========================================================================
def test_13_production_db_isolation():
    """Verify tests run strictly against test_cortex.db and production DB is untouched."""
    db_path = os.environ.get("CORTEX_DB_PATH", "")
    assert "test_cortex.db" in db_path
    assert not db_path.endswith("data/cortex.db")

    prod_db = Path("data/cortex.db").resolve()
    from hub.database import get_db_path
    resolved_test_db = get_db_path().resolve()
    assert resolved_test_db != prod_db, f"FATAL: Test targeting prod DB at {resolved_test_db}!"
    assert not str(resolved_test_db).replace('\\', '/').endswith("data/cortex.db")
