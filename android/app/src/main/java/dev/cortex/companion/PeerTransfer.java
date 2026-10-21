package dev.cortex.companion;

import android.content.Context;
import android.net.Network;
import android.net.Uri;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import org.json.JSONObject;
import java.io.*;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.cert.X509Certificate;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import javax.net.ssl.*;

/** Same bounded TLS protocol as shared/peer_transfer.py; no file payload uses BLE. */
final class PeerTransfer implements AutoCloseable {
    static final int CHUNK = 256 * 1024;
    private final Context context;
    private final File root;
    private final Map<String, Grant> grants = new ConcurrentHashMap<>();
    private SSLServerSocket server;
    private String fingerprint;
    volatile Network network;
    private volatile boolean closed;

    private static final class Grant {
        String token, sha256;
        File destination, part;
        long size, expires;
        boolean completed;
    }

    PeerTransfer(Context context) {
        this.context = context;
        root = new File(context.getFilesDir(), "received");
        root.mkdirs();
    }

    static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder();
        for (byte value : bytes) out.append(String.format(Locale.ROOT, "%02x", value & 255));
        return out.toString();
    }

    private static boolean equal(String a, String b) {
        return a != null && b != null && MessageDigest.isEqual(a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
    }

    static JSONObject read(InputStream stream) throws Exception {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        for (int i = 0; i < 8192; i++) {
            int value = stream.read();
            if (value == -1) throw new EOFException();
            if (value == '\n') return new JSONObject(line.toString("UTF-8"));
            line.write(value);
        }
        throw new IOException("Oversized protocol header");
    }

    static void write(OutputStream stream, JSONObject value) throws Exception {
        stream.write((value.toString() + "\n").getBytes(StandardCharsets.UTF_8));
        stream.flush();
    }

    private String checksum(InputStream input) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] chunk = new byte[CHUNK];
        int count;
        while ((count = input.read(chunk)) != -1) digest.update(chunk, 0, count);
        return hex(digest.digest());
    }

    private InputStream openSource(String path) throws Exception {
        Uri uri = Uri.parse(path);
        if (!"content".equals(uri.getScheme())) throw new SecurityException("Select the file with Android's file picker first");
        boolean permitted = false;
        for (android.content.UriPermission permission : context.getContentResolver().getPersistedUriPermissions()) {
            if (permission.isReadPermission() && permission.getUri().equals(uri)) permitted = true;
        }
        if (!permitted) throw new SecurityException("File permission missing");
        InputStream input = context.getContentResolver().openInputStream(uri);
        if (input == null) throw new FileNotFoundException();
        return input;
    }

    JSONObject inspect(String path) throws Exception {
        long size = 0;
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = openSource(path)) {
            byte[] chunk = new byte[CHUNK];
            int count;
            while ((count = input.read(chunk)) != -1) { size += count; digest.update(chunk, 0, count); }
        }
        return new JSONObject().put("size", size).put("sha256", hex(digest.digest()));
    }

    synchronized JSONObject prepare(JSONObject args) throws Exception {
        if (server == null) startReceiver();
        String id = args.getString("transfer_id");
        String token = args.getString("token");
        String relative = args.getString("destination");
        String hash = args.getString("sha256");
        long size = args.getLong("size");
        long expires = (long)(args.getDouble("expires_at") * 1000);
        File destination = new File(root, relative).getCanonicalFile();
        if (!id.matches("[A-Za-z0-9_-]{1,80}") || token.length() < 32 || !hash.matches("[a-f0-9]{64}") ||
                size < 0 || size > (1L << 40) || expires <= System.currentTimeMillis() ||
                expires > System.currentTimeMillis() + 3600000 || relative.startsWith("/") ||
                !destination.getPath().startsWith(root.getCanonicalPath() + File.separator) || destination.exists() || grants.containsKey(id)) {
            throw new SecurityException("Invalid receiving grant");
        }
        Grant grant = new Grant();
        grant.token = token; grant.sha256 = hash; grant.size = size; grant.expires = expires;
        grant.destination = destination; grant.part = new File(root, ".cortex-" + id + ".part");
        grants.put(id, grant);
        // Route selection supplies the actual interface; an unreachable address never counts as success.
        return new JSONObject().put("port", server.getLocalPort()).put("fingerprint", fingerprint)
            .put("host", reachableAddress());
    }

    private String reachableAddress() throws Exception {
        android.net.ConnectivityManager manager = context.getSystemService(android.net.ConnectivityManager.class);
        Network chosen = network != null ? network : manager.getActiveNetwork();
        android.net.LinkProperties properties = manager.getLinkProperties(chosen);
        if (properties != null) for (android.net.LinkAddress address : properties.getLinkAddresses()) {
            if (address.getAddress() instanceof java.net.Inet4Address && !address.getAddress().isLoopbackAddress()) return address.getAddress().getHostAddress();
        }
        throw new IOException("No reachable private interface");
    }

    private void startReceiver() throws Exception {
        KeyStore keys = KeyStore.getInstance("AndroidKeyStore"); keys.load(null);
        String alias = "cortex-peer-tls";
        if (!keys.containsAlias(alias)) {
            KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
            generator.initialize(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                .setDigests(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_SHA512)
                .setCertificateSubject(new javax.security.auth.x500.X500Principal("CN=Cortex paired peer"))
                .setCertificateNotAfter(new Date(System.currentTimeMillis() + 365L * 86400000)).build());
            generator.generateKeyPair();
        }
        fingerprint = hex(MessageDigest.getInstance("SHA-256").digest(keys.getCertificate(alias).getEncoded()));
        KeyManagerFactory managers = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        managers.init(keys, null);
        SSLContext tls = SSLContext.getInstance("TLSv1.3"); tls.init(managers.getKeyManagers(), null, null);
        server = (SSLServerSocket) tls.getServerSocketFactory().createServerSocket(0);
        server.setEnabledProtocols(new String[]{"TLSv1.3"});
        new Thread(() -> {
            while (!closed) {
                try (SSLSocket socket = (SSLSocket)server.accept()) {
                    socket.setSoTimeout(30000); socket.startHandshake();
                    receive(socket);
                } catch (Exception ignored) { /* Peer rejection never means completed. */ }
            }
        }, "cortex-peer-receiver").start();
    }

    private void receive(SSLSocket socket) throws Exception {
        OutputStream output = socket.getOutputStream();
        try {
            JSONObject header = read(socket.getInputStream());
            String id = header.getString("transfer_id");
            Grant grant = grants.get(id);
            if (grant == null || !equal(grant.token, header.optString("token")) || System.currentTimeMillis() >= grant.expires) throw new SecurityException();
            synchronized (grant) {
                String operation = header.getString("operation");
                long offset = grant.part.exists() ? grant.part.length() : 0;
                if (grant.completed && !operation.equals("status")) throw new SecurityException();
                JSONObject result = new JSONObject().put("status", grant.completed ? "complete" : "ready")
                    .put("offset", grant.completed ? grant.size : offset);
                if (grant.completed) result.put("sha256", grant.sha256);
                else if (operation.equals("chunk")) {
                    int length = header.getInt("length");
                    if (length <= 0 || length > CHUNK || header.getLong("offset") != offset || offset + length > grant.size) throw new IOException();
                    byte[] bytes = new byte[length];
                    new DataInputStream(socket.getInputStream()).readFully(bytes);
                    if (!equal(hex(MessageDigest.getInstance("SHA-256").digest(bytes)), header.getString("sha256"))) throw new IOException();
                    try (FileOutputStream stream = new FileOutputStream(grant.part, true)) { stream.write(bytes); stream.getFD().sync(); }
                    result.put("offset", offset + length);
                } else if (operation.equals("finish")) {
                    if (grant.size == 0 && !grant.part.exists()) grant.part.createNewFile();
                    String hash;
                    try (InputStream stream = new FileInputStream(grant.part)) { hash = checksum(stream); }
                    if (offset != grant.size || !equal(hash, grant.sha256)) throw new IOException();
                    grant.destination.getParentFile().mkdirs();
                    // link() is atomic and refuses to overwrite an existing destination.
                    android.system.Os.link(grant.part.getPath(), grant.destination.getPath());
                    grant.part.delete(); grant.completed = true;
                    result.put("status", "complete").put("offset", grant.size).put("sha256", hash);
                } else if (operation.equals("cancel")) {
                    grant.part.delete(); grants.remove(id); result.put("status", "cancelled");
                } else if (!operation.equals("status")) throw new IOException();
                write(output, result);
            }
        } catch (Exception rejected) { write(output, new JSONObject().put("status", "error").put("message", "Transfer rejected")); }
    }

    private JSONObject request(JSONObject args, String operation, byte[] payload, long offset) throws Exception {
        String pin = args.getString("fingerprint");
        if (!pin.matches("[a-f0-9]{64}")) throw new SecurityException("Pairing fingerprint required");
        X509TrustManager trust = new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String auth) throws java.security.cert.CertificateException { throw new java.security.cert.CertificateException(); }
            public void checkServerTrusted(X509Certificate[] chain, String auth) throws java.security.cert.CertificateException {
                try {
                    if (chain.length == 0 || !equal(pin, hex(MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded())))) throw new java.security.cert.CertificateException("Pairing mismatch");
                } catch (GeneralSecurityException error) { throw new java.security.cert.CertificateException(error); }
            }
        };
        SSLContext tls = SSLContext.getInstance("TLSv1.3"); tls.init(null, new TrustManager[]{trust}, null);
        Socket raw = network == null ? new Socket() : network.getSocketFactory().createSocket();
        raw.connect(new java.net.InetSocketAddress(args.getString("host"), args.getInt("port")), 10000);
        try (SSLSocket socket = (SSLSocket)tls.getSocketFactory().createSocket(raw, args.getString("host"), args.getInt("port"), true)) {
            socket.setEnabledProtocols(new String[]{"TLSv1.3"}); socket.setSoTimeout(30000); socket.startHandshake();
            JSONObject header = new JSONObject().put("operation", operation).put("transfer_id", args.getString("transfer_id")).put("token", args.getString("token"));
            if (payload != null) header.put("offset", offset).put("length", payload.length).put("sha256", hex(MessageDigest.getInstance("SHA-256").digest(payload)));
            write(socket.getOutputStream(), header);
            if (payload != null) { socket.getOutputStream().write(payload); socket.getOutputStream().flush(); }
            JSONObject result = read(socket.getInputStream());
            if (result.optString("status").equals("error")) throw new IOException("Peer rejected transfer");
            return result;
        } finally { raw.close(); }
    }

    JSONObject send(JSONObject args) throws Exception {
        String path = args.getString("path");
        JSONObject manifest = inspect(path);
        JSONObject state = request(args, "status", null, 0);
        long offset = state.getLong("offset"), size = manifest.getLong("size");
        if (offset < 0 || offset > size) throw new IOException("Invalid resume offset");
        if (!state.optString("status").equals("complete")) {
            try (InputStream stream = openSource(path)) {
                long remaining = offset;
                byte[] chunk = new byte[CHUNK];
                while (remaining > 0) { int count = stream.read(chunk, 0, (int)Math.min(CHUNK, remaining)); if (count < 0) throw new EOFException(); remaining -= count; }
                int count;
                while ((count = stream.read(chunk)) != -1) {
                    state = request(args, "chunk", Arrays.copyOf(chunk, count), offset); offset += count;
                    if (state.getLong("offset") != offset) throw new IOException("Unacknowledged chunk");
                }
            }
            state = request(args, "finish", null, 0);
        }
        if (!state.getString("status").equals("complete") || state.getLong("offset") != size || !equal(state.getString("sha256"), manifest.getString("sha256"))) throw new IOException("Final integrity mismatch");
        return state;
    }

    void cancel(String id) { Grant grant = grants.get(id); if (grant == null) throw new IllegalStateException("Unknown transfer"); synchronized(grant) { if (grant.completed) throw new IllegalStateException("Already complete"); grants.remove(id); grant.part.delete(); } }
    File receivedRoot() { return root; }
    public void close() throws IOException { closed = true; if (server != null) server.close(); }
}
