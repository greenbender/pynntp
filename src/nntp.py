#!/usr/bin/python
"""
An NNTP library - a bit more useful than the nntplib one (hopefully).
Copyright (C) 2013  Byron Platt

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

import ssl
import zlib
import socket
import datetime
import iodict
import fifo
import yenc
import date


class NNTPError(Exception):
    """Base class for all NNTP errors.
    """
    pass

class NNTPReplyError(NNTPError):
    """NNTP response status errors.
    """
    def __init__(self, code, message):
        NNTPError.__init__(self, code, message)

    def code(self):
        """The response status code.
        """
        return self.args[0]

    def message(self):
        """The response message.
        """
        return self.args[1]

    def __str__(self):
        return "%d: %s" % self.args

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

    Protcol errors are raised when the response status is invalid.
    """
    pass

class NNTPDataError(NNTPError):
    """NNTP data error.

    Data errors are raised when the content of a response cannot be parsed.
    """
    pass


class Reader(object):
    """NNTP Reader.

    Implements many of the commands that are commonly used by current usenet
    servers. Including handling commands that use compressed responses.

    Implements generators for commands for which generators are likely to
    yield (bad pun warning) perfomance gains. These gains will be in the form
    of lower memory consumption and the added ability to process and receive
    data in parallel. If you are using commands that can take a range as an
    argument or can return large amounts of data there should be a _gen()
    version of the command and it should be used in preference to the standard
    version.

    Note: All commands can raise the following exceptions:
            NNTPProtocolError
            NNTPPermanentError
            NNTPReplyError
            IOError (socket.error)

    Note: All commands that use compressed responses can also raise an
        NNTPDataError.
    """
            
    def __init__(self, host, port=119, username="anonymous", password="anonymous", timeout=30, use_ssl=False):
        """Constructor for NNTP Reader.

        Connects to usenet server and enters reader mode.

        Args:
            host: Hostname for usenet server.
            port: Port for usenet server.
            username: Username for usenet account (default "anonymous")
            password: Password for usenet account (default "anonymous")
            timeout: Connection timeout (default 30 seconds)
            use_ssl: Should we use ssl (default False)

        Raises:
            socket.error: On error in underlying socket and/or ssl wrapper. See
                socket and ssl modules for further details.
            NNTPReplyError: On bad response code from server.
        """
        self.socket = socket.socket()
        if use_ssl:
            self.socket = ssl.wrap_socket(self.socket)
        self.socket.settimeout(timeout)

        self.__buffer = fifo.Fifo()

        self.username = username
        self.password = password

        # connect
        self.socket.connect((host, port))
        code, message = self.__status()
        if not code in [200, 201]:
            raise NNTPReplyError(code, message)

        # reader
        self.mode_reader()

    def __line_gen(self):
        """Generator that reads a line of data from the server.

        It first attempts to read from the internal buffer. If there is not
        enough data to read a line it then requests more data from the server
        and adds it to the buffer. This process repeats until a line of data
        can be read from the internal buffer. When a line of data is read
        it is yielded.

        A terminating line (line containing single period) is received the
        generator exits.

        If there is a line begining with an 'escaped' period then the extra
        period is trimmed. 
        """
        while True:
            line = self.__buffer.readline()
            if not line:
                self.__buffer.write(self.socket.recv(4096))
                continue
            if line == ".\r\n":
                return
            if line.startswith(".."):
                yield line[1:]
            yield line

    def __buf_gen(self, length=0):
        """Generator that reads a block of data from the server.

        It first attempts to read from the internal buffer. If there is not
        enough data in the internal buffer it then requests more data from the
        server and adds it to the buffer. This process repeats until a line of
        unitl there is enough data at which point the data is yielded.

        Args:
            length: An optional amount of data to retrieve. A length of 0 (the
                default) will retrieve a least one buffer of data.

        Note:
            If a length of 0 is supplied then the size of the yielded buffer can
            vary. If there is data in the internal buffer it will yield all of
            that data otherwise it will yield the the data returned by a recv
            on the socket.
        """
        while True:
            buf = self.__buffer.read(length)
            if not buf:
                self.__buffer.write(self.socket.recv(4096))
                continue
            yield buf

    def __drain(self):
        """Reads lines until a termiating line is recieved.
        """
        for line in self.__line_gen():
            pass

    def __status(self):
        """Reads a command response status.

        If there is no response message then the returned status message will
        be an empty string. 
        
        Raises:
            NNTPProtocolError: If the status line can't be parsed.
            NNTPTemporaryError: For status code 400-499
            NNTPPermanentError: For status code 500-599

        Returns:
            A tuple of status code (as an integer) and status message.
        """
        line = next(self.__line_gen()).rstrip()
        parts = line.split(None, 1)

        try:
            code, message = int(parts[0]), ""
        except ValueError:
            raise NNTPProtocolError(line)

        if code < 100 or code >= 600:
            raise NNTPProtocolError(line)

        if len(parts) > 1:
            message = parts[1]
        
        if 400 <= code <= 499:
            raise NNTPTemporaryError(code, message)

        if 500 <= code <= 599:
            raise NNTPPermanentError(code, message)

        return code, message

    def __info_compressed_yenc_zlib_gen(self):
        """Generator for the lines of a compressed info (textual) response.

        Compressed responses are an extension to the NNTP protocol supported by
        some usenet servers to reduce the bandwidth of heavily used range style
        commands that can return large amounts of textual data. The server
        returns that same data as it would for the uncompressed versions of the
        command the difference being that the data is zlib deflated and then
        yEnc encoded.

        This function will produce that same output as the __info_gen()
        function. In other words it takes care of decoding and decompression.

        The usaged principles for the __info_gen() function also apply here.

        Raises:
            NNTPDataError: When there is an error parsing the yEnc header or
                trailer or if the CRC check fails.
        """
        escape = 0
        dcrc32 = 0
        inflate = zlib.decompressobj(-15)

        # header
        header = next(self.__line_gen())
        if not header.startswith("=ybegin"):
            self.__drain()
            raise NNTPDataError("Bad yEnc header")

        # data
        buf, trailer = fifo.Fifo(), ""
        for line in self.__line_gen():
            if line.startswith("=yend"):
                trailer = line
                continue
            data, escape, dcrc32 = yenc.decode(line, escape, dcrc32)
            data = inflate.decompress(data)
            if not data:
                continue
            buf.write(data)
            for l in buf:
                yield l

        # trailer
        if not trailer:
            raise NNTPDataError("Missing yEnc trailer")

        # expected crc32
        ecrc32 = yenc.crc32(trailer)
        if ecrc32 is None:
            raise NNTPDataError("Bad yEnc trailer")

        # check crc32
        if ecrc32 != dcrc32 & 0xffffffff:
            raise NNTPDataError("Bad yEnc CRC")

    def __info_compressed_gzip_gen(self):
        """Generator for the lines of a compressed info (textual) response.

        Compressed responses are an extension to the NNTP protocol supported by
        some usenet servers to reduce the bandwidth of heavily used range style
        commands that can return large amounts of textual data.

        This function handles gzip compressed responses that have the
        terminating line inside or outside the compressed data. From experience
        if the 'XFEATURE COMPRESS GZIP' command causes the terminating '.\\r\\n'
        to follow the compressed data and 'XFEATURE COMPRESS GZIP TERMINATOR'
        causes the terminator to be the last part of the compressed data (i.e
        the reply the gzipped version of the original reply - terminating line
        included)

        This function will produce that same output as the __info_plain_gen()
        function. In other words it takes care of decoding and decompression.

        The usaged principles for the __info_plain_gen() function also apply here.
        """
        inflate = zlib.decompressobj(15+32)

        # data
        buf = fifo.Fifo()
        unused = ""
        for data in self.__buf_gen():
            data = inflate.decompress(data)
            unused += inflate.unused_data
            if data:
                buf.write(data)
                for line in buf:
                    if line == ".\r\n":
                        return
                    if line.startswith(".."):
                        yield line[1:]
                    yield line
            if unused == ".\r\n":
                return

    def __info_plain_gen(self):
        """Generator for the lines of an info (textual) response.

        For commands that can yield large amounts of data this should be used in
        preference to __info_plain() so that memory use can be minimised and that
        the data can be processed in parallel to being received.

        This is equivient to the __line_gen() generator.
        """
        return self.__line_gen()

    def __info_plain(self):
        """The complete content of an info (textual) response.

        This should only used for commands that return small or known amounts of
        data.

        Returns:
            A the complete content of a textual response.
        """
        return "".join([x for x in self.__info_plain()])
   
    def __info_gen(self, code, message, compressed=False):
        """Dispatcher for the info generators.

        Determines which __info_*_gen() should be used based on the supplied
        parameters.

        Args:
            code: The status code for the command response.
            message: The status message for the command reponse.
            compressed: Force decompression. Useful for xz* commands.

        Returns:
            An info generator.
        """
        if "COMPRESS=GZIP" in message:
            return self.__info_compressed_gzip_gen()
        if compressed:
            return self.__info_compressed_yenc_zlib_gen()
        return self.__info_plain_gen()

    def __info(self, code, message, compressed=False):
        """The complete content of an info response.

        This should only used for commands that return small or known amounts of
        data.

        Returns:
            A the complete content of a textual response.
        """
        return "".join([x for x in self.__info_gen(code, message, compressed)])
 
    def __command(self, verb, args=None):
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
        """
        cmd = verb
        if args:
            cmd += " " + args
        cmd += "\r\n"

        self.socket.sendall(cmd)

        try:
            code, message = self.__status()
        except NNTPTemporaryError as e:
            if e.code() != 480:
                raise e
            code, message = self.__command("AUTHINFO USER", self.username)
            if code == 381:
                code, message = self.__command("AUTHINFO PASS", self.password)
            if code != 281:
                raise NNTPReplyError(code, message)
            code, message = self.__command(verb, args)

        return code, message


    # helpers

    @staticmethod
    def __parse_msgid_article(obj):
        """Parse a message-id or article number argument.
        """
        return str(obj)

    @staticmethod
    def __parse_range(obj):
        """Parse a range argument.
        """
        if isinstance(obj, (int, long)):
            return str(obj)

        if isinstance(obj, tuple):
            arg = str(obj[0]) + "-"
            if len(obj) > 1:
                arg += str(obj[1])
            return arg
        
        raise ValueError("Must be an integer or tuple")

    @staticmethod
    def __parse_msgid_range(obj):
        """Parse a message-id or range argument.
        """
        if isinstance(obj, basestring):
            return obj

        return Reader.__parse_range(obj)

    @staticmethod
    def __parse_newsgroup(line):
        """Parse a newsgroup info line to python types.
        """
        parts = line.split()
        try:
            group = parts[0]
            low = int(parts[1])
            high = int(parts[2])
            status = parts[3]
        except (IndexError, ValueError):
            raise NNTPDataError("Invalid newsgroup info")
        return group, low, high, status


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

        Returns:
            A list of capabilities supported by the server. The VERSION
            capability is the first capability in the list.
        """
        args = keyword

        code, message = self.__command("CAPABILITIES", args)
        if code != 101:
            raise NNTPReplyError(code, message)

        return [x.strip() for x in self.__info_gen(code, message)]

    def mode_reader(self):
        """MODE READER command.

        Instructs a mode-switching server to switch modes.

        See <http://tools.ietf.org/html/rfc3977#section-5.3>

        Returns:
            Boolean value indicating whether posting is allowed or not.
        """
        code, message = self.__command("MODE READER")
        if not code in [200, 201]:
            raise NNTPReplyError(code, message)

        return code == 200

    def quit(self):
        """QUIT command.

        Tells the server to close the connection. After the server acknowledges
        the request to quit the connection is closed both at the server and
        client.

        Once this method has been called, no other methods of the Reader object
        should be called.

        See <http://tools.ietf.org/html/rfc3977#section-5.4>
        """
        code, message = self.__command("QUIT")
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
        code, message = self.__command("DATE")
        if code != 111:
            raise NNTPReplyError(code, message)

        try:
            ts = datetime.datetime.strptime(message, "%Y%m%d%H%M%S")
        except TypeError:
            raise NNTPDataError("Bad timestamp")

        return ts.replace(tzinfo=date.TZ_GMT)

    def help(self):
        """HELP command.

        Provides a short summary of commands that are understood by the usenet
        server.

        See <http://tools.ietf.org/html/rfc3977#section-7.2>

        Returns:
            The help text from the server.
        """
        code, message = self.__command("HELP")
        if code != 100:
            raise NNTPReplyError(code, message)
        
        return self.__info(code, message)

    def newgroups_gen(self, timestamp):
        """Generator for the NEWGROUPS command.

        Generates a list of newsgroups created on the server since the specified
        timestamp.

        See <http://tools.ietf.org/html/rfc3977#section-7.3>

        Args:
            timestamp: Datetime object giving 'created since' datetime.

        Yields:
            A tuple containing the name, low water mark, high water mark,
            and status for each newsgroup.

        Note: For more detail on the status field see the list_active() command
            which has the same return format.

        Note: If the datetime object supplied as the timestamp is naive (tzinfo
            is None) then it is assumed to be given as GMT. If tzinfo is set
            then it will be converted to GMT.
        """
        if timestamp.tzinfo:
            ts = timestamp.asttimezone(date.TZ_GMT)
        else:
            ts = timestamp.replace(tzinfo=date.TZ_GMT)

        args = ts.strftime("%Y%m%d %H%M%S %Z")

        code, message = self.__command("NEWGROUPS", args)
        if code != 231:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen(code, message):
            yield self.__parse_newsgroup(line)
    
    def newgroups(self, timestamp):
        """NEWGROUPS command.

        Retreives a list of newsgroups created on the server since the specified
        timestamp. See newgroups_gen() for more details.

        See <http://tools.ietf.org/html/rfc3977#section-7.3>

        Args:
            timestamp: Datetime object giving 'created since' datetime.

        Returns:
            A list of tuples in the format given by newgroups_gen()
        """
        return [x for x in self.newgroups_gen(timestamp)]

    def newnews_gen(self, pattern, timestamp):
        """Generator for the NEWNEWS command.

        Generates a list of message-ids for articles created since the specified
        timestamp for newsgroups with names that match the given pattern.

        See <http://tools.ietf.org/html/rfc3977#section-7.4>

        Args:
            pattern: Glob matching newsgroups of intrest.
            timestamp: Datetime object giving 'created since' datetime.

        Yields:
            A message-id as string.

        Note: If the datetime object supplied as the timestamp is naive (tzinfo
            is None) then it is assumed to be given as GMT. If tzinfo is set
            then it will be converted to GMT by this function.
        """
        if timestamp.tzinfo:
            ts = timestamp.asttimezone(date.TZ_GMT)
        else:
            ts = timestamp.replace(tzinfo=date.TZ_GMT)

        args = pattern
        args += " " + ts.strftime("%Y%m%d %H%M%S %Z")

        code, message = self.__command("NEWNEWS", args)
        if code != 230:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen(code, message):
            yield line.strip()

    def newnews(self, pattern, timestamp):
        """NEWNEWS command.

        Retrieves a list of message-ids for articles created since the specified
        timestamp for newsgroups with names that match the given pattern. See
        newnews_gen() for more details.

        See <http://tools.ietf.org/html/rfc3977#section-7.4>

        Args:
            pattern: Glob matching newsgroups of intrest.
            timestamp: Datetime object giving 'created since' datetime.

        Returns:
            A list of message-ids as given by newnews_gen()
        """
        return [x for x in self.newnews_gen(pattern, timestamp)]


    # list commands

    def list_active_gen(self, pattern=None):
        """Generator for the LIST ACTIVE command.

        See list_active() for more information.

        Yields:
            An element in the list returned by list_active().
        """
        args = pattern

        if args is None:
            cmd = "LIST"
        else:
            cmd = "LIST ACTIVE"
            
        code, message = self.__command(cmd, args)
        if code != 215:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen(code, message):
            yield self.__parse_newsgroup(line)

    def list_active(self, pattern=None):
        """LIST ACTIVE command.
        """
        return [x for x in self.list_active_gen(pattern)]

    def list_active_times_gen(self, pattern=None):
        """Generator for the LIST ACTIVE TIMES command.
        """
        raise NotImplementedError()
 
    def list_active_times(self, pattern=None):
        """LIST ACTIVE TIMES command.
        """
        return [x for x in self.list_active_times_gen(pattern)]

    def list_distrib_pats_gen(self):
        """Generator for the LIST DISTRIB.PATS command.
        """
        raise NotImplementedError()

    def list_distrib_pats(self):
        """LIST DISTRIB.PATS command.
        """
        return [x for x in self.list_distrib_pats_gen()]

    def list_headers_gen(self, arg=None):
        """Generator for the LIST HEADERS command.
        """
        raise NotImplementedError()

    def list_headers(self, arg=None):
        """LIST HEADERS command.
        """
        return [x for x in self.list_headers_gen(arg)]

    def list_newsgroups_gen(self, pattern=None):
        """Generator for the LIST NEWSGROUPS command.
        """
        args = pattern

        code, message = self.__command("LIST NEWSGROUPS", args)
        if code != 215:
            raise NNTPReplyError(code, message)

        return self.__info_gen(code, message)
    
    def list_newsgroups(self, pattern=None):
        """LIST NEWSGROUPS command.
        """
        return [x for x in self.list_newsgroups_gen(pattern)]

    def list_overview_fmt_gen(self):
        """Generator for the LIST OVERVIEW.FMT

        See list_overview_fmt() for more information.

        Yields:
            An element in the list returned by list_overview_fmt().
        """
        code, message = self.__command("LIST OVERVIEW.FMT")
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen(code, message):
            try:
                name, suffix = line.rstrip().split(":")
            except ValueError:
                raise NNTPDataError("Invalid LIST OVERVIEW.FMT")
            if suffix and not name:
                name, suffix = suffix, name
            if suffix and suffix != "full":
                raise NNTPDataError("Invalid LIST OVERVIEW.FMT")
            yield (name, suffix == "full")

    def list_overview_fmt(self):
        """LIST OVERVIEW.FMT command.
        """
        return [x for x in self.list_overview_fmt_gen()]

    def list_extensions_gen(self):
        """Generator for the LIST EXTENSIONS command.
        """
        code, message = self.__command("LIST EXTENSIONS")
        if code != 202:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen(code, message):
            yield line.strip()

    def list_extensions(self):
        """LIST EXTENSIONS command.
        """
        return [x for x in self.list_extensions_gen()]

    def list_gen(self, keyword=None, arg=None):
        """Generator for LIST command.

        See list() for more information.

        Yields:
            An element in the list returned by list().
        """
        if keyword:
            keyword = keyword.upper()

        if keyword is None or keyword == "ACTIVE":
            return self.list_active_gen(arg)
        if keyword == "ACTIVE.TIMES":
            return self.list_active_times_gen(arg)
        if keyword == "DISTRIB.PATS":
            return self.list_distrib_pats_gen()
        if keyword == "HEADERS":
            return self.list_headers_gen(arg)
        if keyword == "NEWSGROUPS":
            return self.list_newsgroups_gen(arg)
        if keyword == "OVERVIEW.FMT":
            return self.list_overview_fmt_gen()
        if keyword == "EXTENSIONS":
            return self.list_extensions_gen()

        raise NotImplementedError()

    def list(self, keyword=None, arg=None):
        """LIST command.

        A wrapper for all of the other list commands. The output of this command
        depends on the keyword specified. The output format for each keyword can
        be found in the list function that corresponds to the keyword.

        Args:
            keyword: Information requested.
            arg: Pattern or keyword specific argument.

        Note: Keywords supported by this function are include ACTIVE,
            ACTIVE.TIMES, DISTRIB.PATS, HEADERS, NEWSGROUPS, OVERVIEW.FMT and
            EXTENSIONS.

        Raises:
            NotImplementedError: For unsupported keywords.
        """
        return [x for x in self.list_gen(keyword, arg)]

    def group(self, name):
        """GROUP command.
        """
        args = name

        code, message = self.__command("GROUP", args)
        if code != 211:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 4)
        try:
            total = int(parts[0])
            first = int(parts[1])
            last  = int(parts[2])
            group = parts[3]
        except (IndexError, ValueError):
            raise NNTPDataError("Invalid GROUP status '%s'" % message)

        return total, first, last, group

    def next(self):
        """NEXT command.
        """
        code, message = self.__command("NEXT")
        if code != 223:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 3)
        try:
            article = int(parts[0])
            ident = parts[1]
        except (IndexError, ValueError):
            raise NNTPDataError("Invalid NEXT status")

        return article, ident

    def last(self):
        """LAST command.
        """
        code, message = self.__command("LAST")
        if code != 223:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 3)
        try:
            article = int(parts[0])
            ident = parts[1]
        except (IndexError, ValueError):
            raise NNTPDataError("Invalid LAST status")

        return article, ident

    def article(self, msgid_article=None):
        """ARTICLE command.
        """
        args = None
        if msgid_article is not None:
            args = self.__parse_msgid_article(msgid_article)

        code, message = self.__command("ARTICLE", args)
        if code != 221:
            raise NNTReplyError(code, message)

        return self.__info(code, message)

    def head(self, msgid_article=None):
        """HEAD command.
        """
        args = None
        if msgid_article is not None:
            args = self.__parse_msgid_article(msgid_article)

        code, message = self.__command("HEAD", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info(code, message)

    def xgtitle(self, pattern=None):
        """XGTITLE command.
        """
        args = pattern

        code, message = self.__command("XGTITLE", args)
        if code != 282:
            raise NNTPReplyError(code, message)

        return self.__info(code, message)

    def xhdr(self, header, msgid_range=None):
        """XHDR command.
        """
        args = header
        if range is not None:
            args += " " + self.__parse_msgid_range(msgid_range)

        code, message = self.__command("XHDR", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info(code, message)
    
    def xzhdr(self, header, msgid_range=None):
        """XZHDR command.

        Args:
            msgid_range: A message-id as a string, or an article number as an
                integer, or a tuple of specifying a range of article numbers in
                the form (first, [last]) - if last is omitted then all articles
                after first are included. A msgid_range of None (the default)
                uses the current article.
        """
        args = header
        if msgid_range is not None:
            args += " " + self.__parse_msgid_range(msgid_range)

        code, message = self.__command("XZHDR", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info(code, message, compressed=True)

    def xover_gen(self, range=None):
        """Generator for the XOVER command.

        The XOVER command returns information from the overview database for
        the article(s) specified.

        <http://tools.ietf.org/html/rfc2980#section-2.8>

        Args:
            range: An article number as an integer, or a tuple of specifying a
                range of article numbers in the form (first, [last]). If last is
                omitted then all articles after first are included. A range of
                None (the default) uses the current article.

        Returns:
            A list of fields as given by the overview database for each
            available article in the specified range. The fields that are
            returned can be determined using the LIST OVERVIEW.FMT command if
            the server supports it.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """
        args = None
        if range is not None:
            args = self.__parse_range(range)

        code, message = self.__command("XOVER", args)
        if code != 224:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen(code, message):
            yield line.rstrip().split("\t")

    def xover(self, range=None):
        """The XOVER command.

        The XOVER command returns information from the overview database for
        the article(s) specified.

        <http://tools.ietf.org/html/rfc2980#section-2.8>

        Args:
            range: An article number as an integer, or a tuple of specifying a
                range of article numbers in the form (first, [last]). If last is
                omitted then all articles after first are included. A range of
                None (the default) uses the current article.

        Returns:
            A table (list of lists) of articles and their fields as given by the
            overview database for each available article in the specified range.
            The fields that are given can be determined using the LIST
            OVERVIEW.FMT command if the server supports it.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """
        return [x for x in self.xover_gen(range)]

    def xzver_gen(self, range=None):
        """Generator for the XZVER command.

        The XZVER command returns information from the overview database for
        the article(s) specified. It is part of the compressed headers
        extensions that are supported by some usenet servers. It is the
        compressed version of the XOVER command.

        <http://helpdesk.astraweb.com/index.php?_m=news&_a=viewnews&newsid=9>

        Args:
            range: An article number as an integer, or a tuple of specifying a
                range of article numbers in the form (first, [last]). If last is
                omitted then all articles after first are included. A range of
                None (the default) uses the current article.

        Returns:
            A list of fields as given by the overview database for each
            available article in the specified range. The fields that are
            returned can be determined using the LIST OVERVIEW.FMT command if
            the server supports it.

        Raises:
            NNTPTemporaryError: If no such article exists or the currently
                selected newsgroup is invalid.
            NNTPDataError: If the compressed response cannot be decoded.
        """
        args = None
        if range is not None:
            args = self.__parse_range(range)

        code, message = self.__command("XZVER", args)
        if code != 224:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen(code, message, True):
            yield line.rstrip().split("\t")

    def xzver(self, range=None):
        """XZVER command.

        The XZVER command returns information from the overview database for
        the article(s) specified. It is part of the compressed headers
        extensions that are supported by some usenet servers. It is the
        compressed version of the XOVER command.

        <http://helpdesk.astraweb.com/index.php?_m=news&_a=viewnews&newsid=9>

        Args:
            range: An article number as an integer, or a tuple of specifying a
                range of article numbers in the form (first, [last]). If last is
                omitted then all articles after first are included. A range of
                None (the default) uses the current article.

        Returns:
            A list of fields as given by the overview database for each
            available article in the specified range. The fields that are
            returned can be determined using the LIST OVERVIEW.FMT command if
            the server supports it.

        Raises:
            NNTPTemporaryError: If no such article exists or the currently
                selected newsgroup is invalid.
            NNTPDataError: If the compressed response cannot be decoded.
        """
        return [x for x in self.xzver_gen(range)]

    def xpat_gen(self, header, msgid_range, *pattern):
        """Generator for the XPAT command.
        """
        args = " ".join(
            [header, self.__parse_msgid_range(msgid_range)] + list(pattern)
        )

        code, message = self.__command("XPAT", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen(code, message):
            yield line.strip()

    def xpat(self, header, id_range, *pattern):
        """XPAT command.
        """
        return [x for x in self.xpat_gen(header, id_range, *pattern)]
    
    def xfeature_compress_gzip(self, terminator=False):
        """XFEATURE COMPRESS GZIP command.
        """
        args = "TERMINATOR" if terminator else None

        code, message = self.__command("XFEATURE COMPRESS GZIP", args)
        if code != 290:
            raise NNTPReplyError(code, message)

        return True
        
if __name__ == "__main__":

    import sys
    import hashlib

    try:
        host = sys.argv[1]
        port = int(sys.argv[2])
        username = sys.argv[3]
        password = sys.argv[4]
        use_ssl = int(sys.argv[5])
    except:
        print "%s <host> <port> <username> <password> <ssl(0|1)>" % sys.argv[0]
        sys.exit(1)

    r = Reader(host, port, username, password, use_ssl=use_ssl)

    print "HELP"
    try:
        print r.help()
    except NNTPReplyError as e:
        print e
    print

    print "DATE"
    try:
        print r.date()
    except NNTPReplyError as e:
        print e
    print

    print "NEWGROUPS"
    try:
        print r.newgroups(datetime.datetime.utcnow() - datetime.timedelta(days=50))
    except NNTPReplyError as e:
        print e
    print

    print "NEWNEWS"
    try:
        print r.newnews("alt.binaries.*", datetime.datetime.utcnow() - datetime.timedelta(minutes=1))
    except NNTPReplyError as e:
        print e
    print

    print "CAPABILITIES"
    try:
        print r.capabilities()
    except NNTPReplyError as e:
        print e
    print

    print "GROUP alt.binaries.boneless"
    try:
        total, first, last, name = r.group("alt.binaries.boneless")
        print total, first, last, name
    except NNTPReplyError as e:
        print e
    print

    print "HEAD"
    try:
        print r.head()
    except NNTPReplyError as e:
        print e
    print

    print "XHDR Date", "%d-%d" % (last-10, last)
    try:
        print r.xhdr("Date", (last-10, last))
    except NNTPReplyError as e:
        print e
    print

    print "XZHDR Date", "%d-%d" % (last-10, last)
    try:
        print r.xzhdr("Date", (last-10, last))
    except NNTPReplyError as e:
        print e
    print

    print "XOVER" , "%d-%d" % (last-10, last)
    try:
        result = r.xover((last-10, last))
        print "Entries", len(result), "Hash", hashlib.md5(
            "".join(["".join(x) for x in result])
        ).hexdigest()
    except NNTPReplyError as e:
        print e
    print

    print "XZVER" , "%d-%d" % (last-10, last)
    try:
        result = r.xzver((last-10, last))
        print "Entries", len(result), "Hash", hashlib.md5(
            "".join(["".join(x) for x in result])
        ).hexdigest()
    except NNTPReplyError as e:
        print e
    print

    print "XFEATURE COMPRESS GZIP"
    try:
        print r.xfeature_compress_gzip()
    except NNTPReplyError as e:
        print e
    print

    print "XOVER" , "%d-%d" % (last-10, last)
    try:
        result = r.xover((last-10, last))
        print "Entries", len(result), "Hash", hashlib.md5(
            "".join(["".join(x) for x in result])
        ).hexdigest()
    except NNTPReplyError as e:
        print e
    print

    print "XFEATURE COMPRESS GZIP TERMINATOR"
    try:
        print r.xfeature_compress_gzip()
    except NNTPReplyError as e:
        print e
    print

    print "XOVER" , "%d-%d" % (last-10, last)
    try:
        result = r.xover((last-10, last))
        print "Entries", len(result), "Hash", hashlib.md5(
            "".join(["".join(x) for x in result])
        ).hexdigest()
    except NNTPReplyError as e:
        print e
    print
    
    print "LIST"
    try:
        print "Entries", len(r.list())
    except NNTPReplyError as e:
        print e
    print

    print "LIST ACTIVE"
    try:
        print "Entries", len(r.list("ACTIVE"))
    except NNTPReplyError as e:
        print e
    print

    print "LIST ACTIVE alt.binaries.*"
    try:
        print "Entries", len(r.list("ACTIVE", "alt.binaries.*"))
    except NNTPReplyError as e:
        print e
    print
    
    print "LIST NEWSGROUPS"
    try:
        print "Entries", len(r.list("NEWSGROUPS"))
    except NNTPReplyError as e:
        print e
    print

    print "LIST NEWSGROUPS alt.binaries.*"
    try:
        print "Entries", len(r.list("NEWSGROUPS", "alt.binaries.*"))
    except NNTPReplyError as e:
        print e
    print

    print "LIST OVERVIEW.FMT"
    try:
        print r.list("OVERVIEW.FMT")
    except NNTPReplyError as e:
        print e
    print

    print "LIST EXTENSIONS"
    try:
        print r.list("EXTENSIONS")
    except NNTPReplyError as e:
        print e
    print
