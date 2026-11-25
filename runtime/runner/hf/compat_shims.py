"""Compatibility guard HaloFuse installs into ComfyUI's custom_nodes before ComfyUI starts (owner 2026-11-24/25).

Node packs such as ComfyUI-MiniMaxH3-Cache (last updated 2026-08-03) replace MiniMaxH3Model._forward and _run_blocks
with copies of an older core. On core 0.46+ those copies are WRONG, not just incompatible: core 0.46 gives each
masked latent row its own timestep (SetLatentNoiseMask / denoise_mask) and the old copy does not, so a masked render
(e.g. a face replacement) comes out as noise. Measured 2026-11-25 on real core 0.46.0 with the real pack: max error
0.16 with a noise mask, 0 without one.

So, at the first H3 sampling step, when _forward is not core's own code and does not take core's `denoise_mask`
argument, core's own methods are put back (taken from a fresh, unpatched copy of comfy/ldm/minimax/model.py). The
render is then exactly core's; only the pack's cache speed-up is lost, and one plain log line says so. Errors raised
from inside such a copy (older layouts) also restore core's code and retry the step.

The 2026-11-24 shims that made the old copy run (FinalLayer default arguments, time_shift_slope = 1) are removed on
purpose: they let a wrong result through silently.

Loaded as a ComfyUI custom node file: never raises at import, registers no nodes.
"""
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


def _restore_core_methods(mm, owner) -> list:
    """Put core's own MiniMaxH3Model methods back on `owner`, from a fresh, unpatched copy of
    comfy/ldm/minimax/model.py (a node pack may already have replaced them: custom nodes load in no fixed order).
    Every method whose code comes from another file is restored (the cache pack replaces _forward and _run_blocks).
    Returns the restored names."""
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
        if getattr(mine, "__code__", None) is not None and getattr(value, "__code__", None) is not None \
                and mine.__code__.co_filename != value.__code__.co_filename:
            setattr(owner, name, value)
            restored.append(name)
    return restored


def _foreign_forward(mm, owner):
    """The file of `owner._forward` when it is not core's code, else None."""
    code = getattr(getattr(owner, "_forward", None), "__code__", None)
    if code is None:
        return None
    return code.co_filename if code.co_filename != (mm.__file__ or "") else None


def _outdated_forward(mm, owner) -> bool:
    """True when a node pack's copy of _forward predates core's denoise_mask handling (core takes the argument)."""
    import inspect
    if _foreign_forward(mm, owner) is None:
        return False
    try:
        core_params = inspect.signature(_core_forward(mm)).parameters
        pack_params = inspect.signature(owner._forward).parameters
    except (TypeError, ValueError):
        return True  # cannot compare: core's own code is the safe choice
    return "denoise_mask" in core_params and "denoise_mask" not in pack_params


_CORE_FORWARD = {}


def _core_forward(mm):
    if "fn" not in _CORE_FORWARD:
        import importlib.util
        spec = importlib.util.spec_from_file_location("halofuse_core_minimax_signature", mm.__file__)
        ref = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ref)
        _CORE_FORWARD["fn"] = ref.MiniMaxH3Model._forward
    return _CORE_FORWARD["fn"]


def _use_core(mm, owner, why) -> None:
    restored = _restore_core_methods(mm, owner)
    owner._halofuse_core_forward = True
    print(f"[halofuse] compat: a node pack replaced MiniMax H3's sampling code with an older copy ({why}); "
          f"running MiniMax H3 with this ComfyUI's own code instead (correct result, without that pack's cache "
          f"speed-up; restored: {', '.join(restored) or 'nothing'})", flush=True)


def _install_minimax_guard():
    try:
        import comfy.ldm.minimax.model as mm
    except Exception:  # noqa: BLE001 - no MiniMax in this ComfyUI: nothing to guard
        return False
    cls = getattr(mm, "MiniMaxH3Model", None)
    if cls is None or getattr(cls, "_halofuse_fallback_wrapped", False):
        return False
    original_forward = cls.forward

    def forward(self, *args, **kwargs):
        owner = type(self)
        if not getattr(owner, "_halofuse_core_forward", False):
            source = _foreign_forward(mm, owner)
            if source is not None and _outdated_forward(mm, owner):
                _use_core(mm, owner, f"{source} has no denoise_mask: masked renders would come out as noise")
        try:
            return original_forward(self, *args, **kwargs)
        except (TypeError, AttributeError, ValueError) as err:
            source = _foreign_forward(mm, owner)
            if source is None or getattr(owner, "_halofuse_core_forward", False):
                raise
            tb, from_pack = err.__traceback__, False
            while tb is not None:
                if (tb.tb_frame.f_code.co_filename or "") == source:
                    from_pack = True
                    break
                tb = tb.tb_next
            if not from_pack:
                raise
            _use_core(mm, owner, f"{type(err).__name__}: {err}")
            return original_forward(self, *args, **kwargs)

    forward.__wrapped__ = original_forward
    cls.forward = forward
    cls._halofuse_fallback_wrapped = True
    return True


for _shim in (_install_minimax_guard,):
    try:
        _shim()
    except Exception as _err:  # noqa: BLE001 - a shim must never stop ComfyUI from starting
        print(f"[halofuse] compat: {_shim.__name__} not installed ({type(_err).__name__})", flush=True)
