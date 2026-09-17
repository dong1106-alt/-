#!/usr/bin/env python3
from research_cloud_factor_overlay import _lowest_quantile_codes, _patched_source


rows = [("c", -0.01), ("b", 0.02), ("a", -0.01), ("d", float("nan"))]
assert _lowest_quantile_codes(rows, 0.5) == {"a", "c"}
try:
    _lowest_quantile_codes(rows, 0)
    raise AssertionError("invalid quantile was accepted")
except ValueError:
    pass

source = _patched_source()
assert source.count("research_intraday_quantile") == 1
compile(source, "cloud-factor-overlay", "exec")
print("research_cloud_factor_overlay_ok")
