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


def _restore_core_methods(mm, owner) -> list:
    """Put core's own MiniMaxH3Model methods back on `owner`, taken from a fresh, unpatched copy of
    comfy/ldm/minimax/model.py (a node pack may already have replaced them: custom nodes load in no fixed order).
    Every method of the fresh class whose code differs from the one on `owner` is restored (the cache pack replaces
    both _forward and _run_blocks). Returns the restored names."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("halofuse_core_minimax_reference", mm.__file__)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    restored = []
    core_cls = ref.MiniMaxH3Model
    for name, value in vars(core_cls).items():
        if not callable(value) or name.startswith("__") or name == "forward":
            continue
        mine = owner.__dict__.get(name)
        if getattr(mine, "__code__", None) is not None and getattr(value, "__code__", None) is not None                 and mine.__code__.co_filename != value.__code__.co_filename:
            setattr(owner, name, value)
            restored.append(name)
    for name in [n for n, v in vars(owner).items() if callable(v) and n not in vars(core_cls)
                 and n.startswith("_") and not n.startswith("__") and n != "forward"]:
        pass  # pack-only helpers stay; core code never calls them
    return restored


def _install_minimax_pack_fallback():
    """Owner 2026-11-25: ComfyUI-MiniMaxH3-Cache replaces MiniMaxH3Model._forward with a copy of older core code that
    breaks on core 0.46 in several places (FinalLayer args, time_shift_slope, PackedLayout(frame_count=...)). When an
    error comes from inside that pack's code, run core's own _forward for the rest of this ComfyUI session: the render
    is correct, only the pack's cache speed-up is lost. One plain log line says so."""
    try:
        import comfy.ldm.minimax.model as mm
    except Exception:  # noqa: BLE001
        return False
    cls = getattr(mm, "MiniMaxH3Model", None)
    if cls is None or getattr(cls, "_halofuse_fallback_wrapped", False):
        return False
    original_forward = cls.forward

    def forward(self, *args, **kwargs):
        try:
            return original_forward(self, *args, **kwargs)
        except (TypeError, AttributeError, ValueError) as err:
            owner = type(self)
            patched = getattr(owner, "_forward", None)
            pack_file = getattr(getattr(patched, "__code__", None), "co_filename", "") or ""
            core_file = mm.__file__ or ""
            tb = err.__traceback__
            from_pack = False
            while tb is not None:
                fn = tb.tb_frame.f_code.co_filename or ""
                # the frame belongs to the code that replaced core's _forward (any folder name), or names the pack
                if (pack_file and fn == pack_file and pack_file != core_file) or "MiniMaxH3-Cache" in fn:
                    from_pack = True
                    break
                tb = tb.tb_next
            if not from_pack or getattr(owner, "_halofuse_core_forward", False):
                raise
            restored = _restore_core_methods(mm, owner)
            owner._halofuse_core_forward = True
            print(f"[halofuse] compat: ComfyUI-MiniMaxH3-Cache does not work with this ComfyUI "
                  f"({type(err).__name__}: {err}); running MiniMax H3 with core's own code instead "
                  f"(same result, without the pack's cache speed-up; restored: {', '.join(restored) or 'nothing'})", flush=True)
            return original_forward(self, *args, **kwargs)

    forward.__wrapped__ = original_forward
    cls.forward = forward
    cls._halofuse_fallback_wrapped = True
    return True


for _shim in (_install_minimax_final_layer_shim, _install_minimax_time_shift_slope_shim,
              _install_minimax_pack_fallback):
    try:
        _shim()
    except Exception as _err:  # noqa: BLE001 - a shim must never stop ComfyUI from starting
        print(f"[halofuse] compat: {_shim.__name__} not installed ({type(_err).__name__})", flush=True)
