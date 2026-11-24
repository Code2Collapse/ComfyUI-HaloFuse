# Live ComfyUI (L9.345)

"Open Live ComfyUI" runs the real, interactive ComfyUI on a cloud GPU and opens it in a new browser tab.
It is **off by default**. It needs a relay gateway on a public HTTPS address, and the owner decides where that runs.

## What the cloud can do (checked 2026-11-24)

| Question | Answer | Evidence |
|---|---|---|
| (a) Can a job run for a long time as a server? | Yes. The maximum runtime is optional (`SubmitSpec.max_wall_clock_s`). The cloud ends a job only when its output, CPU and GPU are all quiet for the inactivity window (1800 s by default). | `halofuse_bridge/provider/backend.py:58-70`, `halofuse_bridge/config.py:160-171` |
| (b) Can a job accept inbound connections or publish a port? | **No.** ComfyUI listens only on the container's own loopback address, and the cloud publishes no ports. The client API we use has no call for opening a port. | `runtime/runner/halofuse_run.py:27-28`, `runtime/README.md:23`, `halofuse_bridge/provider/cloud_adapter.py` (the full list of client calls) |
| (c) Can a job open outbound connections? | Yes. Real job logs show HTTPS requests to model hosts. | owner log `LOGS_HALOFUSE.md`, line 165 and lines 641-643 (outside this repository) |
| (d) Runtime limit and billing | No fixed limit that we know of: the hold can be up to 86400 s, and this feature caps a session at 4 hours by default (`live_max_runtime_seconds`, 600-86400). The GPU is billed per hour for the whole time the job runs, **idle time included**. | `halofuse_bridge/warm_machine.py:53`, `runtime/USER-GUIDE.md` (queue billing section) |

The client SDK is not installed in this environment, so the evidence comes from the adapter's calls, the image
contract and real job logs. Nothing was tested against the live cloud (no paid requests).

**Result:** the cloud cannot accept inbound connections, but a job can stay up and can dial out. So the container
opens a connection **out** to a relay. The user's PC sits behind NAT, so the container cannot reach the local
bridge. The relay therefore has to be a **public HTTPS server**.

## Design

```
browser ──HTTPS/WSS──> relay gateway <──WSS (agent dials out)── cloud job: halofuse_live.py ──> ComfyUI 127.0.0.1:<port>
   ^                        ^
   └── one-shot link ── HaloFuse bridge (start / status / stop, polls the gateway's /status)
```

- **Bridge** (`halofuse_bridge/live_comfy.py`, `routes_live.py`): one session per ComfyUI user. Each session is its own
  cloud job, so users never share a ComfyUI. Each session gets a unique internal port from 8190-8999. 8188 is never
  used, because it stays the headless render port. The **Render path is unchanged**: a live session never runs on the
  warm machine, so renders never wait behind it.
- **Agent** (`runtime/runner/halofuse_live.py`, in the image): starts ComfyUI on loopback at `LIVE_PORT`, dials the
  gateway with `wss://` only, relays HTTP and WebSockets, reconnects with backoff, and prints a heartbeat so the
  inactivity detector does not end it.
- **Gateway** (`halofuse_bridge/live_gateway.py`): relays each browser request and WebSocket over the agent's single
  connection. It refuses to start without TLS (`--cert/--key`, or `--behind-tls-proxy`) and without the secret.

## Security

- HMAC-SHA256 tokens (`live_proto.py`), each bound to a role, session, user and expiry:
  - agent: the session's maximum runtime + 10 min;
  - viewer link: 2 min;
  - browser cookie: 15 min, renewed while in use; HttpOnly, Secure, SameSite=Lax;
  - bridge status: 1 min.
- Without a valid cookie, nothing is relayed (401). A cookie only reaches its own session. Cookie and Authorization
  headers are never forwarded to ComfyUI.
- The shared secret comes from env `HALOFUSE_LIVE_SECRET` (or the setting `live_gateway_secret`). No view or route
  ever returns it.
- Known limit: the agent token travels in the job's environment for the session's lifetime. It is scoped to one session.

## Lifecycle

- **Start:** start a new session, or reuse this user's active one.
- **Ready:** the session counts as ready once the gateway reports that the agent has connected.
- **Idle auto-shutdown:** uses the warm machine's idle duration (`warm_machine.idle_timeout_seconds`) and its warning
  window (`warm_machine._warn_window`). Relay traffic counts as activity, and so does an open ComfyUI tab, because it
  holds a WebSocket.
- **Max runtime:** the session stops when the maximum runtime is reached.
- **Job ended:** a cloud job that ended closes its session.
- **Cleanup:** after a restart, `recover()` re-checks active sessions. The gateway sweeps sessions whose agent has been
  gone past the grace period or whose token has expired.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `live_comfy_enabled` | false | Turns the button on. |
| `live_gateway_url` | empty | The gateway's `https://` address. |
| `HALOFUSE_LIVE_SECRET` | none | The shared secret. Must be 16 characters or more, and the same on the gateway and the bridge. |
| `live_max_runtime_seconds` | 14400 | Maximum runtime per session. |

## Owner decisions

1. Where the gateway is hosted: a small VPS with TLS, for example. It needs a domain and a certificate. Who pays for it?
2. The published image has to be rebuilt so that it contains `/runner/halofuse_live.py`. Until then, a live job fails
   to start.
3. Whether a live session may instead run on a running warm machine. That would save the cold start, but the machine's
   session queue would then hold renders until Live stops.

## Status

- Tokens, the gateway relay (HTTP, WebSocket text and binary, isolation, 401 and 502 cases), the agent configuration
  guards, and the session manager (ports, reuse, idle, recovery, max runtime): UNIT TESTED / INTEGRATION TESTED on
  loopback, with a fake ComfyUI and a fake cloud.
- Real cloud job, real ComfyUI frontend through the relay, the panel button in a browser: NOT RUN.
