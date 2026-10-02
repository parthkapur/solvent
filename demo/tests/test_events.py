from demo.events import EventBus, read_events


def test_publish_stamps_time_and_buffers():
    ticks = iter([1.0, 2.0])
    bus = EventBus(clock=lambda: next(ticks))
    bus.publish({"lane": "client"})
    bus.publish({"lane": "client", "t": 5})
    assert [e["t"] for e in bus.buffer] == [1000, 5]


async def test_late_subscriber_gets_the_buffer_then_live_events():
    bus = EventBus()
    bus.publish({"n": 1})
    q = bus.subscribe()
    bus.publish({"n": 2})
    assert (await q.get())["n"] == 1
    assert (await q.get())["n"] == 2


async def test_close_ends_every_stream_including_later_ones():
    bus = EventBus()
    q = bus.subscribe()
    bus.close()
    assert await q.get() is None
    late = bus.subscribe()
    assert await late.get() is None


def test_publish_after_close_is_ignored():
    bus = EventBus()
    bus.close()
    bus.publish({"n": 1})
    assert bus.buffer == []


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "out" / "events.jsonl"
    bus = EventBus(path)
    bus.publish({"lane": "a", "t": 1})
    bus.publish({"lane": "b", "t": 2})
    bus.close()
    assert read_events(path) == [{"lane": "a", "t": 1}, {"lane": "b", "t": 2}]


def test_read_events_skips_a_truncated_last_line_and_blanks(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text('{"a":1}\n\n{"a":2}\n{"a"')
    assert read_events(path) == [{"a": 1}, {"a": 2}]
