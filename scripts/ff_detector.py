#!/usr/bin/env python3
"""AST-based look-ahead detector for strategy and feature code."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = sorted(
    path for path in ROOT.rglob("*.py")
    if not path.name.startswith("test_")
    and not {".git", ".venv", "__pycache__"}.intersection(path.parts)
)


def _negative_number(node: ast.AST) -> bool:
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float))) \
        or (isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and node.value < 0)


def _positive_offset(node: ast.AST) -> bool:
    return (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, int)
            and node.right.value > 0)


class LookAheadVisitor(ast.NodeVisitor):
    def __init__(self):
        self.issues: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        periods = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg == "periods"), None
        )
        if name in {"shift", "pct_change", "diff"} and periods is not None and _negative_number(periods):
            self.issues.append((node.lineno, f"negative {name} reads future rows"))
        if name == "rolling":
            for keyword in node.keywords:
                if keyword.arg == "center" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    self.issues.append((node.lineno, "centered rolling window reads future rows"))
        if name == "bfill":
            self.issues.append((node.lineno, "bfill can copy future values backward"))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript):
        value = node.value
        if (isinstance(value, ast.Attribute) and value.attr in {"iloc", "iat"}
                and not isinstance(node.slice, ast.Slice) and _positive_offset(node.slice)):
            self.issues.append((node.lineno, "positive iloc/iat offset can read a future row"))
        self.generic_visit(node)


def detect(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = LookAheadVisitor()
    visitor.visit(tree)
    return visitor.issues


def main(argv: list[str]) -> int:
    targets = [Path(arg).resolve() for arg in argv] if argv else DEFAULT_TARGETS
    failed = False
    for path in targets:
        issues = detect(path)
        for line, message in issues:
            failed = True
            print(f"{path.relative_to(ROOT)}:{line}: {message}")
    if failed:
        print("future_function_ast_failed")
        return 1
    print("future_function_ast_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
