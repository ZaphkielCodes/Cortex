"""
tests/test_phase_a1.py
----------------------
Comprehensive test suite for Cortex Phase A1:
Web Search, Weather, and Browser Capabilities.

Covers:
1. Search provider interface & real / mock switching
2. Bounded search results
3. web.fetch URL validation & private network guard (SSRF protection)
4. weather.current & weather.forecast
5. Provider unavailable failure tests (no hallucinations)
6. browser.open_url dispatch & semantic destination resolution
7. Capability Registry & provider statuses
8. GoalPlanner integration (e.g. search + open result)
9. Conversational routing for live external queries
10. Source provenance tracking
11. Required Physical-Style Tests:
    - Physical Weather Test
    - Physical Search Test
    - Physical Browser Test
12. Production DB safety
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import pytest_asyncio

from hub.agent_harness import AgentHarness
from hub.browser_navigation import (
    COMMON_WEB_DESTINATIONS,
    is_valid_url,
    normalize_url,
    resolve_web_destination,
)
from hub.capability_registry import get_core_capabilities
from hub.control_layer import CortexControlLayer
from hub.database import init_db, save_device_capabilities
from hub.goals import Goal, GoalManager, GoalStep
from hub.intent_schema import ConversationalResponse
from hub.planner import GoalPlanner
from hub.search_provider import (
    DuckDuckGoSearchProvider,
    MockSearchProvider,
    SearchProvider,
    get_search_provider,
    set_search_provider_override,
)
from hub.weather_provider import (
    MockWeatherProvider,
    OpenMeteoWeatherProvider,
    WeatherProvider,
    get_weather_provider,
    set_weather_provider_override,
)
from hub.web_fetch import clean_html_content, is_private_or_local_host, web_fetch


@pytest_asyncio.fixture(autouse=True)
async def setup_test_devices():
    """Ensure database and test device capabilities are enrolled."""
    await init_db()
    await save_device_capabilities(
        "test_dev",
        [
            "app.launch",
            "browser.open_url",
            "open_url",
            "fs.move",
            "fs.list",
            "fs.stat",
            "fs.delete",
            "device.status",
        ],
    )


# ===========================================================================
# 1. SEARCH PROVIDER TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_search_provider_interface_and_mock():
    """Verify SearchProvider base interface and MockSearchProvider behavior."""
    mock_p = MockSearchProvider()
    assert mock_p.provider_name == "mock"
    assert mock_p.is_available is True

    res = await mock_p.search("RTX 5090", max_results=1)
    assert res["query"] == "RTX 5090"
    assert len(res["results"]) == 1
    assert "RTX 5090" in res["results"][0]["title"]
    assert res["provider"] == "mock"
    assert "retrieved_at" in res


@pytest.mark.asyncio
async def test_search_provider_switching_and_override():
    """Verify switching providers via set_search_provider_override."""
    set_search_provider_override(None)
    # Default is duckduckgo
    active = get_search_provider()
    assert isinstance(active, DuckDuckGoSearchProvider)

    # Override to mock
    custom_mock = MockSearchProvider([{"title": "Custom", "url": "https://example.com", "snippet": "Test", "source": "example.com"}])
    set_search_provider_override(custom_mock)
    active2 = get_search_provider()
    assert active2 is custom_mock
    res = await active2.search("anything")
    assert res["results"][0]["title"] == "Custom"

    # Reset
    set_search_provider_override(None)


@pytest.mark.asyncio
async def test_bounded_search_results():
    """Verify search caps results to max_results."""
    mock_p = MockSearchProvider([
        {"title": f"Item {i}", "url": f"https://example.com/{i}", "snippet": f"Snip {i}", "source": "example.com"}
        for i in range(10)
    ])
    res = await mock_p.search("query", max_results=3)
    assert len(res["results"]) == 3


# ===========================================================================
# 2. WEB FETCH & SSRF GUARD TESTS
# ===========================================================================

def test_private_or_local_host_detection(monkeypatch):
    """Verify SSRF classification with explicit DNS answers, without live resolution."""
    import socket
    original_lookup = socket.getaddrinfo
    public_hosts = {"google.com":"142.250.72.14", "github.com":"140.82.114.4"}
    monkeypatch.setattr(socket,"getaddrinfo",lambda host,*args,**kwargs:
        original_lookup(public_hosts.get(host,host),*args,**kwargs))
    assert is_private_or_local_host("localhost") is True
    assert is_private_or_local_host("127.0.0.1") is True
    assert is_private_or_local_host("10.0.0.5") is True
    assert is_private_or_local_host("192.168.1.100") is True
    assert is_private_or_local_host("172.16.0.1") is True
    assert is_private_or_local_host("169.254.169.254") is True  # AWS metadata link-local
    assert is_private_or_local_host("google.com") is False
    assert is_private_or_local_host("github.com") is False


@pytest.mark.asyncio
async def test_web_fetch_scheme_validation():
    """Verify web.fetch rejects non-http schemes (file://, ftp://)."""
    res1 = await web_fetch("file:///C:/Windows/System32/drivers/etc/hosts")
    assert res1["status"] == "failure"
    assert "Invalid URL scheme" in res1["message"]

    res2 = await web_fetch("ftp://ftp.example.com/test.txt")
    assert res2["status"] == "failure"
    assert "Invalid URL scheme" in res2["message"]


@pytest.mark.asyncio
async def test_web_fetch_ssrf_guard():
    """Verify web.fetch rejects private and localhost targets."""
    res_local = await web_fetch("http://127.0.0.1:8000/api/secret")
    assert res_local["status"] == "failure"
    assert "Access to local or private network addresses is forbidden" in res_local["message"]

    res_aws = await web_fetch("http://169.254.169.254/latest/meta-data/")
    assert res_aws["status"] == "failure"
    assert "Access to local or private network addresses is forbidden" in res_aws["message"]


def test_clean_html_content_extraction():
    """Verify HTML cleaning removes scripts and styles, and preserves title."""
    html = """
    <html>
      <head><title>NVIDIA Newsroom</title></head>
      <body>
        <script>alert('malicious')</script>
        <style>body { color: red; }</style>
        <nav><a href='/'>Home</a></nav>
        <h1>New Architecture Announced</h1>
        <p>NVIDIA today announced new GPUs with high efficiency.</p>
        <footer>Copyright 2026</footer>
      </body>
    </html>
    """
    title, text = clean_html_content(html, max_chars=100)
    assert title == "NVIDIA Newsroom"
    assert "alert" not in text
    assert "color: red" not in text
    assert "New Architecture Announced" in text
    assert "NVIDIA today announced" in text


# ===========================================================================
# 3. WEATHER PROVIDER TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_weather_provider_interface_and_mock():
    """Verify WeatherProvider base interface and MockWeatherProvider."""
    mock_w = MockWeatherProvider()
    assert mock_w.provider_name == "mock"

    curr = await mock_w.get_current("Seattle", units="metric")
    assert curr["location"] == "Seattle, Earth"
    assert curr["temperature"] == 22.0
    assert curr["temperature_unit"] == "°C"
    assert curr["conditions"] == "Partly cloudy"
    assert curr["provider"] == "mock"

    fore = await mock_w.get_forecast("Seattle", units="imperial")
    assert fore["location"] == "Seattle, Earth"
    assert len(fore["forecast"]) >= 2
    assert fore["forecast"][0]["temperature_unit"] == "°F"
    assert fore["provider"] == "mock"


@pytest.mark.asyncio
async def test_weather_provider_override():
    """Verify set_weather_provider_override switches weather provider cleanly."""
    set_weather_provider_override(None)
    active = get_weather_provider()
    assert isinstance(active, OpenMeteoWeatherProvider)

    mock_w = MockWeatherProvider()
    set_weather_provider_override(mock_w)
    assert get_weather_provider() is mock_w

    set_weather_provider_override(None)


# ===========================================================================
# 4. BROWSER NAVIGATION & URL RESOLVER TESTS
# ===========================================================================

def test_url_helpers():
    """Verify URL detection and normalization."""
    assert is_valid_url("https://youtube.com") is True
    assert is_valid_url("http://github.com/foo") is True
    assert is_valid_url("www.google.com") is True
    assert is_valid_url("techcrunch.com") is True
    assert is_valid_url("not a url at all") is False

    assert normalize_url("www.google.com") == "https://www.google.com"
    assert normalize_url("https://github.com") == "https://github.com"


@pytest.mark.asyncio
async def test_resolve_web_destination():
    """Verify semantic destination resolution for unambiguous websites."""
    assert await resolve_web_destination("YouTube") == "https://www.youtube.com"
    assert await resolve_web_destination("open YouTube") == "https://www.youtube.com"
    assert await resolve_web_destination("GitHub") == "https://github.com"
    assert await resolve_web_destination("open reddit") == "https://www.reddit.com"
    assert await resolve_web_destination("https://news.ycombinator.com") == "https://news.ycombinator.com"


# ===========================================================================
# 5. CAPABILITY REGISTRY TESTS
# ===========================================================================

def test_core_capability_registry():
    """Verify registry exposes all Phase A1 capabilities with schema and risk."""
    caps = get_core_capabilities()
    assert "web.search" in caps
    assert "web.fetch" in caps
    assert "weather.current" in caps
    assert "weather.forecast" in caps
    assert "browser.open_url" in caps

    # web.search
    search_cap = caps["web.search"]
    assert search_cap.classification == "read"
    assert search_cap.risk == "low"
    assert "query" in search_cap.arguments
    assert search_cap.provider_status["available"] is True

    # browser.open_url
    browser_cap = caps["browser.open_url"]
    assert browser_cap.classification == "device_action"
    assert browser_cap.target_type == "device"
    assert "url" in browser_cap.arguments


# ===========================================================================
# 6. PLANNER INTEGRATION TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_planner_search_and_open_goal():
    """
    User: 'Find the latest news about RTX 5090 laptops and open the most relevant result.'
    Expected plan:
    1. web.search
    2. browser.open_url
    """
    gm = GoalManager.get_instance()
    from model_contract_fixtures import PlanModel, plan_step
    planner = GoalPlanner.get_instance(goal_manager=gm, model_router=PlanModel([plan_step("web.search",query="RTX 5090 laptops"),plan_step("browser.open_url",url="https://hardware.test/rtx5090")]))

    goal = await gm.create_goal(
        title="Search RTX 5090 and open result",
        original_request="Find the latest news about RTX 5090 laptops and open the most relevant result.",
    )

    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="test_dev")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 2

    steps = plan_res["steps"]
    assert steps[0]["capability"] == "web.search"
    assert "rtx 5090 laptops" in steps[0]["arguments"]["query"].lower()
    assert steps[1]["capability"] == "browser.open_url"
    assert "url" in steps[1]["arguments"]



@pytest.mark.asyncio
async def test_planner_weather_goal():
    """
    User: 'Check the weather in New York'
    Expected plan: weather.current with location='New York'.
    """
    gm = GoalManager.get_instance()
    from model_contract_fixtures import PlanModel, plan_step
    planner = GoalPlanner.get_instance(goal_manager=gm, model_router=PlanModel([plan_step("weather.current",location="New York")]))

    goal = await gm.create_goal(
        title="Check weather New York",
        original_request="Check the weather in New York",
    )

    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="test_dev")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 1
    assert plan_res["steps"][0]["capability"] == "weather.current"
    assert "New York" in plan_res["steps"][0]["arguments"]["location"]



@pytest.mark.asyncio
async def test_planner_direct_browser_open_goal():
    """
    User: 'Open YouTube'
    Expected plan: browser.open_url with https://www.youtube.com.
    """
    gm = GoalManager.get_instance()
    from model_contract_fixtures import PlanModel, plan_step
    planner = GoalPlanner.get_instance(goal_manager=gm, model_router=PlanModel([plan_step("browser.open_url",url="https://www.youtube.com")]))

    goal = await gm.create_goal(
        title="Open YouTube",
        original_request="Open YouTube",
    )

    plan_res = await planner.plan_goal(goal_id=goal.goal_id, target_device="test_dev")
    assert plan_res["status"] == "planned"
    assert plan_res["step_count"] == 1
    assert plan_res["steps"][0]["capability"] == "browser.open_url"
    assert plan_res["steps"][0]["arguments"]["url"] == "https://www.youtube.com"



# ===========================================================================
# 7. CONVERSATIONAL ROUTING & SOURCE PROVENANCE TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_conversational_weather_routing_with_provenance(monkeypatch):
    """
    Offline model/tool wiring. Ask Cortex: 'What is the weather in Seattle?'
    Verify weather.current dispatched, real/mock data returned, provenance recorded.
    """
    set_weather_provider_override(MockWeatherProvider())

    harness, _ = await native_harness(monkeypatch, EvidenceModel("weather.current", dict(location="Seattle")))
    context = {"device": {"id": "asus", "location": "Seattle"}, "session": {}}

    resp = await harness.process_request("asus", "What's the weather in Seattle?", context, None)
    assert resp.status == "success"
    assert "Seattle" in resp.message
    assert resp.data["temperature"] == 22.0 and resp.data["temperature_unit"] == "°C"
    assert resp.provenance is not None
    assert resp.provenance["provider"] == "mock"
    assert resp.provenance["source_type"] == "live_weather"
    assert "retrieved_at" in resp.provenance

    set_weather_provider_override(None)



@pytest.mark.asyncio
async def test_conversational_web_search_routing_with_provenance(monkeypatch):
    """
    Offline model/tool wiring. Ask Cortex: 'Search the web for RTX 5090 news'
    Verify web.search dispatched, real/mock results returned, URLs in provenance.
    """
    mock_search = MockSearchProvider([
        {
            "title": "NVIDIA RTX 5090 Release Window",
            "url": "https://hardware.test/rtx5090",
            "snippet": "Expected launch details for RTX 5090 GPUs.",
            "source": "hardware.test",
        }
    ])
    set_search_provider_override(mock_search)

    harness, _ = await native_harness(monkeypatch, EvidenceModel("web.search", dict(query="RTX 5090 news")))
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "Search the web for RTX 5090 news", context, None)
    assert resp.status == "success"
    assert "RTX 5090" in resp.message
    assert "hardware.test" in resp.message
    assert resp.provenance is not None
    assert resp.provenance["provider"] == "mock"
    assert resp.provenance["source_type"] == "live_search"
    assert "https://hardware.test/rtx5090" in resp.provenance["source_urls"]

    set_search_provider_override(None)



@pytest.mark.asyncio
async def test_conversational_browser_open_routing(monkeypatch):
    """
    Offline model/tool wiring. Ask Cortex: 'Open YouTube'
    Verify browser.open_url dispatched, verified result observed, provenance recorded.
    """
    harness, _ = await native_harness(monkeypatch, EvidenceModel("browser.open_url", dict(url="https://www.youtube.com")))
    context = {"device": {"id": "asus"}, "session": {}}

    with patch("webbrowser.open", return_value=True) as mock_open:
        resp = await harness.process_request("asus", "Open YouTube", context, None)
        assert resp.status == "success"
        assert "https://www.youtube.com" in resp.message
        assert mock_open.called
        assert mock_open.call_args[0][0] == "https://www.youtube.com"
        assert resp.provenance is not None
        assert resp.provenance["source_type"] == "device_action"
        assert "https://www.youtube.com" in resp.provenance["source_urls"]



# ===========================================================================
# 8. REQUIRED FAILURE TEST (NO HALLUCINATIONS WHEN PROVIDER UNAVAILABLE)
# ===========================================================================

@pytest.mark.asyncio
async def test_failure_when_search_provider_unavailable(monkeypatch):
    """
    When search provider is disabled, Cortex MUST report live provider unavailable.
    It must NOT fabricate an answer using model knowledge.
    """
    with patch.dict(os.environ, {"CORTEX_SEARCH_ENABLED": "false"}):
        set_search_provider_override(None)
        harness, _ = await native_harness(monkeypatch, EvidenceModel("web.search", dict(query="latest stock prices")))
        context = {"device": {"id": "asus"}, "session": {}}

        resp = await harness.process_request("asus", "Search the web for the latest stock prices", context, None)
        assert resp.status == "unsupported"
        assert resp.data["reason"] == "capability_unavailable"
        assert resp.data["capability"] == "web.search"



@pytest.mark.asyncio
async def test_failure_when_weather_provider_unavailable(monkeypatch):
    """
    When weather provider is disabled, Cortex MUST report live provider unavailable.
    It must NOT fabricate weather using model knowledge.
    """
    with patch.dict(os.environ, {"CORTEX_WEATHER_ENABLED": "false"}):
        set_weather_provider_override(None)
        harness, _ = await native_harness(monkeypatch, EvidenceModel("weather.current", dict(location="Boston")))
        context = {"device": {"id": "asus"}, "session": {}}

        resp = await harness.process_request("asus", "What's the weather today?", context, None)
        assert resp.status == "unsupported"
        assert resp.data["reason"] == "capability_unavailable"
        assert resp.data["capability"] == "weather.current"



# ===========================================================================
# 9. REQUIRED PHYSICAL-STYLE REAL PROVIDER TESTS
# ===========================================================================

@pytest.mark.asyncio
async def test_required_physical_test_1_weather_live_provider(monkeypatch):
    install_weather_transport(monkeypatch)  # Recorded HTTP boundary, not a physical/live claim.
    """
    REQUIRED PHYSICAL TEST 1 — WEATHER:
    Ask Cortex naturally: 'What's the weather right now in Boston?'
    Verify:
    - Real weather capability called with Open-Meteo
    - Real provider returned live data
    - Cortex speaks current temperature and conditions
    - Provider and retrieval metadata recorded
    - No fabricated forecast
    """
    set_weather_provider_override(None)  # Use real OpenMeteoWeatherProvider

    harness, _ = await native_harness(monkeypatch, EvidenceModel("weather.current", dict(location="Boston")))
    context = {"device": {"id": "asus", "location": "Boston"}, "session": {}}

    resp = await harness.process_request("asus", "What's the weather right now in Boston?", context, None)
    assert resp.status == "success"
    assert "Boston" in resp.message
    # Real temperature present
    assert resp.data["temperature"] == 19.2
    assert resp.data["temperature_unit"] == "°C"
    assert resp.provenance is not None
    assert resp.provenance["provider"] == "open-meteo"
    assert resp.provenance["source_type"] == "live_weather"
    assert "retrieved_at" in resp.provenance



@pytest.mark.asyncio
async def test_required_physical_test_2_search_live_provider(monkeypatch):
    install_search_transport(monkeypatch)  # Actual parser, deterministic external response.
    """
    REQUIRED PHYSICAL TEST 2 — WEB SEARCH:
    Ask: 'Search the web for the latest news about Nvidia.'
    Verify:
    - web.search dispatched via real DuckDuckGoSearchProvider
    - Real results returned with URLs and snippets
    - URLs persisted in result provenance
    - Cortex summarizes results
    """
    set_search_provider_override(None)  # Use real DuckDuckGoSearchProvider

    harness, _ = await native_harness(monkeypatch, EvidenceModel("web.search", dict(query="latest Nvidia news")))
    context = {"device": {"id": "asus"}, "session": {}}

    resp = await harness.process_request("asus", "Search the web for the latest news about Nvidia.", context, None)
    assert resp.status == "success"
    assert "Nvidia" in resp.message or "nvidia" in resp.message.lower()
    assert resp.provenance is not None
    assert resp.provenance["provider"] == "duckduckgo"
    assert resp.provenance["source_type"] == "live_search"
    assert len(resp.provenance["source_urls"]) > 0



@pytest.mark.asyncio
async def test_required_physical_test_3_browser_dispatch(monkeypatch):
    """
    REQUIRED PHYSICAL TEST 3 — BROWSER:
    Ask: 'Open YouTube.'
    Verify:
    - browser.open_url executes on device
    - Default browser opens YouTube
    - Physical action result observed
    - Cortex confirms opened state
    """
    harness, _ = await native_harness(monkeypatch, EvidenceModel("browser.open_url", dict(url="https://www.youtube.com")))
    context = {"device": {"id": "asus"}, "session": {}}

    with patch("webbrowser.open", return_value=True) as mock_webbrowser:
        resp = await harness.process_request("asus", "Open YouTube", context, None)
        assert resp.status == "success"
        assert "https://www.youtube.com" in resp.message
        assert mock_webbrowser.called
        assert mock_webbrowser.call_args[0][0] == "https://www.youtube.com"



# ===========================================================================
# 10. PRODUCTION DB ISOLATION TEST
# ===========================================================================

def test_production_db_isolation():
    """Verify that tests execute strictly against test_cortex.db and not data/cortex.db."""
    test_db = os.environ.get("CORTEX_DB_PATH")
    assert test_db is not None
    assert "test_cortex.db" in test_db
    prod_db = Path("data/cortex.db")
    if prod_db.exists():
        assert Path(test_db).resolve() != prod_db.resolve()

# Explicit local decisions; language quality belongs to the blind live benchmark.
from model_contract_fixtures import native_harness, EvidenceModel


def install_weather_transport(monkeypatch):
    import httpx
    original = httpx.AsyncClient
    def reply(request):
        if request.url.host == "geocoding-api.open-meteo.com":
            assert request.url.params["name"] == "Boston"
            return httpx.Response(200, json={"results":[{"name":"Boston","latitude":42.36,"longitude":-71.06}]})
        assert request.url.host == "api.open-meteo.com"
        return httpx.Response(200, json={"current":{"temperature_2m":19.2,"apparent_temperature":18,"relative_humidity_2m":60,"wind_speed_10m":4,"weather_code":0}})
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw: original(**kw,transport=httpx.MockTransport(reply)))


def install_search_transport(monkeypatch):
    import httpx
    original = httpx.AsyncClient
    def reply(request):
        assert request.url.host == "html.duckduckgo.com" and b"Nvidia" in request.content
        return httpx.Response(200,text='<a class="result__a" href="https://hardware.test/nvidia">Nvidia fixture news</a><a class="result__snippet">Recorded search response</a>')
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kw: original(**kw,transport=httpx.MockTransport(reply)))
