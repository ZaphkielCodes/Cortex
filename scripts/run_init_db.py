import asyncio
from hub.database import init_db, close_db, get_db

async def main():
    await init_db()
    async with get_db() as db:
        async with db.execute("PRAGMA table_info(devices)") as cursor:
            cols = [row[1] for row in await cursor.fetchall()]
            print("COLS:", cols)
        async with db.execute("SELECT device_id, device_name, role, trust_level FROM devices") as cursor:
            rows = await cursor.fetchall()
            print("DEVICES:", rows)
    await close_db()

if __name__ == "__main__":
    asyncio.run(main())
