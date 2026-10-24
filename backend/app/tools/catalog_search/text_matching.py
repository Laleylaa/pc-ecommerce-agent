"""Pure multi-field name retrieval; similarity proposes identities, never confirms them.

The current bounded catalog is scored in memory after repository retrieval. No LLM,
SQL, request parsing or hard specification filtering belongs in this module.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Generic, TypeVar

from app.tools.catalog_fields import ALIASES, CATEGORY_ALIASES, CATEGORY_FIELDS

if TYPE_CHECKING:
    from app.repositories.catalog_search import CatalogIdentityRecord, CatalogRecord

Record = TypeVar("Record", "CatalogRecord", "CatalogIdentityRecord")

# Weights affect ordering, not whether a SKU satisfies a structured condition.
FIELD_WEIGHTS = {"spu_title": 4, "title": 3, "brand": 2, "category": 1}
SIMILARITY_THRESHOLD = 0.72
_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
_ALIASES = {**CATEGORY_ALIASES, **ALIASES["brand"], **ALIASES["color"]}
_ALIAS_PATTERN = re.compile("|".join(
    (r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")
    if alias.isascii() else re.escape(alias)
    for alias in sorted(_ALIASES, key=lambda value: (-len(value), value))
))


def fold_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def normalized_text(value: str) -> str:
    value = fold_text(value)
    value = _ALIAS_PATTERN.sub(lambda match: f" {_ALIASES[match[0]]} ", value)
    # Formatting variants of a model are equal; changing its digits is not.
    return re.sub(r"([a-z]+)[\s_-]+(?=\d)", r"\1", value)


def compact_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", normalized_text(value))


@dataclass(frozen=True)
class TextMatch(Generic[Record]):
    record: Record
    confirmed: bool
    score: float
    matched_terms: tuple[str, ...]
    unmatched_terms: tuple[str, ...]


class CatalogTextMatcher(Generic[Record]):
    def __init__(self, records: list[Record]):
        self.records = records
        self.fields = {
            r.product.sku_id: {
                key: normalized_text(getattr(r.product, key) or "") for key in FIELD_WEIGHTS
            }
            for r in records
        }
        self.brands = sorted({
            compact_name(r.product.brand) for r in records
            if compact_name(r.product.brand).isascii() and compact_name(r.product.brand)
        }, key=lambda word: (-len(word), word))
        # Catalog-backed vocabulary, not a hand-maintained intent/keyword patch list.
        words = set(CATEGORY_FIELDS)
        words.update(
            term for color in ALIASES["color"].values()
            for term in _TOKEN.findall(normalized_text(color)) if not term.isascii()
        )
        for record in records:
            words.update(
                term for value in self.fields[record.product.sku_id].values()
                for term in _TOKEN.findall(value) if not term.isascii()
            )
        self.words = sorted(words, key=lambda word: (-len(word), word))
        self.tokens = {
            sku_id: {key: self.terms(value) for key, value in fields.items()}
            for sku_id, fields in self.fields.items()
        }

    def terms(self, value: str) -> tuple[str, ...]:
        terms = []
        for chunk in _TOKEN.findall(normalized_text(value)):
            if chunk.isascii():
                # A catalog brand joined to a model (KeychronK8) is a layout variant.
                brand = next((
                    b for b in self.brands if chunk.startswith(b) and len(chunk) > len(b)
                    and any(c.isdigit() for c in chunk[len(b):])
                ), None)
                terms.extend((brand, chunk[len(brand):]) if brand else (chunk,))
                continue
            # Preserve every unknown remainder. Never drop an unmatched model fragment.
            pending = ""
            while chunk:
                word = next((w for w in self.words if chunk.startswith(w)), None)
                if word:
                    if pending:
                        terms.append(pending)
                        pending = ""
                    terms.append(word)
                    chunk = chunk[len(word):]
                else:
                    pending += chunk[0]
                    chunk = chunk[1:]
            if pending:
                terms.append(pending)
        return tuple(dict.fromkeys(terms))

    def _hit(self, term: str, sku_id: int) -> int:
        return max((
            weight for key, weight in FIELD_WEIGHTS.items()
            if (term in self.tokens[sku_id][key] if term.isascii()
                else term in self.fields[sku_id][key])
        ), default=0)

    def rank(self, text: str, records: list[Record] | None = None) -> list[TextMatch[Record]]:
        wanted = self.terms(text)
        if not wanted:
            return []
        ranked = []
        for record in self.records if records is None else records:
            sku_id = record.product.sku_id
            hits = {term: self._hit(term, sku_id) for term in wanted}
            matched = tuple(term for term, weight in hits.items() if weight)
            unmatched = tuple(term for term in wanted if term not in matched)
            if not unmatched:
                exact = compact_name(text) in {
                    compact_name(record.product.title), compact_name(record.product.spu_title or "")
                }
                score = 1.0 if exact else 0.80 + 0.19 * sum(hits.values()) / (4 * len(wanted))
                ranked.append(TextMatch(record, True, score, matched, ()))
                continue
            actual = {term for terms in self.tokens[sku_id].values() for term in terms}
            similarities = {
                term: 1.0 if hits[term] else max((
                    SequenceMatcher(None, term, other, autojunk=False).ratio()
                    for other in actual
                    if term.isascii() == other.isascii()
                ), default=0.0)
                for term in wanted
            }
            weights = {
                term: 0.5 if term in CATEGORY_FIELDS else 3 if any(c.isdigit() for c in term) else 2
                for term in wanted
            }
            score = sum(similarities[t] * weights[t] for t in wanted) / sum(weights.values())
            # A category alone cannot rescue an unrelated or unknown product name.
            anchored = any(t not in CATEGORY_FIELDS and similarities[t] >= 0.6 for t in wanted)
            if anchored and score >= SIMILARITY_THRESHOLD:
                ranked.append(TextMatch(record, False, 0.79 * score, matched, unmatched))
        return sorted(ranked, key=lambda hit: (
            not hit.confirmed, -hit.score, hit.record.product.spu_id, hit.record.product.sku_id,
        ))

    def matches(self, text: str, record: Record) -> bool:
        wanted = self.terms(text)
        return bool(wanted) and all(self._hit(term, record.product.sku_id) for term in wanted)

    def matches_model(self, text: str, record: Record) -> bool:
        """Does the name identify the model without using any SKU-title terms?"""
        wanted = self.terms(text)
        sku_id = record.product.sku_id
        return bool(wanted) and all(
            any(
                term in self.tokens[sku_id][key] if term.isascii()
                else term in self.fields[sku_id][key]
                for key in ("spu_title", "brand", "category")
            )
            for term in wanted
        )

    def resolve(self, text: str, records: list[Record]) -> list[Record]:
        """Confirmed lexical matches only; shared by ID cross-check and facets."""
        exact = [
            r for r in records if compact_name(text) in {
                compact_name(r.product.title), compact_name(r.product.spu_title or ""),
            }
        ]
        return exact or [r for r in records if self.matches(text, r)]
