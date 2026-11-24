"""Compatibility shims HaloFuse installs into ComfyUI's custom_nodes before ComfyUI starts (owner 2026-11-24).

MiniMax H3 final layer (core 0.46+): FinalLayer.forward gained (sigma, sample_sigmas, grids). Node packs that replace
MiniMaxH3Model._forward with a copy of the older core code (e.g. ComfyUI-MiniMaxH3-Cache, last updated 2026-08-03)
still call final_layer(h, t_emb, video_seg, audio_seg) and crash with
"FinalLayer.forward() missing 3 required positional arguments". For a plain checkpoint (one output head per stream)
those arguments are not used, so the shim fills them in and the result is exactly what core computes. A PDD
checkpoint (several heads per stream) needs the sampler's sigma schedule: the shim raises a plain-English error that
names the outdated pack instead of guessing.

Loaded as a ComfyUI custom node file: never raises at import, registers no nodes.
"""
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _install_minimax_final_layer_shim():
    try:
        import inspect
        import comfy.ldm.minimax.model as mm
    except Exception:  # noqa: BLE001 - no MiniMax in this ComfyUI: nothing to adapt
        return False
    layer = getattr(mm, "FinalLayer", None)
    if layer is None or getattr(layer, "_halofuse_compat", False):
        return False
    try:
        params = inspect.signature(layer.forward).parameters
    except (TypeError, ValueError):
        return False
    if "grids" not in params:
        return False  # older core: the old 4-argument call is the right one
    original = layer.forward

    def forward(self, x, t_emb, video_seg, audio_seg, sigma=None, sample_sigmas=None, grids=None):
        if grids is None:
            if self.pdd_steps > 1:
                raise ValueError(
                    "A node pack replaced the MiniMax H3 model code with a copy older than this ComfyUI "
                    "(typically ComfyUI-MiniMaxH3-Cache), and this PDD checkpoint needs the newer code. "
                    "Bypass that cache node, or use a non-PDD checkpoint.")
            grids = (None, None)
        return original(self, x, t_emb, video_seg, audio_seg, sigma, sample_sigmas, grids)

    forward.__wrapped__ = original
    layer.forward = forward
    layer._halofuse_compat = True
    print("[halofuse] compat: MiniMax H3 FinalLayer accepts the pre-0.46 call (older node packs keep working)",
          flush=True)
    return True


try:
    _install_minimax_final_layer_shim()
except Exception as _err:  # noqa: BLE001 - a shim must never stop ComfyUI from starting
    print(f"[halofuse] compat: MiniMax H3 shim not installed ({type(_err).__name__})", flush=True)
