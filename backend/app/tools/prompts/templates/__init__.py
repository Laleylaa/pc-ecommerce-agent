"""Strict, single-pass Markdown rendering; no expressions or recursive includes."""

import re
from importlib.resources import files
from pathlib import PurePosixPath

_SLOT = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def load_prompt(name: str) -> str:
    """Load only developer-owned package resources, never user-supplied paths."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md" or "\\" in name:
        raise ValueError("Prompt must be a relative Markdown resource")
    return files(__package__).joinpath(*path.parts).read_text(encoding="utf-8").strip()


def render_prompt(name: str, **values: str) -> str:
    template = load_prompt(name)
    required = set(_SLOT.findall(template))
    if required != values.keys():
        raise ValueError(
            f"Prompt {name}: missing={sorted(required - values.keys())}, "
            f"unexpected={sorted(values.keys() - required)}"
        )
    if any(not isinstance(value, str) for value in values.values()):
        raise TypeError("Prompt replacements must be strings")
    # JSON braces and inserted text are literal; never evaluate or re-render values.
    return _SLOT.sub(lambda match: values[match[1]], template)
