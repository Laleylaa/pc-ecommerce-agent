"""Shared requirement and validation semantics, independent of tool operation."""

from app.tools.prompts.templates import load_prompt

PREFERENCES = load_prompt("catalog/preferences.md")

VALIDATION_BOUNDARY = load_prompt("catalog/validation_boundary.md")
