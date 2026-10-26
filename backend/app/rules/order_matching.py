"""Purchased-item text matching; approximate names propose orders, never confirm identity."""

import re
import unicodedata
from difflib import SequenceMatcher


def normalize_item_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"([a-z]+)[\s_-]+(?=\d)", r"\1", value)
    return re.sub(r"([\u4e00-\u9fff])[\s_-]+(?=[\u4e00-\u9fff])", r"\1", value)


def item_name_parts(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"([a-z]+)[\s_-]+(?=\d)", r"\1", value)
    return tuple(dict.fromkeys(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", value)))


def item_part_pattern(value: str) -> str:
    value = normalize_item_text(value)
    escaped = re.escape(value)
    # K1 must not silently identify K10, K1Pro, or a different alphanumeric model.
    return rf"(^|[^a-z0-9]){escaped}($|[^a-z0-9])" if value.isascii() else escaped


def item_name_similarity(terms: list[str], name: str, specs: dict | None) -> float:
    wanted = [part for term in terms for part in item_name_parts(term)]
    actual_values = [name, *(str(v) for v in (specs or {}).values())]
    actual = [part for value in actual_values for part in item_name_parts(value)]
    if not wanted or not actual:
        return 0.0
    scores = []
    for part in wanted:
        if any(re.search(item_part_pattern(part), normalize_item_text(v)) for v in actual_values):
            scores.append(1.0)
        else:
            scores.append(max((SequenceMatcher(None, part, other, autojunk=False).ratio()
                               for other in actual if part.isascii() == other.isascii()),
                              default=0.0))
    # Do not rescue an unrelated name solely because its category matches.
    return sum(scores) / len(scores) if min(scores) >= 0.5 else 0.0
