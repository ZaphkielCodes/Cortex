"""
hub/weather_provider.py
-----------------------
Weather Provider interface and implementations for Cortex (Phase A1).

Supports:
- Open-Meteo API (free, open, no API key required)
- Geocoding + Current Weather + Multi-day Forecast
- Unit conversion (metric vs imperial)
- Mock provider for testing
- Provider unavailable detection (NEVER hallucinate weather when unavailable)
"""

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import logging

log = logging.getLogger("cortex.weather_provider")

WMO_WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def get_wmo_condition(code: Optional[int]) -> str:
    if code is None:
        return "Unknown"
    return WMO_WEATHER_CODES.get(code, f"Weather code {code}")


class WeatherProvider(ABC):
    """Abstract base class for Cortex weather providers."""

    @abstractmethod
    async def get_current(self, location: str, units: str = "metric") -> Dict[str, Any]:
        """
        Get current weather conditions for a location.
        """
        pass

    @abstractmethod
    async def get_forecast(self, location: str, units: str = "metric") -> Dict[str, Any]:
        """
        Get multi-day weather forecast for a location.
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the weather provider."""
        pass


class OpenMeteoWeatherProvider(WeatherProvider):
    """
    Real weather provider using Open-Meteo Geocoding and Weather APIs.
    Requires no API keys.
    """

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    @property
    def provider_name(self) -> str:
        return "open-meteo"

    async def _geocode(self, location: str) -> Dict[str, Any]:
        """Resolve a city or place name to latitude, longitude, and formatted name."""
        import httpx

        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": location.strip(), "count": 1, "language": "en", "format": "json"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(geo_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results")
        if not results:
            raise ValueError(f"Could not find coordinates for location '{location}'.")

        res = results[0]
        name = res.get("name", location)
        admin1 = res.get("admin1", "")
        country = res.get("country", "")

        parts = [p for p in (name, admin1, country) if p]
        display_name = ", ".join(parts)

        return {
            "latitude": res["latitude"],
            "longitude": res["longitude"],
            "display_name": display_name,
            "timezone": res.get("timezone", "auto"),
        }

    async def get_current(self, location: str, units: str = "metric") -> Dict[str, Any]:
        import httpx

        geo = await self._geocode(location)
        weather_url = "https://api.open-meteo.com/v1/forecast"

        temp_unit = "fahrenheit" if units.lower() == "imperial" else "celsius"
        wind_unit = "mph" if units.lower() == "imperial" else "kmh"

        params = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "temperature_unit": temp_unit,
            "wind_speed_unit": wind_unit,
            "timezone": "auto",
        }

        now_iso = datetime.now(timezone.utc).isoformat()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(weather_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m")
        feels_like = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind_speed = current.get("wind_speed_10m")
        w_code = current.get("weather_code")
        conditions = get_wmo_condition(w_code)

        temp_symbol = "°F" if units.lower() == "imperial" else "°C"
        wind_str = f"{wind_speed} {wind_unit}" if wind_speed is not None else None

        return {
            "location": geo["display_name"],
            "temperature": temp,
            "temperature_unit": temp_symbol,
            "feels_like": feels_like,
            "conditions": conditions,
            "humidity": humidity,
            "wind": wind_str,
            "units": units.lower(),
            "observed_at": now_iso,
            "provider": self.provider_name,
        }

    async def get_forecast(self, location: str, units: str = "metric") -> Dict[str, Any]:
        import httpx

        geo = await self._geocode(location)
        weather_url = "https://api.open-meteo.com/v1/forecast"

        temp_unit = "fahrenheit" if units.lower() == "imperial" else "celsius"
        wind_unit = "mph" if units.lower() == "imperial" else "kmh"

        params = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": temp_unit,
            "wind_speed_unit": wind_unit,
            "timezone": "auto",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(weather_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily", {})
        times = daily.get("time", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        precips = daily.get("precipitation_probability_max", [])
        codes = daily.get("weather_code", [])

        temp_symbol = "°F" if units.lower() == "imperial" else "°C"
        forecast_days = []
        for i in range(len(times)):
            forecast_days.append({
                "date": times[i],
                "high": max_temps[i] if i < len(max_temps) else None,
                "low": min_temps[i] if i < len(min_temps) else None,
                "conditions": get_wmo_condition(codes[i] if i < len(codes) else None),
                "precipitation_probability": precips[i] if i < len(precips) else None,
                "temperature_unit": temp_symbol,
            })

        return {
            "location": geo["display_name"],
            "forecast": forecast_days,
            "units": units.lower(),
            "provider": self.provider_name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }


class MockWeatherProvider(WeatherProvider):
    """Mock weather provider for deterministic testing."""

    def __init__(self, custom_data: Optional[Dict[str, Any]] = None):
        self._custom_data = custom_data

    @property
    def provider_name(self) -> str:
        return "mock"

    async def get_current(self, location: str, units: str = "metric") -> Dict[str, Any]:
        temp = 72.0 if units.lower() == "imperial" else 22.0
        temp_sym = "°F" if units.lower() == "imperial" else "°C"
        return {
            "location": f"{location}, Earth",
            "temperature": temp,
            "temperature_unit": temp_sym,
            "feels_like": temp + 1.0,
            "conditions": "Partly cloudy",
            "humidity": 55,
            "wind": f"8 {'mph' if units.lower() == 'imperial' else 'kmh'}",
            "units": units.lower(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "provider": self.provider_name,
        }

    async def get_forecast(self, location: str, units: str = "metric") -> Dict[str, Any]:
        temp_sym = "°F" if units.lower() == "imperial" else "°C"
        high = 75.0 if units.lower() == "imperial" else 24.0
        low = 60.0 if units.lower() == "imperial" else 15.0
        return {
            "location": f"{location}, Earth",
            "forecast": [
                {
                    "date": "2026-10-13",
                    "high": high,
                    "low": low,
                    "conditions": "Sunny",
                    "precipitation_probability": 10,
                    "temperature_unit": temp_sym,
                },
                {
                    "date": "2026-10-14",
                    "high": high + 2,
                    "low": low + 1,
                    "conditions": "Partly cloudy",
                    "precipitation_probability": 20,
                    "temperature_unit": temp_sym,
                },
            ],
            "units": units.lower(),
            "provider": self.provider_name,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }


_WEATHER_PROVIDER_OVERRIDE: Optional[WeatherProvider] = None


def set_weather_provider_override(provider: Optional[WeatherProvider]) -> None:
    """Explicitly set or reset the weather provider override (for testing)."""
    global _WEATHER_PROVIDER_OVERRIDE
    _WEATHER_PROVIDER_OVERRIDE = provider


def get_weather_provider() -> Optional[WeatherProvider]:
    """
    Resolve the active weather provider according to environment configuration.
    Returns None if disabled or no provider is configured.
    """
    global _WEATHER_PROVIDER_OVERRIDE
    if _WEATHER_PROVIDER_OVERRIDE is not None:
        return _WEATHER_PROVIDER_OVERRIDE

    enabled_env = os.environ.get("CORTEX_WEATHER_ENABLED", "true").lower()
    if enabled_env in ("false", "0", "no"):
        return None

    provider_type = os.environ.get("CORTEX_WEATHER_PROVIDER", "open-meteo").lower().strip()

    if provider_type == "mock":
        return MockWeatherProvider()
    elif provider_type in ("open-meteo", "openmeteo"):
        return OpenMeteoWeatherProvider()
    elif provider_type in ("none", "disabled"):
        return None
    else:
        log.warning(f"Unknown CORTEX_WEATHER_PROVIDER: '{provider_type}'")
        return None
