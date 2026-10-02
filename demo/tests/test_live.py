import json

from starlette.testclient import TestClient

from demo import live
from demo.engine import Config, Engine
from demo.events import EventBus

H = {"x-demo-token": "tok"}


class StubEngine:
    def __init__(self, raises=None):
        self.calls, self.raises = [], raises

    def handle(self, action, **kw):
        if self.raises:
            raise self.raises
        self.calls.append((action, kw))
        return {"ok": True}


def client(engine=None, bus=None):
    engine = engine or StubEngine()
    app = live.make_app(bus or EventBus(), engine, "<p>__TOKEN__</p>", "tok")
    return TestClient(app, base_url="http://127.0.0.1"), engine


def test_control_needs_the_token():
    c, eng = client()
    assert c.post("/control", json={"action": "pause"}).status_code == 401
    assert c.post("/control", json={"action": "pause"},
                  headers={"x-demo-token": "wrong"}).status_code == 401
    assert c.post("/control", json={"action": "pause"},
                  headers={"x-demo-token": "é".encode()}).status_code == 401  # must not 500
    assert eng.calls == []


def test_control_passes_the_action_and_its_arguments():
    c, eng = client()
    r = c.post("/control", json={"action": "fault", "ms": 3000}, headers=H)
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert eng.calls == [("fault", {"ms": 3000})]


def test_control_rejects_a_bad_body():
    c, eng = client()
    assert c.post("/control", content=b"not json", headers=H).status_code == 400
    assert c.post("/control", json={"nope": 1}, headers=H).status_code == 400
    assert eng.calls == []


def test_an_engine_value_error_is_a_400_not_a_500():
    c, _ = client(StubEngine(raises=ValueError("ms")))
    assert c.post("/control", json={"action": "fault", "ms": "abc"}, headers=H).status_code == 400


def test_index_embeds_the_token():
    c, _ = client()
    assert c.get("/").text == "<p>tok</p>"


def test_events_replays_the_buffer_to_a_late_subscriber_and_ends_on_close():
    bus = EventBus()
    bus.publish({"lane": "client", "n": 1})
    bus.publish({"lane": "client", "n": 2})
    bus.close()
    c, _ = client(bus=bus)
    r = c.get("/events")
    assert r.headers["content-type"].startswith("text/event-stream")
    payloads = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert [p["n"] for p in payloads] == [1, 2]


def test_a_foreign_host_header_is_refused_so_dns_rebinding_cannot_reach_the_token():
    c, eng = client()
    assert c.get("/", headers={"host": "evil.com"}).status_code == 400
    assert c.get("/events", headers={"host": "evil.com"}).status_code == 400
    r = c.post("/control", json={"action": "pause"}, headers={**H, "host": "evil.com"})
    assert r.status_code == 400
    assert eng.calls == []
    assert c.get("/", headers={"host": "localhost:8765"}).status_code == 200


def test_infinite_or_huge_fault_ms_is_a_400_not_a_500():
    engine = Engine(Config(url="http://t", api_key="k"), EventBus())
    c = TestClient(live.make_app(EventBus(), engine, "x", "tok"), base_url="http://127.0.0.1")
    for body in (b'{"action":"fault","ms":Infinity}', b'{"action":"fault","ms":1e999}',
                 b'{"action":"fault","ms":NaN}', b'{"action":"fault","ms":"abc"}'):
        r = c.post("/control", content=body, headers={**H, "content-type": "application/json"})
        assert r.status_code == 400, body


def test_page_keeps_exactly_one_token_and_one_events_placeholder():
    # The server rewrites every "__TOKEN__" and the report rewrites the events marker; a second
    # occurrence of either (say in a property name) would be rewritten too and break the page.
    html = live.load_html()
    assert html.count("__TOKEN__") == 1
    assert html.count(live.EVENTS_MARKER) == 1


def test_inline_events_is_script_safe():
    html = "x<script>window.__EVENTS__=/*__EVENTS__*/null;</script>"
    out = live.inline_events(html, [{"reason": "</script><b>"}])
    assert "/*__EVENTS__*/" not in out
    assert out.count("</script>") == 1  # the payload cannot close the tag
