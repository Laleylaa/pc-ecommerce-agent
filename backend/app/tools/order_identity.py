"""Pure recognition of explicit order identifiers, without identity or access inference."""

import re


def explicit_order_ids(text: str, *, include_unlabelled: bool = True) -> set[int]:
    """Recognize labelled IDs, bare replies, and the existing long order-number format."""
    labelled = re.findall(
        r"(?:订单\s*(?:编号|号码|号|ID)?|order\s*(?:number|id)?)\s*[:：#]?\s*"
        r"([0-9]+)(?![0-9])",
        text,
        re.IGNORECASE,
    )
    long_ids = (
        re.findall(r"(?<![A-Za-z0-9_.])[0-9]{8,20}(?![A-Za-z0-9_.])", text)
        if include_unlabelled
        else []
    )
    bare = [text.strip()] if include_unlabelled and re.fullmatch(r"[0-9]+", text.strip()) else []
    return {int(value) for value in labelled + long_ids + bare if int(value) > 0}
