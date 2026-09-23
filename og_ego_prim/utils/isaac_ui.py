"""Narrow compatibility guard for Isaac Sim's optional Property window."""

from __future__ import annotations


def _guard_scroll_method(window_class: type) -> None:
    original = window_class.save_scroll_pos
    if getattr(original, "_isbench_null_frame_guard", False):
        return

    def save_scroll_pos(window, reset=False):
        # Isaac Sim can emit SELECTION_CHANGED before rebuilding this frame.
        # Its USD widget then asks the Property window to read scroll_y_max
        # from None, generating an unrelated UI traceback during simulation.
        if not reset and getattr(window, "properties_frame", None) is None:
            return None
        return original(window, reset=reset)

    save_scroll_pos._isbench_null_frame_guard = True
    window_class.save_scroll_pos = save_scroll_pos


def guard_property_window_scroll() -> None:
    try:
        from omni.kit.window.property.window import PropertyWindow
    except (ImportError, AttributeError):
        return
    _guard_scroll_method(PropertyWindow)
