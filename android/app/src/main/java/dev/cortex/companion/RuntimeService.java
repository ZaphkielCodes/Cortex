package dev.cortex.companion;

import android.app.*;
import android.content.*;
import android.os.*;
import org.json.*;
import java.util.*;
import java.util.concurrent.*;
import okhttp3.*;

/** Explicit foreground companion session; no cloud-host or background-survival claim. */
public final class RuntimeService extends Service {
    public interface Listener { void update(String text); }
    private final Binder binder = new LocalBinder();
    public final class LocalBinder extends Binder { RuntimeService get() { return RuntimeService.this; } }
    volatile Listener listener;
    PeerTransfer transfer;
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor();
    private final ExecutorService commands = Executors.newSingleThreadExecutor();
    private final OkHttpClient client = new OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).build();
    private WebSocket socket;
    private String hub, device, secret;
    private boolean authenticated, stopped = true;
    private int generation;
    private final LinkedHashMap<String, JSONObject> results = new LinkedHashMap<>();
    private final Set<String> running = new HashSet<>();
    private final StringBuilder transcript = new StringBuilder();

    public void onCreate() { super.onCreate(); transfer = new PeerTransfer(this); }
    public IBinder onBind(Intent intent) { return binder; }
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) return START_NOT_STICKY;
        if ("stop".equals(intent.getAction())) { stopSelf(); return START_NOT_STICKY; }
        try {
            hub = intent.getStringExtra("hub"); device = intent.getStringExtra("device"); secret = intent.getStringExtra("secret");
            java.net.URI uri = new java.net.URI(hub);
            if (!"wss".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null || device == null || !device.matches("[A-Za-z0-9_-]{1,80}") || secret == null || secret.length() < 32) throw new IllegalArgumentException();
            NotificationManager manager = getSystemService(NotificationManager.class);
            manager.createNotificationChannel(new NotificationChannel("runtime", "Cortex device session", NotificationManager.IMPORTANCE_LOW));
            PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, RuntimeService.class).setAction("stop"), PendingIntent.FLAG_IMMUTABLE);
            startForeground(1, new Notification.Builder(this, "runtime").setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("Cortex companion running").setContentText("Connected device session; tap Stop to disconnect")
                .addAction(new Notification.Action.Builder(null, "Stop", stop).build()).build());
            stopped = false; connect();
        } catch (Exception error) { report("Connection needs a WSS hub URL, enrolled device ID and secret."); stopSelf(); }
        return START_NOT_STICKY;
    }

    synchronized String transcript() { return transcript.toString(); }
    synchronized void report(String text) {
        transcript.append(text).append('\n');
        if (transcript.length() > 12000) transcript.delete(0, transcript.length() - 12000);
        Listener target = listener;
        if (target != null) new Handler(getMainLooper()).post(() -> target.update(transcript()));
    }

    private synchronized void connect() {
        final int current = ++generation;
        authenticated = false;
        if (socket != null) socket.cancel();
        socket = client.newWebSocket(new Request.Builder().url(hub.replaceAll("/+$", "") + "/ws/" + device).build(), new WebSocketListener() {
            public void onMessage(WebSocket ws, String text) {
                if (current != generation || stopped) return;
                try {
                    JSONObject message = new JSONObject(text);
                    String type = message.optString("type");
                    if (type.equals("auth_challenge")) {
                        JSONArray caps = new JSONArray(Arrays.asList("device.info", "device.status", "transfer.inspect", "transfer.send", "transfer.prepare", "transfer.cancel"));
                        ws.send(new JSONObject().put("type", "auth_response").put("device_id", device).put("response", secret)
                            .put("platform", "Android").put("device_type", "phone").put("capabilities", caps)
                            .put("transports", new JSONObject().put("websocket", true).put("lan", true).put("ble", false).put("wifi_direct", false)).toString());
                    } else if (type.equals("auth_result")) {
                        authenticated = message.optBoolean("success");
                        report(authenticated ? "Authenticated. Typed commands and direct file transfer are available." : "Authentication rejected. Check enrollment.");
                        if (authenticated) heartbeat(current);
                    } else if (authenticated && type.equals("voice_response")) {
                        report("Cortex: " + message.optString("message"));
                    } else if (authenticated && type.equals("command")) {
                        String id = message.getString("command_id");
                        synchronized (results) {
                            if (results.containsKey(id)) { ws.send(results.get(id).toString()); return; }
                            if (!running.add(id)) return;
                        }
                        commands.execute(() -> execute(ws, current, message));
                    }
                } catch (Exception error) { report("Rejected malformed hub message."); }
            }
            public void onFailure(WebSocket ws, Throwable error, Response response) { disconnected(current); }
            public void onClosed(WebSocket ws, int code, String reason) { disconnected(current); }
        });
    }

    private synchronized void disconnected(int current) {
        if (current != generation || stopped) return;
        authenticated = false; report("Hub disconnected. Retrying; no commands are simulated.");
        timer.schedule(() -> { if (current == generation && !stopped) connect(); }, 5, TimeUnit.SECONDS);
    }

    private void heartbeat(int current) {
        if (current != generation || stopped || !authenticated) return;
        try {
            socket.send(new JSONObject().put("type", "heartbeat").put("device_id", device)
                .put("timestamp", System.currentTimeMillis() / 1000.0).put("agent_version", "android-0.1")
                .put("state", "online").put("active_task_count", running.size()).toString());
        } catch (Exception ignored) { }
        timer.schedule(() -> heartbeat(current), 20, TimeUnit.SECONDS);
    }

    void sendText(String text) {
        if (!authenticated || socket == null) { report("Not connected; request was not sent."); return; }
        try {
            socket.send(new JSONObject().put("type", "voice_request").put("text", text)
                .put("command_id", UUID.randomUUID().toString().replace("-", "")).toString());
            report("You: " + text);
        } catch (Exception error) { report("Request could not be sent."); }
    }

    void pending(java.util.function.Consumer<JSONArray> ready) {
        if (!authenticated) { report("Connect before reviewing approvals."); return; }
        Request request = new Request.Builder().url(hub.replaceFirst("^wss:", "https:").replaceAll("/+$", "") + "/pending")
            .header("X-Cortex-Device", device).header("X-Cortex-Secret", secret).build();
        client.newCall(request).enqueue(new Callback() {
            public void onFailure(Call call, java.io.IOException error) { report("Approval list unavailable."); }
            public void onResponse(Call call, Response response) {
                try (Response value = response) {
                    if (!value.isSuccessful() || value.body() == null) throw new java.io.IOException();
                    JSONArray all = new JSONObject(value.body().string()).getJSONArray("pending"), own = new JSONArray();
                    for (int i = 0; i < all.length(); i++) if (device.equals(all.getJSONObject(i).optString("requested_by"))) own.put(all.getJSONObject(i));
                    new Handler(getMainLooper()).post(() -> ready.accept(own));
                } catch (Exception error) { report("Approval list unavailable."); }
            }
        });
    }

    void resolve(String id, boolean approved) {
        if (!authenticated) { report("Not connected; approval was not sent."); return; }
        try { socket.send(new JSONObject().put("type", "confirmation_response").put("command_id", id)
            .put("decision", approved ? "approve" : "deny").toString()); report("Approval decision sent for verification."); }
        catch (Exception error) { report("Approval decision could not be sent."); }
    }

    private void execute(WebSocket ws, int current, JSONObject message) {
        String id = message.optString("command_id"), command = message.optString("command");
        JSONObject result = new JSONObject();
        try {
            JSONObject args = message.optJSONObject("args");
            if (args == null) args = new JSONObject();
            JSONObject data;
            switch (command) {
                case "device.info": case "device.status":
                    data = new JSONObject().put("platform", "Android").put("model", Build.MODEL).put("sdk", Build.VERSION.SDK_INT); break;
                case "transfer.inspect": data = transfer.inspect(args.getString("path")); break;
                case "transfer.prepare": data = transfer.prepare(args); break;
                case "transfer.send": data = transfer.send(args); break;
                case "transfer.cancel": transfer.cancel(args.getString("transfer_id")); data = new JSONObject().put("cancelled", true); break;
                default: throw new UnsupportedOperationException();
            }
            result.put("status", "success").put("message", "Device operation verified.").put("data", data);
        } catch (Exception error) {
            try { result.put("status", "failure").put("message", "Operation unavailable, rejected or interrupted; completion is unverified."); } catch (JSONException ignored) { }
        }
        try { result.put("type", "result").put("command_id", id).put("command", command); } catch (JSONException ignored) { }
        synchronized (results) {
            running.remove(id); results.put(id, result);
            while (results.size() > 128) results.remove(results.keySet().iterator().next());
        }
        if (generation == current && !stopped) ws.send(result.toString());
        report(command + ": " + result.optString("status"));
    }

    public void onDestroy() {
        stopped = true; authenticated = false; generation++;
        if (socket != null) socket.cancel(); secret = null;
        timer.shutdownNow(); commands.shutdownNow();
        try { transfer.close(); } catch (Exception ignored) { }
        client.dispatcher().executorService().shutdown();
        super.onDestroy();
    }
}
