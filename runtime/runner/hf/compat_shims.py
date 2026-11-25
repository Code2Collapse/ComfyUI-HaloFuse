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


def _install_minimax_time_shift_slope_shim():
    """Core 0.46 removed time_shift_slope: the audio carry/slope moved into MiniMaxH3Model.forward(), which wraps
    _forward. Packs that replace _forward with the older copy still multiply the audio output by it; inside the new
    forward() that factor must be 1, which makes their output exactly core 0.46's [-video, -audio]
    (owner 2026-11-25: "module 'comfy.ldm.minimax.model' has no attribute 'time_shift_slope'")."""
    try:
        import comfy.ldm.minimax.model as mm
    except Exception:  # noqa: BLE001
        return False
    if hasattr(mm, "time_shift_slope"):
        return False

    def time_shift_slope(sigma, shift_v=None, shift_a=None):
        try:
            import torch
            return torch.ones_like(sigma) if isinstance(sigma, torch.Tensor) else torch.tensor(1.0)
        except Exception:  # noqa: BLE001
            return 1.0

    mm.time_shift_slope = time_shift_slope
    print("[halofuse] compat: MiniMax H3 time_shift_slope restored for older node packs (factor 1: core 0.46 "
          "applies the audio slope outside _forward)", flush=True)
    return True


for _shim in (_install_minimax_final_layer_shim, _install_minimax_time_shift_slope_shim):
    try:
        _shim()
    except Exception as _err:  # noqa: BLE001 - a shim must never stop ComfyUI from starting
        print(f"[halofuse] compat: {_shim.__name__} not installed ({type(_err).__name__})", flush=True)
