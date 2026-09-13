"""The example ROS 2 nodes compile, stay copyable and are registered."""

import ast
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "mission_runner" / "examples"
NAMES = ["start_mission", "answer_requests", "ask_robot", "watch_missions"]


@pytest.mark.parametrize("name", NAMES)
def test_example_node_is_plain_rclpy_and_registered(name):
    source = (EXAMPLES / f"{name}.py").read_text("utf-8")
    source.encode("ascii")  # ASCII only
    tree = ast.parse(source)
    assert ast.get_docstring(tree) and "ros2 run mission_runner example_" in ast.get_docstring(tree)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("mission_runner") for m in imported), "examples must be copyable without mission_runner"
    assert "main" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert len(source.splitlines()) <= 130
    setup = (ROOT / "setup.py").read_text("utf-8")
    assert re.search(rf'"example_{name} = mission_runner\.examples\.{name}:main"', setup)


@pytest.mark.parametrize("name", NAMES)
def test_example_node_imports_with_rclpy(name):
    pytest.importorskip("rclpy")
    if name == "start_mission":
        pytest.importorskip("mission_msgs")
    importlib.import_module(f"mission_runner.examples.{name}")
