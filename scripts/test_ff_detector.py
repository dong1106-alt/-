#!/usr/bin/env python3
"""The AST gate must reject common future-row spellings."""
import ast

from ff_detector import LookAheadVisitor


for source in (
    "x.shift(-1)", "x.shift(periods=-2)", "x.pct_change(periods=-1)",
    "x.rolling(5, center=True)", "x.bfill()", "x.iloc[i + 1]",
):
    visitor = LookAheadVisitor()
    visitor.visit(ast.parse(source))
    assert visitor.issues, source

for source in ("x.shift(1)", "x.rolling(5)", "x.iloc[i - 1]"):
    visitor = LookAheadVisitor()
    visitor.visit(ast.parse(source))
    assert not visitor.issues, source

print("future_function_detector_ok")
