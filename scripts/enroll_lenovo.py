import asyncio
import secrets
import bcrypt
import json
from datetime import datetime, timezone
from hub.database import get_db, init_db, close_db, get_device

async def main():
    await init_db()
    existing = await get_device("lenovo")
    if existing:
        print(f"Lenovo is already enrolled: role={existing['role']}, trust_level={existing.get('trust_level')}")
        # Update role to trusted_worker and trust_level to trusted if needed
        async with get_db() as db:
            await db.execute(
                "UPDATE devices SET role = 'trusted_worker', trust_level = 'trusted', device_type = 'laptop' WHERE device_id = 'lenovo'"
            )
            await db.commit()
        print("Updated existing Lenovo record to role=trusted_worker, trust_level=trusted.")
        return

    # Generate durable 32-byte secret
    secret = secrets.token_hex(32)
    secret_hash = bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    enrolled_at = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO devices (
                device_id, device_name, role, secret_hash, enrolled_at,
                connection_state, platform, device_type, trust_level,
                transports, metadata
            )
            VALUES (?, ?, ?, ?, ?, 'disconnected', 'Windows', 'laptop', 'trusted', ?, ?)
            """,
            (
                "lenovo",
                "Lenovo ThinkPad",
                "trusted_worker",
                secret_hash,
                enrolled_at,
                json.dumps({"websocket": True, "lan": True, "ble": False, "wifi_direct": False, "cloud_relay": False}),
                json.dumps({"durable_node": True}),
            ),
        )
        await db.commit()

    with open("lenovo_secret.txt", "w", encoding="utf-8") as f:
        f.write(secret)

    print("=================================================")
    print("LENOVO ENROLLED SUCCESSFULLY:")
    print("Device ID:   lenovo")
    print("Device Name: Lenovo ThinkPad")
    print("Role:        trusted_worker")
    print("Trust Level: trusted")
    print("Platform:    Windows")
    print(f"Secret:      {secret}")
    print("Saved to:    lenovo_secret.txt")
    print("=================================================")
    await close_db()

if __name__ == "__main__":
    asyncio.run(main())
