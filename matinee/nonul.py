"""One input layer that refuses a NUL byte, whatever form it arrives in.

Postgres cannot store U+0000 in a text value ("A string literal cannot contain
NUL (0x00) characters"), so one in a review's notes was an unhandled error and
a 500. The same goes for every free-text field. Rather than guard each handler,
this middleware answers 400 before any handler runs. Keep did the same for its
half of devondoes queue #612.

WHAT IT LOOKS AT, regardless of the Content-Type header (a client chooses that,
and a lying one must not skip the check):
  - the path and the query string, raw and percent-decoded;
  - the body as raw bytes, as a percent-encoded form (%00), and as text holding
    a JSON escape (backslash-u-0000), which also covers a JSON document posted
    as a form field, as /import/confirm's rows are;
  - a body that parses as JSON, walked key by key, and refused past a nesting
    depth of MAX_DEPTH (a deep document is how a parser is made to recurse);
  - multipart: every part that is not a file upload. An uploaded file's bytes
    are the file's own business (a UTF-16 CSV is full of NULs and gets the
    importer's sentence, not a 400).

A literal backslash-u-0000 typed into a text field is refused too: without
parsing every field as JSON it cannot be told from the escape.
"""
import json
import re
from urllib.parse import unquote, unquote_plus

MAX_DEPTH = 20
# Larger than the importer's 5 MB file limit, so that limit still gives its own
# sentence; anything beyond this is not a form somebody filled in.
MAX_BODY = 8 * 1024 * 1024

# The JSON escape for NUL, preceded by an even number of backslashes (an odd
# number means the backslash before the u is itself escaped: literal text).
_JSON_NUL = re.compile(r"(?<!\\)(?:\\\\)*\\u0000", re.IGNORECASE)
_BOUNDARY = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


def _text_has_nul(text: str) -> bool:
    return "\x00" in text or bool(_JSON_NUL.search(text))


def _bytes_have_nul(raw: bytes, percent_encoded: bool = True) -> bool:
    if b"\x00" in raw:
        return True
    text = raw.decode("utf-8", errors="replace")
    return _text_has_nul(text) or (percent_encoded and _text_has_nul(unquote_plus(text)))


def _json_bad(node, depth=0) -> bool:
    """True if a parsed JSON value is too deep or holds a NUL in a key or string."""
    if depth > MAX_DEPTH:
        return True
    if isinstance(node, str):
        return "\x00" in node
    if isinstance(node, list):
        return any(_json_bad(v, depth + 1) for v in node)
    if isinstance(node, dict):
        return any("\x00" in k or _json_bad(v, depth + 1) for k, v in node.items())
    return False


def _multipart_bad(body: bytes, content_type: str):
    """None if this is not a multipart body we can split; else whether a part
    other than a file upload holds a NUL."""
    m = _BOUNDARY.search(content_type)
    if not m or not content_type.lower().lstrip().startswith("multipart/"):
        return None
    delim = b"--" + m.group(1).encode("latin-1", errors="ignore")
    parts = body.split(delim)
    if len(parts) < 3:
        return None
    for part in parts[1:-1]:
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = part.partition(b"\n\n")
        if b"\x00" in head or _text_has_nul(head.decode("utf-8", errors="replace")):
            return True
        if b"filename=" in head.lower():
            continue
        if _bytes_have_nul(data, percent_encoded=False):   # nothing decodes %00 here
            return True
    return False


def body_is_bad(body: bytes, content_type: str) -> bool:
    if not body:
        return False
    verdict = _multipart_bad(body, content_type)
    if verdict is not None:
        return verdict
    if _bytes_have_nul(body):
        return True
    if body.lstrip()[:1] in (b"{", b"[", b'"'):
        try:
            return _json_bad(json.loads(body))
        except RecursionError:
            return True          # nested past what the parser will take
        except ValueError:
            return False         # not JSON after all: the checks above stand
    return False


def target_is_bad(scope) -> bool:
    raw_path = scope.get("raw_path") or scope.get("path", "").encode()
    query = scope.get("query_string", b"")
    for raw in (raw_path, query):
        if b"\x00" in raw:
            return True
        text = raw.decode("utf-8", errors="replace")
        if _text_has_nul(unquote(text)) or _text_has_nul(unquote_plus(text)):
            return True
    return False


async def _reply(send, status: int, text: str):
    body = text.encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RefuseNul:
    """Pure ASGI, so the body can be read here and handed on unchanged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        refusal = (400, "That contains a NUL character, which cannot be saved.")
        if target_is_bad(scope):
            return await _reply(send, *refusal)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        length = headers.get(b"content-length", b"0")
        if length.isdigit() and int(length) > MAX_BODY:
            return await _reply(send, 413, "That is too large.")

        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] != "http.request":      # the client went away
                return
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            more = message.get("more_body", False)
            if size > MAX_BODY:
                return await _reply(send, 413, "That is too large.")
        body = b"".join(chunks)
        if body_is_bad(body, headers.get(b"content-type", b"").decode("latin-1")):
            return await _reply(send, *refusal)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()                       # disconnects, as ever
        await self.app(scope, replay, send)
