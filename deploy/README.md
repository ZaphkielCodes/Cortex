# Independent coordinator

No server is provisioned by these files. A single coordinator must actually be
running on a machine that remains powered on. When it survives an ASUS shutdown,
hub-owned work can continue; ASUS UI/file actions wait for ASUS. Android is a
worker/client, not an automatic replacement SQLite coordinator.

Run `docker compose -f deploy/compose.yaml up --build -d` on a host you control.
Docker is not installed on the development machine, so this image still needs an
image-build and startup acceptance check. Use the existing bootstrap/enrollment
flow through a private VPN or TLS tunnel. Do not expose port 8000 publicly or
disable peer filtering. Keep exactly one hub process/worker for this database.

To move an existing installation: stop the old coordinator first; take a SQLite
backup using its backup API (not a live copy of only cortex.db while WAL is active);
restore it into the new persistent volume; configure agents' `--hub` endpoint;
start the new coordinator and reconnect agents. Never run two independent hubs
against copied databases. There is no quorum or automatic leader election here.
If no coordinator survives, tasks cannot execute. Existing interrupted-turn
recovery intentionally stops uncertain actions rather than replaying them.

The container defaults to `CORTEX_HOST_LOCALITY=cloud_worker`. An Ollama process
next to a cloud hub is still cloud processing. Private reasoning requires a real
trusted private host or private worker; never relabel a cloud VM as a local device.

# Local reasoning on ASUS

Install Ollama, pull a model you choose, then set:

```
CORTEX_LLM_PROVIDER=ollama
CORTEX_LOCAL_MODEL=<installed model name>
CORTEX_OLLAMA_URL=http://127.0.0.1:11434
CORTEX_HOST_LOCALITY=local_device
```

Setting `CORTEX_LOCAL_MODEL` defaults all reasoning to the private provider.
For semantic privacy screening followed by cloud reasoning only for public and
internal turns, explicitly set `CORTEX_REASONING_ROUTE=auto` and keep the existing cloud provider.
`CORTEX_REASONING_ROUTE=local` keeps all reasoning local. Classification uses the
utterance and bounded conversation history; failure stops before cloud dispatch.
Classification is model inference and must still be evaluated with your chosen
model. Explicit stronger privacy labels always win. No model is installed or
downloaded automatically; the local provider reports unavailable until configured.

# Real direct transfer

On a receiving Windows worker set `CORTEX_RECEIVE_ROOT` to a dedicated folder,
`CORTEX_TRANSFER_BIND` to the intended local/VPN interface IP, and
`CORTEX_TRANSFER_ADVERTISE_HOST` to that same reachable IP. The default listener
is loopback only. Allow only the selected private interface/port through your
firewall. No firewall rule is changed automatically.

Request the canonical `hub.transfer_file` capability with exact enrolled source
and destination IDs, source path, and a destination path relative to that folder.
Approval, both-device permissions, privacy eligibility, and current enrollment
are checked before source/destination commands are sent. TLS pins and expiring
tickets travel over the authenticated control connection. Source bytes go
directly to the receiving worker. Network interruption leaves a private partial
file; retry with the same ticket resumes while the worker session remains alive.
The initial coordinator path has a bounded result wait; it reports unverified
completion if that wait expires. It never deletes the source or reports a move.
