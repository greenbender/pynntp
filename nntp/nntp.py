"""
An NNTP library - a bit more useful than the nntplib one (hopefully).
Copyright (C) 2013-2023  Byron Platt

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import io
import itertools
import math
import re
import socket
import ssl
import tempfile
import urllib.parse
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from typing import IO, TYPE_CHECKING, Literal, NoReturn, TypedDict

from . import utils
from .fifo import BytesFifo
from .headerdict import HeaderDict
from .types import SSLMode
from .yenc import YEnc, check_trailer, parse_keywords

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from types import TracebackType
    from typing import Self, Unpack
    from zlib import _Compress, _Decompress

    from .types import Newsgroup, Range

__all__ = [
    "BaseNNTPClient",
    "FromURLOptions",
    "NNTPClient",
    "NNTPDataError",
    "NNTPError",
    "NNTPPermanentError",
    "NNTPProtocolError",
    "NNTPReplyError",
    "NNTPSyncError",
    "NNTPTemporaryError",
    "NNTPYEncError",
]


# RFC 3977 section 3.6: "<", 1-248 printable US-ASCII characters except ">",
# then ">"
_MESSAGE_ID_RE = re.compile(r"<[!-=?-~]{1,248}>")

# URL scheme -> (use_ssl, ssl_mode) for NNTPClient.from_url()
_URL_SCHEMES = {
    "nntp": (False, SSLMode.IMPLICIT),
    "nntps": (True, SSLMode.IMPLICIT),
    "snews": (True, SSLMode.IMPLICIT),  # The historic name for nntps
    "nntp+starttls": (True, SSLMode.STARTTLS),
}
_URL_QUERY = ("timeout", "reader")
_URL_TRUE = ("1", "true", "yes", "on")
_URL_FALSE = ("0", "false", "no", "off")


class FromURLOptions(TypedDict, total=False):
    """Keyword arguments of NNTPClient.from_url(); each overrides the URL."""

    username: str
    password: str
    timeout: float | None
    ssl_context: ssl.SSLContext | None
    reader: bool


def _url_settings(parts: urllib.parse.SplitResult) -> FromURLOptions:
    """The constructor settings given by a from_url() URL."""
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if unknown := set(query) - set(_URL_QUERY):
        raise ValueError(f"Unsupported URL query parameter: {sorted(unknown)}")
    if repeated := sorted(name for name, values in query.items() if len(values) > 1):
        raise ValueError(f"Repeated URL query parameter: {repeated}")

    settings: FromURLOptions = {}
    if parts.username is not None:
        settings["username"] = urllib.parse.unquote(parts.username)
    if parts.password is not None:
        settings["password"] = urllib.parse.unquote(parts.password)
    if "timeout" in query:
        settings["timeout"] = _url_timeout(query["timeout"][0])
    if "reader" in query:
        settings["reader"] = _url_bool("reader", query["reader"][0])
    return settings


def _url_timeout(value: str) -> float | None:
    """Parse a URL timeout: seconds, or "none" to wait forever."""
    if value.lower() == "none":
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        if 0 < seconds < math.inf:  # Also rejects nan
            return seconds
    raise ValueError(f"Invalid URL timeout: {value!r}")


def _url_bool(name: str, value: str) -> bool:
    if value.lower() in _URL_TRUE:
        return True
    if value.lower() in _URL_FALSE:
        return False
    raise ValueError(f"Invalid URL {name}: {value!r}")


# RFC 3977 section 3.2: a status code is three digits, the first 1-5
_STATUS_CODE_RE = re.compile(rb"[1-5][0-9]{2}")

_DIGITS_RE = re.compile(r"[0-9]+")

# RFC 3977 3.4.1 (and 2980 for 282): replies followed by a multi-line block.
# 211 is left out: it is multi-line only for LISTGROUP.
_MULTILINE = frozenset({100, 101, 215, 220, 221, 222, 224, 225, 230, 231, 282})

_MAX_COMMAND = 512  # RFC 3977 3.1: command line length, including CRLF
_MAX_LINE = 0x10000  # Default max_line_length: a response line, including CRLF
_INFLATE_SIZE = 0x10000  # Most decompressed data produced at a time
_SEND_SIZE = 0x10000  # Batch POST body lines into writes of about this size
_SPOOL_SIZE = 0x100000  # Articles larger than this are prepared on disk


def _timestamp(timestamp: datetime) -> str:
    """Format a datetime as NEWGROUPS/NEWNEWS arguments (RFC 3977 7.3).

    Naive datetimes are taken as UTC. The zone token must be "GMT"; servers
    reject "UTC".
    """
    if timestamp.tzinfo:
        timestamp = timestamp.astimezone(UTC)
    # %Y isn't zero-padded to 4 digits on every platform
    return f"{timestamp.year:04d}{timestamp:%m%d %H%M%S} GMT"


def _after_compress_forbidden(words: list[str]) -> bool:
    """Whether RFC 8054 forbids a command, given its first two words in upper case."""
    if words[:1] in (["AUTHINFO"], ["STARTTLS"], ["COMPRESS"]):
        return True
    return words == ["MODE", "READER"]


def _msgid_article(msgid_article: str | int | None) -> str | None:
    return None if msgid_article is None else str(msgid_article)


def _inflate(inflate: _Decompress, data: bytes) -> bytes:
    """Decompress at most _INFLATE_SIZE bytes of output.

    Input that is not used yet is kept in inflate.unconsumed_tail and used
    first on the next call. If the output is _INFLATE_SIZE bytes long, more may
    be pending: call again, with b"" if there is no new input.

    Raises:
        NNTPDataError: If decompression fails.
    """
    try:
        return inflate.decompress(inflate.unconsumed_tail + data, _INFLATE_SIZE)
    except zlib.error as e:
        raise NNTPDataError("Decompression failed") from e


class NNTPError(Exception):
    """Base class for all NNTP errors."""


class NNTPSyncError(NNTPError):
    """The connection is busy or out of sync.

    Raised when a command is issued while the response to a previous command
    is still being read, or after a response was abandoned or failed part way
    through, leaving unread data on the connection, or after close(). Call
    reconnect() to recover.
    """


class NNTPReplyError(NNTPError):
    """The server replied with an unexpected status code.

    The code and message attributes hold the status line.
    """

    def __init__(self, code: int, message: str) -> None:
        """Create the error.

        Args:
            code: The status code.
            message: The status message.
        """
        self.code = code
        self.message = message
        super().__init__(code, message)

    def __str__(self) -> str:
        return f"{self.code} {self.message}"


class NNTPTemporaryError(NNTPReplyError):
    """A 4xx (temporary) error reply."""


class NNTPPermanentError(NNTPReplyError):
    """A 5xx (permanent) error reply."""


class NNTPProtocolError(NNTPError):
    """The server sent a status line that could not be parsed."""


class NNTPDataError(NNTPError):
    """The content of a response could not be parsed or failed a check."""


class NNTPYEncError(NNTPDataError):
    """yEnc data failed its size or CRC check.

    The decoded data is kept in `data`, so it can still be used, e.g. for
    par2 repair.
    """

    def __init__(self, message: str, data: bytes) -> None:
        super().__init__(message)
        self.data = data


class BaseNNTPClient:
    """NNTP connection, command and response handling.

    Responses compressed with XFEATURE COMPRESS GZIP or as yEnc-encoded zlib
    (XZVER, XZHDR), and the whole connection after COMPRESS DEFLATE, are
    decompressed transparently.

    A response line, after decompression and including its CRLF, may be at
    most max_line_length octets long (64 KiB by default; set it on the class
    or the instance to change it). A longer line raises NNTPDataError and
    marks the connection out of sync, without reading the rest of the line.
    """

    encoding = "utf-8"
    errors = "surrogateescape"
    max_line_length = _MAX_LINE

    def __init__(
        self,
        host: str,
        port: int | None = None,
        username: str = "",
        password: str = "",
        *,
        timeout: float | None = 30,
        use_ssl: bool = False,
        ssl_mode: SSLMode = SSLMode.IMPLICIT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Connect to the server and read the welcome message.

        Args:
            host: Server hostname.
            port: Server port. Defaults to 563 for implicit TLS, otherwise 119.
            username: Username, sent if the server asks for authentication.
                Without TLS it is sent in clear text, as is the password.
            password: Password, sent if the server asks for authentication.
            timeout: Socket timeout in seconds, or None to wait forever.
            use_ssl: Use TLS.
            ssl_mode: IMPLICIT (TLS from the start, usually port 563) or STARTTLS.
            ssl_context: TLS settings, e.g. to trust a private CA. Defaults to
                ssl.create_default_context() with TLS 1.2 as the minimum,
                which verifies the certificate.

        Raises:
            OSError: On socket or TLS errors.
            NNTPReplyError: If the server does not accept the connection.
        """
        self.username = username
        self.password = password
        if port is None:
            port = 563 if use_ssl and ssl_mode == SSLMode.IMPLICIT else 119
        self._address = (host, port)
        self._timeout = timeout
        self._use_ssl = use_ssl
        self._ssl_mode = ssl_mode
        self._ssl_context = ssl_context
        self._connect()

    def _connect(self) -> None:
        """Open the connection and read the welcome message."""
        self._buffer = BytesFifo()
        self._reading = False  # A multi-line response is being read
        self._broken = False  # Closed, or unread/unknown data on the socket
        self._deflate: _Compress | None = None  # Set by COMPRESS (RFC 8054)
        self._inflate: _Decompress | None = None
        self._inflate_more = False  # _inflate may have more output pending

        host = self._address[0]
        self.socket = socket.create_connection(self._address, timeout=self._timeout)
        try:
            if self._use_ssl and self._ssl_mode == SSLMode.IMPLICIT:
                self._enable_tls(host)

            code, message = self.status()
            if code not in (200, 201):
                raise NNTPReplyError(code, message)

            if self._use_ssl and self._ssl_mode == SSLMode.STARTTLS:
                self.command("STARTTLS", expect=382)
                if len(self._buffer):
                    # Data sent before TLS must not be read as if protected
                    raise NNTPProtocolError("Unexpected data after STARTTLS reply")
                self._enable_tls(host)
        except BaseException:
            self.close()
            raise

    def reconnect(self) -> None:
        """Close the connection and open a new one with the same settings.

        Use this to recover from NNTPSyncError. Session state such as the
        selected group, the current article and XFEATURE settings is not
        restored.
        """
        self.close()
        self._connect()

    @contextmanager
    def _sync(self) -> Iterator[None]:
        """Mark the connection broken if the wrapped exchange fails part way.

        An NNTP error reply means the status line was read in full, so the
        connection is still in sync.
        """
        try:
            yield
        except NNTPReplyError:
            raise
        except BaseException:
            self._broken = True
            raise

    def _enable_tls(self, host: str) -> None:
        context = self._ssl_context
        if context is None:
            context = ssl.create_default_context()
            # Python leaves the minimum to OpenSSL's configuration
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.socket = context.wrap_socket(
            self.socket,
            server_hostname=host,
        )

    def _recv(self, size: int = 4096) -> None:
        """Read data from the socket into the buffer.

        Raises:
            NNTPError: If the server closed the connection.
            OSError: On socket errors, including timeouts.
        """
        if self._inflate_more:
            data = b""  # Take the pending decompressed data first
        else:
            data = self.socket.recv(size)
            if not data:
                raise NNTPError("Failed to read from socket")
        self._buffer.write(self._decompress(data))

    def _decompress(self, data: bytes) -> bytes:
        """Inflate received data if COMPRESS is active.

        At most _INFLATE_SIZE bytes are returned; the rest is kept for the next
        call, so a small compressed chunk can't fill memory before the line
        length is checked.
        """
        if self._inflate is None:
            return data
        try:
            data = _inflate(self._inflate, data)
        except NNTPDataError:
            self.close()  # RFC 8054: close on corrupt compressed data
            raise
        if self._inflate.unused_data:
            # The stream lasts as long as the connection; an end means corruption
            self.close()
            raise NNTPDataError("Decompression failed: data after end of stream")
        self._inflate_more = len(data) == _INFLATE_SIZE
        return data

    def _send(self, data: bytes) -> None:
        """Write data to the socket, compressed if COMPRESS is active."""
        if self._deflate is not None:
            data = self._deflate.compress(data) + self._deflate.flush(zlib.Z_SYNC_FLUSH)
        self.socket.sendall(data)

    def _readline(self) -> bytes:
        """Read one line, receiving more data from the socket as needed.

        Raises:
            NNTPDataError: If the line is longer than max_line_length.
        """
        while not (line := self._buffer.readline()):
            if len(self._buffer) >= self.max_line_length:  # Too long, with CRLF
                self._line_too_long()
            self._recv()
        if len(line) > self.max_line_length:
            self._line_too_long()
        return line

    def _lines(self, buf: BytesFifo) -> Iterator[bytes]:
        """Yield the complete lines in buf, checking their length as _readline()."""
        while line := buf.readline():
            if len(line) > self.max_line_length:
                self._line_too_long()
            yield line
        if len(buf) >= self.max_line_length:
            self._line_too_long()

    def _line_too_long(self) -> NoReturn:
        self._broken = True  # The rest of the line is unread
        raise NNTPDataError(f"Line longer than {self.max_line_length} octets")

    def _read(self) -> bytes:
        """Read whatever is buffered, or the next recv if the buffer is empty."""
        while not (data := self._buffer.read()):
            self._recv()
        return data

    def status(self) -> tuple[int, str]:
        """Read a status line.

        Returns:
            The status code and message. The message is "" if there is none.

        Raises:
            NNTPError: If the server closed the connection.
            NNTPProtocolError: If the status line can't be parsed.
            NNTPDataError: If the status line is longer than max_line_length.
            NNTPTemporaryError: For status codes 400-499.
            NNTPPermanentError: For status codes 500-599.
        """
        line = self._readline().rstrip()
        parts = line.split(None, 1)
        if not parts or not _STATUS_CODE_RE.fullmatch(parts[0]):
            raise NNTPProtocolError(line.decode(self.encoding, self.errors))
        code = int(parts[0])
        message = parts[1].decode(self.encoding, self.errors) if len(parts) > 1 else ""

        if 400 <= code <= 499:
            raise NNTPTemporaryError(code, message)

        if 500 <= code <= 599:
            raise NNTPPermanentError(code, message)

        return code, message

    def _info_plain(self) -> Iterator[bytes]:
        """Yield the lines of a multi-line response, up to its terminator.

        Leading dots added by the server (dot-stuffing) are removed.
        """
        while True:
            line = self._readline()
            if line == b".\r\n":
                return
            yield line[1:] if line.startswith(b".") else line

    def _info_gzip(self) -> Iterator[bytes]:
        """Yield the lines of a gzip-compressed multi-line response.

        Handles the terminator both after the compressed data (XFEATURE COMPRESS
        GZIP) and inside it (XFEATURE COMPRESS GZIP TERMINATOR). The lines are the
        same as _info_plain() would give for the uncompressed response.

        Raises:
            NNTPDataError: If decompression fails, or a line is too long.
        """
        inflate = zlib.decompressobj(15 + 32)

        done, more, buf = False, False, BytesFifo()
        while not done:
            if inflate.eof:
                # Data after the end of the gzip stream (the terminator).
                buf.write(self._read())
            else:
                data = _inflate(inflate, b"" if more else self._read())
                more = len(data) == _INFLATE_SIZE
                buf.write(data)
                buf.write(inflate.unused_data)
            for line in self._lines(buf):
                if line == b".\r\n":
                    done = True
                    break
                yield line[1:] if line.startswith(b".") else line

        # With TERMINATOR, the end of the gzip stream can follow the terminator
        while not inflate.eof:
            data = _inflate(inflate, b"" if more else self._read())
            more = len(data) == _INFLATE_SIZE
            buf.write(inflate.unused_data)
        # Anything left over belongs to the next reply
        self._buffer.write(buf.read())

    def _info_yenczlib(self) -> Iterator[bytes]:
        """Yield the lines of a yEnc-encoded, zlib-compressed response.

        Used by XZVER and XZHDR. The lines are the same as _info_plain() would
        give for the uncompressed command.

        Raises:
            NNTPDataError: If the yEnc header or trailer is bad, the size or CRC
                check fails, decompression fails or ends early, or a line is
                too long.
        """
        decoder = YEnc()
        inflate = zlib.decompressobj(-15)
        lines = self._info_plain()

        header = next(lines, None)
        if header is None:  # An empty response
            return
        if not header.startswith(b"=ybegin "):
            raise NNTPDataError("Bad yEnc header")

        buf, trailer, size = BytesFifo(), b"", 0
        for line in lines:
            if line.startswith(b"=yend"):
                trailer = line
                continue
            data = decoder.decode(line)
            size += len(data)
            while True:
                data = _inflate(inflate, data)
                buf.write(data)
                yield from self._lines(buf)
                if len(data) < _INFLATE_SIZE:
                    break
                data = b""  # More output may be pending

        if not trailer:
            raise NNTPDataError("Missing yEnc trailer")
        if mismatch := check_trailer(trailer, size, decoder.crc32, part=False):
            raise NNTPDataError(f"Bad yEnc data: {mismatch}")
        if not inflate.eof:
            raise NNTPDataError("Decompression failed: compressed data is truncated")

    def _info(self, code: int, message: str, yz: bool = False) -> Iterator[bytes]:
        """Return the lines of a multi-line response as bytes.

        The connection is busy until the iterator is exhausted; if it is abandoned
        or fails part way, the connection is marked out of sync.

        Args:
            code: The status code (unused).
            message: The status message; "COMPRESS=GZIP" in it selects gzip.
            yz: The response is yEnc-encoded zlib (XZVER, XZHDR).
        """
        if "COMPRESS=GZIP" in message:
            lines = self._info_gzip()
        else:
            lines = self._info_yenczlib() if yz else self._info_plain()
        self._reading = True  # Set now, not on first next()
        return self._response(lines)

    def _response(self, lines: Iterator[bytes]) -> Iterator[bytes]:
        buffer, complete = self._buffer, False
        try:
            while True:
                # After reconnect() the next line would come from the new
                # connection, not from this response
                if self._buffer is not buffer:
                    raise NNTPSyncError(
                        "Response iterator used after reconnect(); its response is gone"
                    )
                try:
                    line = next(lines)
                except StopIteration:
                    break
                yield line
            complete = True
        finally:
            # An iterator abandoned before reconnect() must not touch the new
            # connection when it is finally closed
            if self._buffer is buffer:
                self._reading = False
                if not complete:
                    self._broken = True

    @contextmanager
    def _data_errors(self, what: str) -> Iterator[None]:
        """Raise NNTPDataError for a ValueError from parsing a response.

        If a response is being read, the rest of it is left unread, so the
        connection is marked out of sync now: the response iterator may not be
        closed for a while, as the exception's traceback can keep it alive.
        """
        try:
            yield
        except ValueError as e:
            if self._reading:
                self._reading, self._broken = False, True
            raise NNTPDataError(f"Invalid {what}: {e}") from e

    def info(self, code: int, message: str, yz: bool = False) -> Iterator[str]:
        """Return the lines of a multi-line response as strings.

        Read the iterator to the end before issuing another command; abandoning
        it part way leaves the connection out of sync.

        Args:
            code: The status code (unused).
            message: The status message; "COMPRESS=GZIP" in it selects gzip.
            yz: The response is yEnc-encoded zlib (XZVER, XZHDR).
        """
        return self._decode(self._info(code, message, yz))

    def _decode(self, lines: Iterator[bytes]) -> Iterator[str]:
        return (line.decode(self.encoding, self.errors) for line in lines)

    def command(
        self,
        verb: str,
        args: str | None = None,
        *,
        expect: int | Iterable[int] | None = None,
    ) -> tuple[int, str]:
        """Send a command and read its status line.

        If the server replies 480, AUTHINFO USER/PASS is sent and the command
        retried, except for AUTHINFO, MODE READER and STARTTLS, where the 480
        is raised. A full command line can also be passed as verb.

        Args:
            verb: The command.
            args: The command arguments, if any.
            expect: The status code, or codes, that mean success. Any other
                reply raises NNTPReplyError.

        Returns:
            The status code and message.

        Raises:
            ValueError: If the command contains CR, LF or NUL, or is longer
                than 512 octets. Nothing is sent.
            NNTPSyncError: If a response is still being read, or the connection
                is closed or out of sync.
            NNTPReplyError: If authentication fails, or the reply is not one
                of expect. A multi-line block after such a reply is read
                first, so the connection stays in sync.
            NNTPTemporaryError: For status codes 400-499.
            NNTPPermanentError: For status codes 500-599.
            NNTPProtocolError: If the status line can't be parsed.
        """
        code, message = self._command(verb, args)
        if expect is not None:
            codes = (expect,) if isinstance(expect, int) else tuple(expect)
            if code not in codes:
                self._unexpected(code, message)
        return code, message

    def _unexpected(self, code: int, message: str) -> NoReturn:
        """Raise NNTPReplyError, first reading any multi-line block that follows."""
        if code in _MULTILINE:
            for _ in self._info(code, message):
                pass
        raise NNTPReplyError(code, message)

    def _command(
        self, verb: str, args: str | None, *, login: bool = True
    ) -> tuple[int, str]:
        if self._reading:
            raise NNTPSyncError(
                "Command issued while a response is being read; read it to the "
                "end, or call reconnect()"
            )
        if self._broken:
            raise NNTPSyncError("Connection is closed or out of sync; call reconnect()")

        cmd = f"{verb} {args}" if args else verb
        words = cmd.upper().split()[:2]
        # Never put credentials into an error message
        shown = f"{' '.join(cmd.split()[:2])} ..." if words[:1] == ["AUTHINFO"] else cmd
        if any(c in cmd for c in "\r\n\0"):
            raise ValueError(f"Command contains CR, LF or NUL: {shown!r}")
        if self._deflate is not None and _after_compress_forbidden(words):
            raise NNTPError(f"{words[0]} is not allowed after COMPRESS (RFC 8054)")
        line = (cmd + "\r\n").encode(self.encoding, self.errors)
        if len(line) > _MAX_COMMAND:
            raise ValueError(f"Command longer than {_MAX_COMMAND} octets: {shown!r}")

        with self._sync():
            self._send(line)
            try:
                return self.status()
            except NNTPTemporaryError as e:
                # No login for AUTHINFO itself (it would recurse), nor for
                # MODE READER (RFC 4643 2.2: not allowed after a login)
                if (
                    e.code != 480
                    or not self.username
                    or words[:1] == ["AUTHINFO"]
                    or words == ["MODE", "READER"]
                ):
                    raise
                if not login:
                    # Logged in already; logging in again could go on forever
                    raise NNTPTemporaryError(
                        e.code, f"{e.message} (still asked to log in after logging in)"
                    ) from e
                if words[:1] == ["STARTTLS"]:
                    # Logging in now would send the password before TLS
                    raise NNTPTemporaryError(
                        e.code, f"{e.message} (won't log in before STARTTLS)"
                    ) from e
                if self._deflate is not None:
                    # RFC 8054: never authenticate on a compressed connection
                    raise NNTPTemporaryError(
                        e.code, f"{e.message} (can't authenticate after COMPRESS)"
                    ) from e

        self.login()
        return self._command(verb, args, login=False)

    def login(self, username: str | None = None, password: str | None = None) -> None:
        """AUTHINFO USER and PASS commands.

        Logs in now rather than waiting for the server to ask (480). Use it
        before compress() on servers that only ask for a login later, since
        AUTHINFO isn't allowed once compression is active. Without TLS the
        credentials are sent in clear text.

        See <https://datatracker.ietf.org/doc/html/rfc4643>

        Args:
            username: Username. Defaults to the constructor's; a value given
                here replaces it, including for reconnect().
            password: Password, likewise.

        Raises:
            ValueError: If there is no username, or a credential contains CR,
                LF or NUL or makes its command longer than 512 octets. Nothing
                is sent.
            NNTPReplyError: If the login is rejected, or a password is asked
                for and there is none.
        """
        username = self.username if username is None else username
        password = self.password if password is None else password
        if not username:
            raise ValueError("No username to log in with")
        if any(c in username + password for c in "\r\n\0"):
            raise ValueError("Username or password contains CR, LF or NUL")
        for verb, value in (("AUTHINFO USER", username), ("AUTHINFO PASS", password)):
            line = f"{verb} {value}\r\n".encode(self.encoding, self.errors)
            if len(line) > _MAX_COMMAND:
                raise ValueError(f"{verb} would be longer than {_MAX_COMMAND} octets")
        self.username, self.password = username, password
        code, message = self._command("AUTHINFO USER", self.username)
        if code == 381:
            if not self.password:
                raise NNTPReplyError(code, f"{message} (no password)")
            code, message = self._command("AUTHINFO PASS", self.password)
        if code != 281:
            self._unexpected(code, message)

    def close(self) -> None:
        """Close the connection without sending QUIT.

        After this, only reconnect() can be used.
        """
        self._broken = True
        self.socket.close()


class NNTPClient(BaseNNTPClient):
    """NNTP client implementing the common reader commands.

    Commands that can return a lot of data (LIST, XOVER, HDR, ...) return
    iterators that stream the response, so memory use stays low and data can be
    processed as it arrives. The command is sent and its status line read when
    the method is called, so a reply error is raised there. Read each iterator
    to the end before issuing the next command; abandoning one part way leaves
    the connection out of sync (see NNTPSyncError and reconnect()). That
    includes never reading it: calling e.g. xover() and dropping the result
    blocks the connection until reconnect().

    Any command can raise NNTPReplyError (and its subclasses), NNTPSyncError,
    NNTPProtocolError, NNTPDataError (e.g. for a line longer than
    max_line_length) or OSError.
    """

    def __init__(
        self,
        host: str,
        port: int | None = None,
        username: str = "",
        password: str = "",
        *,
        timeout: float | None = 30,
        use_ssl: bool = False,
        ssl_mode: SSLMode = SSLMode.IMPLICIT,
        ssl_context: ssl.SSLContext | None = None,
        reader: bool = True,
    ) -> None:
        """Connect to the server and, by default, enter reader mode.

        Args:
            host: Server hostname.
            port: Server port. Defaults to 563 for implicit TLS, otherwise 119.
            username: Username, sent if the server asks for authentication.
                Without TLS it is sent in clear text, as is the password.
            password: Password, sent if the server asks for authentication.
            timeout: Socket timeout in seconds, or None to wait forever.
            use_ssl: Use TLS.
            ssl_mode: IMPLICIT (TLS from the start, usually port 563) or STARTTLS.
            ssl_context: TLS settings, e.g. to trust a private CA. Defaults to
                ssl.create_default_context() with TLS 1.2 as the minimum,
                which verifies the certificate.
            reader: Send MODE READER after connecting, and after reconnect().

        Raises:
            OSError: On socket or TLS errors.
            NNTPReplyError: If the server does not accept the connection.
        """
        self._reader = reader
        super().__init__(
            host=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            use_ssl=use_ssl,
            ssl_mode=ssl_mode,
            ssl_context=ssl_context,
        )

    @classmethod
    def from_url(cls, url: str, **options: Unpack[FromURLOptions]) -> Self:
        """Connect using a URL such as "nntps://user:password@news.example.com".

        Schemes: "nntp" (plain text, port 119), "nntps" or "snews" (implicit
        TLS, port 563) and "nntp+starttls" (STARTTLS, port 119). A port in the
        URL overrides the default.

        The username and password come from the URL's user info
        ("user:password@", percent-encoded). The query can set timeout
        (seconds, or "none") and reader ("true"/"false", "1"/"0", "yes"/"no" or
        "on"/"off").

        Keyword arguments override the URL, and the constructor's defaults
        apply to anything set by neither. Passing the password as an argument
        keeps it out of the URL, which tends to end up in logs.

        Args:
            url: The server URL. It may not have a path or fragment.
            **options: username, password, timeout, ssl_context and reader, as
                for the constructor.

        Raises:
            ValueError: If the URL is not valid for NNTP: an unknown scheme or
                query parameter, a repeated parameter, a path or fragment, no
                host, or a bad port, timeout or reader value. The message never
                includes the URL, which may hold a password.
        """
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in _URL_SCHEMES:
            raise ValueError(f"Unsupported URL scheme: {parts.scheme!r}")
        if not parts.hostname:
            raise ValueError("NNTP URL has no host")
        if parts.path not in ("", "/") or parts.fragment:
            raise ValueError("NNTP URL may not have a path or fragment")
        if parts.port == 0:  # Accessing port raises ValueError if it is invalid
            raise ValueError("NNTP URL port may not be 0")

        # The options override the URL; anything set by neither is left to the
        # constructor's defaults.
        settings = _url_settings(parts)
        settings.update(options)

        use_ssl, ssl_mode = _URL_SCHEMES[scheme]
        return cls(
            parts.hostname,
            parts.port,
            use_ssl=use_ssl,
            ssl_mode=ssl_mode,
            **settings,
        )

    def _connect(self) -> None:
        super()._connect()
        if self._reader:
            try:
                try:
                    self.mode_reader()
                except NNTPTemporaryError as e:
                    if e.code != 480 or not self.username:
                        raise
                    self.login()  # RFC 4643 2.2: no MODE READER after logging in
            except BaseException:
                self.close()
                raise

    def __enter__(self) -> Self:
        """Return the client, for use in a with statement."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        """Send QUIT, or just close the connection if it is busy or out of sync.

        If QUIT fails, the connection is closed and the error raised, unless
        the with block is already raising.
        """
        if self._reading or self._broken:  # Closed, or QUIT can't be sent
            self.close()
            return False
        try:
            self.quit()
        except (NNTPError, OSError):
            self.close()
            if exc_type is None:
                raise
            # Otherwise let the with block's own exception propagate
        return False

    # session administration commands
    def capabilities(self, keyword: str | None = None) -> Iterator[str]:
        """CAPABILITIES command.

        Not every server implements it, so expect NNTPPermanentError.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-5.2>

        Args:
            keyword: Passed to the server; RFC 3977 defines no use for it.

        Yields:
            Each capability line, starting with VERSION.
        """
        code, message = self.command("CAPABILITIES", keyword, expect=101)
        lines = self.info(code, message)

        def capabilities() -> Iterator[str]:
            for line in lines:
                yield line.strip()

        return capabilities()

    def mode_reader(self) -> bool:
        """MODE READER command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-5.3>

        Returns:
            Whether posting is allowed.
        """
        code, _ = self.command("MODE READER", expect=(200, 201))

        return code == 200

    def compress(self) -> None:
        """COMPRESS DEFLATE command.

        Compresses everything sent and received on this connection from now
        on: commands, replies and articles. Use it after MODE READER and
        any login; once it is active, AUTHINFO, STARTTLS, MODE READER and
        COMPRESS are refused, and a server asking for a login (480) raises
        rather than being sent credentials. reconnect() does not restore it.

        INN before 2.7.2 can hang on POST or IHAVE over COMPRESS without TLS.

        See <https://datatracker.ietf.org/doc/html/rfc8054>

        Raises:
            NNTPError: If compression (COMPRESS or TLS-level) is already active.
            NNTPReplyError: If the server refuses (403, or 502 if unavailable).
        """
        if self._deflate is not None:
            raise NNTPError("Compression is already active")
        tls_compression = getattr(self.socket, "compression", None)
        if tls_compression is not None and tls_compression():
            raise NNTPError("TLS-level compression is already active (RFC 8054)")
        self.command("COMPRESS", "DEFLATE", expect=206)
        self._deflate = zlib.compressobj(wbits=-15)
        self._inflate = zlib.decompressobj(wbits=-15)
        # Compression starts right after the 206 line, so anything already
        # buffered behind it is compressed too.
        if pending := self._buffer.read():
            self._buffer.write(self._decompress(pending))

    def quit(self) -> None:
        """QUIT command.

        Asks the server to close the connection, then closes it. If the
        connection is out of sync use close() instead. After this, only
        reconnect() can be used.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-5.4>
        """
        self.command("QUIT", expect=205)

        self.close()

    # information commands
    def date(self) -> datetime:
        """DATE command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.1>

        Returns:
            The server's current time, in UTC.

        Raises:
            NNTPDataError: If the time can't be parsed.
        """
        _, message = self.command("DATE", expect=111)
        # The server may add text after the date (RFC 3977 section 3.2)
        value = message.split(None, 1)[0] if message.strip() else message

        with self._data_errors("DATE response"):
            return utils.parse_date(value)

    def help(self) -> str:
        """HELP command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.2>

        Returns:
            The help text.
        """
        code, message = self.command("HELP", expect=100)

        return "".join(self.info(code, message))

    def newgroups(self, timestamp: datetime) -> Iterator[Newsgroup]:
        """NEWGROUPS command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.3>

        Args:
            timestamp: List groups created since this time. A naive datetime is
                taken as UTC.

        Yields:
            Each new newsgroup.

        Raises:
            NNTPDataError: If a line can't be parsed.
        """
        code, message = self.command("NEWGROUPS", _timestamp(timestamp), expect=231)
        lines = self.info(code, message)

        def groups() -> Iterator[Newsgroup]:
            for line in lines:
                with self._data_errors("NEWGROUPS line"):
                    group = utils.parse_newsgroup(line)
                yield group

        return groups()

    def newnews(self, pattern: str, timestamp: datetime) -> Iterator[str]:
        """NEWNEWS command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.4>

        Args:
            pattern: Wildmat matching the newsgroups of interest.
            timestamp: List articles posted since this time. A naive datetime is
                taken as UTC.

        Yields:
            The message-id of each new article.
        """
        code, message = self.command(
            "NEWNEWS", f"{pattern} {_timestamp(timestamp)}", expect=230
        )
        lines = self.info(code, message)

        def message_ids() -> Iterator[str]:
            for line in lines:
                yield line.strip()

        return message_ids()

    # list commands
    def list(
        self,
        keyword: str | None = None,
        args: str | None = None,
        *,
        success: Iterable[int] = (215,),
    ) -> Iterator[str]:
        """LIST command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.6.1>

        Args:
            keyword: The list to return, e.g. "ACTIVE" or "NEWSGROUPS". The server
                default (ACTIVE) if omitted.
            args: Keyword-specific arguments, such as a wildmat.
            success: Status codes that mean success.

        Returns:
            The lines of the response.
        """
        if keyword:
            keyword = keyword.upper()

        cmd = f"LIST {keyword}" if keyword else "LIST"

        code, message = self.command(cmd, args, expect=success)

        return self.info(code, message)

    def list_active(self, pattern: str | None = None) -> Iterator[Newsgroup]:
        """LIST ACTIVE command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.6.3>

        Args:
            pattern: Wildmat matching the newsgroups of interest.

        Yields:
            Each matching newsgroup.

        Raises:
            NNTPDataError: If a line can't be parsed.
        """
        lines = self.list("ACTIVE", args=pattern)

        def groups() -> Iterator[Newsgroup]:
            for line in lines:
                with self._data_errors("LIST ACTIVE line"):
                    group = utils.parse_newsgroup(line)
                yield group

        return groups()

    def list_active_times(self) -> Iterator[tuple[str, datetime, str]]:
        """LIST ACTIVE.TIMES command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.6.4>

        Yields:
            The name, creation time and creator of each newsgroup.

        Raises:
            NNTPDataError: If a line can't be parsed.
        """
        lines = self.list("ACTIVE.TIMES")

        def times() -> Iterator[tuple[str, datetime, str]]:
            for line in lines:
                with self._data_errors("LIST ACTIVE.TIMES line"):
                    name, created, creator = utils.parse_active_time(line)
                yield name, created, creator

        return times()

    def list_headers(
        self, variant: Literal["MSGID", "RANGE"] | None = None
    ) -> Iterator[str]:
        """LIST HEADERS command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-8.6>

        Args:
            variant: "MSGID" or "RANGE" for the fields available to that form of
                HDR, or None for all.

        Yields:
            Each field name.
        """
        lines = self.list("HEADERS", args=variant)

        def headers() -> Iterator[str]:
            for line in lines:
                yield line.strip()

        return headers()

    def list_newsgroups(
        self,
        pattern: str | None = None,
    ) -> Iterator[tuple[str, str]]:
        """LIST NEWSGROUPS command.

        Servers may leave out groups that have no description; use
        list_active() to find every group.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-7.6.6>

        Args:
            pattern: Wildmat matching the newsgroups of interest.

        Yields:
            The name and description of each matching newsgroup.
        """
        lines = self.list("NEWSGROUPS", args=pattern)

        def groups() -> Iterator[tuple[str, str]]:
            for line in lines:
                with self._data_errors("LIST NEWSGROUPS line"):
                    name, description = utils.parse_newsgroup_description(line)
                yield name, description

        return groups()

    def list_overview_fmt(self) -> Iterator[tuple[str, bool]]:
        """LIST OVERVIEW.FMT command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-8.4>

        Yields:
            The name of each overview field, in order, and whether it is a "full"
            field (its value includes the header name, e.g. Xref).

        Raises:
            NNTPDataError: If a line can't be parsed.
        """
        lines = self.list("OVERVIEW.FMT")

        def fields() -> Iterator[tuple[str, bool]]:
            for line in lines:
                with self._data_errors("LIST OVERVIEW.FMT line"):
                    name, full = utils.parse_overview_fmt(line)
                yield name, full

        return fields()

    def list_extensions(self) -> Iterator[str]:
        """LIST EXTENSIONS command.

        See <https://datatracker.ietf.org/doc/html/draft-ietf-nntpext-base-20#section-5.3>

        Yields:
            Each extension name.
        """
        lines = self.list("EXTENSIONS", success=(202,))

        def extensions() -> Iterator[str]:
            for line in lines:
                yield line.strip()

        return extensions()

    def group(self, name: str) -> tuple[int, int, int, str]:
        """GROUP command.

        Selects a newsgroup.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.1.1>

        Args:
            name: The newsgroup.

        Returns:
            The estimated number of articles, the first and last article numbers,
            and the group name.

        Raises:
            NNTPReplyError: If there is no such newsgroup.
            NNTPDataError: If the reply can't be parsed.
        """
        _, message = self.command("GROUP", name, expect=211)

        parts = message.split(None, 4)
        try:
            total = int(parts[0])
            first = int(parts[1])
            last = int(parts[2])
            group = parts[3]
        except (IndexError, ValueError) as e:
            raise NNTPDataError(f'Invalid GROUP status "{message}"') from e

        return total, first, last, group

    def listgroup(
        self, name: str | None = None, range: Range | None = None
    ) -> Iterator[int]:
        """LISTGROUP command.

        Selects a newsgroup, like group(), and lists its article numbers.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.1.2>

        Args:
            name: The newsgroup. None (the default) uses the selected group.
            range: Only list these article numbers: an article number, or a
                (first,) or (first, last) range. Requires name.

        Yields:
            Each article number.

        Raises:
            ValueError: If range is given without name.
            NNTPReplyError: If there is no such newsgroup, or none selected.
            NNTPDataError: If a line can't be parsed.
        """
        if range is not None and name is None:
            raise ValueError("LISTGROUP needs a group name to use a range")
        args = name
        if range is not None:
            args = f"{name} {utils.unparse_range(range)}"

        code, message = self.command("LISTGROUP", args, expect=211)
        lines = self.info(code, message)

        def numbers() -> Iterator[int]:
            for line in lines:
                with self._data_errors("LISTGROUP line"):
                    number = int(line)
                    if number < 1:
                        raise ValueError(f"{number} is not an article number")
                yield number

        return numbers()

    def next(self) -> tuple[int, str]:
        """NEXT command.

        Moves to the next article in the selected newsgroup.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.1.4>

        Returns:
            The article number and message-id.

        Raises:
            NNTPReplyError: If there is no next article or no newsgroup selected.
            NNTPDataError: If the reply can't be parsed.
        """
        return self._select("NEXT")

    def last(self) -> tuple[int, str]:
        """LAST command.

        Moves to the previous article in the selected newsgroup.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.1.3>

        Returns:
            The article number and message-id.

        Raises:
            NNTPReplyError: If there is no previous article or no newsgroup
                selected.
            NNTPDataError: If the reply can't be parsed.
        """
        return self._select("LAST")

    def stat(self, msgid_article: str | int | None = None) -> tuple[int, str]:
        """STAT command.

        Checks that an article exists, and selects it if given by number.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.2.4>

        Args:
            msgid_article: A message-id or article number. None (the default)
                uses the current article.

        Returns:
            The article number and message-id. The number is 0 when a
            message-id is given.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPDataError: If the reply can't be parsed.
        """
        return self._select("STAT", _msgid_article(msgid_article))

    def _select(self, verb: str, args: str | None = None) -> tuple[int, str]:
        _, message = self.command(verb, args, expect=223)

        parts = message.split(None, 2)
        if (
            len(parts) < 2
            or not _DIGITS_RE.fullmatch(parts[0])
            or not _MESSAGE_ID_RE.fullmatch(parts[1])
        ):
            raise NNTPDataError(f'Invalid {verb} status "{message}"')
        return int(parts[0]), parts[1]

    def _read_body(self, lines: Iterator[bytes], decode: bool | None = None) -> bytes:
        """Read an article body, decoding yEnc if decode is set or detected.

        Detection: the body is yEnc if its first non-blank line starts with
        "=ybegin ". Every path reads the response to its end.

        Raises:
            NNTPYEncError: As for _decode_yenc().
        """
        if decode is not None:
            return self._decode_yenc(lines) if decode else b"".join(lines)
        blank: list[bytes] = []
        for line in lines:
            if line.startswith(b"=ybegin "):
                # Leading blank lines are not part of the yEnc data
                return self._decode_yenc(itertools.chain([line], lines))
            if line != b"\r\n":
                return b"".join(itertools.chain(blank, [line], lines))
            blank.append(line)
        return b"".join(blank)

    def _decode_yenc(self, lines: Iterator[bytes]) -> bytes:
        """Decode yEnc lines, checking each block against its =yend trailer.

        The size is checked, and pcrc32 for a part of a multipart post or
        crc32 otherwise. The whole response is read before a mismatch is
        raised, so the connection stays in sync.

        Only lines between =ybegin and =yend are decoded; text outside them,
        such as a signature, is dropped.

        Raises:
            NNTPYEncError: If yEnc data does not match its trailer, a =ybegin
                has no =yend, or there is no =ybegin. The decoded body is in
                its data attribute.
        """
        body: list[bytes] = []
        encoded: list[bytes] = []  # yEnc data lines not yet decoded
        decoder, size, multipart, open_block = YEnc(), 0, False, False
        seen_block = False
        error: str | None = None

        def decode_pending() -> None:
            # Decoding a block in one call is much faster than line by line
            nonlocal size
            if encoded:
                data = decoder.decode(b"".join(encoded))
                encoded.clear()
                size += len(data)
                body.append(data)

        for line in lines:
            if line[:2] != b"=y":
                if open_block:  # Text outside blocks (e.g. a signature) is dropped
                    encoded.append(line)
                continue
            decode_pending()
            if line.startswith(b"=ybegin "):
                decoder, size, open_block, seen_block = YEnc(), 0, True, True
                multipart = "part" in parse_keywords(line)
            elif line.startswith(b"=yend"):
                mismatch = check_trailer(line, size, decoder.crc32, part=multipart)
                error = error or mismatch
                open_block = False
            # =ypart and other =y lines carry nothing to decode

        decode_pending()
        if open_block:
            error = error or "missing =yend trailer"
        elif not seen_block:
            error = "no =ybegin line"
        data = b"".join(body)
        if error:
            raise NNTPYEncError(f"Bad yEnc data: {error}", data)
        return data

    def article(
        self,
        msgid_article: str | int | None = None,
        decode: bool | None = None,
    ) -> tuple[int, HeaderDict, bytes]:
        """ARTICLE command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.2.1>

        Args:
            msgid_article: A message-id or article number. None (the default)
                uses the current article.
            decode: Decode yEnc. None (the default) detects it from the body.

        Returns:
            The article number, headers and body. The body is bytes.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPDataError: If the reply or headers can't be parsed.
            NNTPYEncError: If yEnc data fails its size or CRC check. The decoded
                body is in its data attribute.
        """
        code, message = self.command(
            "ARTICLE", _msgid_article(msgid_article), expect=220
        )

        # Headers and body come from one response: parse_headers() stops at
        # the blank line and _read_body() reads the rest.
        lines = self._info(code, message)
        with self._data_errors("article headers"):
            headers = utils.parse_headers(self._decode(lines))
        body = self._read_body(lines, decode=decode)

        # Parsed only now, so a bad number never leaves the article unread
        parts = message.split(None, 1)
        try:
            articleno = int(parts[0])
        except (IndexError, ValueError) as e:
            raise NNTPDataError(f'Invalid ARTICLE status "{message}"') from e

        return articleno, headers, body

    def head(self, msgid_article: str | int | None = None) -> HeaderDict:
        """HEAD command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.2.2>

        Args:
            msgid_article: A message-id or article number. None (the default)
                uses the current article.

        Returns:
            The article headers.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPDataError: If the headers can't be parsed.
        """
        code, message = self.command("HEAD", _msgid_article(msgid_article), expect=221)

        with self._data_errors("article headers"):
            return utils.parse_headers(self.info(code, message))

    def body(
        self,
        msgid_article: str | int | None = None,
        decode: bool | None = None,
    ) -> bytes:
        """BODY command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.2.3>

        Args:
            msgid_article: A message-id or article number. None (the default)
                uses the current article.
            decode: Decode yEnc. None (the default) detects it from the body.

        Returns:
            The article body.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPYEncError: If yEnc data fails its size or CRC check. The decoded
                body is in its data attribute.
        """
        code, message = self.command("BODY", _msgid_article(msgid_article), expect=222)

        return self._read_body(self._info(code, message), decode=decode)

    def _hdr(
        self,
        header: str,
        msgid_range: str | Range | None = None,
        verb: str = "HDR",
    ) -> Iterator[tuple[int, str]]:
        args = header
        if msgid_range is not None:
            args += " " + utils.unparse_msgid_range(msgid_range)

        code, message = self.command(verb, args, expect=225 if verb == "HDR" else 221)
        return self._number_values(verb, self.info(code, message, yz=verb == "XZHDR"))

    def _number_values(
        self, verb: str, lines: Iterator[str]
    ) -> Iterator[tuple[int, str]]:
        """Parse "<article number> <value>" lines, as sent by HDR and XPAT."""
        for line in lines:
            with self._data_errors(f"{verb} response"):
                number, value = utils.parse_number_value(line)
            yield number, value

    def hdr(
        self,
        header: str,
        msgid_range: str | Range | None = None,
    ) -> Iterator[tuple[int, str]]:
        """HDR command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-8.5>

        Args:
            header: The header or metadata field, e.g. "Subject" or ":bytes".
            msgid_range: A message-id, an article number, or a (first,) or
                (first, last) range. None (the default) uses the current article.

        Yields:
            The article number and field value for each article. The number is
            0 when a message-id is given.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPDataError: If a line can't be parsed.
        """
        return self._hdr(header, msgid_range)

    def xhdr(
        self,
        header: str,
        msgid_range: str | Range | None = None,
    ) -> Iterator[tuple[int, str]]:
        """XHDR command, the RFC 2980 form of HDR. See hdr().

        Prefer hdr() when the server lists HDR in its capabilities.
        """
        return self._hdr(header, msgid_range, verb="XHDR")

    def xzhdr(
        self,
        header: str,
        msgid_range: str | Range | None = None,
    ) -> Iterator[tuple[int, str]]:
        """XZHDR command, a compressed XHDR. See hdr()."""
        return self._hdr(header, msgid_range, verb="XZHDR")

    @cached_property
    def _overview_fields(self) -> tuple[tuple[str, bool], ...]:
        try:
            return tuple(self.list_overview_fmt())
        except NNTPPermanentError:
            # Not supported (5xx): use the default. Other errors propagate, so a
            # temporary failure isn't cached as the format.
            names = ("Subject", "From", "Date", "Message-ID", "References")
            return tuple((name, False) for name in (*names, "Bytes", "Lines"))

    @property
    def overview_fmt(self) -> tuple[str, ...]:
        """Overview field names, from LIST OVERVIEW.FMT or the RFC 2980 default."""
        return tuple(name for name, _ in self._overview_fields)

    def _xover(
        self,
        range: str | Range | None = None,
        verb: str = "XOVER",
    ) -> Iterator[tuple[int, HeaderDict]]:
        # get overview fmt before entering generator
        names = self.overview_fmt
        keys = tuple(name.casefold() for name in names)
        # "full" fields include the header name in the value (RFC 3977 8.4.2)
        full = [i for i, (_, is_full) in enumerate(self._overview_fields) if is_full]

        args = None
        if range is not None:
            args = utils.unparse_msgid_range(range)

        code, message = self.command(verb, args, expect=224)
        lines = self.info(code, message, yz=verb == "XZVER")

        def overviews() -> Iterator[tuple[int, HeaderDict]]:
            for line in lines:
                parts = line.rstrip("\r\n").split("\t")
                with self._data_errors(f"{verb} response"):
                    articleno = int(parts[0])
                values = parts[1:]
                for i in full:
                    if i < len(values):
                        name, sep, value = values[i].partition(":")
                        if sep and name.strip().casefold() == keys[i]:
                            values[i] = value.strip()
                # Same-package fast path; names are unique per LIST OVERVIEW.FMT.
                yield articleno, HeaderDict._from_fields(keys, names, values)  # noqa: SLF001

        return overviews()

    def xover(
        self,
        range: Range | None = None,
    ) -> Iterator[tuple[int, HeaderDict]]:
        """XOVER command.

        Prefer over() when the server lists OVER in its capabilities.

        See <https://datatracker.ietf.org/doc/html/rfc2980#section-2.8>

        Args:
            range: An article number, or a (first,) or (first, last) range. None
                (the default) uses the current article.

        Yields:
            The article number and overview fields of each article. Field names
            come from overview_fmt; "full" fields have the header name removed.

        Raises:
            NNTPReplyError: If there is no such article or no newsgroup selected.
            NNTPDataError: If a line can't be parsed.
        """
        return self._xover(range)

    def xzver(
        self,
        range: Range | None = None,
    ) -> Iterator[tuple[int, HeaderDict]]:
        """XZVER command, a compressed XOVER. See xover()."""
        return self._xover(range, verb="XZVER")

    def over(
        self,
        msgid_range: str | Range | None = None,
    ) -> Iterator[tuple[int, HeaderDict]]:
        """OVER command, the RFC 3977 form of XOVER.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-8.3>

        Args:
            msgid_range: A message-id, an article number, or a (first,) or
                (first, last) range. None (the default) uses the current
                article.

        Yields:
            The article number and overview fields of each article, as for
            xover(). The article number is 0 when a message-id is given.

        Raises:
            NNTPReplyError: If there is no such article or no newsgroup
                selected.
            NNTPDataError: If a line can't be parsed.
        """
        return self._xover(msgid_range, verb="OVER")

    def xpat(
        self,
        header: str,
        msgid_range: str | Range,
        *pattern: str,
    ) -> Iterator[tuple[int, str]]:
        """XPAT command.

        Finds articles whose header matches a pattern.

        See <https://datatracker.ietf.org/doc/html/rfc2980#section-2.9>

        Args:
            header: The header to match against.
            msgid_range: A message-id, an article number, or a (first,) or
                (first, last) range.
            *pattern: The wildmat pattern(s), passed to the server as given.

        Yields:
            The article number and header value of each matching article.

        Raises:
            NNTPReplyError: If there is no such article.
            NNTPDataError: If a line can't be parsed.
        """
        args = " ".join([header, utils.unparse_msgid_range(msgid_range), *pattern])

        code, message = self.command("XPAT", args, expect=221)
        return self._number_values("XPAT", self.info(code, message))

    def xfeature_compress_gzip(self, terminator: bool = False) -> None:
        """XFEATURE COMPRESS GZIP command.

        Asks the server to gzip-compress multi-line responses from now on; they
        are decompressed transparently.

        Args:
            terminator: Put the terminating line inside the compressed data.

        Raises:
            NNTPReplyError: If the server does not support it.
        """
        args = "TERMINATOR" if terminator else None

        self.command("XFEATURE COMPRESS GZIP", args, expect=290)

    def post(
        self,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | Iterable[bytes] = b"",
    ) -> str | bool:
        """POST command.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.3.1>

        LF line endings in the body are sent as CRLF and lines starting with "."
        are dot-stuffed.

        Args:
            headers: The article headers.
            body: The body as str, bytes, or an iterable of byte lines such as a
                binary file.

        Returns:
            The message-id if the server's reply includes one (common, though not
            standard), otherwise True.

        Raises:
            ValueError: If a header name or value is invalid (see
                utils.unparse_headers). Nothing is sent to the server.
            NNTPDataError: If a body line contains CR or NUL, which NNTP cannot
                carry. Nothing is sent.
            NNTPReplyError: If posting is not allowed or fails.
        """
        # Validate headers before starting the POST, so a bad header can't
        # leave the server waiting for an article.
        hdrs = utils.unparse_headers(headers or {})
        message = self._transfer("POST", None, hdrs, body, ready=340, done=240)

        # Servers often include the message-id, e.g. INN's "Article received <id>"
        match = _MESSAGE_ID_RE.search(message)
        return match.group() if match else True

    def ihave(
        self,
        message_id: str,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | Iterable[bytes] = b"",
    ) -> None:
        """IHAVE command.

        Offers an article to the server, as a peer does when feeding news.
        Most servers only accept it from configured peers.

        See <https://datatracker.ietf.org/doc/html/rfc3977#section-6.3.2>

        Args:
            message_id: The article's message-id, e.g. "<abc@example.com>".
                A Message-ID header is added if headers has none.
            headers: The article headers.
            body: The body, as for post().

        Raises:
            ValueError: If message_id is invalid or differs from the
                Message-ID header, or a header is invalid. Nothing is sent.
            NNTPTemporaryError: If the server doesn't take the article; its
                code is 435 (not wanted), 436 (try again later) or 437
                (rejected).
            NNTPDataError: If a body line contains CR or NUL, as for post().
                Nothing is sent.
        """
        if not _MESSAGE_ID_RE.fullmatch(message_id):
            raise ValueError(f"Invalid message-id: {message_id!r}")
        headers = HeaderDict(headers or {})
        headers.setdefault("Message-ID", message_id)
        if headers["Message-ID"] != message_id:
            raise ValueError("message_id differs from the Message-ID header")
        hdrs = utils.unparse_headers(headers)
        self._transfer("IHAVE", message_id, hdrs, body, ready=335, done=235)

    def _transfer(
        self,
        verb: str,
        args: str | None,
        hdrs: str,
        body: str | bytes | Iterable[bytes],
        *,
        ready: int,
        done: int,
    ) -> str:
        """Send verb, then the article once the server replies ready.

        The whole article is prepared first, so a bad body raises before
        anything is sent.

        Returns:
            The status message of the final reply, which must be done.
        """
        with self._prepare_article(hdrs, body) as article:
            code, message = self.command(verb, args, expect=ready)

            with self._sync():
                while chunk := article.read(_SEND_SIZE):
                    self._send(chunk)
                code, message = self.status()

        if code != done:
            self._unexpected(code, message)
        return message

    @contextmanager
    def _prepare_article(
        self, hdrs: str, body: str | bytes | Iterable[bytes]
    ) -> Iterator[IO[bytes]]:
        """Build the article on the wire: headers, dot-stuffed CRLF body, ".".

        Raises:
            NNTPDataError: If a body line contains CR or NUL.
        """
        if isinstance(body, str):
            body = body.encode(self.encoding, self.errors)
        if isinstance(body, bytes):
            body = io.BytesIO(body)

        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_SIZE) as article:
            article.write(hdrs.encode(self.encoding, self.errors))
            for line in body:
                if line.startswith(b"."):
                    line = b"." + line
                if line.endswith(b"\n"):
                    line = line[:-1].removesuffix(b"\r")
                if b"\0" in line or b"\r" in line:
                    raise NNTPDataError("Body line contains CR or NUL")
                article.write(line + b"\r\n")
            article.write(b".\r\n")
            article.seek(0)
            yield article
