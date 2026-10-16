from app.stream_parser import _suffix_for_cumulative


def test_first_value_is_emitted_whole():
    assert _suffix_for_cumulative("hello", "") == "hello"


def test_growing_value_emits_only_the_new_text():
    assert _suffix_for_cumulative("hello world", "hello") == " world"
    assert _suffix_for_cumulative("hello world!", "hello world") == "!"


def test_repeated_value_emits_nothing():
    assert _suffix_for_cumulative("same", "same") is None


def test_unrelated_value_is_emitted_whole():
    assert _suffix_for_cumulative("brand new", "something else") == "brand new"


def _fake_response(lines):
    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            for line in lines:
                yield line
    return FakeResponse()


def test_resending_a_growing_block_does_not_repeat_it():
    """Notion re-sends a block's whole value as it grows; only the new text may be emitted."""
    import json
    from app.stream_parser import parse_stream

    path = "/s/2/v/0/content"
    part1 = "甲" * 120
    part2 = "乙" * 60
    part3 = "丙" * 60
    lines = [
        json.dumps({"type": "patch", "v": [{"o": "a", "p": "/s/-", "v": {
            "type": "agent-inference", "value": [{"type": "text", "content": part1}]}}]}),
        json.dumps({"type": "patch", "v": [{"o": "x", "p": path, "v": part1 + part2}]}),
        json.dumps({"type": "patch", "v": [{"o": "x", "p": path, "v": part1 + part2 + part3}]}),
        json.dumps({"type": "patch", "v": [{"o": "x", "p": path, "v": part1 + part2 + part3}]}),
    ]
    emitted = "".join(str(i.get("text") or "") for i in parse_stream(_fake_response(lines)))
    assert emitted.count("甲" * 120) == 1, emitted.count("甲" * 120)
    assert emitted.count("乙" * 60) == 1, emitted.count("乙" * 60)
    assert emitted.count("丙" * 60) == 1, emitted.count("丙" * 60)
    assert len(emitted) < 400, len(emitted)
