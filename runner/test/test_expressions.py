import pytest

from mission_runner.expressions import ExpressionError, collect_expressions, evaluate, is_reference, preprocess, resolve


def test_aliases():
    assert preprocess("$.a == 'x$y' and $b") == "payload.a == 'x$y' and b"
    assert evaluate("$goal.x + 1", {"goal": {"x": 2}}) == 3
    assert evaluate("$.percentage < 0.2", {"payload": {"percentage": 0.1}}) is True


def test_operators_and_functions():
    scope = {"a": 3, "b": [1, 2, 3], "s": "Hello", "p": {"x": 0, "y": 0}, "q": {"x": 3, "y": 4}}
    assert evaluate("a * 2 + len(b)", scope) == 9
    assert evaluate("lower(s) == 'hello' and not (a > 5)", scope)
    assert evaluate("b[1] + b[-1]", scope) == 5
    assert evaluate("distance(p, q)", scope) == 5.0
    assert evaluate("'yes' if a == 3 else 'no'", scope) == "yes"
    assert evaluate("2 in b and 9 not in b", scope)
    assert evaluate("round(10 / 3, 1)", scope) == 3.3


def test_missing_keys_are_none_but_unknown_names_fail():
    assert evaluate("payload.missing.deeper", {"payload": {}}) is None
    with pytest.raises(ExpressionError):
        evaluate("nope + 1", {})


@pytest.mark.parametrize("expr", ["__import__('os')", "a.__class__", "lambda: 1", "x = 1", "open('f')", "a if b", "[i for i in b]"])
def test_rejects_unsafe_syntax(expr):
    with pytest.raises(ExpressionError):
        evaluate(expr, {"a": 1, "b": [1]})


def test_resolve_forms():
    scope = {"goal": {"x": 1.5, "y": 2}, "rounds": 3, "go": {"ok": True}}
    assert resolve("$goal.x", scope) == 1.5
    assert resolve("${goal.y * 2}", scope) == 4
    assert resolve("Round ${rounds} done ${'SUCCEEDED' if go.ok else 'FAILED'}", scope) == "Round 3 done SUCCEEDED"
    assert resolve("plain text", scope) == "plain text"
    assert resolve({"pose": {"x": "$goal.x", "y": 7}, "list": ["$rounds", 1]}, scope) == {"pose": {"x": 1.5, "y": 7}, "list": [3, 1]}
    assert resolve(42, scope) == 42


def test_helpers():
    assert is_reference("$a.b[0]")
    assert is_reference("${a + 1}")
    assert not is_reference("$a + 1")
    assert not is_reference("text ${a}")
    assert collect_expressions({"a": "$x.y", "b": "hi ${z} and ${w}", "c": ["$q"]}) == ["x.y", "z", "w", "q"]
