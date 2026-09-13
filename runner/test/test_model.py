import copy
import json

import pytest

from conftest import EXAMPLES, example
from mission_runner.bt import render_behavior_tree
from mission_runner.model import MissionValidationError, SitesBook, check_mission_cycles, load_mission, validate_mission

EXAMPLE_NAMES = sorted(p.stem for p in EXAMPLES.glob("*.json") if p.stem != "sites")


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_examples_are_valid(name, sites_doc):
    sites = SitesBook.from_dict(sites_doc)
    errors, warnings, m = validate_mission(example(name), sites=sites, missions=set(EXAMPLE_NAMES), connectors={"broker", "plc1"})
    assert not errors, [str(e) for e in errors]
    assert m is not None and m.name == name
    assert not warnings, [str(w) for w in warnings]


def test_edge_strict_round_trip(sites_doc):
    doc = copy.deepcopy(sites_doc)
    doc["maps"]["demo_room"]["edges"] = [{"from": "Home", "to": "A", "strict": True}, {"from": "A", "to": "B"}]
    book = SitesBook.from_dict(doc)
    m = book.map("demo_room")
    assert [e.strict for e in m.edges] == [True, False]
    assert [leg.strict for leg in m.plan_route("Home", "B")] == [True, False]
    assert book.to_dict()["maps"]["demo_room"]["edges"] == doc["maps"]["demo_room"]["edges"]
    assert SitesBook.from_dict(book.to_dict()).to_dict() == book.to_dict()
    doc["maps"]["demo_room"]["edges"][0]["strict"] = "yes"
    with pytest.raises(MissionValidationError):
        SitesBook.from_dict(doc)
    step = {"schema": "mission/1", "name": "s", "flow": [{"type": "nav.follow_route", "to": "A", "waypoint_spacing_m": -1}]}
    errors, _, _ = validate_mission(step)
    assert errors


def test_step_parsing():
    m = load_mission(example("pickup_job"))
    ids = [s.id for s in m.iter_steps()]
    assert ids[:3] == ["wait", "again", "to_pickup"]
    loop = m.step_by_id("again")
    assert loop is not None and [s.id for s in loop.container("body")][:2] == ["to_pickup", "wait_loaded"]
    ask = m.step_by_id("ask")
    assert ask is not None and ask.timeout_s == 30 and ask.out == "answer"
    to_drop = m.step_by_id("to_drop")
    assert to_drop is not None and to_drop.on_fail is not None and to_drop.on_fail.retry == 2
    assert to_drop.on_fail.before_retry[0].type == "nav.clear_costmap"
    assert to_drop.on_fail.before_retry[0].path == ("flow", 1, "body", 2, "on_fail", "before_retry", 0)
    assert m.interrupts[0].run == "go_charge" and m.interrupts[0].policy == "interrupt_and_resume"
    assert m.triggers[1].summary() == "POST /hooks/pickup"


def test_schema_errors_are_reported_with_paths():
    doc = example("go_to_point")
    doc["flow"][1]["pose"] = {"x": "not a number"}
    with pytest.raises(MissionValidationError) as ei:
        load_mission(doc)
    assert any("flow/1/pose" in str(e) for e in ei.value.errors)

    doc = {"schema": "mission/1", "name": "Bad Name", "flow": []}
    errors, _, _ = validate_mission(doc)
    assert errors and errors[0].path == ["name"]


def test_semantic_checks(sites_doc):
    sites = SitesBook.from_dict(sites_doc)
    doc = {
        "schema": "mission/1",
        "name": "bad",
        "inputs": {"last": {"type": "string"}},
        "interrupts": [{"id": "i", "type": "timer.interval", "seconds": 1, "run": "bad"}],
        "flow": [
            {"id": "a", "type": "break"},
            {"id": "a", "type": "loop", "count": 1, "while": "x", "body": []},
            {"id": "b", "type": "nav.go_to_pose", "pose": "Nowhere"},
            {"id": "c", "type": "if", "condition": "1 +", "then": []},
            {"id": "d", "type": "ask_user", "text": "?", "options": ["A"], "default": "B"},
            {"id": "e", "type": "run_mission", "mission": "bad"},
            {"id": "f", "type": "mqtt.publish", "connector": "nope", "topic": "t"},
        ],
    }
    errors, warnings, m = validate_mission(doc, sites=sites, missions={"bad"}, connectors=set())
    msgs = "\n".join(str(e) for e in errors)
    for needle in ["reserved name", "duplicate step id", "inside a loop", "either 'count' or 'while'", "does not exist in any map", "syntax error", "not one of the options", "cannot run itself", "cannot run its own mission"]:
        assert needle in msgs, needle
    assert any("connector 'nope'" in str(w) for w in warnings)


def test_site_not_in_default_map_warns(sites_doc):
    sites = SitesBook.from_dict(sites_doc)
    doc = {"schema": "mission/1", "name": "m", "flow": [{"type": "nav.go_to_pose", "pose": "Inbound"}]}
    errors, warnings, _ = validate_mission(doc, sites=sites)
    assert not errors and any("not in the default map" in str(w) for w in warnings)
    doc["flow"].insert(0, {"type": "nav.change_map", "map": "warehouse_b"})
    errors, warnings, _ = validate_mission(doc, sites=sites)
    assert not errors and not warnings


def test_cycle_detection():
    a = load_mission({"schema": "mission/1", "name": "a", "flow": [{"type": "run_mission", "mission": "b"}]})
    b = load_mission({"schema": "mission/1", "name": "b", "flow": [{"type": "run_mission", "mission": "a"}]})
    findings = check_mission_cycles({"a": a, "b": b})
    assert findings and "cycle" in findings[0].message


def test_sites_roundtrip(sites_doc):
    book = SitesBook.from_dict(sites_doc)
    assert book.default_map == "demo_room"
    assert book.lookup("demo_room", "Rack3").yaw_deg == -90
    assert book.maps_with_site("Home") == ["demo_room", "warehouse_b"]
    again = SitesBook.from_dict(book.to_dict())
    assert again.to_dict() == book.to_dict()


def test_sha_is_stable_under_key_order():
    doc = example("patrol")
    shuffled = json.loads(json.dumps(dict(reversed(list(doc.items())))))
    assert load_mission(doc).sha256 == load_mission(shuffled).sha256
    changed = copy.deepcopy(doc)
    changed["title"] = "x"
    assert load_mission(changed).sha256 != load_mission(doc).sha256


def test_behavior_tree_templates():
    xml = render_behavior_tree({"template": "navigate_with_recovery", "retries": 4, "recoveries": ["clear_costmap", "spin"], "spin_deg": 180})
    assert 'number_of_retries="4"' in xml and 'spin_dist="3.142"' in xml and "BackUp" not in xml
    xml2 = render_behavior_tree({"template": "navigate_through_poses_with_recovery"})
    assert "ComputePathThroughPoses" in xml2 and "RemovePassedGoals" in xml2
    xml3 = render_behavior_tree({"template": "navigate_with_recovery", "recoveries": []})
    assert "AlwaysFailure" in xml3
    with pytest.raises(ValueError):
        render_behavior_tree({"template": "nope"})


def test_map_image_pgm_and_synthetic(tmp_path):
    from mission_runner.mapimage import load_map, synthetic_map

    w, h = 40, 20
    (tmp_path / "m.pgm").write_bytes(b"P5\n# made by map_saver\n%d %d\n255\n" % (w, h) + bytes([254] * (w * h)))
    (tmp_path / "m.yaml").write_text("image: m.pgm\nresolution: 0.05\norigin: [-1.0, -0.5, 0.0]\nnegate: 0\n")
    img = load_map(tmp_path / "m.yaml")
    assert (img.width, img.height, img.resolution) == (w, h, 0.05)
    assert img.png.startswith(b"\x89PNG\r\n\x1a\n")
    assert img.meta()["bounds"] == [-1.0, -0.5, 1.0, 0.5]

    syn = synthetic_map([(0, 0), (3, 0.3), (3, -3)])
    bounds = syn.meta()["bounds"]
    assert bounds[0] <= -2 and bounds[2] >= 5 and syn.source == "synthetic"
    assert syn.png.startswith(b"\x89PNG")


def test_route_graph(sites_doc):
    book = SitesBook.from_dict(sites_doc)
    m = book.map("demo_room")
    assert len(m.edges) >= 8
    legs = m.plan_route("Home", "Rack3")
    assert legs is not None and [l.to for l in legs] == ["A", "B", "Conveyor1", "Rack3"]
    assert any(l.speed_mps == 0.3 for l in legs)
    assert m.plan_route("Home", "Home") == []
    assert m.plan_route("Home", "Inbound") is None  # not on this map
    assert m.nearest_site(1.05, 0.95, only_on_graph=True).name == "A"

    # one-way and blocked lanes change the answer
    from mission_runner.model import Edge

    m.edges.append(Edge("A", "Rack3", bidirectional=False))
    assert [l.to for l in m.plan_route("A", "Rack3")] == ["Rack3"]
    assert [l.to for l in m.plan_route("Rack3", "A")] != ["A"]
    m.edges[-1].blocked = True
    assert [l.to for l in m.plan_route("A", "Rack3")] == ["B", "Conveyor1", "Rack3"]


def test_follow_route_validation(sites_doc):
    sites = SitesBook.from_dict(sites_doc)
    doc = {
        "schema": "mission/1",
        "name": "r",
        "flow": [
            {"id": "a", "type": "nav.follow_route", "to": "Rack3"},
            {"id": "b", "type": "nav.follow_route", "to": "Nowhere"},
        ],
    }
    errors, warnings, _ = validate_mission(doc, sites=sites)
    assert any("Nowhere" in str(e) for e in errors)
    assert not any("Rack3" in str(e) for e in errors)

    bare = SitesBook.from_dict({"schema": "sites/1", "default_map": "m", "maps": {"m": {"sites": {"X": {"x": 0, "y": 0}}}}})
    errors, warnings, _ = validate_mission({"schema": "mission/1", "name": "r", "flow": [{"type": "nav.follow_route", "to": "X"}]}, sites=bare)
    assert not errors and any("no route edges" in str(w) for w in warnings)
