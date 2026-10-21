package dev.cortex.companion;

import android.app.Activity;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.net.*;
import android.net.wifi.WifiNetworkSpecifier;
import android.os.ParcelUuid;
import java.util.*;
import java.util.function.Consumer;

/** Real radios with explicit OS consent. Advertisements are untrusted discovery only. */
final class NearbyRadio implements AutoCloseable {
    static final ParcelUuid SERVICE = ParcelUuid.fromString("a435f8de-7ade-4490-9d42-164c9d36b431");
    private final Activity activity;
    private final Consumer<String> report;
    private BluetoothLeScanner scanner;
    private BluetoothLeAdvertiser advertiser;
    private ConnectivityManager.NetworkCallback connection;
    private Consumer<Network> networkConsumer;
    private final Set<String> found = new HashSet<>();
    private final ScanCallback scan = new ScanCallback() {
        public void onScanResult(int type, ScanResult result) {
            String address = result.getDevice().getAddress();
            if (found.add(address)) report.accept("Nearby Cortex beacon " + address + " (untrusted; enroll/pair before transfer)");
        }
        public void onScanFailed(int error) { report.accept("BLE scan unavailable (" + error + ")."); }
    };
    private final AdvertiseCallback advertise = new AdvertiseCallback() {
        public void onStartSuccess(AdvertiseSettings settings) { report.accept("BLE discovery beacon active. No file bytes or credentials are advertised."); }
        public void onStartFailure(int error) { report.accept("BLE advertising unavailable (" + error + ")."); }
    };
    NearbyRadio(Activity activity, Consumer<String> report) { this.activity = activity; this.report = report; }
    void discover() {
        try {
            BluetoothAdapter adapter = activity.getSystemService(BluetoothManager.class).getAdapter();
            if (adapter == null || !adapter.isEnabled()) throw new IllegalStateException();
            scanner = adapter.getBluetoothLeScanner(); advertiser = adapter.getBluetoothLeAdvertiser();
            scanner.startScan(Collections.singletonList(new ScanFilter.Builder().setServiceUuid(SERVICE).build()), new ScanSettings.Builder().build(), scan);
            if (advertiser != null) advertiser.startAdvertising(new AdvertiseSettings.Builder().setConnectable(false).build(), new AdvertiseData.Builder().addServiceUuid(SERVICE).build(), advertise);
            activity.getWindow().getDecorView().postDelayed(this::stopBle, 15000);
            report.accept("Scanning for Cortex for 15 seconds.");
        } catch (Exception error) { report.accept("Nearby radio unavailable. Enable Bluetooth and grant Nearby Devices permission."); }
    }
    void connectWifi(String ssid, String password, Consumer<Network> ready) {
        ConnectivityManager manager = activity.getSystemService(ConnectivityManager.class);
        try {
            if (connection != null) manager.unregisterNetworkCallback(connection);
            networkConsumer = ready;
            WifiNetworkSpecifier peer = new WifiNetworkSpecifier.Builder().setSsid(ssid).setWpa2Passphrase(password).build();
            connection = new ConnectivityManager.NetworkCallback() {
                public void onAvailable(Network network) { ready.accept(network); report.accept("Peer Wi-Fi connected. Hub traffic keeps its normal network."); }
                public void onUnavailable() { report.accept("Peer Wi-Fi connection was declined or unavailable."); }
                public void onLost(Network network) { ready.accept(null); report.accept("Peer Wi-Fi disconnected; file completion remains unverified until checksum confirmation."); }
            };
            manager.requestNetwork(new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).setNetworkSpecifier(peer).build(), connection);
            report.accept("Approve Android's connection prompt for your Windows Wi-Fi Direct group.");
        } catch (Exception error) { report.accept("Peer Wi-Fi unavailable. Check permissions, SSID, password and Windows adapter support."); }
    }
    private void stopBle() {
        try { if (scanner != null) scanner.stopScan(scan); if (advertiser != null) advertiser.stopAdvertising(advertise); } catch (SecurityException ignored) { }
        scanner = null; advertiser = null;
    }
    public void close() {
        stopBle();
        if (connection != null) { try { activity.getSystemService(ConnectivityManager.class).unregisterNetworkCallback(connection); } catch (Exception ignored) { } connection = null; }
        if (networkConsumer != null) networkConsumer.accept(null);
    }
}
