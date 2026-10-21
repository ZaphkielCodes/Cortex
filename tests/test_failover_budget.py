"""Deterministic scheduling probes; no live providers or desktop executors."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
import time

import pytest

from hub.direct_router import DirectModelRouter
from hub.failover_budget import allocate_attempt
from hub.intent_schema import ModelResult
from hub.llm_provider import ProviderHTTPError, ReasoningProviderUnavailable
from hub.provider_health import ProviderHealthStore
from hub.routing_profiles import ModelProfile, TaskProfile


class Clock:
    def __init__(self):
        self.now = 100.

    def monotonic(self):
        return self.now

    @asynccontextmanager
    async def timeout(self, _seconds):
        yield


class Candidate:
    locality = "cloud_worker"
    max_privacy_class = "SENSITIVE"
    configured = True
    supports_vision = False

    def __init__(self, name, clock, duration=0., failure=None, **profile):
        self.name, self.model = name, name + "-model"
        self.clock, self.duration, self.failure = clock, duration, failure
        self.profile = ModelProfile(self.model, self.name, **profile)
        self.calls = []

    async def generate_result(self, _text, context):
        self.calls.append(dict(context))
        budget = context["_router_timeout_seconds"]
        if self.duration is None or self.duration > budget:
            self.clock.now += budget
            raise TimeoutError()
        self.clock.now += self.duration
        if self.failure:
            raise self.failure
        return ModelResult(type="RESPONSE", response_text="Verified public answer")


def router(*providers):
    return DirectModelRouter(list(providers), profiles={(p.name, p.model): p.profile for p in providers})


def virtual_time(monkeypatch, clock):
    import hub.direct_router as module
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic, time=time.time))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(timeout=clock.timeout,
        CancelledError=asyncio.CancelledError, Lock=asyncio.Lock, sleep=asyncio.sleep))


def allocation(r, remaining=60., background=True):
    ranked, _ = r.selector.rank(r.routes, TaskProfile.from_context({"privacy_class": "PUBLIC"}))
    return allocate_attempt(remaining, ranked, r.health, background=background, attempts_left=6)


@pytest.mark.asyncio
async def test_three_fallbacks_after_primary_quota_all_keep_meaningful_windows(monkeypatch):
    clock = Clock()
    virtual_time(monkeypatch, clock)
    quota = ProviderHTTPError("synthetic", 429)
    quota.quota_scopes = ["TPD"]
    primary = Candidate("primary", clock, failure=quota, configured_priority=1)
    fallbacks = [Candidate("first", clock, None), Candidate("second", clock, None), Candidate("last", clock, 12.)]
    r = router(primary, *fallbacks)
    result = await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC", "execution_mode": "background"})
    assert result.response_text == "Verified public answer"
    assert [len(p.calls) for p in [primary, *fallbacks]] == [1, 1, 1, 1]
    assert all(p.calls[0]["_router_timeout_seconds"] >= 10 for p in fallbacks)
    assert clock.now - 100 < 60
    assert [e["selected_provider"] for e in r.events if e["event"] == "routing_succeeded"] == ["last"]


@pytest.mark.asyncio
async def test_fast_failure_redistributes_unused_time(monkeypatch):
    clock = Clock()
    virtual_time(monkeypatch, clock)
    first = Candidate("first", clock, .1, ProviderHTTPError("synthetic", 503))
    second = Candidate("second", clock, 22.)
    third = Candidate("third", clock)
    r = router(first, second, third)
    await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC", "execution_mode": "background"})
    assert second.calls[0]["_router_timeout_seconds"] > first.calls[0]["_router_timeout_seconds"]
    assert clock.now - 100 == pytest.approx(22.1)
    assert not third.calls


@pytest.mark.parametrize("background,expected", [(False, 12.), (True, 45.)])
def test_only_viable_candidate_gets_full_lane_cap(background, expected):
    r = router(Candidate("only", Clock()))
    assert allocation(r, background=background).seconds == expected


def test_observed_latency_changes_allocation_without_reordering():
    r = router(Candidate("first", Clock()), Candidate("second", Clock()), Candidate("third", Clock()))
    before = allocation(r)
    r.health.get("first", "first-model").average_latency = 35.
    ranked = [(route, {}) for route in r.routes]  # Keep the already-chosen ranking fixed.
    after = allocate_attempt(60., ranked, r.health, background=True, attempts_left=6)
    assert after.seconds > before.seconds
    assert after.latency_source == "observed" and after.latency_seconds == 35.
    assert after.reserved_seconds >= 20.


def test_unknown_latency_stays_unknown_and_keeps_a_meaningful_window():
    r = router(Candidate("first", Clock()), Candidate("second", Clock()))
    result = allocation(r)
    assert result.latency_seconds is None and result.latency_source == "unknown"
    assert r.health.get("first", "first-model").average_latency is None
    assert r.routes[0].profile.expected_latency is None
    assert result.seconds >= 10.


def test_profile_latency_is_a_prior_not_an_observation():
    r = router(Candidate("first", Clock(), expected_latency=30.), Candidate("second", Clock()))
    result = allocate_attempt(60., [(route, {}) for route in r.routes], r.health, background=True, attempts_left=6)
    assert result.latency_source == "profile_prior" and result.latency_seconds == 30.
    assert r.health.get("first", "first-model").average_latency is None


@pytest.mark.parametrize("remaining", [0., -1., .0001, 9.99, float("nan"), float("inf")])
def test_no_zero_negative_or_tiny_accidental_background_attempt(remaining):
    assert allocation(router(Candidate("only", Clock())), remaining) is None


def test_attempt_limit_only_reserves_for_candidates_that_can_still_run():
    r = router(*(Candidate(str(i), Clock()) for i in range(10)))
    ranked = [(route, {}) for route in r.routes]
    result = allocate_attempt(60., ranked, r.health, background=True, attempts_left=2)
    assert result.viable_candidates == 2 and result.seconds > 25.


@pytest.mark.asyncio
@pytest.mark.parametrize("background,ceiling", [(False, 30.), (True, 60.)])
async def test_lane_total_deadline_and_no_retry_loop(monkeypatch, background, ceiling):
    clock = Clock()
    virtual_time(monkeypatch, clock)
    providers = [Candidate(str(i), clock, None) for i in range(3)]
    r = router(*providers)
    with pytest.raises(ReasoningProviderUnavailable):
        await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC",
            "execution_mode": "background" if background else "interactive"})
    assert clock.now - 100 <= ceiling + .001
    assert all(len(p.calls) == 1 for p in providers)
    assert all(0 < p.calls[0]["_router_timeout_seconds"] <= (45 if background else 12) for p in providers)


@pytest.mark.asyncio
async def test_budget_exhaustion_is_not_external_unavailability(monkeypatch):
    import hub.config
    monkeypatch.setattr(hub.config, "get_lane_deadlines", lambda _lane: (1., 1.))
    candidate = Candidate("uninvoked", Clock())
    r = router(candidate)
    with pytest.raises(ReasoningProviderUnavailable) as error:
        await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC"})
    assert not candidate.calls
    assert error.value.routing_outcome == "NO_USABLE_RESPONSE_WITHIN_FAILOVER_BUDGET"
    assert error.value.unattempted_candidates == [{"provider": "uninvoked", "model": "uninvoked-model",
        "outcome": "NOT_ATTEMPTED_BUDGET_EXHAUSTED"}]
    assert not any(e["event"] == "provider_failed" for e in r.events)


@pytest.mark.asyncio
async def test_lock_wait_is_bounded_by_the_same_deadline(monkeypatch):
    import hub.config
    monkeypatch.setattr(hub.config, "get_lane_deadlines", lambda _lane: (1., 2.1))
    r = router(Candidate("locked", Clock()))
    await r.routes[0].lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(ReasoningProviderUnavailable) as error:
            await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC"})
    finally:
        r.routes[0].lock.release()
    assert time.monotonic() - started < 2.5
    assert error.value.routing_outcome == "NO_USABLE_RESPONSE_WITHIN_FAILOVER_BUDGET"
    assert not r.routes[0].provider.calls


@pytest.mark.asyncio
async def test_real_hanging_calls_obey_total_deadline_without_retry(monkeypatch):
    import hub.config
    monkeypatch.setattr(hub.config, "get_lane_deadlines", lambda _lane: (1., 6.))
    providers = [Candidate("first", Clock()), Candidate("second", Clock())]
    counts = [0, 0]
    for index, candidate in enumerate(providers):
        async def hang(_text, _context, index=index):
            counts[index] += 1
            await asyncio.Event().wait()
        candidate.generate_result = hang
    started = time.monotonic()
    r = router(*providers)
    with pytest.raises(ReasoningProviderUnavailable):
        await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC"})
    assert time.monotonic() - started < 6.5
    assert counts == [1, 1]
    assert all(e["attempt_outcome"] == "PROVIDER_TIMEOUT" for e in r.events if e["event"] == "provider_failed")


@pytest.mark.asyncio
async def test_fair_failover_keeps_persisted_task_and_parent_identity(monkeypatch):
    from hub.database import add_task, get_task
    clock = Clock()
    virtual_time(monkeypatch, clock)
    await add_task({"id": "budget-task", "title": "Synthetic identity check", "type": "router_validation",
        "command": "router_validation", "context": {"parent_request_id": "budget-parent"}})
    first = Candidate("first", clock, None)
    second = Candidate("second", clock, 11.)
    r = router(first, second)
    await r.generate_result("Synthetic public input", {"privacy_class": "PUBLIC", "execution_mode": "background",
        "operational_task_id": "budget-task", "request_id": "budget-parent"})
    saved = json.loads((await get_task("budget-task"))["context"])
    assert saved["parent_request_id"] == "budget-parent"
    assert all(e["task_id"] == "budget-task" and e["parent_task_id"] == "budget-parent" for e in r.events)
    assert len(first.calls) == len(second.calls) == 1
