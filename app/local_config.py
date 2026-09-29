"""Utilidades locales (sin persistir secretos en el PC)."""

from __future__ import annotations


def mask_secret(value: str, show: int = 4) -> str:
    """Muestra extremos del secreto; el resto queda enmascarado."""
    text = (value or "").strip()
    if not text:
        return "(vacío)"
    if len(text) <= show * 2:
        return "*" * len(text)
    return f"{text[:show]}…{text[-show:]}"
