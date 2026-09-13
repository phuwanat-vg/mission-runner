"""station_answer decisions, without ROS."""

import json

import pytest

from mission_runner.station_answer import StationAnswerer, StationConfig, parse_request, slug


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def req(rid="r1", **kw) -> str:
    return json.dumps({"id": rid, "text": "Part OK?", **kw})


def make(clock=None, **params):
    cfg = StationConfig.from_params(params, "station_answer_test")
    return StationAnswerer(cfg, clock or Clock())


def answers(actions):
    return [(a.request_id, a.message["answer"]) for a in actions if a.kind == "publish"]


def test_config_defaults_and_topics():
    c = StationConfig.from_params({"station": "Conveyor 1"}, "n1")
    assert slug("Conveyor 1") == "conveyor_1" and slug("Rack-3/B") == "rack_3_b"
    assert (c.request_topic, c.answer_topic, c.mode, c.by) == ("/station/conveyor_1/request", "/station/conveyor_1/answer", "auto", "n1")
    c = StationConfig.from_params({"station": "", "delay_s": 2, "match_station": "false"})
    assert (c.request_topic, c.answer_topic, c.delay_s, c.match_station) == ("/iviz/request", "/iviz/answer", 2.0, False)
    c = StationConfig.from_params({"station": "A", "request_topic": "/cell/req", "source_type": "Int32", "mode": "topic", "source_topic": "/x"})
    assert c.request_topic == "/cell/req" and c.answer_topic == "/station/a/answer" and c.source_type == "std_msgs/msg/Int32"


@pytest.mark.parametrize(
    "params, text",
    [
        ({"mode": "blink"}, "mode must be"),
        ({"mode": "topic"}, "source_topic"),
        ({"mode": "topic", "source_topic": "/x", "source_type": "std_msgs/msg/Image"}, "source_type"),
        ({"mode": "service"}, "service"),
        ({"mode": "gpio"}, "gpio_pins"),
        ({"mode": "gpio", "gpio_pins": [17, 27], "gpio_answers": ["Done"]}, "one answer per pin"),
        ({"match_station": True}, "match_station needs station"),
        ({"delay_s": "soon"}, "delay_s"),
    ],
)
def test_config_errors(params, text):
    with pytest.raises(ValueError, match=text):
        StationConfig.from_params(params)


def test_parse_request():
    assert parse_request("not json") is None and parse_request("[1]") is None
    assert parse_request(json.dumps({"text": "no id"})) is None
    r = parse_request(req("7", options=["OK", "Reject"], default="OK", station="A", timeout_s=5))
    assert (r.id, r.options, r.default, r.station, r.timeout_s) == ("7", ["OK", "Reject"], "OK", "A", 5.0)


def test_auto_answer_choice_and_once_per_id():
    s = make(station="A")
    acts = s.on_request(req("r1", options=["Yes", "No"], default="No"))
    assert answers(acts) == [("r1", "No")]
    assert acts[0].message == {"id": "r1", "answer": "No", "by": "station_answer_test", "station": "A"}
    assert s.on_request(req("r1", options=["Yes", "No"], default="No")) == []  # answered already
    assert answers(s.on_request(req("r2", options=["Yes", "No"]))) == [("r2", "Yes")]
    assert answers(s.on_request(req("r3"))) == [("r3", "OK")]
    assert s.on_request(json.dumps({"text": "no id"})) == []
    s = make(answer="Go", by="cell-3")
    acts = s.on_request(req("r1", default="No", station="B"))
    assert acts[0].message == {"id": "r1", "answer": "Go", "by": "cell-3", "station": "B"}  # station from the request
    assert "station" not in make().on_request(req("r9"))[0].message


def test_auto_delay():
    clock = Clock()
    s = make(clock, delay_s=2.0)
    assert s.on_request(req("r1")) == []
    clock.t += 1.0
    assert s.on_timer() == []
    clock.t += 1.5
    assert answers(s.on_timer()) == [("r1", "OK")]
    assert s.on_timer() == [] and s.on_request(req("r1")) == []


def test_match_station():
    s = make(station="Conveyor 1", match_station=True)
    assert s.on_request(req("r1", station="Rack3")) == []
    assert s.on_request(req("r2")) == []
    assert answers(s.on_request(req("r3", station="Conveyor 1"))) == [("r3", "OK")]
    assert answers(s.on_request(req("r4", station="conveyor_1"))) == [("r4", "OK")]
    assert answers(make(station="Conveyor 1").on_request(req("r5", station="Rack3"))) == [("r5", "OK")]


def test_topic_mode_values():
    clock = Clock()
    s = make(clock, station="A", mode="topic", source_topic="/part", source_type="std_msgs/msg/Bool")
    assert s.on_request(req("r1")) == []  # no value yet: wait
    assert answers(s.on_value(True)) == [("r1", "OK")]
    assert answers(s.on_request(req("r2"))) == [("r2", "OK")]  # any earlier value counts (max_age_s 0)
    assert s.on_value(False) == []  # nothing pending
    assert answers(s.on_request(req("r3"))) == [("r3", "Reject")]
    assert s.value_answer(0) == "Reject" and s.value_answer(3) == "OK" and s.value_answer(0.0) == "Reject" and s.value_answer(2.5) == "OK"
    assert s.value_answer("Retry") == "Retry" and s.value_answer("") is None
    t = make(clock, mode="topic", source_topic="/s", source_type="std_msgs/msg/String", true_answer="Yes")
    t.on_request(req("q1"))
    assert t.on_value("") == [] and answers(t.on_value("Later")) == [("q1", "Later")]


def test_topic_mode_stale_value_waits_for_a_fresh_one():
    clock = Clock()
    s = make(clock, mode="topic", source_topic="/part", max_age_s=2.0)
    s.on_value(True)
    clock.t += 1.0
    assert answers(s.on_request(req("r1"))) == [("r1", "OK")]
    clock.t += 5.0
    assert s.on_request(req("r2")) == []  # stale
    assert s.on_timer() == []
    assert answers(s.on_value(False)) == [("r2", "Reject")]


def test_service_mode():
    clock = Clock()
    s = make(clock, mode="service", service="/washer/ready")
    acts = s.on_request(req("r1"))
    assert [(a.kind, a.request_id) for a in acts] == [("call", "r1")]
    assert s.on_service_result("r1", None) == []  # not reachable
    clock.t += 0.5
    assert s.on_timer() == []
    clock.t += 0.6
    assert [(a.kind, a.request_id) for a in s.on_timer()] == [("call", "r1")]
    assert answers(s.on_service_result("r1", True)) == [("r1", "OK")]
    assert s.on_service_result("r1", False) == []  # once per id
    s.on_request(req("r2"))
    assert answers(s.on_service_result("r2", False)) == [("r2", "Reject")]
    assert s.on_service_result("unknown", True) == []


def test_gpio_mode_press_answers_what_is_waiting():
    s = make(station="Rack3", mode="gpio", gpio_pins=[17, 27], gpio_answers=["Done", "Retry"])
    assert s.on_press(0) == []  # nothing waiting: the press is not kept for later
    assert s.on_request(req("r1")) == []
    assert s.on_press(5) == []
    assert answers(s.on_press(1)) == [("r1", "Retry")]
    assert s.on_press(0) == []


def test_expired_requests_are_dropped():
    clock = Clock()
    s = make(clock, mode="topic", source_topic="/part")
    s.on_request(req("r1", timeout_s=2))
    clock.t += 3.5
    s.on_timer()
    assert s.on_value(True) == [] and s.on_request(req("r1", timeout_s=2)) == []
