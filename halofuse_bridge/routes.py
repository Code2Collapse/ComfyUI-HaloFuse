"""HaloFuse HTTP routes on ComfyUI's server: this module only wires the route modules.

Every route lives under /halofuse/* and goes through routes_account.request_guard (loopback peer unless allowed, Host
allowlist, the X-HaloFuse-Request header on writes). Blocking work runs in executors inside the route modules.
"""
from __future__ import annotations

from server import PromptServer

routes = PromptServer.instance.routes


# R-WS1-05: settings, account / credentials, GPU list and estimate live in routes_account (guarded: loopback + custom
# header + Host allowlist; branded, URL-free errors). The old unguarded handlers that stood here are gone (X1: a
# settings POST could redirect the login to another server).
from . import routes_account  # noqa: E402

routes_account.register(routes, prefix="/halofuse")


# R-WS2-05: submit, job, log paging, cancel and queue routes live in routes_jobs (persisted job record, real
# cancellation, timeouts, reattachment on start). register() also runs lifecycle.boot() = reattach.
from . import delivery, lifecycle, routes_delivery  # noqa: E402
# R-WS3-01: delivery is THE terminal hook; it replaces the transitional 0.3.4 image copy
delivery.install()
lifecycle.unregister_terminal_hook("legacy_images")
routes_delivery.register(routes, prefix="/halofuse")  # also resumes interrupted deliveries
from . import routes_jobs  # noqa: E402

routes_jobs.register(routes, prefix="/halofuse")


# R-WS5-01: the /halofuse/models/* + preflight / draft / capacity / library routes, all through
# routes_account.request_guard (the old handlers here had no guard).
from . import routes_preflight  # noqa: E402

routes_preflight.register(routes, prefix="/halofuse")
