# Cortex Android companion (test APK built; hardware acceptance pending)

Native Java, Android 10/API 29 or later, compile/target API 35. Build with JDK 17,
Gradle 8.11.1, Android SDK platform 35 and build-tools 35.0.0. Open this folder in
Android Studio after you accept Google's SDK terms, then build the debug APK with
`gradle :app:assembleDebug`. No SDK, Gradle wrapper binary, APK or signing key is
bundled in the repository. A 1.2 MB test APK was built separately using the
Robolectric API 35 framework Maven artifact, standalone AAPT2 and D8, and apksig.
Java compilation, DEX packaging, manifest parsing, and APK signature verification
for API 29+ passed. This was not a normal Gradle/Android SDK build and the APK has
not been installed or run on a phone. The standalone tools' Maven POMs declare
open-source licenses; no proprietary SDK license was accepted on your behalf.

Enroll the phone using the existing administrator enrollment flow. Use a WSS hub
endpoint with a normally trusted certificate, ideally through a private VPN. The
app refuses cleartext hub connections because enrollment secrets travel through
that control connection. The secret stays in memory and is not backed up or saved.
The notification's Stop action ends the session. Android may still stop a service;
the app does not claim uninterrupted execution while the phone is powered off.

Implemented source paths:
- Authenticated hub WebSocket, heartbeat, typed requests and correlated results.
- Scoped file picker grants for source files; no broad storage or accessibility access.
- TLS 1.3 transfer send/receive using the Python peer protocol, Android Keystore
  identity, exact paired fingerprint, expiring ticket, bounded chunks, resume and SHA-256.
- Received files remain in private app storage until exported with Android's picker.
- Real BLE discovery/advertising for the shared Cortex UUID. Beacons grant no trust.
- Android peer Wi-Fi connection with OS consent via WifiNetworkSpecifier; file
  sockets use the selected Network, while hub traffic keeps its normal network.

On Windows install the optional `agent/requirements-nearby.txt` packages, then run
`py -m agent.nearby host-direct`. It uses Windows' actual Wi-Fi Direct publisher,
checks STARTED state and keeps the group alive until stopped. Enter its SSID and
password locally in the Android app and approve Android's system prompt. No
credentials or tickets are broadcast over BLE. `py -m agent.nearby scan` performs
real discovery. Missing packages/radio/driver support reports unavailable.

Acceptance still required on an actual Windows/Android pair: normal Gradle/SDK build,
install/start/stop/permission denial, enrollment and reconnect, both transfer
directions, TLS pin mismatch, radio-off, Wi-Fi Direct interface selection, file
larger than RAM budget, disconnect/resume, background behavior, and export. The
initial flow requires manual Wi-Fi credentials and enrollment; automatic BLE
pairing/credential exchange and coordinator migration are not implemented.
