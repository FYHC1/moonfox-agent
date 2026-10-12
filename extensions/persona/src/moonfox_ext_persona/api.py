"""Public entry points. Importing does not register or start an extension."""

from .plugin import EXTENSION, MANIFEST, PersonaExtension
from .render import render_persona
from .schema import DialogueExample, PersonaSettings

__all__ = [
    "EXTENSION",
    "MANIFEST",
    "DialogueExample",
    "PersonaExtension",
    "PersonaSettings",
    "render_persona",
]
