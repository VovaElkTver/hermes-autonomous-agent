"""Turn-result exit policy shared by CLI views.

Provider walls do not spend a task's retry budget; these reason sets let callers
classify failures without knowing provider specifics.
"""

TRANSIENT_PROVIDER_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "billing", "overloaded", "server_error", "timeout",
})
TERMINAL_PROVIDER_REASONS = frozenset({
    "auth", "auth_permanent", "model_not_found", "ssl_cert_verification", "upstream_blocked",
})


def turn_exit_code(
    result, *, credentials_rate_limited: bool = False,
    credentials_terminal: bool = False,
    transient_reasons=TRANSIENT_PROVIDER_REASONS, terminal_reasons=TERMINAL_PROVIDER_REASONS,
) -> int:
    """Preserve completion, interruption and provider-outcome distinctions."""
    if not isinstance(result, dict):
        return 1
    if result.get("interrupted"):
        return 130
    if not (result.get("failed") or result.get("partial") or result.get("completed") is False):
        return 0
    return 1


def credential_failure_flags(exc) -> dict:
    """``turn_exit_code`` credential kwargs for an exception raised before any turn result exists.

    Same predicates as the one-shot CLI's ``_ensure_runtime_credentials``: a quota/rate-limit
    ``AuthError`` is transient, only an explicit re-authentication requirement is terminal. The
    cause chain is walked because agent construction wraps the resolver's error."""
    from hermes_cli.auth import AuthError, is_rate_limited_auth_error
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, AuthError):
            return {"credentials_rate_limited": is_rate_limited_auth_error(exc),
                    "credentials_terminal": bool(exc.relogin_required)}
        exc = exc.__cause__ or exc.__context__
    return {}
