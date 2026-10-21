"""Opt-in real Windows BLE discovery and Wi-Fi Direct autonomous group host.

Discovery carries no tickets, credentials or authorization. The paired TLS/control
connection is still required. Hardware failures are reported rather than simulated.
"""
import argparse
import asyncio
import json
import secrets
from uuid import UUID

SERVICE_UUID = "a435f8de-7ade-4490-9d42-164c9d36b431"


async def discover(timeout=8):
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, service_uuids=[SERVICE_UUID], return_adv=True)
    return [{"address": device.address, "name": advertisement.local_name or "Cortex peer",
             "rssi": advertisement.rssi, "authenticated": False}
            for device, advertisement in found.values()]


async def host_direct():
    from winrt.windows.devices.wifidirect import (WiFiDirectAdvertisementPublisher,
        WiFiDirectAdvertisementPublisherStatus, WiFiDirectAdvertisementListenStateDiscoverability)
    from winrt.windows.devices.bluetooth.advertisement import BluetoothLEAdvertisementPublisher
    publisher = WiFiDirectAdvertisementPublisher()
    publisher.advertisement.is_autonomous_group_owner_enabled = True
    publisher.advertisement.listen_state_discoverability = WiFiDirectAdvertisementListenStateDiscoverability.NORMAL
    settings = publisher.advertisement.legacy_settings
    settings.is_enabled = True
    settings.ssid = "DIRECT-Cortex-" + secrets.token_hex(3)
    settings.passphrase.password = secrets.token_urlsafe(18)
    beacon = BluetoothLEAdvertisementPublisher()
    beacon.advertisement.service_uuids.append(UUID(SERVICE_UUID))
    publisher.start()
    try:
        # Windows publishes asynchronously; never return ready merely because start() returned.
        for _ in range(100):
            if publisher.status == WiFiDirectAdvertisementPublisherStatus.STARTED:
                break
            if publisher.status == WiFiDirectAdvertisementPublisherStatus.ABORTED:
                raise RuntimeError("Windows rejected Wi-Fi Direct; check adapter/driver support")
            await asyncio.sleep(.1)
        else:
            raise TimeoutError("Wi-Fi Direct did not start")
        beacon.start()
        print(json.dumps({"status": "wifi_direct_started", "ssid": settings.ssid,
                          "passphrase": settings.passphrase.password,
                          "note": "Enter these locally on the paired Android device. BLE does not authorize it."}), flush=True)
        # Keep the publisher alive until the user stops this helper.
        await asyncio.Event().wait()
    finally:
        beacon.stop()
        publisher.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["scan", "host-direct"])
    args = parser.parse_args()
    try:
        if args.action == "scan":
            print(json.dumps(asyncio.run(discover())))
        else:
            asyncio.run(host_direct())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Do not echo provider exception text that could include credentials.
        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}))
        raise SystemExit(1)
