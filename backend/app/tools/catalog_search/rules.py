"""Catalog identity matching; shared field rules are re-exported for existing callers."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from app.tools.catalog_fields import (
    ALIASES as ALIASES,
)
from app.tools.catalog_fields import (
    BOOLEAN_ALIASES as BOOLEAN_ALIASES,
)
from app.tools.catalog_fields import (
    CATEGORY_ALIASES as CATEGORY_ALIASES,
)
from app.tools.catalog_fields import (
    CATEGORY_FIELDS as CATEGORY_FIELDS,
)
from app.tools.catalog_fields import (
    FACT_FIELDS as FACT_FIELDS,
)
from app.tools.catalog_fields import (
    FIELD_DEFINITIONS as FIELD_DEFINITIONS,
)
from app.tools.catalog_fields import (
    FILTER_FIELDS as FILTER_FIELDS,
)
from app.tools.catalog_fields import (
    NUMERIC_UNITS as NUMERIC_UNITS,
)
from app.tools.catalog_fields import (
    FieldDefinition as FieldDefinition,
)
from app.tools.catalog_fields import (
    equal as equal,
)
from app.tools.catalog_fields import (
    matches as matches,
)
from app.tools.catalog_fields import (
    normalize as normalize,
)
from app.tools.catalog_fields import (
    normalize_category_hint as normalize_category_hint,
)
from app.tools.catalog_fields import (
    number as number,
)

if TYPE_CHECKING:
    from app.repositories.catalog_search import CatalogRecord


def fold_text(value: str) -> str:
    """Unicode and case folding shared by identity and source-span checks."""
    return unicodedata.normalize("NFKC", value).casefold()


def compact_text(value: str) -> str:
    """Keep punctuation for source evidence; only whitespace is insignificant."""
    return re.sub(r"\s+", "", fold_text(value))


def compact_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", fold_text(value))


class CatalogNameMatcher:
    def __init__(self, records: list[CatalogRecord]):
        self.records = records
        self.brands = {r.product.brand.casefold() for r in records}
        self.colors = {r.product.specs.get("color", "") for r in records} - {""}
        texts = [text for r in records for text in (r.product.title, r.product.spu_title or "")]
        self.words = sorted(
            {word for text in texts for word in self._chunks(text) if not word.isascii()},
            key=lambda word: (-len(word), word),
        )
        self.title_terms = {r.product.sku_id: self._terms(r.product.title) for r in records}

    def _chunks(self, value: str) -> list[str]:
        value = fold_text(value)
        for alias, brand in ALIASES["brand"].items():
            value = value.replace(alias, f" {brand} ")
        for brand in sorted(self.brands, key=lambda word: (-len(word), word)):
            value = re.sub(re.escape(brand), f" {brand} ", value)
        for color in sorted(self.colors, key=lambda word: (-len(word), word)):
            value = value.replace(color, f" {color} ")
        # Optional layout separators inside a model: D 01 / D-01 / D01.
        value = re.sub(r"([a-z]+)[\s_-]+(?=\d)", r"\1", value)
        return re.findall(r"[a-z]+\d+|[a-z]+|\d+|[\u4e00-\u9fff]+", value)

    def _terms(self, value: str) -> list[str]:
        terms = []
        for chunk in self._chunks(value):
            if chunk.isascii():
                terms.append(chunk)
                continue
            # Split only on actual catalog words; keep unknown remainders so they
            # cannot disappear and silently broaden a named lookup.
            while chunk:
                word = next((w for w in self.words if chunk.startswith(w)), None)
                if word is None:
                    terms.append(chunk)
                    break
                terms.append(word)
                chunk = chunk[len(word) :]
        return terms

    def matches(self, name: str, record: CatalogRecord) -> bool:
        wanted = self._terms(name)
        actual = self.title_terms[record.product.sku_id]
        return bool(wanted) and all(
            term in actual if term.isascii() else any(term in word for word in actual)
            for term in wanted
        )

    def resolve(self, name: str, records: list[CatalogRecord]) -> list[CatalogRecord]:
        exact_skus = [r for r in records if compact_name(r.product.title) == compact_name(name)]
        exact_models = [
            r for r in records if compact_name(r.product.spu_title or "") == compact_name(name)
        ]
        return exact_skus or exact_models or [r for r in records if self.matches(name, r)]
