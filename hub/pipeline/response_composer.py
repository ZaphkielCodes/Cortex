"""
hub/pipeline/response_composer.py
---------------------------------
Composes user-facing ConversationalResponse objects (Section 1).
"""

from typing import Any, Dict, Optional

from ..models import ConversationalResponse
from .semantic_interpreter import SemanticIntent


class ResponseComposer:
    """
    Composes structured user-facing responses from execution results and semantic intents.
    """

    @classmethod
    def compose(
        cls,
        intent: SemanticIntent,
        raw_result: Dict[str, Any],
        grounded_target: Optional[Any] = None,
    ) -> ConversationalResponse:
        raw_st = raw_result.get("status", "failure")
        status = "error" if raw_st == "failure" else raw_st
        action = intent.candidate_capabilities[0] if intent.candidate_capabilities else None

        if raw_st != "success":
            return ConversationalResponse(status=status,
                message=raw_result.get("message") or "Completion could not be verified.",
                data=raw_result.get("data"), command_id=raw_result.get("command_id"))

        # 1. Process Telemetry Formatting ("What's taking most of the memory...?")
        if action == "process.list":
            procs = raw_result.get("processes") or raw_result.get("data") or []
            if procs:
                top = procs[0]
                p_name = top.get("process_name", "unknown")
                mem_bytes = top.get("memory_bytes", 0)
                mem_pct = top.get("memory_percent", 0.0)
                mem_gb = mem_bytes / (1024 ** 3)
                mem_mb = mem_bytes / (1024 ** 2)

                mem_display = f"{mem_gb:.2f} GB" if mem_gb >= 1.0 else f"{mem_mb:.0f} MB"
                msg = f"The top process consuming memory right now is '{p_name}', using {mem_display} ({mem_pct}% of RAM)."
                return ConversationalResponse(
                    status="success",
                    message=msg,
                    data={"processes": procs, "top_process": top},
                )
            return ConversationalResponse(
                status="success",
                message="No active running processes were found.",
            )

        # 2. Window Switch Formatting ("Switch apps.")
        if action == "window.focus":
            target = intent.arguments.get("title") or intent.arguments.get("app") or (grounded_target.target_name if grounded_target else "window")
            if status == "success":
                msg = raw_result.get("message") or f"Switched focus to {target}."
                return ConversationalResponse(
                    status="success",
                    message=msg,
                )
            return ConversationalResponse(
                status="error",
                message=f"Could not switch to {target}: {raw_result.get('message', 'Window not found.')}",
            )

        # 3. Application Close Formatting
        if action == "app.close":
            target = intent.arguments.get("app") or (grounded_target.target_name if grounded_target else "application")
            if status == "clarification_needed":
                return ConversationalResponse(
                    status="clarification_needed",
                    message=raw_result.get("message", f"Multiple windows found for {target}."),
                    data=raw_result.get("data") or raw_result,
                )
            if status == "success":
                msg = raw_result.get("message") or f"Closed {target}."
                return ConversationalResponse(
                    status="success",
                    message=msg,
                )
            return ConversationalResponse(
                status="error",
                message=f"Could not close {target}: {raw_result.get('message', 'Failed to terminate process.')}",
            )

        # 4. Application Launch Formatting
        if action == "app.launch":
            target = intent.arguments.get("app") or (grounded_target.target_name if grounded_target else "application")
            if status == "success":
                msg = raw_result.get("message") or f"Opened {target}."
                return ConversationalResponse(
                    status="success",
                    message=msg,
                )
            return ConversationalResponse(
                status="error",
                message=f"Could not open {target}: {raw_result.get('message', 'Application failed to start.')}",
            )

        # 4b. File Operations Formatting
        if action and action.startswith("fs."):
            if status == "success":
                msg = raw_result.get("message") or "File operation completed successfully."
                return ConversationalResponse(status="success", message=msg, data=raw_result.get("data") or raw_result)
            return ConversationalResponse(status="error", message=raw_result.get("message", "File operation failed."))

        # 5. Device Status / Aggregate Telemetry
        if action == "device.status":
            telem = raw_result.get("data") or raw_result
            ram_pct = telem.get("ram_percent")
            if ram_pct is not None:
                msg = f"RAM usage is currently at {ram_pct}%."
                return ConversationalResponse(status="success", message=msg, data=telem, provenance=raw_result.get("provenance"))

        # 6. Web Search
        if action == "web.search":
            data = raw_result.get("data") or {}
            results = data.get("results") or []
            clean_query = intent.arguments.get("query", "")
            if results:
                top_items = []
                for r in results[:3]:
                    t = r.get("title", "")
                    snip = r.get("snippet", "")
                    u = r.get("url", "")
                    top_items.append(f"• {t}: {snip} ({u})")
                summary_body = "\n".join(top_items)
                msg = f"Here is what I found for '{clean_query}':\n{summary_body}"
            else:
                msg = f"I searched the web for '{clean_query}', but found no results."

            source_urls = [r["url"] for r in results if r.get("url")]
            from datetime import datetime, timezone
            prov = raw_result.get("provenance") or data.get("provenance") or {
                "provider": data.get("provider", "duckduckgo"),
                "source_urls": source_urls,
                "retrieved_at": data.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
                "source_type": "live_search",
            }
            return ConversationalResponse(
                status="success" if status == "success" else "error",
                message=msg,
                data=data,
                provenance=prov,
            )

        # 7. Weather
        if action and action.startswith("weather."):
            data = raw_result.get("data") or {}
            loc_name = data.get("location") or intent.arguments.get("location", "current location")
            temp = data.get("temperature")
            unit = data.get("temperature_unit", "°C")
            cond = data.get("conditions")
            feels = data.get("feels_like")
            humidity = data.get("humidity")
            # Truthful failure: never compose "None°C"
            if temp is None or cond is None:
                return ConversationalResponse(
                    status="error",
                    message=f"I couldn't get complete weather data for {loc_name}.",
                )
            msg = f"It's currently {temp}{unit} and {cond} in {loc_name}."
            if feels is not None:
                msg += f" Feels like {feels}{unit}."
            if humidity is not None:
                msg += f" Humidity is {humidity}%."
            from datetime import datetime, timezone
            prov = raw_result.get("provenance") or data.get("provenance") or {
                "provider": data.get("provider", "openmeteo"),
                "retrieved_at": data.get("observed_at") or datetime.now(timezone.utc).isoformat(),
                "source_type": "live_weather",
            }
            return ConversationalResponse(
                status="success" if status == "success" else "error",
                message=msg,
                data=data,
                provenance=prov,
            )

        # 8. Browser Open URL
        if action == "browser.open_url":
            data = raw_result.get("data") or {}
            target_url = intent.arguments.get("url") or data.get("url") or ""
            from datetime import datetime, timezone
            prov = raw_result.get("provenance") or data.get("provenance") or {
                "provider": "system_browser",
                "source_urls": [target_url] if target_url else [],
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source_type": "device_action",
            }
            msg = raw_result.get("message") or f"Opened {target_url} in your default browser."
            return ConversationalResponse(
                status="success" if status == "success" else "error",
                message=msg,
                data=data,
                provenance=prov,
            )

        # 9. Conversational or General Result
        msg = raw_result.get("message") or intent.response_text or "Done."
        return ConversationalResponse(
            status=status,
            message=msg,
            data=raw_result.get("data"),
            provenance=raw_result.get("provenance"),
        )
