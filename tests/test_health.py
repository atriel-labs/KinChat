from kinchat.health import HealthBoard


def test_health_transitions():
    t = [100.0]
    board = HealthBoard(clock=lambda: t[0])
    h = board.add("llm", "Claude API")
    assert h.state == "failed" and h.status.last_ok is None
    t[0] = 110
    h.ok()
    assert (h.state, h.status.last_ok, h.status.since) == ("ok", 110, 110)
    t[0] = 120
    h.degraded(TimeoutError("read timed out"))
    s = h.status
    assert (s.state, s.last_error, s.last_ok, s.since) == ("degraded", "TimeoutError: read timed out", 110, 120)
    h.failed("missing key")
    assert board["llm"].state == "failed" and [x.name for x in board.snapshot()] == ["llm"]
