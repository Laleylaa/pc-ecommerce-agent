"""Import boundaries for the catalog package, in fresh offline subprocesses."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "app.tools.catalog.fields",
    "app.tools.catalog.spec_display",
    "app.tools.catalog.normalization",
    "app.tools.catalog.facets_plan",
    "app.tools.contracts",
])
def test_current_catalog_imports_do_not_load_compatibility(module):
    code = f"""
import importlib, sys
importlib.import_module({module!r})
assert 'app.tools.catalog.compatibility' not in sys.modules
if {module!r} in ('app.tools.catalog.fields', 'app.tools.catalog.normalization',
                 'app.tools.catalog.spec_display'):
    assert 'app.repositories.catalog' not in sys.modules
    assert 'app.core.llm' not in sys.modules
    assert 'app.tools.contracts' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
