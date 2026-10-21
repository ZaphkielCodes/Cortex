package dev.cortex.companion;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.net.Uri;
import android.os.*;
import android.text.InputType;
import android.view.*;
import android.widget.*;
import java.io.*;
import java.util.*;

public final class MainActivity extends Activity {
    private RuntimeService runtime;
    private boolean bound;
    private NearbyRadio nearby;
    private TextView output;
    private EditText hub, device, secret, command, ssid, password;
    private File exporting;
    private final ServiceConnection connection = new ServiceConnection() {
        public void onServiceConnected(ComponentName name, IBinder binder) {
            runtime = ((RuntimeService.LocalBinder)binder).get();
            runtime.listener = text -> output.setText(text); output.setText(runtime.transcript());
        }
        public void onServiceDisconnected(ComponentName name) { runtime = null; output.setText("Companion service stopped."); }
    };
    private void report(String text) { runOnUiThread(() -> { if (runtime != null) runtime.report(text); else output.append(text + "\n"); }); }
    private EditText field(LinearLayout layout, String hint, boolean hidden) {
        EditText field = new EditText(this); field.setHint(hint); field.setSingleLine(true);
        if (hidden) field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        layout.addView(field); return field;
    }
    private void button(LinearLayout layout, String label, Runnable action) {
        Button button = new Button(this); button.setText(label); button.setOnClickListener(view -> action.run()); layout.addView(button);
    }
    public void onCreate(Bundle saved) {
        super.onCreate(saved);
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(24, 32, 24, 24);
        scroll.addView(layout); setContentView(scroll);
        TextView title = new TextView(this); title.setText("Cortex Companion"); title.setTextSize(26); layout.addView(title);
        hub = field(layout, "Secure hub URL (wss://…)", false);
        device = field(layout, "Enrolled Android device ID", false);
        secret = field(layout, "Enrollment secret (kept in memory)", true);
        button(layout, "Connect", () -> {
            Intent intent = new Intent(this, RuntimeService.class).putExtra("hub", hub.getText().toString().trim())
                .putExtra("device", device.getText().toString().trim()).putExtra("secret", secret.getText().toString());
            startForegroundService(intent); secret.setText("");
        });
        button(layout, "Disconnect", () -> { stopService(new Intent(this, RuntimeService.class)); report("Disconnect requested."); });
        command = field(layout, "Type a request", false);
        button(layout, "Send", () -> { if (runtime != null) { runtime.sendText(command.getText().toString()); command.setText(""); } });
        button(layout, "Review my pending approvals", () -> {
            if (runtime == null) return;
            runtime.pending(items -> {
                if (items.length() == 0) { report("No pending approvals for this device."); return; }
                try {
                    org.json.JSONObject item = items.getJSONObject(0);
                    new AlertDialog.Builder(this).setTitle("Approve Cortex action?")
                        .setMessage(item.optString("command") + "\n" + item.optString("target_device") + "\n" + item.optString("args"))
                        .setPositiveButton("Approve", (dialog, which) -> runtime.resolve(item.optString("command_id"), true))
                        .setNegativeButton("Deny", (dialog, which) -> runtime.resolve(item.optString("command_id"), false)).show();
                } catch (Exception error) { report("Approval details were invalid."); }
            });
        });
        button(layout, "Choose a file to send", () -> startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE).setType("*/*").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), 10));
        button(layout, "Export a received file", this::chooseExport);
        button(layout, "Find nearby Cortex devices", () -> nearby.discover());
        ssid = field(layout, "Windows peer Wi-Fi name", false);
        password = field(layout, "Windows peer Wi-Fi password", true);
        button(layout, "Connect peer Wi-Fi", () -> nearby.connectWifi(ssid.getText().toString(), password.getText().toString(),
            network -> { if (runtime != null) runtime.transfer.network = network; }));
        output = new TextView(this); output.setTextIsSelectable(true); output.setText("Disconnected. No requests are simulated.\n"); layout.addView(output);
        nearby = new NearbyRadio(this, this::report);
        bound = bindService(new Intent(this, RuntimeService.class), connection, BIND_AUTO_CREATE);
        ArrayList<String> permissions = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= 31) permissions.addAll(Arrays.asList(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT));
        if (Build.VERSION.SDK_INT >= 33) { permissions.add(Manifest.permission.NEARBY_WIFI_DEVICES); permissions.add(Manifest.permission.POST_NOTIFICATIONS); }
        else permissions.add(Manifest.permission.ACCESS_FINE_LOCATION);
        requestPermissions(permissions.toArray(new String[0]), 1);
    }
    private void chooseExport() {
        if (runtime == null) return;
        File[] files = runtime.transfer.receivedRoot().listFiles(file -> file.isFile() && !file.getName().startsWith(".cortex-"));
        if (files == null || files.length == 0) { report("No completed received files to export."); return; }
        Arrays.sort(files, Comparator.comparing(File::getName));
        new AlertDialog.Builder(this).setTitle("Received files").setItems(Arrays.stream(files).map(File::getName).toArray(String[]::new), (dialog, index) -> {
            exporting = files[index];
            startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType("application/octet-stream").putExtra(Intent.EXTRA_TITLE, exporting.getName()), 11);
        }).show();
    }
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (request == 10) {
            try {
                getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
                report("Selected source file: " + uri + "\nUse this exact source path in your transfer request. Only explicitly selected files can be read.");
            } catch (Exception error) { report("This file provider did not grant persistent read access."); }
        } else if (request == 11 && exporting != null) {
            File source = exporting;
            new Thread(() -> {
                try (InputStream input = new FileInputStream(source); OutputStream target = getContentResolver().openOutputStream(uri, "w")) {
                    if (target == null) throw new IOException();
                    byte[] buffer = new byte[65536]; int count;
                    while ((count = input.read(buffer)) != -1) target.write(buffer, 0, count);
                    target.flush(); report("Received file exported to the location you chose.");
                } catch (Exception error) { report("Export failed; the verified received copy is still kept in Cortex."); }
            }, "cortex-export").start();
        }
    }
    protected void onDestroy() {
        if (runtime != null) runtime.listener = null;
        if (nearby != null) nearby.close();
        if (bound) unbindService(connection);
        super.onDestroy();
    }
}
