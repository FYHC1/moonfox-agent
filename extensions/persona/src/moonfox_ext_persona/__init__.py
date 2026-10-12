"""Optional persona extension; explicitly register EXTENSION with the application."""

from .api import (
    EXTENSION,
    MANIFEST,
    DialogueExample,
    PersonaExtension,
    PersonaSettings,
    render_persona,
)

__all__ = [
    "EXTENSION",
    "MANIFEST",
    "DialogueExample",
    "PersonaExtension",
    "PersonaSettings",
    "render_persona",
]
