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


import io
import ssl
import zlib
import socket
import datetime
from . import utils, iodict, fifo, date, yenc
from .polyfill import cached_property


__all__ = [
    'NNTPError',
    'NNTPReplyError',
    'NNTPTemporaryError',
    'NNTPPermanentError',
    'NNTPProtocolError',
    'NNTPDataError',
    'BaseNNTPClient',
    'NNTPClient',
]


class NNTPError(Exception):
    """Base class for all NNTP errors.
    """

    pass


class NNTPSyncError(NNTPError):
    """NNTP sync errors.

    Generally raised when a command is issued while another command it still
    active.
    """

    pass


class NNTPReplyError(NNTPError):
    """NNTP response status errors.
    """

    @property
    def code(self):
        """The response status code.
        """
        return self.args[0]

    @property
    def message(self):
        """The response message.
        """
        return self.args[1]

    def __str__(self):
        return '%d %s' % (self.code, self.message)


class NNTPTemporaryError(NNTPReplyError):
    """NNTP temporary errors.

    Temporary errors have response codes from 400 to 499.
    """

    pass


class NNTPPermanentError(NNTPReplyError):
    """NNTP permanent errors.

    Permanent errors have response codes from 500 to 599.
    """

    pass


# TODO: Add the status line as a parameter ?
class NNTPProtocolError(NNTPError):
    """NNTP protocol error.

    Protocol errors are raised when the response status is invalid.
    """

    pass


class NNTPDataError(NNTPError):
    """NNTP data error.

    Data errors are raised when the content of a response cannot be parsed.
    """

    pass


class BaseNNTPClient(object):
    """NNTP BaseNNTPClient.

    Base class for NNTP clients implements the basic command interface and
    transparently handles compressed replies.
    """

    encoding = 'utf-8'
    errors = 'surrogateescape'

    def __init__(
        self, host, port=119, username='', password='', timeout=30,
        use_ssl=False
    ):
        """Constructor for BasicNNTPClient.

        Connects to usenet server and enters reader mode.

        Args:
            host: Hostname for usenet server.
            port: Port for usenet server.
            username: Username for usenet account (default "anonymous")
            password: Password for usenet account (default "anonymous")
            timeout: Connection timeout (default 30 seconds)
            use_ssl: Should we use ssl (default False)

        Raises:
            IOError (socket.error): On error in underlying socket and/or ssl
                wrapper. See socket and ssl modules for further details.
            NNTPReplyError: On bad response code from server.
        """
        self._buffer = fifo.Fifo()
        self._generating = False

        self.username = username
        self.password = password

        # connect
        self.socket = socket.create_connection((host, port), timeout=timeout)
        if use_ssl:
            context = ssl.create_default_context()
            self.socket = context.wrap_socket(
                self.socket,
                server_hostname=host,
            )

        code, message = self.status()
        if code not in (200, 201):
            raise NNTPReplyError(code, message)

    def _recv(self, size=4096):
        """Reads data from the socket.

        Raises:
            NNTPError: When connection times out or read from socket fails.
        """
        data = self.socket.recv(size)
        if not data:
            raise NNTPError("Failed to read from socket")
        self._buffer.write(data)

    def _line(self):
        """Generator that reads a line of data from the server.

        It first attempts to read from the internal buffer. If there is not
        enough data to read a line it then requests more data from the server
        and adds it to the buffer. This process repeats until a line of data
        can be read from the internal buffer.

        Yields:
            A line of data when it becomes available.
        """
        while True:
            line = self._buffer.readline()
            if not line:
                self._recv()
                continue
            yield line

    def _buf(self, length=0):
        """Generator that reads a block of data from the server.

        It first attempts to read from the internal buffer. If there is not
        enough data in the internal buffer it then requests more data from the
        server and adds it to the buffer.

        Args:
            length: An optional amount of data to retrieve. A length of 0 (the
                default) will retrieve a least one buffer of data.

        Yields:
            A block of data when enough data becomes available.

        Note:
            If a length of 0 is supplied then the size of the yielded buffer
            can vary. If there is data in the internal buffer it will yield all
            of that data otherwise it will yield the the data returned by a
            recv on the socket.
        """
        while True:
            buf = self._buffer.read(length)
            if not buf:
                self._recv()
                continue
            yield buf

    def status(self):
        """Reads a command response status.

        If there is no response message then the returned status message will
        be an empty string.

        Raises:
            NNTPError: If data is required to be read from the socket and
                fails.
            NNTPProtocolError: If the status line can't be parsed.
            NNTPTemporaryError: For status code 400-499
            NNTPPermanentError: For status code 500-599

        Returns:
            A tuple of status code (as an integer) and status message.
        """
        line = next(self._line()).rstrip()
        parts = line.split(None, 1)

        try:
            code, message = int(parts[0]), b''
        except ValueError:
            raise NNTPProtocolError(line)

        if code < 100 or code >= 600:
            raise NNTPProtocolError(line)

        if len(parts) > 1:
            message = parts[1]

        message = message.decode(self.encoding, self.errors)

        if 400 <= code <= 499:
            raise NNTPTemporaryError(code, message)

        if 500 <= code <= 599:
            raise NNTPPermanentError(code, message)

        return code, message

    def _info_plain(self):
        """Generator for the lines of an info (textual) response.

        When a terminating line (line containing single period) is received the
        generator exits.

        If there is a line beginning with an 'escaped' period then the extra
        period is trimmed.

        Yields:
            A line of the info response.

        Raises:
            NNTPError: If data is required to be read from the socket and
                fails.
        """
        self._generating = True

        for line in self._line():
            if line == b'.\r\n':
                break
            if line.startswith(b'.'):
                yield line[1:]
            yield line

        self._generating = False

    def _info_gzip(self):
        """Generator for the lines of a compressed info (textual) response.

        Compressed responses are an extension to the NNTP protocol supported by
        some usenet servers to reduce the bandwidth of heavily used range style
        commands that can return large amounts of textual data.

        This function handles gzip compressed responses that have the
        terminating line inside or outside the compressed data. From experience
        if the 'XFEATURE COMPRESS GZIP' command causes the terminating
        '.\\r\\n' to follow the compressed data and 'XFEATURE COMPRESS GZIP
        TERMINATOR' causes the terminator to be the last part of the compressed
        data (i.e the reply the gzipped version of the original reply -
        terminating line included)

        This function will produce that same output as the _info_plain()
        function. In other words it takes care of decompression.

        Yields:
            A line of the info response.

        Raises:
            NNTPError: If data is required to be read from the socket and
                fails.
            NNTPDataError: If decompression fails.
        """
        self._generating = True

        inflate = zlib.decompressobj(15+32)

        done, buf = False, fifo.Fifo()
        while not done:
            try:
                data = inflate.decompress(next(self._buf()))
            except zlib.error:
                raise NNTPDataError('Decompression failed')
            if data:
                buf.write(data)
            if inflate.unused_data:
                buf.write(inflate.unused_data)
            for line in buf:
                if line == b'.\r\n':
                    done = True
                    break
                if line.startswith(b'.'):
                    yield line[1:]
                yield line

        self._generating = False

    def _info_yenczlib(self):
        """Generator for the lines of a compressed info (textual) response.

        Compressed responses are an extension to the NNTP protocol supported by
        some usenet servers to reduce the bandwidth of heavily used range style
        commands that can return large amounts of textual data. The server
        returns that same data as it would for the uncompressed versions of the
        command the difference being that the data is zlib deflated and then
        yEnc encoded.

        This function will produce that same output as the info()
        function. In other words it takes care of decoding and decompression.

        Yields:
            A line of the info response.

        Raises:
            NNTPError: If data is required to be read from the socket and
                fails.
            NNTPDataError: When there is an error parsing the yEnc header or
                trailer, if the CRC check fails or decompressing data fails.
        """

        escape = 0
        dcrc32 = 0
        inflate = zlib.decompressobj(-15)

        # header
        header = next(self._info_plain())
        if not header.startswith(b'=ybegin'):
            raise NNTPDataError('Bad yEnc header')

        # data
        buf, trailer = fifo.Fifo(), b''
        for line in self._info_plain():
            if line.startswith(b'=yend'):
                trailer = line
                continue
            data, escape, dcrc32 = yenc.decode(line, escape, dcrc32)
            try:
                data = inflate.decompress(data)
            except zlib.error:
                raise NNTPDataError('Decompression failed')
            if not data:
                continue
            buf.write(data)
            for lined in buf:
                yield lined

        # trailer
        if not trailer:
            raise NNTPDataError('Missing yEnc trailer')

        # expected crc32
        ecrc32 = yenc.crc32(trailer)
        if ecrc32 is None:
            raise NNTPDataError('Bad yEnc trailer')

        # check crc32
        if ecrc32 != dcrc32 & 0xffffffff:
            raise NNTPDataError('Bad yEnc CRC')

    def info(self, code, message, yz=False, encoding=encoding, errors=errors):
        """Dispatcher for the info generators.

        Determines which _info_*() generator should be used based on the
        supplied parameters.

        Args:
            code: The status code for the command response.
            message: The status message for the command response.
            yz: Use yenzlib decompression. Useful for xz* commands.
            encoding: Encoding to use. None means bytes are yielded.
            errors: Error handling for encoding.

        Yields:
            Chunks of the info (textual) response in the requested encoding
            until the entire response has been yielded.
        """
        if 'COMPRESS=GZIP' in message:
            gen = self._info_gzip()
        elif yz:
            gen = self._info_yenczlib()
        else:
            gen = self._info_plain()
        for line in gen:
            if encoding is not None:
                line = line.decode(encoding, errors)
            yield line

    def command(self, verb, args=None):
        """Call a command on the server.

        If the user has not authenticated then authentication will be done
        as part of calling the command on the server.

        For commands that don't return a status message the status message
        will default to an empty string.

        Args:
            verb: The verb of the command to call.
            args: The arguments of the command as a string (default None).

        Returns:
            A tuple of status code (as an integer) and status message.

        Note:
            You can run raw commands by supplying the full command (including
            args) in the verb.

        Note: Although it is possible you shouldn't issue more than one command
            at a time by adding newlines to the verb as it will most likely
            lead to undesirable results.
        """
        if self._generating:
            raise NNTPSyncError('Command issued while a generator is active')

        if args:
            cmd = '%s %s\r\n' % (verb, args)
        else:
            cmd = verb + '\r\n'

        cmd = cmd.encode(self.encoding)
        self.socket.sendall(cmd)

        try:
            code, message = self.status()
        except NNTPTemporaryError as e:
            if e.code != 480:
                raise e
            code, message = self.command('AUTHINFO USER', self.username)
            if code == 381:
                code, message = self.command('AUTHINFO PASS', self.password)
            if code != 281:
                raise NNTPReplyError(code, message)
            code, message = self.command(verb, args)

        return code, message

    def close(self):
        """Closes the connection at the client.

        Once this method has been called, no other methods of the NNTPClient
        object should be called.
        """
        self.socket.close()


class NNTPClient(BaseNNTPClient):
    """NNTP NNTPClient.

    Implements many of the commands that are commonly used by current usenet
    servers. Including handling commands that use compressed responses.

    Implements generators for commands for which generators are likely to
    yield (bad pun warning) performance gains. These gains will be in the form
    of lower memory consumption and the added ability to process and receive
    data in parallel. If you are using commands that can take a range as an
    argument or can return large amounts of data there should be a _gen()
    version of the command and it should be used in preference to the standard
    version.

    Note: All commands can raise the following exceptions:
            NNTPError
            NNTPProtocolError
            NNTPPermanentError
            NNTPReplyError
            IOError (socket.error)

    Note: All commands that use compressed responses can also raise an
        NNTPDataError.
    """

    def __init__(
        self, host, port=119,
        username='', password='',
        timeout=30, use_ssl=False,
        reader=True
    ):
        """Constructor for NNTP NNTPClient.

        Connects to usenet server.

        Args:
            host: Hostname for usenet server.
            port: Port for usenet server.
            username: Username for usenet account (default "")
            password: Password for usenet account (default "")
            timeout: Connection timeout (default 30 seconds)
            use_ssl: Should we use ssl (default False)
            reader: Use reader mode

        Raises:
            socket.error: On error in underlying socket and/or ssl wrapper. See
                socket and ssl modules for further details.
            NNTPReplyError: On bad response code from server.
        """
        super(NNTPClient, self).__init__(
            host, port,
            username, password,
            timeout, use_ssl
        )
        if reader:
            self.mode_reader()
    
    def __enter__(self):
        """Support for the 'with' context manager statement."""
        return self

    def __exit__(self, *args):
        """Support for the 'with' context manager statement."""
        try:
            self.quit()
        except NNTPError:
            self.close()
            raise

    # session administration commands
    def capabilities(self, keyword=None):
        """CAPABILITIES command.

        Determines the capabilities of the server.

        Although RFC3977 states that this is a required command for servers to
        implement not all servers do, so expect that NNTPPermanentError may be
        raised when this command is issued.

        See <http://tools.ietf.org/html/rfc3977#section-5.2>

        Args:
            keyword: Passed directly to the server, however, this is unused by
                the server according to RFC3977.

        Yields:
            Each of the capabilities supported by the server. The VERSION
            capability is the first capability to be yielded.
        """
        args = keyword

        code, message = self.command('CAPABILITIES', args)
        if code != 101:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield line.strip()

    def mode_reader(self):
        """MODE READER command.

        Instructs a mode-switching server to switch modes.

        See <http://tools.ietf.org/html/rfc3977#section-5.3>

        Returns:
            Boolean value indicating whether posting is allowed or not.
        """
        code, message = self.command('MODE READER')
        if code not in (200, 201):
            raise NNTPReplyError(code, message)

        return code == 200

    def quit(self):
        """QUIT command.

        Tells the server to close the connection. After the server acknowledges
        the request to quit the connection is closed both at the server and
        client. Only useful for graceful shutdown. If you are in a generator
        use close() instead.

        Once this method has been called, no other methods of the NNTPClient
        object should be called.

        See <http://tools.ietf.org/html/rfc3977#section-5.4>
        """
        code, message = self.command('QUIT')
        if code != 205:
            raise NNTPReplyError(code, message)

        self.socket.close()

    # information commands
    def date(self):
        """DATE command.

        Coordinated Universal time from the perspective of the usenet server.
        It can be used to provide information that might be useful when using
        the NEWNEWS command.

        See <http://tools.ietf.org/html/rfc3977#section-7.1>

        Returns:
            The UTC time according to the server as a datetime object.

        Raises:
            NNTPDataError: If the timestamp can't be parsed.
        """
        code, message = self.command('DATE')
        if code != 111:
            raise NNTPReplyError(code, message)

        ts = date.datetimeobj(message, fmt='%Y%m%d%H%M%S')

        return ts

    def help(self):
        """HELP command.

        Provides a short summary of commands that are understood by the usenet
        server.

        See <http://tools.ietf.org/html/rfc3977#section-7.2>

        Returns:
            The help text from the server.
        """
        code, message = self.command('HELP')
        if code != 100:
            raise NNTPReplyError(code, message)

        return ''.join(self.info(code, message))

    def newgroups(self, timestamp):
        """NEWGROUPS command.

        Retrieves a list of newsgroups created on the server since the
        specified timestamp.

        See <http://tools.ietf.org/html/rfc3977#section-7.3>

        Args:
            timestamp: Datetime object giving 'created since' datetime.

        Yields:
            A 4-tuple containing the name, low water mark, high water mark, and
            status for the newsgroup, for each newsgroup.

        Note: If the datetime object supplied as the timestamp is naive (tzinfo
            is None) then it is assumed to be given as GMT.
        """
        if timestamp.tzinfo:
            ts = timestamp.asttimezone(date.TZ_GMT)
        else:
            ts = timestamp.replace(tzinfo=date.TZ_GMT)

        args = ts.strftime('%Y%m%d %H%M%S %Z')

        code, message = self.command('NEWGROUPS', args)
        if code != 231:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield utils.parse_newsgroup(line)

    def newnews(self, pattern, timestamp):
        """NEWNEWS command.

        Retrieves a list of message-ids for articles created since the
        specified timestamp for newsgroups with names that match the given
        pattern.

        See <http://tools.ietf.org/html/rfc3977#section-7.4>

        Args:
            pattern: Glob matching newsgroups of interest.
            timestamp: Datetime object giving 'created since' datetime.

        Yields:
            A message-id as string, for each article.

        Note: If the datetime object supplied as the timestamp is naive (tzinfo
            is None) then it is assumed to be given as GMT. If tzinfo is set
            then it will be converted to GMT by this function.
        """
        if timestamp.tzinfo:
            ts = timestamp.asttimezone(date.TZ_GMT)
        else:
            ts = timestamp.replace(tzinfo=date.TZ_GMT)

        args = pattern
        args += ' ' + ts.strftime('%Y%m%d %H%M%S %Z')

        code, message = self.command('NEWNEWS', args)
        if code != 230:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield line.strip()

    # list commands
    def list_active(self, pattern=None):
        """LIST ACTIVE command.

        Retrieves a list of active newsgroups that match the specified pattern.

        See <http://tools.ietf.org/html/rfc3977#section-7.6.3>

        Args:
            pattern: Glob matching newsgroups of interest.

        Yields:
            A 4-tuple containing the name, low water mark, high water mark,
            and status for the newsgroup, for each newsgroup.
        """
        args = pattern

        if args is None:
            cmd = 'LIST'
        else:
            cmd = 'LIST ACTIVE'

        code, message = self.command(cmd, args)
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield utils.parse_newsgroup(line)

    def list_active_times(self):
        """LIST ACTIVE TIMES command.

        Retrieves a list of newsgroups including the creation time and who
        created them.

        See <http://tools.ietf.org/html/rfc3977#section-7.6.4>

        Yields:
            A 3-tuple containing the name, creation date as a datetime object
            and creator as a string for the newsgroup for each newsgroup.
        """
        code, message = self.command('LIST ACTIVE.TIMES')
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            parts = line.split()
            try:
                name = parts[0]
                timestamp = date.datetimeobj_epoch(parts[1])
                creator = parts[2]
            except (IndexError, ValueError):
                raise NNTPDataError('Invalid LIST ACTIVE.TIMES')
            yield name, timestamp, creator

    def list_headers(self, variant=None):
        """LIST HEADERS command.

        Returns a list of fields that may be retrieved using the HDR command.

        See <https://tools.ietf.org/html/rfc3977#section-8.6>

        Args:
            variant: The string 'MSGID' or 'RANGE' or None (the default).
                Different variants of the HDR request may return a different
                fields.

        Yields:
            The field name for each of the fields.
        """
        args = variant

        code, message = self.command('LIST HEADERS', args)
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield line.strip()

    def list_newsgroups(self, pattern=None):
        """LIST NEWSGROUPS command.

        Retrieves a list of newsgroups including the name and a short
        description.

        See <http://tools.ietf.org/html/rfc3977#section-7.6.6>

        Args:
            pattern: Glob matching newsgroups of interest.

        Yields:
            A tuple containing the name, and description for the newsgroup, for
            each newsgroup that matches the pattern.
        """
        args = pattern

        code, message = self.command('LIST NEWSGROUPS', args)
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            parts = line.strip().split()
            name, description = parts[0], ''
            if len(parts) > 1:
                description = parts[1]
            yield name, description

    def list_overview_fmt(self):
        """LIST OVERVIEW.FMT command.

        Returns a description of the fields in the database for which it is
        consistent.

        See <https://tools.ietf.org/html/rfc3977#section-8.4>

        Yields:
            A 2-tuple of the name of the field and a boolean indicating whether
            the the field name is included in the field data, for each field in
            the database which is consistent, fields are yielded in order.
        """
        code, message = self.command('LIST OVERVIEW.FMT')
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            try:
                name, suffix = line.rstrip().split(':')
            except ValueError:
                raise NNTPDataError('Invalid LIST OVERVIEW.FMT')
            if suffix and not name:
                name, suffix = suffix, name
            if suffix and suffix != 'full':
                raise NNTPDataError('Invalid LIST OVERVIEW.FMT')
            yield (name, suffix == 'full')

    def list_extensions(self):
        """LIST EXTENSIONS command.

        Allows a client to determine which extensions are supported by the
        server at any given time.

        See <https://tools.ietf.org/html/draft-ietf-nntpext-base-20#section-5.3>

        Yields:
            The name of the extension of each of the available extensions.
        """
        code, message = self.command('LIST EXTENSIONS')
        if code != 202:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield line.strip()

    def list(self, keyword=None, arg=None):
        """LIST command.

        A wrapper for all of the other list commands.

        Args:
            keyword: Information requested.
            arg: Pattern or keyword specific argument.

        Yields:
            Depends on which list command is specified by the keyword. See the
            list function that corresponds to that keyword.

        Note: Keywords supported by this function are include ACTIVE,
            ACTIVE.TIMES, HEADERS, NEWSGROUPS, OVERVIEW.FMT and EXTENSIONS.

        Raises:
            NotImplementedError: For unsupported keywords.
        """
        if keyword:
            keyword = keyword.upper()

        if keyword is None or keyword == 'ACTIVE':
            return self.list_active(arg)
        if keyword == 'ACTIVE.TIMES':
            return self.list_active_times()
        if keyword == 'HEADERS':
            return self.list_headers(arg)
        if keyword == 'NEWSGROUPS':
            return self.list_newsgroups(arg)
        if keyword == 'OVERVIEW.FMT':
            return self.list_overview_fmt()
        if keyword == 'EXTENSIONS':
            return self.list_extensions()

        raise NotImplementedError()

    def group(self, name):
        """GROUP command.

        Selects a newsgroup as the currently selected newsgroup and returns
        summary information about it.

        See <https://tools.ietf.org/html/rfc3977#section-6.1.1>

        Args:
            name: The group name.

        Returns:
            A 4-tuple of the estimated total articles in the group, the
            articles numbers of the first and last article and the group name.

        Raises:
            NNTPReplyError: If no such newsgroup exists.
        """
        args = name

        code, message = self.command('GROUP', args)
        if code != 211:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 4)
        try:
            total = int(parts[0])
            first = int(parts[1])
            last = int(parts[2])
            group = parts[3]
        except (IndexError, ValueError):
            raise NNTPDataError('Invalid GROUP status "%s"' % message)

        return total, first, last, group

    def next(self):
        """NEXT command.

        Sets the current article number to the next article in the current
        newsgroup.

        See <https://tools.ietf.org/html/rfc3977#section-6.1.4>

        Returns:
            A 2-tuple of the article number and message id.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """
        code, message = self.command('NEXT')
        if code != 223:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 3)
        try:
            article = int(parts[0])
            msgid = parts[1]
        except (IndexError, ValueError):
            raise NNTPDataError('Invalid NEXT status')

        return article, msgid

    def last(self):
        """LAST command.

        Sets the current article number to the previous article in the current
        newsgroup.

        See <https://tools.ietf.org/html/rfc3977#section-6.1.3>

        Returns:
            A 2-tuple of the article number and message id.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """
        code, message = self.command('LAST')
        if code != 223:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 3)
        try:
            article = int(parts[0])
            msgid = parts[1]
        except (IndexError, ValueError):
            raise NNTPDataError('Invalid LAST status')

        return article, msgid

    # TODO: Validate yEnc
    def _body(self, code, message, decode=None):
        escape = crc32 = 0

        # read the body
        body = []
        for line in self.info(code, message, encoding=None):
            # detect yenc
            if decode is None:
                if line.startswith(b'=y'):
                    decode = True
                    del body[:]
                elif line != b'\r\n':
                    decode = False

            # decode yenc
            if decode:
                if line.startswith(b'=y'):
                    continue
                line, escape, crc32 = yenc.decode(line, escape, crc32)

            body.append(line)

        return b''.join(body)

    def article(self, msgid_article=None, decode=None):
        """ARTICLE command.

        Selects an article according to the arguments and presents the entire
        article (that is, the headers, an empty line, and the body, in that
        order) to the client.

        See <https://tools.ietf.org/html/rfc3977#section-6.2.1>

        Args:
            msgid_article: A message-id as a string, or an article number as an
                integer. A msgid_article of None (the default) uses the current
                article.
            decode: Force yenc decoding to be enabled or disabled. A value of
                None (the default) attempts to determine this automatically.

        Returns:
            A 3-tuple of the article number, the article headers, and the
            article body. The headers are decoded to unicode, where as the body
            is returned as bytes.

        Raises:
            NNTPReplyError: If no such article exists.
        """
        args = None
        if msgid_article is not None:
            args = utils.unparse_msgid_article(msgid_article)

        code, message = self.command('ARTICLE', args)
        if code != 220:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 1)

        try:
            articleno = int(parts[0])
        except ValueError:
            raise NNTPProtocolError(message)

        # headers
        headers = utils.parse_headers(self.info(code, message))

        if decode is None and 'yEnc' in headers.get('subject', ''):
            decode = True

        # body
        body = self._body(code, message, decode=decode)

        return articleno, headers, body

    def head(self, msgid_article=None):
        """HEAD command.

        Identical to the ARTICLE command except that only the headers are
        presented.

        See <https://tools.ietf.org/html/rfc3977#section-6.2.2>

        Args:
            msgid_article: A message-id as a string, or an article number as an
                integer. A msgid_article of None (the default) uses the current
                article.

        Returns:
            The article headers.

        Raises:
            NNTPReplyError: If no such article exists.
        """
        args = None
        if msgid_article is not None:
            args = utils.unparse_msgid_article(msgid_article)

        code, message = self.command('HEAD', args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return utils.parse_headers(self.info(code, message))

    def body(self, msgid_article=None, decode=None):
        """BODY command.

        Identical to the ARTICLE command except that only the body is
        presented.

        See <https://tools.ietf.org/html/rfc3977#section-6.2.3>

        Args:
            msgid_article: A message-id as a string, or an article number as an
                integer. A msgid_article of None (the default) uses the current
                article.
            decode: Force yenc decoding to be enabled or disabled. A value of
                None (the default) attempts to determine this automatically.

        Returns:
            A 3-tuple of the article number, the article headers, and the
            article body. The headers are decoded to unicode, where as the body
            is returned as bytes.

        Raises:
            NNTPReplyError: If no such article exists.
        """
        args = None
        if msgid_article is not None:
            args = utils.unparse_msgid_article(msgid_article)

        code, message = self.command('BODY', args)
        if code != 222:
            raise NNTPReplyError(code, message)

        return self._body(code, message, decode=decode)

    def _hdr(self, header, msgid_range=None, verb='HDR'):
        args = header
        if msgid_range is not None:
            args += " " + utils.unparse_msgid_range(msgid_range)

        code, message = self.command(verb, args)
        if code != 221:
            raise NNTPReplyError(code, message)

        yz = verb == 'XZHDR'
        for line in self.info(code, message, yz=yz):
            parts = line.split(None, 1)
            try:
                articleno = int(parts[0])
                value = parts[1] if len(parts) > 1 else ''
            except (IndexError, ValueError):
                raise NNTPDataError('Invalid XHDR response')
            yield articleno, value

    def hdr(self, header, msgid_range=None):
        """HDR command.

        Provides access to specific fields from an article specified by
        message-id, or from a specified article or range of articles in the
        currently selected newsgroup.

        See <https://tools.ietf.org/html/rfc3977#section-8.5>

        Args:
            header: The header field to retrieve.
            msgid_range: A message-id as a string, or an article number as an
                integer, or a tuple of specifying a range of article numbers in
                the form (first, [last]) - if last is omitted then all articles
                after first are included. A msgid_range of None (the default)
                uses the current article.

        Yields:
            A 2-tuple giving the article number and value for the provided
            header field for each article in the given range.

        Raises:
            NNTPDataError: If the response from the server is malformed.
            NNTPReplyError: If no such article exists.
        """
        for entry in self._hdr(header, msgid_range):
            yield entry

    def xhdr(self, header, msgid_range=None):
        """Generator for the XHDR command.

        See hdr_gen()
        """
        for entry in self._hdr(header, msgid_range, verb='XHDR'):
            yield entry

    def xzhdr(self, header, msgid_range=None):
        """Generator for the XZHDR command.

        The compressed version of XHDR. See xhdr().
        """
        for entry in self._hdr(header, msgid_range, verb='XZHDR'):
            yield entry

    @cached_property
    def overview_fmt(self):
        try:
            return tuple(name for name, opt in self.list_overview_fmt())
        except NNTPError:
            return (
                'Subject',
                'From',
                'Date',
                'Message-ID',
                'References',
                'Bytes',
                'Lines',
            )

    def _xover(self, range=None, verb='XOVER'):
        # get overview fmt before entering generator
        fmt = self.overview_fmt

        args = None
        if range is not None:
            args = utils.unparse_range(range)

        code, message = self.command(verb, args)
        if code != 224:
            raise NNTPReplyError(code, message)

        yz = verb == 'XZVER'
        for line in self.info(code, message, yz=yz):
            parts = line.rstrip().split('\t')
            try:
                articleno = int(parts[0])
                overview = iodict.IODict(zip(fmt, parts[1:]))
            except (IndexError, ValueError):
                raise NNTPDataError('Invalid %s response' % verb)
            yield articleno, overview

    def xover(self, range=None):
        """XOVER command.

        The XOVER command returns information from the overview database for
        the article(s) specified.

        <http://tools.ietf.org/html/rfc2980#section-2.8>

        Args:
            range: An article number as an integer, or a tuple of specifying a
                range of article numbers in the form (first, [last]). If last
                is omitted then all articles after first are included. A range
                of None (the default) uses the current article.

        Yields:
            A 2-tuple of the article number and a dictionary of the fields as
            given by the overview database for each available article in the
            specified range. The names of the fields that are returned are
            determined using the LIST OVERVIEW.FMT command if the server
            supports it, otherwise a fallback set of 'required' headers is
            used.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """
        for entry in self._xover(range):
            yield entry

    def xzver(self, range=None):
        """XZVER command.

        The compressed version of XHDR. See xover().
        """
        for entry in self._xover(range, verb='XZVER'):
            yield entry

    def xpat(self, header, msgid_range, *pattern):
        """XPAT command.

        Used to retrieve specific headers from specific articles, based on
        pattern matching on the contents of the header.

        See <https://tools.ietf.org/html/rfc2980#section-2.9>

        Args:
            header: The header field to match against.
            msgid_range: An article number as an integer, or a tuple of
                specifying a range of article numbers in the form (first,
                [last]). If last is omitted then all articles after first are
                included.

        Yields:
            Not sure of the format but the value of the header will be part of
            it.

        Raises:
            NNTPReplyError: If no such article exists.
        """
        args = ' '.join(
            [header, utils.unparse_msgid_range(msgid_range)] + list(pattern)
        )

        code, message = self.command('XPAT', args)
        if code != 221:
            raise NNTPReplyError(code, message)

        for line in self.info(code, message):
            yield line.strip()

    def xfeature_compress_gzip(self, terminator=False):
        """XFEATURE COMPRESS GZIP command.
        """
        args = 'TERMINATOR' if terminator else None

        code, message = self.command('XFEATURE COMPRESS GZIP', args)
        if code != 290:
            raise NNTPReplyError(code, message)

        return True

    def post(self, headers=None, body=b''):
        """POST command.

        Args:
            headers: A dictionary of headers.
            body: A string or file like object containing the post content.

        Raises:
            NNTPDataError: If binary characters are detected in the message
                body.

        Returns:
            A value that evaluates to true if posting the message succeeded.
            (See note for further details)

        Note:
            '\\n' line terminators are converted to '\\r\\n'

        Note:
            Though not part of any specification it is common for usenet
            servers to return the message-id for a successfully posted message.
            If a message-id is identified in the response from the server then
            that message-id will be returned by the function, otherwise True
            will be returned.

        Note:
            Due to protocol issues if illegal characters are found in the body
            the message will still be posted but will be truncated as soon as
            an illegal character is detected. No illegal characters will be
            sent to the server. For information illegal characters include
            embedded carriage returns '\\r' and null characters '\\0' (because
            this function converts line feeds to CRLF, embedded line feeds are
            not an issue)
        """
        code, message = self.command('POST')
        if code != 340:
            raise NNTPReplyError(code, message)

        # TODO: Set some default headers? Require some headers?

        # send headers
        headers = headers or {}
        hdrs = utils.unparse_headers(headers)
        hdrs = hdrs.encode(self.encoding)
        self.socket.sendall(hdrs)

        if isinstance(body, str):
            body = body.encode(self.encoding)
        if isinstance(body, bytes):
            body = io.BytesIO(body)

        # send body
        illegal = False
        for line in body:
            if line.startswith(b'.'):
                line = b'.' + line
            if line.endswith(b'\r\n'):
                line = line[:-2]
            elif line.endswith(b'\n'):
                line = line[:-1]
            if any(c in line for c in b'\0\r'):
                illegal = True
                break
            self.socket.sendall(line + b'\r\n')
        self.socket.sendall(b'.\r\n')

        # get status
        code, message = self.status()

        # check if illegal characters were detected
        if illegal:
            raise NNTPDataError('Illegal characters found')

        # check status
        if code != 240:
            raise NNTPReplyError(code, message)

        # return message-id possible
        message_id = message.split(None, 1)[0]
        if message_id.startswith('<') and message_id.endswith('>'):
            return message_id

        return True


# testing
if __name__ == "__main__":
    import sys

    log = sys.stdout.write

    try:
        host = sys.argv[1]
        port = int(sys.argv[2])
        username = sys.argv[3]
        password = sys.argv[4]
        use_ssl = int(sys.argv[5])
    except (IndexError, TypeError, ValueError):
        prog = sys.argv[0]
        log('%s <host> <port> <username> <password> <ssl(0|1)>\n' % prog)
        sys.exit(1)

    now = datetime.datetime.utcnow()
    fiftydays = datetime.timedelta(days=50)
    onemin = datetime.timedelta(minutes=1)

    nntp_client = NNTPClient(
        host, port,
        username, password,
        use_ssl=use_ssl, reader=False
    )

    try:
        log('HELP\n')
        try:
            log('%s\n' % nntp_client.help())
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('DATE\n')
        try:
            log('%s\n' % nntp_client.date())
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('NEWGROUPS\n')
        try:
            for group in nntp_client.newgroups(now - fiftydays):
                log('%s\n' % group)
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('NEWNEWS\n')
        try:
            for article in nntp_client.newnews('alt.binaries.*', now - onemin):
                log('%s\n' % article)
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('CAPABILITIES\n')
        try:
            for capability in nntp_client.capabilities():
                log('%s\n' % capability)
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('GROUP misc.test\n')
        try:
            total, first, last, name = nntp_client.group('misc.test')
            log('%d %d %d %s\n' % (total, first, last, name))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('HEAD\n')
        try:
            log('%r\n' % nntp_client.head(last))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('BODY\n')
        try:
            log('%r\n' % nntp_client.body(last))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('BODY\n')
        try:
            log('%r\n' % nntp_client.body(910230))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('ARTICLE\n')
        try:
            result = nntp_client.article(last, False)
            log('%d\n%s\n%r\n' % (result[0], result[1], result[2]))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('ARTICLE (auto yEnc decode)\n')
        try:
            result = nntp_client.article(910230)
            log('%d\n%s\n%r\n' % (result[0], result[1], result[2]))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XHDR Date %d-%d\n' % (last-10, last))
        try:
            for article, datestr in nntp_client.xhdr('Date', (last-10, last)):
                log('%d %s\n' % (article, date.datetimeobj(datestr)))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XZHDR Date %d-%d\n' % (last-10, last))
        try:
            for article, datestr in nntp_client.xhdr('Date', (last-10, last)):
                log('%d %s\n' % (article, date.datetimeobj(datestr)))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XOVER %d-%d\n' % (last-10, last))
        try:
            hash_ = count = 0
            for article, overview in nntp_client.xover((last-10, last)):
                log('%d %r\n' % (article, overview))
                hash_ += sum(map(hash, overview.keys()))
                hash_ += sum(map(hash, overview.values()))
                count += 1
            log('Entries %d Hash %s\n' % (count, hash_))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XZVER %d-%d\n' % (last-10, last))
        try:
            hash_ = count = 0
            for article, overview in nntp_client.xzver((last-10, last)):
                hash_ += sum(map(hash, overview.keys()))
                hash_ += sum(map(hash, overview.values()))
                count += 1
            log('Entries %d Hash %s\n' % (count, hash_))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XFEATURE COMPRESS GZIP\n')
        try:
            log('%s\n' % nntp_client.xfeature_compress_gzip())
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XOVER %d-%d\n' % (last-10, last))
        try:
            hash_ = count = 0
            for article, overview in nntp_client.xover((last-10, last)):
                hash_ += sum(map(hash, overview.keys()))
                hash_ += sum(map(hash, overview.values()))
                count += 1
            log('Entries %d Hash %s\n' % (count, hash_))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XFEATURE COMPRESS GZIP TERMINATOR\n')
        try:
            log('%s\n' % nntp_client.xfeature_compress_gzip())
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('XOVER %d-%d\n' % (last-10, last))
        try:
            hash_ = count = 0
            for article, overview in nntp_client.xover((last-10, last)):
                hash_ += sum(map(hash, overview.keys()))
                hash_ += sum(map(hash, overview.values()))
                count += 1
            log('Entries %d Hash %s\n' % (count, hash_))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST\n')
        try:
            log('Entries %d\n' % len(list(nntp_client.list())))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST ACTIVE\n')
        try:
            log('Entries %d\n' % len(list(nntp_client.list('ACTIVE'))))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST ACTIVE alt.binaries.*\n')
        try:
            result = nntp_client.list('ACTIVE', 'alt.binaries.*')
            log('Entries %d\n' % len(list(result)))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST ACTIVE.TIMES\n')
        try:
            log('Entries %d\n' % len(list(nntp_client.list('ACTIVE.TIMES'))))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST NEWSGROUPS\n')
        try:
            log('Entries %d\n' % len(list(nntp_client.list('NEWSGROUPS'))))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST NEWSGROUPS alt.binaries.*\n')
        try:
            result = nntp_client.list('NEWSGROUPS', 'alt.binaries.*')
            log('Entries %d\n' % len(list(result)))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST OVERVIEW.FMT\n')
        try:
            log('%s\n' % list(nntp_client.list('OVERVIEW.FMT')))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST HEADERS\n')
        try:
            log('%s\n' % list(nntp_client.list('HEADERS')))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('LIST EXTENSIONS\n')
        try:
            log('%s\n' % list(nntp_client.list('EXTENSIONS')))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('POST (with illegal characters)\n')
        try:
            log('%s\n' % nntp_client.post(
                iodict.IODict({
                    'From': '"pynntp" <pynntp@not.a.real.doma.in>',
                    'Newsgroups': 'misc.test',
                    'Subject': 'pynntp test article',
                    'Organization': 'pynntp',
                }),
                'pip install pynntp\r\nthis\0contains\rillegal\ncharacters'
            ))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('POST\n')
        try:
            log('%s\n' % nntp_client.post(
                iodict.IODict({
                    'From': '"pynntp" <pynntp@not.a.real.doma.in>',
                    'Newsgroups': 'misc.test',
                    'Subject': 'pynntp test article',
                    'Organization': 'pynntp',
                }),
                'pip install pynntp'
            ))
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

        log('QUIT\n')
        try:
            nntp_client.quit()
        except NNTPError as e:
            log('%s\n' % e)
        log('\n')

    finally:
        log('CLOSING CONNECTION\n')
        nntp_client.close()
