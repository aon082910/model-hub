"""Bounded HTTP JSON calls shared by the AI providers.

A vision-tagging or embedding response should be at most a few KB -- a
couple of short tags, one sentence, or a fixed-length embedding vector.
Without a cap, a provider that gets stuck generating (a known failure mode
especially on smaller/quantized local models, worse when the response is
forced into a JSON grammar it struggles to close) has httpx buffer the
entire runaway response into memory before we ever get to look at it and
discover it's garbage. A single such response has been observed to push a
memory-constrained container from safe to OOM-killed in under a second --
far too fast for any check made only between requests to catch. Streaming
the body and aborting once it crosses the cap means the oversized response
is never actually held in memory, catching the problem at its source
instead of after the fact.
"""
import json

import httpx

MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class ResponseTooLarge(RuntimeError):
    """Raised when a response body is aborted for exceeding the size cap."""


def post_json_bounded(
    url: str, json_payload: dict, timeout: float, headers: dict = None, max_bytes: int = MAX_RESPONSE_BYTES
) -> dict:
    """POST JSON, parse a JSON response, but abort while still streaming if
    the body exceeds max_bytes -- never fully buffers an oversized response."""
    with httpx.stream("POST", url, json=json_payload, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        chunks = bytearray()
        for chunk in resp.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise ResponseTooLarge(
                    f"response from {url} exceeded {max_bytes} bytes before finishing -- aborted"
                )
        return json.loads(bytes(chunks))
