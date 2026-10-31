import itertools
from collections.abc import AsyncIterator

from hypothesis import given
from hypothesis import strategies as st

from gateway.providers.sse import ServerSentEvent, iter_sse


async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def parse(*parts: bytes) -> list[ServerSentEvent]:
    return [event async for event in iter_sse(chunks_of(*parts))]


async def test_parses_events_with_types_and_default_type() -> None:
    events = await parse(b"event: delta\ndata: one\n\ndata: two\n\n")

    assert events == [ServerSentEvent("delta", "one"), ServerSentEvent("message", "two")]


async def test_joins_multiple_data_lines() -> None:
    assert await parse(b"data: a\ndata: b\n\n") == [ServerSentEvent("message", "a\nb")]


async def test_ignores_comments_unknown_fields_and_events_without_data() -> None:
    events = await parse(b": keep-alive\n\nid: 1\nretry: 10\nevent: ping\n\ndata: x\n\n")

    assert events == [ServerSentEvent("message", "x")]


async def test_only_one_leading_space_is_removed() -> None:
    assert await parse(b"data:x\n\ndata:  y\n\n") == [
        ServerSentEvent("message", "x"),
        ServerSentEvent("message", " y"),
    ]


async def test_accepts_crlf_and_cr_line_endings() -> None:
    events = await parse(b"data: a\r\n\r\ndata: b\r\rdata: c\n\n")

    assert [event.data for event in events] == ["a", "b", "c"]


async def test_drops_an_unterminated_final_event() -> None:
    assert await parse(b"data: a\n\ndata: partial") == [ServerSentEvent("message", "a")]


async def test_unicode_line_separators_are_not_line_breaks() -> None:
    payload = '{"text": "a' + "\u2028" + "b" + "\u0085" + 'c"}'

    events = await parse(f"data: {payload}\n\n".encode())

    assert events == [ServerSentEvent("message", payload)]


async def test_strips_leading_byte_order_mark() -> None:
    assert await parse(b"\xef\xbb", b"\xbfdata: a\n\n") == [ServerSentEvent("message", "a")]


event_strategy = st.builds(
    ServerSentEvent,
    event=st.sampled_from(["message", "delta", "content_block_delta"]),
    data=st.text(st.characters(exclude_characters="\r\n", exclude_categories=["Cs"])),
)


@given(
    events=st.lists(event_strategy, max_size=8),
    line_ending=st.sampled_from(["\n", "\r\n", "\r"]),
    split_points=st.lists(st.integers(min_value=0, max_value=10_000), max_size=20),
)
async def test_events_survive_arbitrary_chunk_boundaries(
    events: list[ServerSentEvent], line_ending: str, split_points: list[int]
) -> None:
    encoded = "".join(
        f"event: {event.event}{line_ending}data: {event.data}{line_ending}{line_ending}"
        for event in events
    ).encode()
    boundaries = sorted({0, len(encoded), *(point % (len(encoded) + 1) for point in split_points)})
    parts = [encoded[start:end] for start, end in itertools.pairwise(boundaries)]

    assert await parse(*parts) == events
