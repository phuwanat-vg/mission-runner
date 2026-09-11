"""Safe expression evaluation for mission files.

Expressions use a Python-like syntax evaluated by a small tree walker; no code
is ever ``eval``'d. Supported: literals, names, dotted access into dicts,
``[index]``, arithmetic, comparisons, ``and``/``or``/``not``, ``in``, ternary
``a if c else b``, list/dict/tuple literals and a fixed set of functions.

Aliases accepted for convenience: ``$name`` -> ``name`` and ``$.field`` ->
``payload.field``.

Value resolution (:func:`resolve`) turns the strings a mission author writes
into values:

* ``"$goal.x"``          -> value of ``goal.x`` (any type)
* ``"${rounds + 1}"``    -> value of the expression (any type)
* ``"Round ${rounds}"``  -> interpolated text
* anything else         -> the literal
"""

from __future__ import annotations

import ast
import math
import operator
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["ExpressionError", "evaluate", "parse", "resolve", "is_reference", "has_expression", "preprocess", "FUNCTIONS", "CONTEXT_FUNCTIONS"]


class ExpressionError(ValueError):
    """Raised for syntax errors, unsupported constructs or runtime failures."""


_REF_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])*)$")
_WHOLE_INTERP_RE = re.compile(r"^\$\{([^{}]*)\}$")
_INTERP_RE = re.compile(r"\$\{([^{}]*)\}")
_DOLLAR_RE = re.compile(r"\$(\.|[A-Za-z_])")


def _distance(a: Any, b: Any) -> float:
    ax, ay = _xy(a)
    bx, by = _xy(b)
    return math.hypot(ax - bx, ay - by)


def _xy(p: Any) -> tuple[float, float]:
    if isinstance(p, Mapping):
        if "x" in p and "y" in p:
            return float(p["x"]), float(p["y"])
        pos = p.get("position") or (p.get("pose") or {}).get("position")
        if isinstance(pos, Mapping):
            return float(pos["x"]), float(pos["y"])
    if isinstance(p, Sequence) and not isinstance(p, str) and len(p) >= 2:
        return float(p[0]), float(p[1])
    raise ExpressionError(f"not a point: {p!r}")


def _get(obj: Any, key: Any, default: Any = None) -> Any:
    try:
        if isinstance(obj, Mapping):
            return obj.get(key, default)
        if isinstance(obj, Sequence) and not isinstance(obj, str):
            return obj[int(key)]
    except (IndexError, ValueError, TypeError):
        return default
    return default


FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "now": time.time,
    "distance": _distance,
    "hypot": math.hypot,
    "sqrt": math.sqrt,
    "get": _get,
}

#: Functions the runner injects into the scope at run time. They are accepted by
#: :func:`parse` so a mission validates before it ever runs.
CONTEXT_FUNCTIONS: frozenset[str] = frozenset({"in_zone", "zone_of"})

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.IfExp,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Load,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    *(_BIN_OPS.keys()),
    *(_CMP_OPS.keys()),
)

_MAX_LENGTH = 2000


def preprocess(expr: str) -> str:
    """Rewrite ``$name`` / ``$.field`` aliases outside string literals."""
    out: list[str] = []
    i = 0
    n = len(expr)
    quote: str | None = None
    while i < n:
        c = expr[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(expr[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "$":
            m = _DOLLAR_RE.match(expr, i)
            if m:
                if m.group(1) == ".":
                    out.append("payload.")
                    i += 2
                else:
                    i += 1  # drop the '$'; the identifier follows
                continue
        out.append(c)
        i += 1
    return "".join(out)


def parse(expr: str) -> ast.Expression:
    """Parse and check an expression. Raises :class:`ExpressionError`."""
    if not isinstance(expr, str):
        raise ExpressionError(f"expression must be a string, got {type(expr).__name__}")
    if len(expr) > _MAX_LENGTH:
        raise ExpressionError("expression too long")
    src = preprocess(expr).strip()
    if not src:
        raise ExpressionError("empty expression")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error in {expr!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"unsupported syntax in {expr!r}: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or (node.func.id not in FUNCTIONS and node.func.id not in CONTEXT_FUNCTIONS):
                raise ExpressionError(f"unknown function in {expr!r}")
            if node.keywords:
                raise ExpressionError("keyword arguments are not supported")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError("private attributes are not allowed")
    return tree


class _Evaluator:
    __slots__ = ("scope",)

    def __init__(self, scope: Mapping[str, Any]):
        self.scope = scope

    def visit(self, node: ast.AST) -> Any:  # noqa: C901 - one method per node type is clearer here
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.scope:
                return self.scope[node.id]
            if node.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[node.id]
            raise ExpressionError(f"unknown variable '{node.id}'")
        if isinstance(node, ast.Attribute):
            base = self.visit(node.value)
            return _attr(base, node.attr)
        if isinstance(node, ast.Subscript):
            base = self.visit(node.value)
            if isinstance(node.slice, ast.Slice):
                lo = self.visit(node.slice.lower) if node.slice.lower else None
                hi = self.visit(node.slice.upper) if node.slice.upper else None
                return base[lo:hi]
            key = self.visit(node.slice)
            return _item(base, key)
        if isinstance(node, ast.BinOp):
            fn = _BIN_OPS[type(node.op)]
            try:
                return fn(self.visit(node.left), self.visit(node.right))
            except (TypeError, ZeroDivisionError) as e:
                raise ExpressionError(str(e)) from None
        if isinstance(node, ast.UnaryOp):
            return _UNARY_OPS[type(node.op)](self.visit(node.operand))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                value: Any = True
                for v in node.values:
                    value = self.visit(v)
                    if not value:
                        return value
                return value
            value = False
            for v in node.values:
                value = self.visit(v)
                if value:
                    return value
            return value
        if isinstance(node, ast.Compare):
            left = self.visit(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self.visit(comp)
                try:
                    ok = _CMP_OPS[type(op)](left, right)
                except TypeError:
                    ok = False
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            name = node.func.id  # type: ignore[attr-defined]
            # Context helpers (in_zone, ...) are injected into the scope by the
            # runner; plain data in the scope is never callable.
            bound = self.scope.get(name)
            fn = bound if callable(bound) else FUNCTIONS[name]
            args = [self.visit(a) for a in node.args]
            try:
                return fn(*args)
            except ExpressionError:
                raise
            except Exception as e:  # noqa: BLE001 - surface any function failure as an expression error
                raise ExpressionError(f"{node.func.id}(): {e}") from None  # type: ignore[attr-defined]
        if isinstance(node, ast.IfExp):
            return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)
        if isinstance(node, ast.List):
            return [self.visit(e) for e in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.visit(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values) if k is not None}
        raise ExpressionError(f"unsupported node {type(node).__name__}")


def _attr(base: Any, name: str) -> Any:
    """Dotted access: dict key, list index by name is not allowed, None chains to None."""
    if base is None:
        return None
    if isinstance(base, Mapping):
        return base.get(name)
    if hasattr(base, "_asdict"):  # namedtuple
        return base._asdict().get(name)
    if isinstance(base, (int, float, str, bool, list, tuple)):
        raise ExpressionError(f"cannot read '{name}' of {type(base).__name__}")
    # Plain objects used as records (e.g. dataclasses): allow public attributes only.
    if name.startswith("_") or not hasattr(base, name):
        return None
    value = getattr(base, name)
    if callable(value):
        raise ExpressionError(f"'{name}' is not a value")
    return value


def _item(base: Any, key: Any) -> Any:
    if base is None:
        return None
    try:
        if isinstance(base, Mapping):
            return base.get(key)
        return base[key]
    except (IndexError, KeyError, TypeError):
        return None


def evaluate(expr: str, scope: Mapping[str, Any] | None = None) -> Any:
    """Evaluate ``expr`` against ``scope``. Raises :class:`ExpressionError`."""
    tree = parse(expr)
    return _Evaluator(scope or {}).visit(tree.body)


def is_reference(value: Any) -> bool:
    """True for strings of the form ``$name.path`` or ``${...}`` (whole string)."""
    return isinstance(value, str) and (bool(_REF_RE.match(value)) or bool(_WHOLE_INTERP_RE.match(value)))


def has_expression(value: Any) -> bool:
    """True when the string contains anything that :func:`resolve` would evaluate."""
    return isinstance(value, str) and (is_reference(value) or "${" in value)


def resolve(value: Any, scope: Mapping[str, Any]) -> Any:
    """Recursively resolve expression strings inside ``value``."""
    if isinstance(value, str):
        m = _REF_RE.match(value)
        if m:
            return evaluate(m.group(1), scope)
        m = _WHOLE_INTERP_RE.match(value)
        if m:
            return evaluate(m.group(1), scope)
        if "${" in value:
            return _INTERP_RE.sub(lambda mm: _to_text(evaluate(mm.group(1), scope)), value)
        return value
    if isinstance(value, Mapping):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve(v, scope) for v in value)
    return value


def _to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def collect_expressions(value: Any) -> list[str]:
    """Every expression string contained in ``value`` (for validation)."""
    found: list[str] = []
    if isinstance(value, str):
        m = _REF_RE.match(value)
        if m:
            found.append(m.group(1))
        elif "${" in value:
            found.extend(mm.group(1) for mm in _INTERP_RE.finditer(value))
    elif isinstance(value, Mapping):
        for v in value.values():
            found.extend(collect_expressions(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(collect_expressions(v))
    return found
