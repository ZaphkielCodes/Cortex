"""
hub/database.py
---------------
Async SQLite interface using aiosqlite (Phase 3).

Tables
------
action_logs
  id            INTEGER PRIMARY KEY AUTOINCREMENT
  timestamp     TEXT    — ISO-8601 UTC
  device_id     TEXT    — which agent sent the result
  command_id    TEXT    — unique ID correlating command ↔ result
  command       TEXT    — the command string (e.g. "take_screenshot")
  result        TEXT    — "success" or "failure"
  message       TEXT    — detail returned by the agent

devices
  device_id     TEXT PRIMARY KEY
  device_name   TEXT    — friendly name (e.g. "ASUS Laptop")
  role          TEXT    — "admin" or "sub_admin"
  secret_hash   TEXT    — bcrypt hash of the device's pre-shared secret
  enrolled_at   TEXT    — ISO-8601 UTC
  last_seen     TEXT    — ISO-8601 UTC (updated on every heartbeat)
  connection_state TEXT — "connected" or "disconnected" (Phase 3)

pending_confirmations
  command_id    TEXT PRIMARY KEY
  target_device TEXT    — device the command would be sent to
  command       TEXT
  args          TEXT    — JSON-encoded list
  requested_by  TEXT    — who initiated the command
  tier          INTEGER
  status        TEXT    — pending / approved / denied / expired
  requested_at  TEXT    — ISO-8601 UTC
  resolved_at   TEXT
  resolved_by   TEXT

command_history  (Phase 3)
  command_id    TEXT PRIMARY KEY
  command       TEXT    — the command string
  target_device TEXT    — device the command targets
  requested_by  TEXT    — who initiated the command
  received_at   TEXT    — ISO-8601 UTC, set by the hub (never client-supplied)
  status        TEXT    — pending / dispatched / expired / duplicate
  dispatched_at TEXT    — ISO-8601 UTC
  expired_at    TEXT    — ISO-8601 UTC
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Any, Dict, List, Union, Callable


import aiosqlite
from contextlib import asynccontextmanager

_shared_conn: aiosqlite.Connection | None = None

async def open_db() -> None:
    global _shared_conn
    if not _shared_conn:
        _shared_conn = await aiosqlite.connect(get_db_path())
        await _shared_conn.execute("PRAGMA foreign_keys = ON;")
        await _shared_conn.execute("PRAGMA journal_mode = WAL;")
        await _shared_conn.execute("PRAGMA busy_timeout = 5000;")
        await _shared_conn.execute("PRAGMA synchronous = NORMAL;")

async def close_db() -> None:
    global _shared_conn
    if _shared_conn:
        await _shared_conn.close()
        _shared_conn = None

@asynccontextmanager
async def get_db():
    if not _shared_conn:
        await open_db()
    yield _shared_conn


import os

# SQLite file lives in the hub/ directory alongside main.py
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
def get_db_path() -> Path:
    return Path(os.getenv("CORTEX_DB_PATH", DATA_DIR / "cortex.db"))


def reset_test_db() -> None:
    """Helper for tests to wipe the database safely. Refuses to wipe production DB."""
    path_str = str(get_db_path()).replace('\\', '/')
    prod_path = (DATA_DIR / "cortex.db").resolve()
    current_path = get_db_path().resolve()
    if path_str.endswith("data/cortex.db") or get_db_path().name == "cortex.db" or current_path == prod_path:
        raise RuntimeError(f"FATAL SAFETY GUARD: Attempted to delete or mutate production DB at {get_db_path()}!")
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(get_db_path()) + suffix)
        if target.exists():
            try:
                target.unlink()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables if they don't exist yet, then run migrations."""
    import logging
    log = logging.getLogger("cortex.hub.db")
    log.info(f"Using production database path: {get_db_path()}")
    async with get_db() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS action_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT    NOT NULL,
                device_id  TEXT    NOT NULL,
                command_id TEXT    NOT NULL DEFAULT '',
                command    TEXT    NOT NULL,
                result     TEXT    NOT NULL,
                message    TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id   TEXT PRIMARY KEY,
                device_name TEXT    NOT NULL DEFAULT '',
                role        TEXT    NOT NULL DEFAULT 'standard',
                secret_hash TEXT    NOT NULL,
                enrolled_at TEXT    NOT NULL,
                last_seen   TEXT    NOT NULL DEFAULT '',
                connection_state TEXT NOT NULL DEFAULT 'disconnected',
                platform    TEXT    NOT NULL DEFAULT 'unknown',
                device_type TEXT    NOT NULL DEFAULT 'unknown',
                trust_level TEXT    NOT NULL DEFAULT 'standard',
                agent_version TEXT  NOT NULL DEFAULT '',
                last_heartbeat TEXT NOT NULL DEFAULT '',
                transports  TEXT    NOT NULL DEFAULT '{}',
                resource_profile TEXT NOT NULL DEFAULT '{}',
                workload    TEXT    NOT NULL DEFAULT '{}',
                metadata    TEXT    NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_confirmations (
                command_id    TEXT PRIMARY KEY,
                target_device TEXT    NOT NULL,
                command       TEXT    NOT NULL,
                args          TEXT    NOT NULL DEFAULT '[]',
                requested_by  TEXT    NOT NULL,
                tier          INTEGER NOT NULL DEFAULT 2,
                status        TEXT    NOT NULL DEFAULT 'pending',
                requested_at  TEXT    NOT NULL,
                resolved_at   TEXT    NOT NULL DEFAULT '',
                resolved_by   TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        # Phase 3: command_history for duplicate protection + expiry
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS command_history (
                command_id    TEXT PRIMARY KEY,
                command       TEXT    NOT NULL,
                target_device TEXT    NOT NULL,
                requested_by  TEXT    NOT NULL,
                received_at   TEXT    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'pending',
                dispatched_at TEXT    NOT NULL DEFAULT '',
                expired_at    TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        # Phase 4: Device capabilities and permissions
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_capabilities (
                device_id   TEXT    NOT NULL,
                capability  TEXT    NOT NULL,
                enabled     BOOLEAN NOT NULL DEFAULT 1,
                metadata    TEXT    NOT NULL DEFAULT '{}',
                PRIMARY KEY (device_id, capability)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_permissions (
                device_id   TEXT    NOT NULL,
                capability  TEXT    NOT NULL,
                decision    TEXT    NOT NULL,
                PRIMARY KEY (device_id, capability)
            )
            """
        )
        # Phase 5: Persistent memory, tasks, and integrations
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                id         TEXT PRIMARY KEY,
                category   TEXT    NOT NULL,
                key        TEXT    NOT NULL,
                value      TEXT    NOT NULL,
                source     TEXT    NOT NULL DEFAULT 'user',
                confidence REAL    NOT NULL DEFAULT 1.0,
                last_accessed TEXT NOT NULL DEFAULT '',
                relationship_entity_id TEXT NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id               TEXT PRIMARY KEY,
                title            TEXT    NOT NULL,
                type             TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'pending',
                trigger_time     TEXT    NOT NULL DEFAULT '',
                cron_expression  TEXT    NOT NULL DEFAULT '',
                timezone         TEXT    NOT NULL DEFAULT 'UTC',
                assigned_device  TEXT    NOT NULL DEFAULT '',
                command          TEXT    NOT NULL,
                args             TEXT    NOT NULL DEFAULT '[]',
                required_capability TEXT NOT NULL DEFAULT '',
                reroutable       BOOLEAN NOT NULL DEFAULT 0,
                retry_count      INTEGER NOT NULL DEFAULT 0,
                max_retries      INTEGER NOT NULL DEFAULT 0,
                last_error       TEXT    NOT NULL DEFAULT '',
                execution_id     TEXT    NOT NULL DEFAULT '',
                context          TEXT    NOT NULL DEFAULT '{}',
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS integrations (
                id           TEXT PRIMARY KEY,
                plugin_name  TEXT    NOT NULL,
                account_id   TEXT    NOT NULL DEFAULT '',
                config       TEXT    NOT NULL DEFAULT '{}',
                status       TEXT    NOT NULL DEFAULT 'active',
                created_at   TEXT    NOT NULL
            )
            """
        )
        # Phase 6: Browser session security
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                device_id  TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                expires_at TEXT    NOT NULL
            )
            """
        )
        # Phase 6: Continuity Sessions
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS continuity_sessions (
                id                    TEXT PRIMARY KEY,
                active_task           TEXT    NOT NULL DEFAULT '',
                active_project        TEXT    NOT NULL DEFAULT '',
                active_file           TEXT    NOT NULL DEFAULT '',
                source_device         TEXT    NOT NULL,
                current_device        TEXT    NOT NULL,
                required_capabilities TEXT    NOT NULL DEFAULT '[]',
                last_known_state      TEXT    NOT NULL DEFAULT '{}',
                continuation_metadata TEXT    NOT NULL DEFAULT '{}',
                updated_at            TEXT    NOT NULL
            )
            """
        )
        # Phase 7: Session Context
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS session_context (
                device_id   TEXT PRIMARY KEY,
                turns       TEXT NOT NULL DEFAULT '[]',
                active_references  TEXT NOT NULL DEFAULT '{}',
                active_task TEXT,
                updated_at  TEXT NOT NULL
            )
            """
        )
        # Phase 7A: People and Relationships
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                person_id           TEXT PRIMARY KEY,
                display_name        TEXT NOT NULL,
                aliases             TEXT NOT NULL DEFAULT '[]',
                relationship_type   TEXT NOT NULL DEFAULT '',
                notes               TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                last_interaction_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS person_facts (
                id          TEXT PRIMARY KEY,
                person_id   TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT NOT NULL,
                confidence  REAL NOT NULL DEFAULT 1.0,
                source      TEXT NOT NULL DEFAULT 'user',
                timestamp   TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS relationships (
                id                  TEXT PRIMARY KEY,
                source_person_id    TEXT NOT NULL,
                target_person_id    TEXT NOT NULL,
                relationship_type   TEXT NOT NULL,
                metadata            TEXT NOT NULL DEFAULT '{}',
                created_at          TEXT NOT NULL
            )
            """
        )
        # Phase 7B: Speaker Profiles
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS speaker_profiles (
                speaker_id       TEXT PRIMARY KEY,
                person_id        TEXT,
                label            TEXT NOT NULL,
                is_primary_user  BOOLEAN NOT NULL DEFAULT 0,
                confidence       REAL NOT NULL DEFAULT 1.0,
                metadata         TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )
            """
        )
        # Phase 8A: World State Signals & Context Events
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS world_state_signals (
                signal_key   TEXT PRIMARY KEY,
                source       TEXT NOT NULL,
                timestamp    TEXT NOT NULL,
                confidence   REAL NOT NULL DEFAULT 1.0,
                ttl          REAL NOT NULL DEFAULT 300.0,
                payload      TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS context_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                source      TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                summary     TEXT NOT NULL,
                details     TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        # Phase 8B: Proactive Conditions and Events
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS proactive_conditions (
                condition_id        TEXT PRIMARY KEY,
                condition_type      TEXT NOT NULL DEFAULT 'signal',
                signal_key          TEXT NOT NULL DEFAULT '',
                operator            TEXT NOT NULL DEFAULT 'equals',
                target_value        TEXT NOT NULL DEFAULT '',
                clauses             TEXT NOT NULL DEFAULT '[]',
                combination         TEXT NOT NULL DEFAULT 'AND',
                description         TEXT NOT NULL DEFAULT '',
                related_task_id     TEXT,
                related_goal_id     TEXT,
                metadata            TEXT NOT NULL DEFAULT '{}',
                current_truth_state INTEGER NOT NULL DEFAULT 0,
                last_evaluated_at   TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL,
                expires_at          TEXT,
                is_active           INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS proactive_events (
                event_id            TEXT PRIMARY KEY,
                event_type          TEXT NOT NULL,
                source              TEXT NOT NULL DEFAULT 'proactive_engine',
                related_task_id     TEXT,
                related_goal_id     TEXT,
                condition_id        TEXT NOT NULL,
                trigger_condition   TEXT NOT NULL DEFAULT '{}',
                context_snapshot    TEXT NOT NULL DEFAULT '{}',
                priority            REAL NOT NULL DEFAULT 0.5,
                confidence          REAL NOT NULL DEFAULT 1.0,
                created_at          TEXT NOT NULL,
                expires_at          TEXT,
                status              TEXT NOT NULL DEFAULT 'candidate'
            )
            """
        )
        # Phase 8C: Delivery Decisions
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_decisions (
                decision_id         TEXT PRIMARY KEY,
                proactive_event_id  TEXT NOT NULL,
                target_device_id    TEXT,
                channel             TEXT NOT NULL DEFAULT 'desktop_notification',
                timing              TEXT NOT NULL DEFAULT 'now',
                reason              TEXT NOT NULL DEFAULT '',
                confidence          REAL NOT NULL DEFAULT 1.0,
                score               REAL NOT NULL DEFAULT 0.5,
                created_at          TEXT NOT NULL,
                deliver_after       TEXT,
                expires_at          TEXT,
                status              TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        # Phase 8E: Work Items (Academic + Workload Context)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS work_items (
                work_item_id               TEXT PRIMARY KEY,
                title                      TEXT NOT NULL,
                type                       TEXT NOT NULL DEFAULT 'assignment',
                course_or_project          TEXT NOT NULL DEFAULT '',
                source                     TEXT NOT NULL DEFAULT 'user',
                due_at                     TEXT NOT NULL DEFAULT '',
                estimated_minutes          INTEGER NOT NULL DEFAULT 0,
                remaining_minutes          INTEGER NOT NULL DEFAULT 0,
                priority                   INTEGER NOT NULL DEFAULT 3,
                status                     TEXT NOT NULL DEFAULT 'pending',
                confidence                 REAL NOT NULL DEFAULT 1.0,
                notes                      TEXT NOT NULL DEFAULT '',
                created_at                 TEXT NOT NULL,
                updated_at                 TEXT NOT NULL,
                related_task_id            TEXT NOT NULL DEFAULT '',
                related_calendar_event_id  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Phase 8F: Learned Routines
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_routines (
                routine_id          TEXT PRIMARY KEY,
                label               TEXT NOT NULL,
                category            TEXT NOT NULL,
                days_of_week        TEXT NOT NULL,
                typical_start_time  TEXT NOT NULL,
                typical_end_time    TEXT NOT NULL,
                confidence          REAL NOT NULL,
                observation_count   INTEGER NOT NULL,
                source              TEXT NOT NULL,
                last_observed_at    TEXT NOT NULL,
                metadata            TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        # Phase 9A: Persistent Goals and Goal Steps
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS goals (
                goal_id             TEXT PRIMARY KEY,
                title               TEXT NOT NULL,
                original_request    TEXT NOT NULL,
                source              TEXT NOT NULL DEFAULT 'user',
                status              TEXT NOT NULL DEFAULT 'PENDING',
                priority            INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                started_at          TEXT,
                completed_at        TEXT,
                current_step_index  INTEGER NOT NULL DEFAULT 0,
                max_steps           INTEGER NOT NULL DEFAULT 20,
                failure_count       INTEGER NOT NULL DEFAULT 0,
                max_failures        INTEGER NOT NULL DEFAULT 3,
                max_runtime_seconds INTEGER DEFAULT 3600,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                max_consecutive_failures INTEGER NOT NULL DEFAULT 3,
                replan_count        INTEGER NOT NULL DEFAULT 0,
                max_replans         INTEGER NOT NULL DEFAULT 2,
                checkpoint_data     TEXT NOT NULL DEFAULT '{}',
                waiting_reason      TEXT,
                blocking_reason     TEXT,
                approval_request_id TEXT,
                result_summary      TEXT,
                metadata            TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS goal_steps (
                step_id             TEXT PRIMARY KEY,
                goal_id             TEXT NOT NULL,
                step_index          INTEGER NOT NULL,
                description         TEXT NOT NULL,
                capability          TEXT NOT NULL DEFAULT '',
                arguments           TEXT NOT NULL DEFAULT '{}',
                status              TEXT NOT NULL DEFAULT 'PENDING',
                created_at          TEXT NOT NULL,
                started_at          TEXT,
                completed_at        TEXT,
                result              TEXT,
                error               TEXT,
                checkpoint_data     TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id, step_index)")

        # Phase 9C: Persistent Wait Conditions
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wait_conditions (
                wait_id         TEXT PRIMARY KEY,
                goal_id         TEXT NOT NULL,
                step_id         TEXT,
                condition_type  TEXT NOT NULL,
                condition_key   TEXT NOT NULL DEFAULT '',
                operator        TEXT NOT NULL DEFAULT '==',
                expected_value  TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at      TEXT NOT NULL,
                satisfied_at    TEXT,
                metadata        TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wait_conditions_status ON wait_conditions(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wait_conditions_goal ON wait_conditions(goal_id)")

        # Phase 9D: Autonomy Safety State
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS autonomy_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # Phase A6: Task Ownership, Handoff & Failover
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_executions (
                task_execution_id       TEXT PRIMARY KEY,
                goal_id                 TEXT NOT NULL,
                step_id                 TEXT,
                owner_device_id         TEXT NOT NULL,
                executor_device_id      TEXT NOT NULL,
                lease_id                TEXT NOT NULL,
                lease_acquired_at       TEXT NOT NULL,
                lease_expires_at        TEXT NOT NULL,
                execution_state         TEXT NOT NULL DEFAULT 'ACTIVE',
                required_capabilities   TEXT NOT NULL DEFAULT '[]',
                required_resources      TEXT NOT NULL DEFAULT '[]',
                required_data_locations TEXT NOT NULL DEFAULT '{}',
                movable                 INTEGER NOT NULL DEFAULT 1,
                checkpoint_id           TEXT,
                attempt                 INTEGER NOT NULL DEFAULT 1,
                generation              INTEGER NOT NULL DEFAULT 1,
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL,
                metadata                TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_state ON task_executions(execution_state)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_goal ON task_executions(goal_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_executor ON task_executions(executor_device_id)")

        # Phase A7: Privacy Routing Policies
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS privacy_policies (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """
        )

        # Phase A8: Cross-Device Transport & Resumable File Transfer
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS file_transfers (
                transfer_id             TEXT PRIMARY KEY,
                source_device_id        TEXT NOT NULL,
                destination_device_id   TEXT NOT NULL,
                source_path             TEXT NOT NULL,
                destination_path        TEXT NOT NULL,
                filename                TEXT NOT NULL,
                total_bytes             INTEGER NOT NULL DEFAULT 0,
                transferred_bytes       INTEGER NOT NULL DEFAULT 0,
                chunk_size              INTEGER NOT NULL DEFAULT 65536,
                checksum_algorithm      TEXT NOT NULL DEFAULT 'sha256',
                source_checksum         TEXT NOT NULL DEFAULT '',
                destination_checksum    TEXT NOT NULL DEFAULT '',
                transport               TEXT NOT NULL DEFAULT 'lan',
                state                   TEXT NOT NULL DEFAULT 'PENDING',
                resumable               INTEGER NOT NULL DEFAULT 1,
                encryption              TEXT NOT NULL DEFAULT 'tls_1_3',
                created_at              TEXT NOT NULL,
                started_at              TEXT,
                completed_at            TEXT,
                last_progress_at        TEXT,
                retry_count             INTEGER NOT NULL DEFAULT 0,
                privacy_class           TEXT NOT NULL DEFAULT 'INTERNAL',
                metadata                TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_state ON file_transfers(state)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_src ON file_transfers(source_device_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_dst ON file_transfers(destination_device_id)")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS file_localities (
                file_id                 TEXT PRIMARY KEY,
                filename                TEXT NOT NULL,
                canonical_device_id     TEXT NOT NULL,
                canonical_path          TEXT NOT NULL,
                checksum                TEXT NOT NULL,
                total_bytes             INTEGER NOT NULL DEFAULT 0,
                accessible_locations    TEXT NOT NULL DEFAULT '[]',
                last_verified_at        TEXT NOT NULL,
                privacy_class           TEXT NOT NULL DEFAULT 'INTERNAL',
                metadata                TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_file_localities_device ON file_localities(canonical_device_id)")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transfer_tickets (
                ticket_id               TEXT PRIMARY KEY,
                transfer_id             TEXT NOT NULL,
                source_device_id        TEXT NOT NULL,
                destination_device_id   TEXT NOT NULL,
                token_hash              TEXT NOT NULL,
                issued_at               TEXT NOT NULL,
                expires_at              TEXT NOT NULL,
                used                    INTEGER NOT NULL DEFAULT 0,
                metadata                TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_transfer_tickets_xfer ON transfer_tickets(transfer_id)")
        await db.commit()

    # Run migrations for existing databases
    await _migrate_schema_updates()


async def _migrate_schema_updates() -> None:
    """Run all schema updates sequentially, ignoring 'column already exists' errors."""
    async with get_db() as db:
        # Phase 3
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN connection_state TEXT NOT NULL DEFAULT 'disconnected'")
        except Exception:
            pass
        
        # Phase 4
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN platform TEXT NOT NULL DEFAULT 'unknown'")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE action_logs ADD COLUMN capability TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE action_logs ADD COLUMN permission_decision TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
            
        # Phase 6 Telemetry
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN battery_level INTEGER")
            await db.execute("ALTER TABLE devices ADD COLUMN is_charging BOOLEAN")
            await db.execute("ALTER TABLE devices ADD COLUMN network_state TEXT")
            await db.execute("ALTER TABLE devices ADD COLUMN storage_free INTEGER")
        except Exception:
            pass

        # Phase 7A People & Relationships
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS people (
                    person_id           TEXT PRIMARY KEY,
                    display_name        TEXT NOT NULL,
                    aliases             TEXT NOT NULL DEFAULT '[]',
                    relationship_type   TEXT NOT NULL DEFAULT '',
                    notes               TEXT NOT NULL DEFAULT '',
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    last_interaction_at TEXT NOT NULL DEFAULT ''
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS person_facts (
                    id          TEXT PRIMARY KEY,
                    person_id   TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    confidence  REAL NOT NULL DEFAULT 1.0,
                    source      TEXT NOT NULL DEFAULT 'user',
                    timestamp   TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    id                  TEXT PRIMARY KEY,
                    source_person_id    TEXT NOT NULL,
                    target_person_id    TEXT NOT NULL,
                    relationship_type   TEXT NOT NULL,
                    metadata            TEXT NOT NULL DEFAULT '{}',
                    created_at          TEXT NOT NULL
                )
            """)
        except Exception:
            pass

        # Phase 7B: speaker_profiles
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS speaker_profiles (
                    speaker_id       TEXT PRIMARY KEY,
                    person_id        TEXT,
                    label            TEXT NOT NULL,
                    is_primary_user  BOOLEAN NOT NULL DEFAULT 0,
                    confidence       REAL NOT NULL DEFAULT 1.0,
                    metadata         TEXT NOT NULL DEFAULT '{}',
                    created_at       TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                )
            """)
        except Exception:
            pass

        # Phase 8A: World State Signals & Context Events
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS world_state_signals (
                    signal_key   TEXT PRIMARY KEY,
                    source       TEXT NOT NULL,
                    timestamp    TEXT NOT NULL,
                    confidence   REAL NOT NULL DEFAULT 1.0,
                    ttl          REAL NOT NULL DEFAULT 300.0,
                    payload      TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS context_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type  TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    details     TEXT NOT NULL DEFAULT '{}'
                )
            """)
        except Exception:
            pass

        # Phase 8B: Proactive Conditions and Events
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proactive_conditions (
                    condition_id        TEXT PRIMARY KEY,
                    condition_type      TEXT NOT NULL DEFAULT 'signal',
                    signal_key          TEXT NOT NULL DEFAULT '',
                    operator            TEXT NOT NULL DEFAULT 'equals',
                    target_value        TEXT NOT NULL DEFAULT '',
                    clauses             TEXT NOT NULL DEFAULT '[]',
                    combination         TEXT NOT NULL DEFAULT 'AND',
                    description         TEXT NOT NULL DEFAULT '',
                    related_task_id     TEXT,
                    related_goal_id     TEXT,
                    metadata            TEXT NOT NULL DEFAULT '{}',
                    current_truth_state INTEGER NOT NULL DEFAULT 0,
                    last_evaluated_at   TEXT NOT NULL DEFAULT '',
                    created_at          TEXT NOT NULL,
                    expires_at          TEXT,
                    is_active           INTEGER NOT NULL DEFAULT 1
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proactive_events (
                    event_id            TEXT PRIMARY KEY,
                    event_type          TEXT NOT NULL,
                    source              TEXT NOT NULL DEFAULT 'proactive_engine',
                    related_task_id     TEXT,
                    related_goal_id     TEXT,
                    condition_id        TEXT NOT NULL,
                    trigger_condition   TEXT NOT NULL DEFAULT '{}',
                    context_snapshot    TEXT NOT NULL DEFAULT '{}',
                    priority            REAL NOT NULL DEFAULT 0.5,
                    confidence          REAL NOT NULL DEFAULT 1.0,
                    created_at          TEXT NOT NULL,
                    expires_at          TEXT,
                    status              TEXT NOT NULL DEFAULT 'candidate'
                )
            """)
        except Exception:
            pass

        # Phase 8C: Delivery Decisions
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS delivery_decisions (
                    decision_id         TEXT PRIMARY KEY,
                    proactive_event_id  TEXT NOT NULL,
                    target_device_id    TEXT,
                    channel             TEXT NOT NULL DEFAULT 'desktop_notification',
                    timing              TEXT NOT NULL DEFAULT 'now',
                    reason              TEXT NOT NULL DEFAULT '',
                    confidence          REAL NOT NULL DEFAULT 1.0,
                    score               REAL NOT NULL DEFAULT 0.5,
                    created_at          TEXT NOT NULL,
                    deliver_after       TEXT,
                    expires_at          TEXT,
                    status              TEXT NOT NULL DEFAULT 'pending'
                )
            """)
        except Exception:
            pass

        # Phase 9C: wait_conditions
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS wait_conditions (
                    wait_id         TEXT PRIMARY KEY,
                    goal_id         TEXT NOT NULL,
                    step_id         TEXT,
                    condition_type  TEXT NOT NULL,
                    condition_key   TEXT NOT NULL DEFAULT '',
                    operator        TEXT NOT NULL DEFAULT '==',
                    expected_value  TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at      TEXT NOT NULL,
                    satisfied_at    TEXT,
                    metadata        TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_wait_conditions_status ON wait_conditions(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_wait_conditions_goal ON wait_conditions(goal_id)")
        except Exception:
            pass

        # Phase 9D: Autonomy state & Safety bounds
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS autonomy_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE goals ADD COLUMN max_runtime_seconds INTEGER DEFAULT 3600")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE goals ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE goals ADD COLUMN max_consecutive_failures INTEGER NOT NULL DEFAULT 3")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE goals ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE goals ADD COLUMN max_replans INTEGER NOT NULL DEFAULT 2")
        except Exception:
            pass

        # Phase A5: Distributed Device Handles & Executor Registry
        for col, col_type, default_val in [
            ("device_type", "TEXT", "'unknown'"),
            ("trust_level", "TEXT", "'standard'"),
            ("agent_version", "TEXT", "''"),
            ("last_heartbeat", "TEXT", "''"),
            ("transports", "TEXT", "'{}'"),
            ("resource_profile", "TEXT", "'{}'"),
            ("workload", "TEXT", "'{}'"),
            ("metadata", "TEXT", "'{}'"),
        ]:
            try:
                await db.execute(f"ALTER TABLE devices ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default_val}")
            except Exception:
                pass

        # Migrate existing ASUS record to primary_admin and backfill trust levels
        try:
            await db.execute(
                "UPDATE devices SET role = 'primary_admin', trust_level = 'admin', device_type = 'laptop' WHERE device_id = 'asus'"
            )
            await db.execute(
                "UPDATE devices SET trust_level = 'admin' WHERE role = 'admin' AND (trust_level IS NULL OR trust_level = 'standard')"
            )
            await db.execute(
                "UPDATE devices SET trust_level = 'trusted' WHERE role IN ('sub_admin', 'trusted_worker') AND (trust_level IS NULL OR trust_level = 'standard')"
            )
        except Exception:
            pass

        # Phase A6: Task Ownership, Handoff & Failover
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS task_executions (
                    task_execution_id       TEXT PRIMARY KEY,
                    goal_id                 TEXT NOT NULL,
                    step_id                 TEXT,
                    owner_device_id         TEXT NOT NULL,
                    executor_device_id      TEXT NOT NULL,
                    lease_id                TEXT NOT NULL,
                    lease_acquired_at       TEXT NOT NULL,
                    lease_expires_at        TEXT NOT NULL,
                    execution_state         TEXT NOT NULL DEFAULT 'ACTIVE',
                    required_capabilities   TEXT NOT NULL DEFAULT '[]',
                    required_resources      TEXT NOT NULL DEFAULT '[]',
                    required_data_locations TEXT NOT NULL DEFAULT '{}',
                    movable                 INTEGER NOT NULL DEFAULT 1,
                    checkpoint_id           TEXT,
                    attempt                 INTEGER NOT NULL DEFAULT 1,
                    generation              INTEGER NOT NULL DEFAULT 1,
                    created_at              TEXT NOT NULL,
                    updated_at              TEXT NOT NULL,
                    metadata                TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_state ON task_executions(execution_state)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_goal ON task_executions(goal_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_executor ON task_executions(executor_device_id)")
        except Exception:
            pass

        # Phase A7: Privacy Routing Policies
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS privacy_policies (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
        except Exception:
            pass

        # Phase A8: Cross-Device Transport & Resumable File Transfer
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS file_transfers (
                    transfer_id             TEXT PRIMARY KEY,
                    source_device_id        TEXT NOT NULL,
                    destination_device_id   TEXT NOT NULL,
                    source_path             TEXT NOT NULL,
                    destination_path        TEXT NOT NULL,
                    filename                TEXT NOT NULL,
                    total_bytes             INTEGER NOT NULL DEFAULT 0,
                    transferred_bytes       INTEGER NOT NULL DEFAULT 0,
                    chunk_size              INTEGER NOT NULL DEFAULT 65536,
                    checksum_algorithm      TEXT NOT NULL DEFAULT 'sha256',
                    source_checksum         TEXT NOT NULL DEFAULT '',
                    destination_checksum    TEXT NOT NULL DEFAULT '',
                    transport               TEXT NOT NULL DEFAULT 'lan',
                    state                   TEXT NOT NULL DEFAULT 'PENDING',
                    resumable               INTEGER NOT NULL DEFAULT 1,
                    encryption              TEXT NOT NULL DEFAULT 'tls_1_3',
                    created_at              TEXT NOT NULL,
                    started_at              TEXT,
                    completed_at            TEXT,
                    last_progress_at        TEXT,
                    retry_count             INTEGER NOT NULL DEFAULT 0,
                    privacy_class           TEXT NOT NULL DEFAULT 'INTERNAL',
                    metadata                TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_state ON file_transfers(state)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_src ON file_transfers(source_device_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_file_transfers_dst ON file_transfers(destination_device_id)")
        except Exception:
            pass

        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS file_localities (
                    file_id                 TEXT PRIMARY KEY,
                    filename                TEXT NOT NULL,
                    canonical_device_id     TEXT NOT NULL,
                    canonical_path          TEXT NOT NULL,
                    checksum                TEXT NOT NULL,
                    total_bytes             INTEGER NOT NULL DEFAULT 0,
                    accessible_locations    TEXT NOT NULL DEFAULT '[]',
                    last_verified_at        TEXT NOT NULL,
                    privacy_class           TEXT NOT NULL DEFAULT 'INTERNAL',
                    metadata                TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_file_localities_device ON file_localities(canonical_device_id)")
        except Exception:
            pass

        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS transfer_tickets (
                    ticket_id               TEXT PRIMARY KEY,
                    transfer_id             TEXT NOT NULL,
                    source_device_id        TEXT NOT NULL,
                    destination_device_id   TEXT NOT NULL,
                    token_hash              TEXT NOT NULL,
                    issued_at               TEXT NOT NULL,
                    expires_at              TEXT NOT NULL,
                    used                    INTEGER NOT NULL DEFAULT 0,
                    metadata                TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_transfer_tickets_xfer ON transfer_tickets(transfer_id)")
        except Exception:
            pass

        await db.commit()


# ---------------------------------------------------------------------------
# Action logs
# ---------------------------------------------------------------------------

async def log_action(
    device_id: str,
    command: str,
    result: str,
    message: str = "",
    command_id: str = "",
    capability: str = "",
    permission_decision: str = "",
) -> None:
    """Insert one row into action_logs."""
    timestamp = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO action_logs (timestamp, device_id, command_id, command, result, message, capability, permission_decision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, device_id, command_id, command, result, message, capability, permission_decision),
        )
        await db.commit()


async def get_recent_logs(limit: int = 20) -> list[dict]:
    """Return the most recent `limit` log rows, newest first."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp, device_id, command_id, command, result, message, capability, permission_decision
            FROM   action_logs
            ORDER  BY id DESC
            LIMIT  ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Device enrollment
# ---------------------------------------------------------------------------

async def enroll_device(
    device_id: str,
    device_name: str,
    role: str,
    secret_hash: str,
    device_type: str = "unknown",
    trust_level: Optional[str] = None,
    platform: str = "unknown",
    transports: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Register a new device in the devices table."""
    from .device_handle import normalize_role, normalize_trust_level, normalize_device_type, default_transports
    norm_role = normalize_role(role)
    norm_trust = normalize_trust_level(trust_level, norm_role)
    norm_type = normalize_device_type(device_type)
    transports_json = json.dumps(transports or default_transports())
    meta_json = json.dumps(metadata or {})
    enrolled_at = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO devices (
                device_id, device_name, role, secret_hash, enrolled_at,
                connection_state, platform, device_type, trust_level,
                transports, metadata
            )
            VALUES (?, ?, ?, ?, ?, 'disconnected', ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                device_name,
                norm_role,
                secret_hash,
                enrolled_at,
                platform,
                norm_type,
                norm_trust,
                transports_json,
                meta_json,
            ),
        )
        await db.commit()


async def get_device(device_id: str) -> Optional[dict]:
    """Retrieve a single device record, or None if not enrolled."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def list_devices() -> list[dict]:
    """Return all enrolled devices."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM devices ORDER BY enrolled_at"
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_last_seen(device_id: str) -> None:
    """Update the last_seen timestamp for a device."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE devices SET last_seen = ? WHERE device_id = ?",
            (now, device_id),
        )
        await db.commit()

async def update_device_telemetry(device_id: str, telemetry: dict) -> None:
    """Update optional telemetry fields for a device."""
    set_clauses = []
    values = []
    allowed_fields = {"battery_level", "is_charging", "network_state", "storage_free"}
    
    for k, v in telemetry.items():
        if k in allowed_fields:
            set_clauses.append(f"{k} = ?")
            values.append(int(v) if isinstance(v, bool) else v)
            
    if not set_clauses:
        return
        
    values.append(device_id)
    query = f"UPDATE devices SET {', '.join(set_clauses)} WHERE device_id = ?"
    async with get_db() as db:
        await db.execute(query, tuple(values))
        await db.commit()


async def update_device_heartbeat(
    device_id: str,
    timestamp: Optional[str] = None,
    agent_version: Optional[str] = None,
    telemetry: Optional[dict] = None,
    workload: Optional[dict] = None,
) -> None:
    """Update heartbeat timestamp and telemetry/workload for a device."""
    now = timestamp or datetime.now(timezone.utc).isoformat()
    set_clauses = ["last_heartbeat = ?", "last_seen = ?"]
    values = [now, now]

    if agent_version:
        set_clauses.append("agent_version = ?")
        values.append(agent_version)

    if telemetry is not None:
        set_clauses.append("resource_profile = ?")
        values.append(json.dumps(telemetry))
        if "battery_level" in telemetry:
            set_clauses.append("battery_level = ?")
            values.append(telemetry["battery_level"])
        if "is_charging" in telemetry:
            set_clauses.append("is_charging = ?")
            values.append(int(telemetry["is_charging"]))

    if workload is not None:
        set_clauses.append("workload = ?")
        values.append(json.dumps(workload))

    values.append(device_id)
    query = f"UPDATE devices SET {', '.join(set_clauses)} WHERE device_id = ?"
    async with get_db() as db:
        await db.execute(query, tuple(values))
        await db.commit()


async def has_admin() -> bool:
    """Check whether at least one admin device is enrolled."""
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM devices WHERE role IN ('admin', 'primary_admin')"
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] > 0 if row else False


async def delete_device(device_id: str) -> bool:
    """Remove a device from the registry. Returns True if it existed."""
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM devices WHERE device_id = ?", (device_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Connection state (Phase 3)
# ---------------------------------------------------------------------------

async def update_connection_state(device_id: str, state: str) -> None:
    """Update the connection_state for a device ('connected' or 'disconnected')."""
    async with get_db() as db:
        await db.execute(
            "UPDATE devices SET connection_state = ? WHERE device_id = ?",
            (state, device_id),
        )
        await db.commit()


async def reset_stale_device_connections() -> int:
    """Reset all device connection_state values to 'disconnected' on hub startup."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE devices SET connection_state = 'disconnected' WHERE connection_state = 'connected'"
        )
        await db.commit()
        return cursor.rowcount


async def get_connection_state(device_id: str) -> Optional[str]:
    """Return the connection_state for a device, or None if not enrolled."""
    async with get_db() as db:
        async with db.execute(
            "SELECT connection_state FROM devices WHERE device_id = ?", (device_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Secret rotation (Phase 3)
# ---------------------------------------------------------------------------

async def rotate_device_secret(device_id: str, new_secret_hash: str) -> bool:
    """Replace a device's secret_hash. Returns True if the device existed."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE devices SET secret_hash = ? WHERE device_id = ?",
            (new_secret_hash, device_id),
        )
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Command history — duplicate protection & expiry (Phase 3)
# ---------------------------------------------------------------------------

async def record_command(
    command_id: str,
    command: str,
    target_device: str,
    requested_by: str,
) -> bool:
    """Record a command in command_history with a hub-generated received_at.

    Returns True if the command was recorded (new).
    Returns False if the command_id already exists (duplicate).
    """
    received_at = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        try:
            await db.execute(
                """
                INSERT INTO command_history
                    (command_id, command, target_device, requested_by, received_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (command_id, command, target_device, requested_by, received_at),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Duplicate command_id — PRIMARY KEY violation
            return False


async def is_duplicate_command(command_id: str) -> bool:
    """Check whether a command_id already exists in command_history."""
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM command_history WHERE command_id = ?", (command_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def get_command_received_at(command_id: str) -> Optional[str]:
    """Return the received_at timestamp for a command, or None if not found."""
    async with get_db() as db:
        async with db.execute(
            "SELECT received_at FROM command_history WHERE command_id = ?",
            (command_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def update_command_status(command_id: str, status: str) -> None:
    """Update the status of a command in command_history."""
    now = datetime.now(timezone.utc).isoformat()
    field = "dispatched_at" if status == "dispatched" else "expired_at"
    async with get_db() as db:
        await db.execute(
            f"UPDATE command_history SET status = ?, {field} = ? WHERE command_id = ?",
            (status, now, command_id),
        )
        await db.commit()


async def is_command_expired(command_id: str, ttl_seconds: int) -> bool:
    """Check whether a command's received_at is older than ttl_seconds ago."""
    async with get_db() as db:
        async with db.execute(
            "SELECT received_at FROM command_history WHERE command_id = ?",
            (command_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row or not row[0]:
        return False

    received_at = datetime.fromisoformat(row[0])
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    return received_at < cutoff


async def prune_command_history(retention_hours: int = 24) -> int:
    """Delete command_history rows older than retention_hours.
    Returns the number of rows deleted.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    ).isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM command_history WHERE received_at < ?", (cutoff,)
        )
        await db.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Pending confirmations
# ---------------------------------------------------------------------------

async def create_confirmation(
    command_id: str,
    target_device: str,
    command: str,
    args: list[str],
    requested_by: str,
    tier: int,
) -> None:
    """Insert a pending confirmation for a Tier 2/3 command."""
    requested_at = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO pending_confirmations
                (command_id, target_device, command, args, requested_by, tier, status, requested_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (command_id, target_device, command, json.dumps(args), requested_by, tier, requested_at),
        )
        await db.commit()


async def resolve_confirmation(
    command_id: str,
    decision: str,
    resolved_by: str,
) -> Optional[dict]:
    """Mark a pending confirmation as approved/denied/expired.
    Returns the confirmation row if it was still pending, else None."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # Fetch current state
        async with db.execute(
            "SELECT * FROM pending_confirmations WHERE command_id = ?", (command_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row or row["status"] != "pending":
            return None

        await db.execute(
            """
            UPDATE pending_confirmations
            SET status = ?, resolved_at = ?, resolved_by = ?
            WHERE command_id = ?
            """,
            (decision, now, resolved_by, command_id),
        )
        await db.commit()
        return dict(row)


async def update_confirmation(command_id: str, decision: str, resolved_by: str = "user") -> Optional[dict]:
    """Alias for resolve_confirmation."""
    return await resolve_confirmation(command_id, decision, resolved_by)


async def get_pending_confirmations() -> list[dict]:
    """Return all confirmations with status='pending'."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM pending_confirmations
            WHERE status = 'pending'
            ORDER BY requested_at DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_confirmation(command_id: str) -> Optional[dict]:
    """Retrieve a single confirmation by command_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pending_confirmations WHERE command_id = ?", (command_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Device Capabilities & Permissions (Phase 4)
# ---------------------------------------------------------------------------

async def update_device_capabilities(device_id: str, platform: str, capabilities: list[str]) -> None:
    """Update a device's platform and its supported capabilities."""
    async with get_db() as db:
        await db.execute(
            "UPDATE devices SET platform = ? WHERE device_id = ?",
            (platform, device_id),
        )
        # Clear old capabilities
        await db.execute(
            "DELETE FROM device_capabilities WHERE device_id = ?",
            (device_id,)
        )
        # Insert new capabilities
        for cap in capabilities:
            await db.execute(
                "INSERT INTO device_capabilities (device_id, capability) VALUES (?, ?)",
                (device_id, cap),
            )
        await db.commit()

async def get_device_capabilities(device_id: str) -> list[str]:
    """Return the list of capabilities supported by a device."""
    async with get_db() as db:
        async with db.execute(
            "SELECT capability FROM device_capabilities WHERE device_id = ? AND enabled = 1",
            (device_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def save_device_capabilities(device_id: str, capabilities: list[str], platform: str = "unknown") -> None:
    """Save or update capabilities for a device."""
    await update_device_capabilities(device_id, platform, capabilities)
    try:
        from .executor_registry import ExecutorRegistry
        er = ExecutorRegistry.get_instance()
        handle = er.get_device_handle(device_id)
        if handle:
            for c in capabilities:
                if c not in handle.capabilities:
                    handle.capabilities.append(c)
        else:
            from .device_handle import DeviceHandle
            er.register_device(DeviceHandle(
                device_id=device_id,
                display_name=device_id,
                platform=platform,
                capabilities=list(capabilities),
                online=er.is_device_online(device_id),
            ))
    except Exception:
        pass

async def set_device_permission(device_id: str, capability: str, decision: str) -> None:
    """Set a per-device permission override (allow/deny/ask) for a capability."""
    async with get_db() as db:
        # SQLite UPSERT syntax
        await db.execute(
            """
            INSERT INTO device_permissions (device_id, capability, decision)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id, capability) DO UPDATE SET decision = excluded.decision
            """,
            (device_id, capability, decision),
        )
        await db.commit()

async def get_device_permissions(device_id: str) -> dict[str, str]:
    """Return all permission overrides for a device as a capability -> decision mapping."""
    async with get_db() as db:
        async with db.execute(
            "SELECT capability, decision FROM device_permissions WHERE device_id = ?",
            (device_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}

async def get_device_permission(device_id: str, capability: str) -> Optional[str]:
    """Return the specific permission override for a capability, or None if not set."""
    async with get_db() as db:
        async with db.execute(
            "SELECT decision FROM device_permissions WHERE device_id = ? AND capability = ?",
            (device_id, capability)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None

# ---------------------------------------------------------------------------
# Phase 5: Memory, Tasks, and Integrations
# ---------------------------------------------------------------------------

async def add_task(task_data: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO tasks (
                id, title, type, status, trigger_time, cron_expression, timezone,
                assigned_device, command, args, required_capability, reroutable,
                retry_count, max_retries, last_error, execution_id, context,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_data["id"], task_data["title"], task_data["type"], task_data.get("status", "pending"),
                task_data.get("trigger_time", ""), task_data.get("cron_expression", ""),
                task_data.get("timezone", "UTC"), task_data.get("assigned_device", ""),
                task_data["command"], json.dumps(task_data.get("args", [])),
                task_data.get("required_capability", ""), int(task_data.get("reroutable", False)),
                task_data.get("retry_count", 0), task_data.get("max_retries", 0),
                task_data.get("last_error", ""), task_data.get("execution_id", ""),
                json.dumps(task_data.get("context", {})), now, now
            )
        )
        await db.commit()

async def get_tasks() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks") as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def update_task(task_id: str, updates: dict) -> bool:
    if not updates:
        return False
    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now
    
    set_clauses = []
    values = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        if isinstance(v, (dict, list)):
            values.append(json.dumps(v))
        elif isinstance(v, bool):
            values.append(int(v))
        else:
            values.append(v)
    
    values.append(task_id)
    
    query = f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0

async def get_task(task_id: str) -> Optional[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None

async def add_memory_item(item: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO memory_items (
                id, category, key, value, source, confidence, last_accessed, relationship_entity_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"], item["category"], item["key"], item["value"], item.get("source", "user"),
                item.get("confidence", 1.0), item.get("last_accessed", ""), item.get("relationship_entity_id", ""),
                now, now
            )
        )
        await db.commit()

async def get_memory_items(category: Optional[str] = None) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if category:
            async with db.execute("SELECT * FROM memory_items WHERE category = ?", (category,)) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute("SELECT * FROM memory_items") as cursor:
                rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def update_memory_item(item_id: str, updates: dict) -> bool:
    if not updates:
        return False
    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now
    
    set_clauses = []
    values = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        values.append(v)
    
    values.append(item_id)
    query = f"UPDATE memory_items SET {', '.join(set_clauses)} WHERE id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0

async def add_integration(integration: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO integrations (
                id, plugin_name, account_id, config, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                integration["id"], integration["plugin_name"], integration.get("account_id", ""),
                json.dumps(integration.get("config", {})), integration.get("status", "active"), now
            )
        )
        await db.commit()

async def get_integrations() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM integrations") as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def update_integration(integration_id: str, updates: dict) -> bool:
    if not updates:
        return False
    
    set_clauses = []
    values = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        if isinstance(v, (dict, list)):
            values.append(json.dumps(v))
        else:
            values.append(v)
    
    values.append(integration_id)
    query = f"UPDATE integrations SET {', '.join(set_clauses)} WHERE id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0

# ---------------------------------------------------------------------------
# Phase 6: Session Security
# ---------------------------------------------------------------------------
import secrets

async def create_session(device_id: str, expires_in_days: int = 7) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expires_in_days)).isoformat()
    created_at = now.isoformat()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO sessions (token, device_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, device_id, created_at, expires_at)
        )
        await db.commit()
    return token

async def get_session(token: str) -> Optional[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions WHERE token = ?", (token,)) as cursor:
            row = await cursor.fetchone()
    
    if not row:
        return None
        
    # Check expiry
    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        await delete_session(token)
        return None
        
    return dict(row)

async def delete_session(token: str) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()

# ---------------------------------------------------------------------------
# Phase 6: Continuity Model
# ---------------------------------------------------------------------------

async def create_continuity_session(session: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO continuity_sessions (
                id, active_task, active_project, active_file, source_device, current_device,
                required_capabilities, last_known_state, continuation_metadata, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["id"], session.get("active_task", ""), session.get("active_project", ""),
                session.get("active_file", ""), session["source_device"], session["current_device"],
                json.dumps(session.get("required_capabilities", [])),
                json.dumps(session.get("last_known_state", {})),
                json.dumps(session.get("continuation_metadata", {})),
                now
            )
        )
        await db.commit()

async def get_continuity_sessions() -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM continuity_sessions ORDER BY updated_at DESC") as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def get_continuity_session(session_id: str) -> Optional[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM continuity_sessions WHERE id = ?", (session_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None

async def update_continuity_session(session_id: str, updates: dict) -> bool:
    if not updates:
        return False
    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now
    
    set_clauses = []
    values = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        if isinstance(v, (dict, list)):
            values.append(json.dumps(v))
        else:
            values.append(v)
            
    values.append(session_id)
    query = f"UPDATE continuity_sessions SET {', '.join(set_clauses)} WHERE id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0


async def get_session_context(device_id: str) -> dict:
    """Retrieve the session context for a device."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT turns, active_references, active_task FROM session_context WHERE device_id = ?", (device_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "turns": json.loads(row["turns"]),
                    "references": json.loads(row["active_references"]),
                    "active_task": row["active_task"]
                }
            return {"turns": [], "references": {}, "active_task": None}


async def save_session_context(device_id: str, turns: list, references: dict, active_task: str = None) -> None:
    """Save the session context for a device."""
    timestamp = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO session_context (device_id, turns, active_references, active_task, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                turns=excluded.turns,
                active_references=excluded.active_references,
                active_task=excluded.active_task,
                updated_at=excluded.updated_at
            """,
            (device_id, json.dumps(turns), json.dumps(references), active_task, timestamp)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Phase 7A: People and Relationships
# ---------------------------------------------------------------------------

def _format_person_row(row) -> dict:
    d = dict(row)
    if "aliases" in d and isinstance(d["aliases"], str):
        try:
            d["aliases"] = json.loads(d["aliases"])
        except Exception:
            d["aliases"] = []
    return d


def _format_relationship_row(row) -> dict:
    d = dict(row)
    if "metadata" in d and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    return d


async def create_person(person: dict) -> str:
    """Insert a new person record and return the person_id."""
    person_id = person.get("person_id") or uuid.uuid4().hex
    display_name = person["display_name"]
    aliases = person.get("aliases", [])
    if isinstance(aliases, list):
        aliases_str = json.dumps(aliases)
    else:
        aliases_str = str(aliases)
    relationship_type = person.get("relationship_type", "")
    notes = person.get("notes", "")
    now = datetime.now(timezone.utc).isoformat()
    created_at = person.get("created_at") or now
    updated_at = person.get("updated_at") or now
    last_interaction_at = person.get("last_interaction_at", "")

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO people (person_id, display_name, aliases, relationship_type, notes, created_at, updated_at, last_interaction_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (person_id, display_name, aliases_str, relationship_type, notes, created_at, updated_at, last_interaction_at)
        )
        await db.commit()
    return person_id


async def get_person(person_id: str) -> Optional[dict]:
    """Retrieve a person by person_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM people WHERE person_id = ?", (person_id,)) as cursor:
            row = await cursor.fetchone()
            return _format_person_row(row) if row else None


async def get_person_by_name(display_name: str) -> Optional[dict]:
    """Retrieve a person by exact display_name (case-insensitive)."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM people WHERE LOWER(display_name) = LOWER(?)", (display_name.strip(),)) as cursor:
            row = await cursor.fetchone()
            return _format_person_row(row) if row else None


async def list_people() -> list[dict]:
    """List all people."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM people ORDER BY created_at") as cursor:
            rows = await cursor.fetchall()
            return [_format_person_row(r) for r in rows]


async def update_person(person_id: str, updates: dict) -> bool:
    """Update fields of a person."""
    if not updates:
        return False
    now = datetime.now(timezone.utc).isoformat()
    updates = dict(updates)
    updates["updated_at"] = now

    set_clauses = []
    values = []
    for k, v in updates.items():
        if k in ("display_name", "relationship_type", "notes", "last_interaction_at", "updated_at"):
            set_clauses.append(f"{k} = ?")
            values.append(v)
        elif k == "aliases":
            set_clauses.append(f"{k} = ?")
            values.append(json.dumps(v) if isinstance(v, list) else str(v))

    if not set_clauses:
        return False

    values.append(person_id)
    query = f"UPDATE people SET {', '.join(set_clauses)} WHERE person_id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0


async def delete_person(person_id: str) -> bool:
    """Delete a person and their associated facts."""
    async with get_db() as db:
        await db.execute("DELETE FROM person_facts WHERE person_id = ?", (person_id,))
        await db.execute("DELETE FROM relationships WHERE source_person_id = ? OR target_person_id = ?", (person_id, person_id))
        cursor = await db.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
        await db.commit()
        return cursor.rowcount > 0


async def add_person_fact(fact: dict) -> str:
    """Add a fact for a person."""
    fact_id = fact.get("id") or uuid.uuid4().hex
    person_id = fact["person_id"]
    key = fact["key"]
    value = fact["value"]
    confidence = float(fact.get("confidence", 1.0))
    source = fact.get("source", "user")
    now = datetime.now(timezone.utc).isoformat()
    timestamp = fact.get("timestamp") or now

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO person_facts (id, person_id, key, value, confidence, source, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fact_id, person_id, key, value, confidence, source, timestamp)
        )
        await db.commit()
    return fact_id


async def get_person_facts(person_id: str) -> list[dict]:
    """Retrieve all facts for a person."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM person_facts WHERE person_id = ? ORDER BY timestamp", (person_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def delete_person_fact(fact_id: str) -> bool:
    """Delete a single fact by id."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM person_facts WHERE id = ?", (fact_id,))
        await db.commit()
        return cursor.rowcount > 0


async def add_relationship(rel: dict) -> str:
    """Add a relationship between two people."""
    rel_id = rel.get("id") or uuid.uuid4().hex
    source_person_id = rel["source_person_id"]
    target_person_id = rel["target_person_id"]
    relationship_type = rel["relationship_type"]
    metadata = rel.get("metadata", {})
    metadata_str = json.dumps(metadata) if isinstance(metadata, dict) else str(metadata)
    now = datetime.now(timezone.utc).isoformat()
    created_at = rel.get("created_at") or now

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO relationships (id, source_person_id, target_person_id, relationship_type, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rel_id, source_person_id, target_person_id, relationship_type, metadata_str, created_at)
        )
        await db.commit()
    return rel_id


async def get_relationships(person_id: str) -> list[dict]:
    """Get all relationships where person is source or target."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM relationships WHERE source_person_id = ? OR target_person_id = ? ORDER BY created_at",
            (person_id, person_id)
        ) as cursor:
            rows = await cursor.fetchall()
            return [_format_relationship_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 7B: Speaker Profiles
# ---------------------------------------------------------------------------

def _format_speaker_row(row) -> dict:
    d = dict(row)
    if "metadata" in d and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    d["is_primary_user"] = bool(d.get("is_primary_user", 0))
    d["confidence"] = float(d.get("confidence", 1.0))
    return d


async def create_speaker_profile(data: dict) -> dict:
    """Create a new speaker profile."""
    speaker_id = data.get("speaker_id") or f"spk_{uuid.uuid4().hex[:8]}"
    person_id = data.get("person_id")
    label = data.get("label", "Unknown Speaker")
    is_primary_user = 1 if data.get("is_primary_user") else 0
    confidence = float(data.get("confidence", 1.0))
    metadata = data.get("metadata", {})
    metadata_str = json.dumps(metadata) if isinstance(metadata, dict) else str(metadata)
    now = datetime.now(timezone.utc).isoformat()
    created_at = data.get("created_at") or now
    updated_at = data.get("updated_at") or now

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO speaker_profiles (speaker_id, person_id, label, is_primary_user, confidence, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (speaker_id, person_id, label, is_primary_user, confidence, metadata_str, created_at, updated_at)
        )
        await db.commit()

    return {
        "speaker_id": speaker_id,
        "person_id": person_id,
        "label": label,
        "is_primary_user": bool(is_primary_user),
        "confidence": confidence,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": created_at,
        "updated_at": updated_at
    }


async def get_speaker_profile(speaker_id: str) -> Optional[dict]:
    """Retrieve speaker profile by speaker_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM speaker_profiles WHERE speaker_id = ?", (speaker_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_speaker_row(row)


async def get_speaker_profile_by_label(label: str) -> Optional[dict]:
    """Retrieve speaker profile by label (case-insensitive)."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM speaker_profiles WHERE LOWER(label) = LOWER(?)", (label,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_speaker_row(row)


async def get_speaker_profile_by_person_id(person_id: str) -> Optional[dict]:
    """Retrieve speaker profile linked to a specific person_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM speaker_profiles WHERE person_id = ?", (person_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_speaker_row(row)


async def get_primary_speaker_profile() -> Optional[dict]:
    """Retrieve the primary user speaker profile."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM speaker_profiles WHERE is_primary_user = 1 LIMIT 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_speaker_row(row)


async def list_speaker_profiles() -> list[dict]:
    """List all speaker profiles."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM speaker_profiles ORDER BY is_primary_user DESC, created_at") as cursor:
            rows = await cursor.fetchall()
            return [_format_speaker_row(r) for r in rows]


async def update_speaker_profile(speaker_id: str, updates: dict) -> Optional[dict]:
    """Update speaker profile fields."""
    existing = await get_speaker_profile(speaker_id)
    if not existing:
        return None

    person_id = updates["person_id"] if "person_id" in updates else existing.get("person_id")
    label = updates.get("label", existing["label"])
    is_primary_user = 1 if updates.get("is_primary_user", existing["is_primary_user"]) else 0
    confidence = float(updates.get("confidence", existing["confidence"]))
    metadata = updates.get("metadata", existing.get("metadata", {}))
    metadata_str = json.dumps(metadata) if isinstance(metadata, dict) else str(metadata)
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute(
            """
            UPDATE speaker_profiles
            SET person_id = ?, label = ?, is_primary_user = ?, confidence = ?, metadata = ?, updated_at = ?
            WHERE speaker_id = ?
            """,
            (person_id, label, is_primary_user, confidence, metadata_str, now, speaker_id)
        )
        await db.commit()

    return await get_speaker_profile(speaker_id)


async def delete_speaker_profile(speaker_id: str) -> bool:
    """Delete a speaker profile by speaker_id."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM speaker_profiles WHERE speaker_id = ?", (speaker_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 8A: World State Signals and Context Events
# ---------------------------------------------------------------------------

async def save_world_state_signal(
    signal_key: str,
    source: str,
    timestamp: str,
    confidence: float,
    ttl: float,
    payload: dict
) -> None:
    """Save or update a world state signal."""
    payload_str = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO world_state_signals (signal_key, source, timestamp, confidence, ttl, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (signal_key, source, timestamp, confidence, ttl, payload_str)
        )
        await db.commit()


async def get_world_state_signals() -> list[dict]:
    """Retrieve all persisted world state signals."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM world_state_signals") as cursor:
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                p_raw = r["payload"]
                try:
                    p = json.loads(p_raw) if p_raw else {}
                except Exception:
                    p = {}
                results.append({
                    "signal_key": r["signal_key"],
                    "source": r["source"],
                    "timestamp": r["timestamp"],
                    "confidence": float(r["confidence"]),
                    "ttl": float(r["ttl"]),
                    "payload": p
                })
            return results


async def delete_world_state_signal(signal_key: str) -> bool:
    """Delete a persisted world state signal by key."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM world_state_signals WHERE signal_key = ?", (signal_key,))
        await db.commit()
        return cursor.rowcount > 0


async def add_context_event(
    event_type: str,
    source: str,
    summary: str,
    details: Optional[dict] = None,
    timestamp: Optional[str] = None
) -> int:
    """Record a bounded context event in persistent history."""
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()
    details_str = json.dumps(details) if isinstance(details, dict) else (details or "{}")
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO context_events (event_type, source, timestamp, summary, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, source, timestamp, summary, details_str)
        )
        await db.commit()
        event_id = cursor.lastrowid
        # Bound event table size to avoid unbounded growth
        await db.execute(
            """
            DELETE FROM context_events WHERE id NOT IN (
                SELECT id FROM context_events ORDER BY id DESC LIMIT 100
            )
            """
        )
        await db.commit()
        return event_id


async def get_recent_context_events(limit: int = 20) -> list[dict]:
    """Retrieve the most recent context events."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM context_events ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d_raw = r["details"]
                try:
                    d = json.loads(d_raw) if d_raw else {}
                except Exception:
                    d = {}
                results.append({
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "source": r["source"],
                    "timestamp": r["timestamp"],
                    "summary": r["summary"],
                    "details": d
                })
            # Return chronological order (oldest to newest)
            results.reverse()
            return results


# ---------------------------------------------------------------------------
# Phase 8B: Proactive Conditions & Candidate Events
# ---------------------------------------------------------------------------

async def save_proactive_condition(condition_data: dict) -> None:
    """Save or update a proactive condition."""
    now = datetime.now(timezone.utc).isoformat()
    cond_id = condition_data["condition_id"]
    cond_type = condition_data.get("condition_type", "signal")
    sig_key = condition_data.get("signal_key", "")
    op = condition_data.get("operator", "equals")
    val = condition_data.get("target_value", "")
    if isinstance(val, (dict, list, bool, int, float)):
        val_str = json.dumps(val)
    else:
        val_str = str(val)
    clauses_str = json.dumps(condition_data.get("clauses", []))
    comb = condition_data.get("combination", "AND")
    desc = condition_data.get("description", "")
    rel_task = condition_data.get("related_task_id")
    rel_goal = condition_data.get("related_goal_id")
    meta_str = json.dumps(condition_data.get("metadata", {}))
    truth_state = 1 if condition_data.get("current_truth_state", False) else 0
    last_eval = condition_data.get("last_evaluated_at", "")
    created_at = condition_data.get("created_at", now)
    expires_at = condition_data.get("expires_at")
    is_active = 1 if condition_data.get("is_active", True) else 0

    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO proactive_conditions (
                condition_id, condition_type, signal_key, operator, target_value,
                clauses, combination, description, related_task_id, related_goal_id,
                metadata, current_truth_state, last_evaluated_at, created_at, expires_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cond_id, cond_type, sig_key, op, val_str,
                clauses_str, comb, desc, rel_task, rel_goal,
                meta_str, truth_state, last_eval, created_at, expires_at, is_active
            )
        )
        await db.commit()


def _format_condition_row(r: aiosqlite.Row) -> dict:
    val_raw = r["target_value"]
    try:
        val = json.loads(val_raw)
    except Exception:
        val = val_raw
    clauses_raw = r["clauses"]
    try:
        clauses = json.loads(clauses_raw) if clauses_raw else []
    except Exception:
        clauses = []
    meta_raw = r["metadata"]
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except Exception:
        meta = {}
    return {
        "condition_id": r["condition_id"],
        "condition_type": r["condition_type"],
        "signal_key": r["signal_key"],
        "operator": r["operator"],
        "target_value": val,
        "clauses": clauses,
        "combination": r["combination"],
        "description": r["description"],
        "related_task_id": r["related_task_id"],
        "related_goal_id": r["related_goal_id"],
        "metadata": meta,
        "current_truth_state": bool(r["current_truth_state"]),
        "last_evaluated_at": r["last_evaluated_at"],
        "created_at": r["created_at"],
        "expires_at": r["expires_at"],
        "is_active": bool(r["is_active"])
    }


async def get_proactive_condition(condition_id: str) -> Optional[dict]:
    """Retrieve a proactive condition by ID."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM proactive_conditions WHERE condition_id = ?", (condition_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_condition_row(row)


async def list_proactive_conditions(active_only: bool = True) -> list[dict]:
    """List all registered proactive conditions."""
    query = "SELECT * FROM proactive_conditions"
    params = ()
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY created_at DESC"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [_format_condition_row(r) for r in rows]


async def update_condition_truth_state(condition_id: str, truth_state: bool, evaluated_at: str) -> bool:
    """Update current truth state and evaluation timestamp of a condition."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE proactive_conditions SET current_truth_state = ?, last_evaluated_at = ? WHERE condition_id = ?",
            (1 if truth_state else 0, evaluated_at, condition_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_proactive_condition(condition_id: str, updates: dict) -> bool:
    """Update arbitrary fields of a condition."""
    if not updates:
        return False
    set_clauses = []
    values = []
    for k, v in updates.items():
        set_clauses.append(f"{k} = ?")
        if isinstance(v, (dict, list)):
            values.append(json.dumps(v))
        elif isinstance(v, bool):
            values.append(1 if v else 0)
        else:
            values.append(v)
    values.append(condition_id)
    query = f"UPDATE proactive_conditions SET {', '.join(set_clauses)} WHERE condition_id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0


async def delete_proactive_condition(condition_id: str) -> bool:
    """Delete a proactive condition by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM proactive_conditions WHERE condition_id = ?", (condition_id,))
        await db.commit()
        return cursor.rowcount > 0


async def save_proactive_event(event_data: dict) -> None:
    """Save or update a proactive candidate event."""
    now = datetime.now(timezone.utc).isoformat()
    ev_id = event_data["event_id"]
    ev_type = event_data["event_type"]
    source = event_data.get("source", "proactive_engine")
    rel_task = event_data.get("related_task_id")
    rel_goal = event_data.get("related_goal_id")
    cond_id = event_data["condition_id"]
    trig_cond = json.dumps(event_data.get("trigger_condition", {}))
    ctx_snap = json.dumps(event_data.get("context_snapshot", {}))
    prio = float(event_data.get("priority", 0.5))
    conf = float(event_data.get("confidence", 1.0))
    created_at = event_data.get("created_at", now)
    expires_at = event_data.get("expires_at")
    status = event_data.get("status", "candidate")

    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO proactive_events (
                event_id, event_type, source, related_task_id, related_goal_id,
                condition_id, trigger_condition, context_snapshot, priority,
                confidence, created_at, expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev_id, ev_type, source, rel_task, rel_goal,
                cond_id, trig_cond, ctx_snap, prio,
                conf, created_at, expires_at, status
            )
        )
        await db.commit()


def _format_event_row(r: aiosqlite.Row) -> dict:
    trig_cond_raw = r["trigger_condition"]
    try:
        trig_cond = json.loads(trig_cond_raw) if trig_cond_raw else {}
    except Exception:
        trig_cond = {}
    ctx_raw = r["context_snapshot"]
    try:
        ctx = json.loads(ctx_raw) if ctx_raw else {}
    except Exception:
        ctx = {}
    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "source": r["source"],
        "related_task_id": r["related_task_id"],
        "related_goal_id": r["related_goal_id"],
        "condition_id": r["condition_id"],
        "trigger_condition": trig_cond,
        "context_snapshot": ctx,
        "priority": r["priority"],
        "confidence": r["confidence"],
        "created_at": r["created_at"],
        "expires_at": r["expires_at"],
        "status": r["status"]
    }


async def get_proactive_event(event_id: str) -> Optional[dict]:
    """Retrieve a proactive event by ID."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM proactive_events WHERE event_id = ?", (event_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _format_event_row(row)


async def list_proactive_events(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """List proactive events, optionally filtered by status."""
    query = "SELECT * FROM proactive_events"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return [_format_event_row(r) for r in rows]


async def update_proactive_event_status(event_id: str, status: str) -> bool:
    """Update status of a proactive event (e.g. candidate -> dismissed)."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE proactive_events SET status = ? WHERE event_id = ?",
            (status, event_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def bound_proactive_events(max_events: int = 100) -> None:
    """Bound the total number of proactive events in history."""
    async with get_db() as db:
        await db.execute(
            """
            DELETE FROM proactive_events WHERE event_id NOT IN (
                SELECT event_id FROM proactive_events ORDER BY created_at DESC LIMIT ?
            )
            """,
            (max_events,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Phase 8C: Delivery Decisions
# ---------------------------------------------------------------------------

async def save_delivery_decision(decision_data: dict) -> None:
    """Save or update a delivery decision."""
    now = datetime.now(timezone.utc).isoformat()
    dec_id = decision_data["decision_id"]
    ev_id = decision_data["proactive_event_id"]
    target_d = decision_data.get("target_device_id")
    chan = decision_data.get("channel", "desktop_notification")
    timing = decision_data.get("timing", "now")
    reason = decision_data.get("reason", "")
    conf = float(decision_data.get("confidence", 1.0))
    score = float(decision_data.get("score", 0.5))
    created = decision_data.get("created_at", now)
    deliver_after = decision_data.get("deliver_after")
    expires_at = decision_data.get("expires_at")
    status = decision_data.get("status", "pending")

    async with get_db() as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO delivery_decisions (
                decision_id, proactive_event_id, target_device_id, channel,
                timing, reason, confidence, score, created_at, deliver_after,
                expires_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dec_id, ev_id, target_d, chan,
                timing, reason, conf, score, created, deliver_after,
                expires_at, status
            )
        )
        await db.commit()


async def get_delivery_decision(decision_id: str) -> Optional[dict]:
    """Retrieve a delivery decision by ID."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM delivery_decisions WHERE decision_id = ?", (decision_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_delivery_decisions(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """List delivery decisions, optionally filtered by status."""
    query = "SELECT * FROM delivery_decisions"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_delivery_decision_status(decision_id: str, status: str, updates: Optional[dict] = None) -> bool:
    """Update status and optional fields of a delivery decision."""
    set_clauses = ["status = ?"]
    values = [status]
    if updates:
        for k, v in updates.items():
            set_clauses.append(f"{k} = ?")
            values.append(v)
    values.append(decision_id)
    query = f"UPDATE delivery_decisions SET {', '.join(set_clauses)} WHERE decision_id = ?"
    async with get_db() as db:
        cursor = await db.execute(query, tuple(values))
        await db.commit()
        return cursor.rowcount > 0


async def delete_delivery_decision(decision_id: str) -> bool:
    """Delete a delivery decision by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM delivery_decisions WHERE decision_id = ?", (decision_id,))
        await db.commit()
        return cursor.rowcount > 0


async def bound_delivery_decisions(max_decisions: int = 100) -> None:
    """Bound the total number of delivery decisions in history."""
    async with get_db() as db:
        await db.execute(
            """
            DELETE FROM delivery_decisions WHERE decision_id NOT IN (
                SELECT decision_id FROM delivery_decisions ORDER BY created_at DESC LIMIT ?
            )
            """,
            (max_decisions,)
        )
        await db.commit()







# ---------------------------------------------------------------------------
# Phase 8E: Work Items (Academic + Workload Context)
# ---------------------------------------------------------------------------

async def save_work_item(item: dict) -> None:
    """Save or update a WorkItem in SQLite."""
    query = """
    INSERT INTO work_items (
        work_item_id, title, type, course_or_project, source, due_at,
        estimated_minutes, remaining_minutes, priority, status, confidence,
        notes, created_at, updated_at, related_task_id, related_calendar_event_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(work_item_id) DO UPDATE SET
        title = excluded.title,
        type = excluded.type,
        course_or_project = excluded.course_or_project,
        source = excluded.source,
        due_at = excluded.due_at,
        estimated_minutes = excluded.estimated_minutes,
        remaining_minutes = excluded.remaining_minutes,
        priority = excluded.priority,
        status = excluded.status,
        confidence = excluded.confidence,
        notes = excluded.notes,
        updated_at = excluded.updated_at,
        related_task_id = excluded.related_task_id,
        related_calendar_event_id = excluded.related_calendar_event_id
    """
    params = (
        item.get("work_item_id"),
        item.get("title", ""),
        item.get("type", "assignment"),
        item.get("course_or_project", ""),
        item.get("source", "user"),
        item.get("due_at", ""),
        int(item.get("estimated_minutes", 0)),
        int(item.get("remaining_minutes", 0)),
        int(item.get("priority", 3)),
        item.get("status", "pending"),
        float(item.get("confidence", 1.0)),
        item.get("notes", ""),
        item.get("created_at", datetime.now(timezone.utc).isoformat()),
        item.get("updated_at", datetime.now(timezone.utc).isoformat()),
        item.get("related_task_id", ""),
        item.get("related_calendar_event_id", "")
    )
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_work_item(work_item_id: str) -> Optional[dict]:
    """Retrieve a WorkItem by ID."""
    query = "SELECT * FROM work_items WHERE work_item_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (work_item_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_work_items(status: Optional[str] = None, include_completed: bool = False) -> List[dict]:
    """List WorkItems with optional status filter."""
    query = "SELECT * FROM work_items"
    params = []
    conditions = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    elif not include_completed:
        conditions.append("status != 'completed'")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY due_at ASC, priority DESC"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_work_item(work_item_id: str, updates: dict) -> Optional[dict]:
    """Update fields on a WorkItem."""
    if not updates:
        return await get_work_item(work_item_id)
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clauses = [f"{k} = ?" for k in updates.keys()]
    values = list(updates.values()) + [work_item_id]
    query = f"UPDATE work_items SET {', '.join(set_clauses)} WHERE work_item_id = ?"
    async with get_db() as db:
        await db.execute(query, tuple(values))
        await db.commit()
    return await get_work_item(work_item_id)


async def delete_work_item(work_item_id: str) -> bool:
    """Delete a WorkItem by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM work_items WHERE work_item_id = ?", (work_item_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 8F: Learned Routines
# ---------------------------------------------------------------------------

async def save_learned_routine(routine: dict) -> None:
    """Save or update a LearnedRoutine in SQLite."""
    query = """
    INSERT INTO learned_routines (
        routine_id, label, category, days_of_week, typical_start_time,
        typical_end_time, confidence, observation_count, source,
        last_observed_at, metadata
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(routine_id) DO UPDATE SET
        label = excluded.label,
        category = excluded.category,
        days_of_week = excluded.days_of_week,
        typical_start_time = excluded.typical_start_time,
        typical_end_time = excluded.typical_end_time,
        confidence = excluded.confidence,
        observation_count = excluded.observation_count,
        source = excluded.source,
        last_observed_at = excluded.last_observed_at,
        metadata = excluded.metadata
    """
    days = routine.get("days_of_week", [])
    if isinstance(days, (list, tuple, set)):
        days_str = json.dumps(list(days))
    else:
        days_str = str(days)

    meta = routine.get("metadata", {})
    if isinstance(meta, dict):
        meta_str = json.dumps(meta)
    else:
        meta_str = str(meta)

    params = (
        routine.get("routine_id"),
        routine.get("label", ""),
        routine.get("category", "personal"),
        days_str,
        routine.get("typical_start_time", "09:00"),
        routine.get("typical_end_time", "10:00"),
        float(routine.get("confidence", 0.8)),
        int(routine.get("observation_count", 3)),
        routine.get("source", "calendar"),
        routine.get("last_observed_at", datetime.now(timezone.utc).isoformat()),
        meta_str
    )
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_learned_routine(routine_id: str) -> Optional[dict]:
    """Retrieve a LearnedRoutine by ID."""
    query = "SELECT * FROM learned_routines WHERE routine_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (routine_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["days_of_week"] = json.loads(res["days_of_week"])
            except Exception:
                res["days_of_week"] = []
            try:
                res["metadata"] = json.loads(res["metadata"])
            except Exception:
                res["metadata"] = {}
            return res


async def list_learned_routines(category: Optional[str] = None) -> List[dict]:
    """List all LearnedRoutines with optional category filter."""
    query = "SELECT * FROM learned_routines"
    params = []
    if category:
        query += " WHERE category = ?"
        params.append(category)
    query += " ORDER BY typical_start_time ASC, confidence DESC"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["days_of_week"] = json.loads(item["days_of_week"])
                except Exception:
                    item["days_of_week"] = []
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except Exception:
                    item["metadata"] = {}
                out.append(item)
            return out


async def delete_learned_routine(routine_id: str) -> bool:
    """Delete a LearnedRoutine by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM learned_routines WHERE routine_id = ?", (routine_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 9A: Persistent Goals & Steps
# ---------------------------------------------------------------------------

async def save_goal(goal: dict) -> None:
    """Insert or update a Goal record."""
    query = """
        INSERT INTO goals (
            goal_id, title, original_request, source, status, priority,
            created_at, updated_at, started_at, completed_at,
            current_step_index, max_steps, failure_count, max_failures,
            max_runtime_seconds, consecutive_failures, max_consecutive_failures,
            replan_count, max_replans,
            checkpoint_data, waiting_reason, blocking_reason,
            approval_request_id, result_summary, metadata
        ) VALUES (
            :goal_id, :title, :original_request, :source, :status, :priority,
            :created_at, :updated_at, :started_at, :completed_at,
            :current_step_index, :max_steps, :failure_count, :max_failures,
            :max_runtime_seconds, :consecutive_failures, :max_consecutive_failures,
            :replan_count, :max_replans,
            :checkpoint_data, :waiting_reason, :blocking_reason,
            :approval_request_id, :result_summary, :metadata
        )
        ON CONFLICT(goal_id) DO UPDATE SET
            title = excluded.title,
            original_request = excluded.original_request,
            source = excluded.source,
            status = excluded.status,
            priority = excluded.priority,
            updated_at = excluded.updated_at,
            started_at = excluded.started_at,
            completed_at = excluded.completed_at,
            current_step_index = excluded.current_step_index,
            max_steps = excluded.max_steps,
            failure_count = excluded.failure_count,
            max_failures = excluded.max_failures,
            max_runtime_seconds = excluded.max_runtime_seconds,
            consecutive_failures = excluded.consecutive_failures,
            max_consecutive_failures = excluded.max_consecutive_failures,
            replan_count = excluded.replan_count,
            max_replans = excluded.max_replans,
            checkpoint_data = excluded.checkpoint_data,
            waiting_reason = excluded.waiting_reason,
            blocking_reason = excluded.blocking_reason,
            approval_request_id = excluded.approval_request_id,
            result_summary = excluded.result_summary,
            metadata = excluded.metadata
    """
    params = dict(goal)
    params.setdefault("max_runtime_seconds", 3600)
    params.setdefault("consecutive_failures", 0)
    params.setdefault("max_consecutive_failures", 3)
    params.setdefault("replan_count", 0)
    params.setdefault("max_replans", 2)
    if isinstance(params.get("checkpoint_data"), (dict, list)):
        params["checkpoint_data"] = json.dumps(params["checkpoint_data"])
    if isinstance(params.get("metadata"), (dict, list)):
        params["metadata"] = json.dumps(params["metadata"])
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_goal(goal_id: str) -> Optional[dict]:
    """Retrieve a Goal by goal_id."""
    query = "SELECT * FROM goals WHERE goal_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (goal_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["checkpoint_data"] = json.loads(res["checkpoint_data"])
            except Exception:
                res["checkpoint_data"] = {}
            try:
                res["metadata"] = json.loads(res["metadata"])
            except Exception:
                res["metadata"] = {}
            if "max_runtime_seconds" in res and res["max_runtime_seconds"] is not None:
                res["max_runtime_seconds"] = int(res["max_runtime_seconds"])
            res["consecutive_failures"] = int(res.get("consecutive_failures", 0))
            res["max_consecutive_failures"] = int(res.get("max_consecutive_failures", 3))
            res["replan_count"] = int(res.get("replan_count", 0))
            res["max_replans"] = int(res.get("max_replans", 2))
            return res


async def list_goals(status: Optional[str] = None) -> List[dict]:
    """List goals, optionally filtered by status, ordered deterministically by created_at ASC."""
    query = "SELECT * FROM goals"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at ASC"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["checkpoint_data"] = json.loads(item["checkpoint_data"])
                except Exception:
                    item["checkpoint_data"] = {}
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except Exception:
                    item["metadata"] = {}
                if "max_runtime_seconds" in item and item["max_runtime_seconds"] is not None:
                    item["max_runtime_seconds"] = int(item["max_runtime_seconds"])
                item["consecutive_failures"] = int(item.get("consecutive_failures", 0))
                item["max_consecutive_failures"] = int(item.get("max_consecutive_failures", 3))
                item["replan_count"] = int(item.get("replan_count", 0))
                item["max_replans"] = int(item.get("max_replans", 2))
                out.append(item)
            return out


async def delete_goal(goal_id: str) -> bool:
    """Delete a Goal (and cascades to its steps)."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
        await db.commit()
        return cursor.rowcount > 0


async def save_goal_step(step: dict) -> None:
    """Insert or update a GoalStep record."""
    query = """
        INSERT INTO goal_steps (
            step_id, goal_id, step_index, description, capability,
            arguments, status, created_at, started_at, completed_at,
            result, error, checkpoint_data
        ) VALUES (
            :step_id, :goal_id, :step_index, :description, :capability,
            :arguments, :status, :created_at, :started_at, :completed_at,
            :result, :error, :checkpoint_data
        )
        ON CONFLICT(step_id) DO UPDATE SET
            goal_id = excluded.goal_id,
            step_index = excluded.step_index,
            description = excluded.description,
            capability = excluded.capability,
            arguments = excluded.arguments,
            status = excluded.status,
            started_at = excluded.started_at,
            completed_at = excluded.completed_at,
            result = excluded.result,
            error = excluded.error,
            checkpoint_data = excluded.checkpoint_data
    """
    params = dict(step)
    if "index" in params and "step_index" not in params:
        params["step_index"] = params["index"]
    if isinstance(params.get("arguments"), (dict, list)):
        params["arguments"] = json.dumps(params["arguments"])
    if isinstance(params.get("result"), (dict, list)):
        params["result"] = json.dumps(params["result"])
    if isinstance(params.get("checkpoint_data"), (dict, list)):
        params["checkpoint_data"] = json.dumps(params["checkpoint_data"])
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_goal_step(step_id: str) -> Optional[dict]:
    """Retrieve a GoalStep by step_id."""
    query = "SELECT * FROM goal_steps WHERE step_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (step_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            res["index"] = res["step_index"]
            try:
                res["arguments"] = json.loads(res["arguments"])
            except Exception:
                res["arguments"] = {}
            if res.get("result"):
                try:
                    res["result"] = json.loads(res["result"])
                except Exception:
                    pass
            try:
                res["checkpoint_data"] = json.loads(res["checkpoint_data"])
            except Exception:
                res["checkpoint_data"] = {}
            return res


async def list_goal_steps(goal_id: str) -> List[dict]:
    """List all steps for a goal ordered deterministically by step_index ASC."""
    query = "SELECT * FROM goal_steps WHERE goal_id = ? ORDER BY step_index ASC"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (goal_id,)) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["index"] = item["step_index"]
                try:
                    item["arguments"] = json.loads(item["arguments"])
                except Exception:
                    item["arguments"] = {}
                if item.get("result"):
                    try:
                        item["result"] = json.loads(item["result"])
                    except Exception:
                        pass
                try:
                    item["checkpoint_data"] = json.loads(item["checkpoint_data"])
                except Exception:
                    item["checkpoint_data"] = {}
                out.append(item)
            return out


async def delete_goal_step(step_id: str) -> bool:
    """Delete a GoalStep by step_id."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM goal_steps WHERE step_id = ?", (step_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 9C: Wait Conditions Persistence
# ---------------------------------------------------------------------------

async def save_wait_condition(wait: dict) -> None:
    """Insert or update a WaitCondition record."""
    query = """
        INSERT INTO wait_conditions (
            wait_id, goal_id, step_id, condition_type, condition_key,
            operator, expected_value, status, created_at, satisfied_at, metadata
        ) VALUES (
            :wait_id, :goal_id, :step_id, :condition_type, :condition_key,
            :operator, :expected_value, :status, :created_at, :satisfied_at, :metadata
        )
        ON CONFLICT(wait_id) DO UPDATE SET
            goal_id = excluded.goal_id,
            step_id = excluded.step_id,
            condition_type = excluded.condition_type,
            condition_key = excluded.condition_key,
            operator = excluded.operator,
            expected_value = excluded.expected_value,
            status = excluded.status,
            satisfied_at = excluded.satisfied_at,
            metadata = excluded.metadata
    """
    params = dict(wait)
    if isinstance(params.get("expected_value"), (dict, list)):
        params["expected_value"] = json.dumps(params["expected_value"])
    elif params.get("expected_value") is not None:
        params["expected_value"] = str(params["expected_value"])
    else:
        params["expected_value"] = ""

    if isinstance(params.get("metadata"), (dict, list)):
        params["metadata"] = json.dumps(params["metadata"])
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_wait_condition(wait_id: str) -> Optional[dict]:
    """Retrieve a WaitCondition by wait_id."""
    query = "SELECT * FROM wait_conditions WHERE wait_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (wait_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["metadata"] = json.loads(res["metadata"])
            except Exception:
                res["metadata"] = {}
            if res.get("expected_value") and str(res["expected_value"]).startswith(("{", "[")):
                try:
                    res["expected_value"] = json.loads(res["expected_value"])
                except Exception:
                    pass
            return res


async def list_wait_conditions(goal_id: Optional[str] = None, status: Optional[str] = None) -> List[dict]:
    """List wait conditions, optionally filtered by goal_id and/or status."""
    query = "SELECT * FROM wait_conditions"
    params = []
    clauses = []
    if goal_id:
        clauses.append("goal_id = ?")
        params.append(goal_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at ASC"

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except Exception:
                    item["metadata"] = {}
                if item.get("expected_value") and str(item["expected_value"]).startswith(("{", "[")):
                    try:
                        item["expected_value"] = json.loads(item["expected_value"])
                    except Exception:
                        pass
                out.append(item)
            return out


async def delete_wait_condition(wait_id: str) -> bool:
    """Delete a WaitCondition by wait_id."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM wait_conditions WHERE wait_id = ?", (wait_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase 9D: Autonomy State Persistence
# ---------------------------------------------------------------------------

async def set_autonomy_state(key: str, value: str) -> None:
    """Set a key-value pair in autonomy_state."""
    query = """
        INSERT INTO autonomy_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """
    async with get_db() as db:
        await db.execute(query, (key, value))
        await db.commit()


async def get_autonomy_state(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve a value by key from autonomy_state."""
    query = "SELECT value FROM autonomy_state WHERE key = ?"
    async with get_db() as db:
        async with db.execute(query, (key,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            return default


# ---------------------------------------------------------------------------
# Phase A6: Task Executions Persistence
# ---------------------------------------------------------------------------

def _unpack_task_execution_row(res: Dict[str, Any]) -> None:
    res["movable"] = bool(res.get("movable", 1))
    for key in ("required_capabilities", "required_resources"):
        try:
            res[key] = json.loads(res.get(key) or "[]")
        except Exception:
            res[key] = []
    for key in ("required_data_locations", "metadata"):
        try:
            res[key] = json.loads(res.get(key) or "{}")
        except Exception:
            res[key] = {}


async def save_task_execution(execution_data: Dict[str, Any]) -> None:
    """Insert or update a task_executions record."""
    params = dict(execution_data)
    if isinstance(params.get("required_capabilities"), (list, set, tuple)):
        params["required_capabilities"] = json.dumps(list(params["required_capabilities"]))
    if isinstance(params.get("required_resources"), (list, set, tuple)):
        params["required_resources"] = json.dumps(list(params["required_resources"]))
    if isinstance(params.get("required_data_locations"), dict):
        params["required_data_locations"] = json.dumps(params["required_data_locations"])
    if isinstance(params.get("metadata"), dict):
        params["metadata"] = json.dumps(params["metadata"])
    if "movable" in params:
        params["movable"] = 1 if params["movable"] else 0

    now_iso = datetime.now(timezone.utc).isoformat()
    if not params.get("created_at"):
        params["created_at"] = now_iso
    params["updated_at"] = now_iso

    query = """
        INSERT INTO task_executions (
            task_execution_id, goal_id, step_id, owner_device_id, executor_device_id,
            lease_id, lease_acquired_at, lease_expires_at, execution_state,
            required_capabilities, required_resources, required_data_locations,
            movable, checkpoint_id, attempt, generation, created_at, updated_at, metadata
        )
        VALUES (
            :task_execution_id, :goal_id, :step_id, :owner_device_id, :executor_device_id,
            :lease_id, :lease_acquired_at, :lease_expires_at, :execution_state,
            :required_capabilities, :required_resources, :required_data_locations,
            :movable, :checkpoint_id, :attempt, :generation, :created_at, :updated_at, :metadata
        )
        ON CONFLICT(task_execution_id) DO UPDATE SET
            executor_device_id = excluded.executor_device_id,
            lease_id = excluded.lease_id,
            lease_acquired_at = excluded.lease_acquired_at,
            lease_expires_at = excluded.lease_expires_at,
            execution_state = excluded.execution_state,
            required_capabilities = excluded.required_capabilities,
            required_resources = excluded.required_resources,
            required_data_locations = excluded.required_data_locations,
            movable = excluded.movable,
            checkpoint_id = excluded.checkpoint_id,
            attempt = excluded.attempt,
            generation = excluded.generation,
            updated_at = excluded.updated_at,
            metadata = excluded.metadata
    """
    async with get_db() as db:
        await db.execute(query, params)
        await db.commit()


async def get_task_execution(task_execution_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a TaskExecution record by ID."""
    query = "SELECT * FROM task_executions WHERE task_execution_id = ?"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (task_execution_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            _unpack_task_execution_row(res)
            return res


async def get_task_execution_by_step(goal_id: str, step_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a TaskExecution record by goal_id and step_id."""
    query = "SELECT * FROM task_executions WHERE goal_id = ? AND step_id = ? ORDER BY generation DESC, attempt DESC LIMIT 1"
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, (goal_id, step_id)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            _unpack_task_execution_row(res)
            return res


async def list_task_executions(
    goal_id: Optional[str] = None,
    executor_device_id: Optional[str] = None,
    state: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List TaskExecution records matching filters."""
    query = "SELECT * FROM task_executions WHERE 1=1"
    params: List[Any] = []
    if goal_id:
        query += " AND goal_id = ?"
        params.append(goal_id)
    if executor_device_id:
        query += " AND executor_device_id = ?"
        params.append(executor_device_id)
    if state:
        query += " AND execution_state = ?"
        params.append(state)
    query += " ORDER BY updated_at DESC"

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                _unpack_task_execution_row(item)
                out.append(item)
            return out


async def delete_task_execution(task_execution_id: str) -> bool:
    """Delete a TaskExecution record."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM task_executions WHERE task_execution_id = ?", (task_execution_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Phase A7: Privacy Policies
# ---------------------------------------------------------------------------

async def save_privacy_policy(key: str, value: Any) -> None:
    """Save or update a privacy policy key-value setting."""
    val_json = json.dumps(value) if not isinstance(value, str) else value
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO privacy_policies (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, val_json, now_iso),
        )
        await db.commit()


async def get_privacy_policy(key: str) -> Optional[Any]:
    """Retrieve a single privacy policy value, parsed from JSON if applicable."""
    async with get_db() as db:
        async with db.execute("SELECT value FROM privacy_policies WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            val = row[0]
            try:
                return json.loads(val)
            except Exception:
                return val


async def list_privacy_policies() -> Dict[str, Any]:
    """Retrieve all privacy policy key-value settings."""
    async with get_db() as db:
        async with db.execute("SELECT key, value FROM privacy_policies") as cursor:
            rows = await cursor.fetchall()
            out = {}
            for k, v in rows:
                try:
                    out[k] = json.loads(v)
                except Exception:
                    out[k] = v
            return out


# ---------------------------------------------------------------------------
# Phase A8: File Transfers, Locality & Tickets
# ---------------------------------------------------------------------------

def _unpack_transfer_row(item: Dict[str, Any]) -> None:
    if "metadata" in item and isinstance(item["metadata"], str):
        try:
            item["metadata"] = json.loads(item["metadata"])
        except Exception:
            item["metadata"] = {}
    if "resumable" in item:
        item["resumable"] = bool(item["resumable"])


async def save_transfer_session(data: Dict[str, Any]) -> None:
    """Save or update a TransferSession record in SQLite."""
    transfer_id = data["transfer_id"]
    source_device_id = data["source_device_id"]
    destination_device_id = data["destination_device_id"]
    source_path = data["source_path"]
    destination_path = data["destination_path"]
    filename = data.get("filename", "")
    total_bytes = int(data.get("total_bytes", 0))
    transferred_bytes = int(data.get("transferred_bytes", 0))
    chunk_size = int(data.get("chunk_size", 65536))
    checksum_algorithm = data.get("checksum_algorithm", "sha256")
    source_checksum = data.get("source_checksum", "")
    destination_checksum = data.get("destination_checksum", "")
    transport = data.get("transport", "lan")
    state = data.get("state", "PENDING")
    resumable = 1 if data.get("resumable", True) else 0
    encryption = data.get("encryption", "tls_1_3")
    created_at = data.get("created_at") or datetime.now(timezone.utc).isoformat()
    started_at = data.get("started_at")
    completed_at = data.get("completed_at")
    last_progress_at = data.get("last_progress_at")
    retry_count = int(data.get("retry_count", 0))
    privacy_class = data.get("privacy_class", "INTERNAL")
    meta_json = json.dumps(data.get("metadata") or {})

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO file_transfers (
                transfer_id, source_device_id, destination_device_id, source_path,
                destination_path, filename, total_bytes, transferred_bytes, chunk_size,
                checksum_algorithm, source_checksum, destination_checksum, transport,
                state, resumable, encryption, created_at, started_at, completed_at,
                last_progress_at, retry_count, privacy_class, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transfer_id) DO UPDATE SET
                source_device_id = excluded.source_device_id,
                destination_device_id = excluded.destination_device_id,
                source_path = excluded.source_path,
                destination_path = excluded.destination_path,
                filename = excluded.filename,
                total_bytes = excluded.total_bytes,
                transferred_bytes = excluded.transferred_bytes,
                chunk_size = excluded.chunk_size,
                checksum_algorithm = excluded.checksum_algorithm,
                source_checksum = excluded.source_checksum,
                destination_checksum = excluded.destination_checksum,
                transport = excluded.transport,
                state = excluded.state,
                resumable = excluded.resumable,
                encryption = excluded.encryption,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                last_progress_at = excluded.last_progress_at,
                retry_count = excluded.retry_count,
                privacy_class = excluded.privacy_class,
                metadata = excluded.metadata
            """,
            (
                transfer_id, source_device_id, destination_device_id, source_path,
                destination_path, filename, total_bytes, transferred_bytes, chunk_size,
                checksum_algorithm, source_checksum, destination_checksum, transport,
                state, resumable, encryption, created_at, started_at, completed_at,
                last_progress_at, retry_count, privacy_class, meta_json,
            ),
        )
        await db.commit()


async def get_transfer_session(transfer_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single TransferSession by transfer_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM file_transfers WHERE transfer_id = ?", (transfer_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            _unpack_transfer_row(item)
            return item


async def list_transfer_sessions(
    source_device_id: Optional[str] = None,
    destination_device_id: Optional[str] = None,
    state: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query TransferSessions with optional filters."""
    query = "SELECT * FROM file_transfers WHERE 1=1"
    params: List[Any] = []
    if source_device_id:
        query += " AND source_device_id = ?"
        params.append(source_device_id)
    if destination_device_id:
        query += " AND destination_device_id = ?"
        params.append(destination_device_id)
    if state:
        query += " AND state = ?"
        params.append(state)
    query += " ORDER BY created_at DESC"

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                _unpack_transfer_row(item)
                out.append(item)
            return out


async def delete_transfer_session(transfer_id: str) -> bool:
    """Delete a transfer session record."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM file_transfers WHERE transfer_id = ?", (transfer_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# File Locality Records
# ---------------------------------------------------------------------------

def _unpack_locality_row(item: Dict[str, Any]) -> None:
    if "accessible_locations" in item and isinstance(item["accessible_locations"], str):
        try:
            item["accessible_locations"] = json.loads(item["accessible_locations"])
        except Exception:
            item["accessible_locations"] = []
    if "metadata" in item and isinstance(item["metadata"], str):
        try:
            item["metadata"] = json.loads(item["metadata"])
        except Exception:
            item["metadata"] = {}


async def save_file_locality(data: Dict[str, Any]) -> None:
    """Save or update a FileLocalityRecord in SQLite."""
    file_id = data["file_id"]
    filename = data.get("filename", "")
    canonical_device_id = data["canonical_device_id"]
    canonical_path = data["canonical_path"]
    checksum = data.get("checksum", "")
    total_bytes = int(data.get("total_bytes", 0))
    acc_json = json.dumps(data.get("accessible_locations") or [])
    last_verified_at = data.get("last_verified_at") or datetime.now(timezone.utc).isoformat()
    privacy_class = data.get("privacy_class", "INTERNAL")
    meta_json = json.dumps(data.get("metadata") or {})

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO file_localities (
                file_id, filename, canonical_device_id, canonical_path, checksum,
                total_bytes, accessible_locations, last_verified_at, privacy_class, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                filename = excluded.filename,
                canonical_device_id = excluded.canonical_device_id,
                canonical_path = excluded.canonical_path,
                checksum = excluded.checksum,
                total_bytes = excluded.total_bytes,
                accessible_locations = excluded.accessible_locations,
                last_verified_at = excluded.last_verified_at,
                privacy_class = excluded.privacy_class,
                metadata = excluded.metadata
            """,
            (
                file_id, filename, canonical_device_id, canonical_path, checksum,
                total_bytes, acc_json, last_verified_at, privacy_class, meta_json,
            ),
        )
        await db.commit()


async def get_file_locality(file_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve FileLocalityRecord by file_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM file_localities WHERE file_id = ?", (file_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            _unpack_locality_row(item)
            return item


async def get_file_locality_by_path(device_id: str, path: str) -> Optional[Dict[str, Any]]:
    """Retrieve FileLocalityRecord where canonical path or any replica path matches."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # First check canonical
        async with db.execute(
            "SELECT * FROM file_localities WHERE canonical_device_id = ? AND canonical_path = ?",
            (device_id, path),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                item = dict(row)
                _unpack_locality_row(item)
                return item

        # Next search all localities for replicas
        async with db.execute("SELECT * FROM file_localities") as cursor:
            rows = await cursor.fetchall()
            clean_p = path.replace("\\", "/").lower()
            for r in rows:
                item = dict(r)
                _unpack_locality_row(item)
                for loc in item.get("accessible_locations", []):
                    if (
                        loc.get("device_id") == device_id
                        and loc.get("path", "").replace("\\", "/").lower() == clean_p
                    ):
                        return item
    return None


async def list_file_localities(canonical_device_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all FileLocalityRecords."""
    query = "SELECT * FROM file_localities"
    params: List[Any] = []
    if canonical_device_id:
        query += " WHERE canonical_device_id = ?"
        params.append(canonical_device_id)
    query += " ORDER BY last_verified_at DESC"

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            out = []
            for r in rows:
                item = dict(r)
                _unpack_locality_row(item)
                out.append(item)
            return out


# ---------------------------------------------------------------------------
# Transfer Tickets
# ---------------------------------------------------------------------------

async def save_transfer_ticket(data: Dict[str, Any]) -> None:
    """Persist a short-lived transfer ticket."""
    ticket_id = data["ticket_id"]
    transfer_id = data["transfer_id"]
    source_device_id = data["source_device_id"]
    destination_device_id = data["destination_device_id"]
    token_hash = data["token_hash"]
    issued_at = data.get("issued_at") or datetime.now(timezone.utc).isoformat()
    expires_at = data["expires_at"]
    used = 1 if data.get("used", False) else 0
    meta_json = json.dumps(data.get("metadata") or {})

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO transfer_tickets (
                ticket_id, transfer_id, source_device_id, destination_device_id,
                token_hash, issued_at, expires_at, used, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                used = excluded.used,
                metadata = excluded.metadata
            """,
            (
                ticket_id, transfer_id, source_device_id, destination_device_id,
                token_hash, issued_at, expires_at, used, meta_json,
            ),
        )
        await db.commit()


async def get_transfer_ticket(ticket_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve transfer ticket by ticket_id."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM transfer_tickets WHERE ticket_id = ?", (ticket_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            item = dict(row)
            item["used"] = bool(item.get("used", 0))
            if "metadata" in item and isinstance(item["metadata"], str):
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except Exception:
                    item["metadata"] = {}
            return item


async def mark_transfer_ticket_used(ticket_id: str) -> bool:
    """Mark a transfer ticket as used."""
    async with get_db() as db:
        cursor = await db.execute("UPDATE transfer_tickets SET used = 1 WHERE ticket_id = ?", (ticket_id,))
        await db.commit()
        return cursor.rowcount > 0



