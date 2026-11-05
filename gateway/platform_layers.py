"""Platform-layer hook resolution shared by the gateway command mixins (stage001 axiom 2).

The core asks the caller's PLATFORM LAYER questions it must not answer itself — "is this
session one of my consoles?", "what is your wording for this line?", "describe your own
scope for /status" — and never branches on a platform enum to do it. Each question is one
method-name lookup: a live adapter's method (the receiving bot), then the routing adapters
map, then the ``<method_name>_fn`` the layer declared at ``register()`` (see
``gateway.platform_registry.PlatformEntry``). A layer that does not answer — every platform
without the declaration — leaves the caller on its platform-agnostic fallback path.

Mixed into both GatewaySessionCommandsMixin and GatewayStatusCommandsMixin; GatewayRunner
composes them, so every helper resolves against the same runner state (``adapters``,
``_gateway_session_origin_for_id``).
"""
from __future__ import annotations

import inspect
import logging
from typing import Optional

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py


class PlatformLayerHooksMixin:
    """Layer-hook resolution + the three layer questions used by /resume, /sessions, /status."""

    def _gateway_session_origin_for_id(self, session_id: str):
        """Origin lookup provided by the composing mixin (``gateway.slash_commands_session``);
        a composition without it hands the layer ``origin=None`` (fail-closed there)."""
        return

    def _layer_hook(self, source, method_name: str):
        """Resolve the caller's platform-LAYER declaration of *method_name*: the live adapter's
        method (the receiving bot), then the routing adapters map, then the ``<method_name>_fn``
        the layer declared at ``register()``. Returns a callable or ``None`` (layer declares
        nothing — callers keep their platform-agnostic path)."""
        for probe in (
            lambda: getattr(self, "_delivery_adapter_for", lambda _s: None)(source),
            lambda: (getattr(self, "adapters", None) or {}).get(getattr(source, "platform", None)),
        ):
            try:
                candidate = getattr(probe(), method_name, None)
            except Exception:
                candidate = None
            if callable(candidate):
                return candidate
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(getattr(source.platform, "value", None) or "")
        except Exception:
            entry = None
        declared = getattr(entry, method_name + "_fn", None) if entry is not None else None
        return declared if callable(declared) else None

    async def _layer_proves_same_origin(self, source, target_id: str, *, name: str = "",
                                        allow_cross_room: bool = False):
        """Ask the caller's PLATFORM LAYER whether *target_id* is one of its own consoles — the
        stage001 axiom-2 boundary: the core does not know what a room/thread/channel is and never
        answers this itself. Verdict contract: ``True`` allow, non-empty ``str`` the layer's own
        refusal text, ``False`` proven foreign, ``None``/anything else = no answer — the caller
        falls through to the platform-agnostic owner check (fail-closed there)."""
        hook = self._layer_hook(source, "owns_session")
        if hook is None:
            return None
        try:
            verdict = hook(source, target_id,
                           origin=self._gateway_session_origin_for_id(target_id),
                           name=name, allow_cross_room=allow_cross_room)
            if inspect.isawaitable(verdict):
                verdict = await verdict
        except Exception:
            logger.debug("origin-layer hook failed for %s", target_id, exc_info=True)
            return None
        if verdict is True or verdict is False or (isinstance(verdict, str) and verdict):
            return verdict
        return None  # untyped answer (test doubles included): treat as "no answer", fall to generic

    async def _layer_resume_text(self, source, key: str, **ctx) -> Optional[str]:
        """The layer's own wording for a /resume or /sessions line (stage001 A3): the core asks,
        the layer may return a string it owns (its i18n keys, its topology vocabulary), ``None``
        means no opinion and the core prints its platform-agnostic text. Anything untyped is
        treated as no answer (same fail-safe as :meth:`_layer_proves_same_origin`)."""
        hook = self._layer_hook(source, "resume_text")
        if hook is None:
            return None
        try:
            text = hook(source, key, **ctx)
            if inspect.isawaitable(text):
                text = await text
        except Exception:
            logger.debug("resume-text layer hook failed for %s", key, exc_info=True)
            return None
        return text if isinstance(text, str) and text else None

    async def _layer_describe_scope(self, source, session_key: str) -> list[str]:
        """Extra /status lines owned by the caller's layer (stage001 A3): room/thread/scope rows
        the layer renders from its own fields and env; the core appends them verbatim and knows
        nothing about what a room is. Non-list answers are treated as no block."""
        hook = self._layer_hook(source, "describe_scope")
        if hook is None:
            return []
        try:
            out = hook(source, session_key)
            if inspect.isawaitable(out):
                out = await out
        except Exception:
            logger.debug("describe-scope layer hook failed", exc_info=True)
            return []
        return [str(line) for line in out] if isinstance(out, (list, tuple)) else []
