"""Compatibility exports; field semantics and text matching each have one implementation."""

from __future__ import annotations

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

# Transitional test/support imports; remove after the agreed fixture migration.
from app.tools.catalog_search.text_matching import (
    CatalogTextMatcher as CatalogNameMatcher,  # noqa: F401 - compatibility export
)
from app.tools.catalog_search.text_matching import (
    compact_name as compact_name,
)
from app.tools.catalog_search.text_matching import (
    fold_text as fold_text,
)
