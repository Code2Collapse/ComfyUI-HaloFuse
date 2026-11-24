# Live ComfyUI (L9.345)

"Open Live ComfyUI" runs the real, interactive ComfyUI on a cloud GPU and opens it in a new browser tab.
It is **off by default**. It needs a relay gateway on a public HTTPS address. The owner chose (2026-11-24) to run the
gateway on this computer behind a free Cloudflare quick tunnel: one switch, **Advanced > Live ComfyUI**, does it all
(see "One-click host" below). An external gateway (`live_gateway_url`) still works and wins when it is set.

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

## One-click host (`halofuse_bridge/live_tunnel.py`)

- Turning the switch on (POST `/halofuse/live/host {on:true}`, saved as `live_comfy_enabled`) starts the gateway
  in-process on **127.0.0.1, a free port, plain HTTP**. Plain HTTP is refused on any non-loopback address
  (`check_bind`; also `live_gateway.py --behind-tunnel`).
- cloudflared is found on PATH or in `<HaloFuse settings dir>/tools`. If missing, the panel shows
  **Download cloudflared** (POST `/halofuse/live/host/download`, the only place a download starts): the pinned
  release `2026.10.7` asset for this OS/arch from github.com/cloudflare/cloudflared/releases, SHA-256 checked against
  the release checksums (pinned in the module).
- Quick tunnel: `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<port>`; the address is the
  `https://*.trycloudflare.com` URL it prints. It is restarted (backoff 2-30 s) when it dies, and stopped with the
  bridge. **A quick-tunnel restart gives a new address**: a live session started before it cannot reconnect and
  ends at its idle timeout; start it again. A new quick address can take tens of seconds before DNS resolves.
- Named tunnel (fixed address, needs a Cloudflare account): POST `/halofuse/live/tunnel {token, hostname}` keeps the
  token in the keychain (or env `HALOFUSE_TUNNEL_TOKEN`) and `live_tunnel_hostname` in the settings; cloudflared then
  runs `tunnel --no-autoupdate run` with the token in its environment (never on the command line).
  `{clear:true}` goes back to a quick tunnel.
- The gateway secret is generated on first use (`secrets.token_urlsafe`) and kept in the system keychain; without a
  keychain it lasts until ComfyUI restarts. No route returns it. The bridge polls `/status` on the loopback gateway.
- States shown in the panel, each with its next action: off (turn on), needs cloudflared (Download), downloading /
  starting (wait), ready + address (Open Live ComfyUI), error in words (Try again). The Open button's disabled
  tooltip names exactly what is missing (switch, cloudflared, address starting, GPU not chosen).
- The runner needs no change: `GATEWAY_URL` becomes `wss://<name>.trycloudflare.com/agent`; Cloudflare tunnels
  carry WebSockets.

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
| `live_comfy_enabled` | false | Turns Live ComfyUI on (the Advanced switch writes it). |
| `live_gateway_url` | empty | An external gateway's `https://` address. Empty = the one-click host on this computer. |
| `live_tunnel_hostname` | empty | Named tunnel's fixed host name (with a tunnel token). Empty = quick tunnel. |
| `HALOFUSE_LIVE_SECRET` | none | Only for an external gateway: the shared secret (16+ characters). The one-click host generates its own. |
| `live_max_runtime_seconds` | 14400 | Maximum runtime per session. |

## Owner decisions

1. Hosting: decided 2026-11-24, this computer + Cloudflare quick tunnel (free, no account). A named tunnel gives a
   fixed address that survives restarts.
2. The published image has to be rebuilt so that it contains `/runner/halofuse_live.py`. Until then, a live job fails
   to start.
3. Whether a live session may instead run on a running warm machine. That would save the cold start, but the machine's
   session queue would then hold renders until Live stops.

## Status

- Tokens, the gateway relay (HTTP, WebSocket text and binary, isolation, 401 and 502 cases), the agent configuration
  guards, and the session manager (ports, reuse, idle, recovery, max runtime): UNIT TESTED / INTEGRATION TESTED on
  loopback, with a fake ComfyUI and a fake cloud.
- One-click host: tunnel start / URL parse / restart / stop with a fake cloudflared, loopback refusal (must-fail
  verified), checksum download with a fake opener, panel states: UNIT TESTED. A real cloudflared 2026.10.7 download
  (checksum matched) and quick tunnel answered the gateway's 401 over public HTTPS (INTEGRATION TESTED, evidence
  docs/evidence/L9.345-tunnel/ outside this repository).
- Real cloud job, real ComfyUI frontend through the relay, the panel switch in a browser: NOT RUN.
