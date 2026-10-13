"""The provider picker must survive release assembly, not only checkout imports."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def test_catalog_module_is_part_of_release_dashboard():
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location("catalog_package_builder", root / "scripts" / "build_package.py")
    assert spec is not None and spec.loader is not None
    builder = module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert "model_catalog.py" in builder.DASHBOARD
