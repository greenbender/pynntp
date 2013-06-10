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

#NOTE: docstrings beed to be added

import re
import ssl
import zlib
import shlex
import socket
import datetime
import iodict
import fifo
import yenc

class NNTPError(Exception):
    pass

class NNTPReplyError(NNTPError):
    def __init__(self, code, message):
        NNTPError.__init__(self, code, message)
    def code(self):
        return self.args[0]
    def message(self):
        return self.args[1]
    def __str__(self):
        return "%d: %s" % self.args

class NNTPTemporaryError(NNTPReplyError):
    pass

class NNTPPermanentError(NNTPReplyError):
    pass

class NNTPProtocolError(NNTPError):
    pass

class NNTPDataError(NNTPError):
    pass


class GMT(datetime.tzinfo):
    """GMT"""

    def utcoffset(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "GMT"

    def dst(self, dt):
        return datetime.timedelta(0)

gmt = GMT()


class UTC(GMT):
    """UTC"""

    def tzname(self, dt):
        return "UTC"

utc = UTC()


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

        raises:
            socket.error: On error in underlying socket and/or ssl wrapper. See
                socket and ssl modules for further details.
            NNTPReplyError: On bad response code from server.
        """

        self.socket = socket.socket()
        self.socket.settimeout(timeout)
        if use_ssl:
            self.socket = ssl.wrap_socket(self.socket)

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

    def __info_gen(self):
        """Generator for the lines of an info (textual) response.

        For commands that can yield large amounts of data this should be used in
        preference to __info() so that memory use can be minimised and that the
        data can be processed in parallel to being received.

        This is equivient to the __line_gen() generator.
        """

        return self.__line_gen()

    def __info(self):
        """The complete content of an info (textual) response.

        This should only used for commands that return small or known amounts of
        data.

        Returns:
            A the complete content of a textual response.
        """

        return "".join([x for x in self.__info_gen()])
    
    def __info_compressed_gen(self):
        """Generator for the lines of a compressed info (textual) response.

        Compressed responses are an extension to the NNTP protocol supported by
        some usenet servers to reduce the bandwidth of heavily used range style
        commands that can return large amounts of textual data. The server
        returns that same data as it would for the uncompressed versions of the
        command the difference being that the data is gzip compressed and then
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

    def __info_compressed(self):
        """The complete content of a compressed info (textual) response.

        See the __info_compressed_gen() function for further information
        regarding compressed commands and responses.

        This should only used for commands that return small or known amounts of
        data.

        Returns:
            A the complete content of a compressed textual response.
        """

        return "".join([x for x in self.__info_compressed_gen()])

    def __command(self, verb, args=None):
        """Call a command on the server.

        If the user has not authenticated then authentication will be done
        as part of making calling the command on the server.

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
            cmd += " %s" % args
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

        return [x.strip() for x in self.__info_gen()]

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
        the request to quit the connection is closed both at the server and by
        this function.

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
            return datetime.datetime.strptime(message, "%Y%m%d%H%M%S").replace(tzinfo=utc)
        except TypeError:
            raise NNTPDataError("Bad timestamp")

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
        
        return self.__info()

    def newgroups_gen(self, timestamp):
        """Generator for the NEWGROUPS command.

        Yields a tuple containing the name, low water mark, high water mark,
        and status of the next newsgroup that was created on the server since
        the specified timestamp.

        See the newsgroup() function for more detailed information.

        See <http://tools.ietf.org/html/rfc3977#section-7.3>

        Args:
            timestamp: Datetime object giving create-after datetime.

        Note: If the datetime object supplied as the timestamp is naive (tzinfo
            is None) then it is assumed to be given as GMT. If tzinfo is set
            then it will be converted to GMT by this function.
        """

        ts = timestamp
        if ts.tzinfo:
            ts = ts.asttimezone(gmt)

        args = ts.strftime("%Y%m%d %H%M%S %Z")

        code, message = self.__command("NEWGROUPS", args)
        if code != 231:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen():
            parts = line.rstrip().split()
            try:
                group = parts[0]
                high = int(parts[1])
                low  = int(parts[2])
                status  = parts[3]
            except (IndexError, ValueError):
                raise NNTPDataError("Invalid LIST info")
            yield group, high, low, status
    
    def newgroups(self, timestamp):
        """NEWGROUPS command.

        Retreives a list of newsgroups created on the server since the specified
        timestamp. The timestamp will be converted to GMT.

        See <http://tools.ietf.org/html/rfc3977#section-7.3>

        Args:
            timestamp: Datetime object giving create-after datetime.

        Returns:
            A list of newsgroups created after the specified timestamp, each
            entry in the list is a tuple containing the newgroups name,
            highwater mark (as an integer), low water mark (as an integer) and
            status.

        Note: More more detail on the status field see the list_active() command
            which has the same return format.
        """

        return [x for x in self.newgroups_gen(timestamp)]

    def newnews(self):
        """NEWNEWS command.

        See <http://tools.ietf.org/html/rfc3977#section-7.4>
        """

        raise NotImplementedError()


    # list commands

    def list_active_gen(self, pattern=None):
        """Generator for the LIST ACTIVE command.

        Note: The status field is typically one of the following:
            "y" Posting is permitted.
            "n" Posting is not permitted.
            "m" Postings will be forwarded to the newsgroup moderator.
        """

        args = pattern

        if args is None:
            cmd = "LIST"
        else:
            cmd = "LIST ACTIVE"
            
        code, message = self.__command(cmd, args)
        if code != 215:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen():
            parts = line.rstrip().split()
            try:
                group = parts[0]
                first = int(parts[1])
                last  = int(parts[2])
                post  = {"y": True, "n": False, "m": None}[parts[3]]
            except (IndexError, ValueError, KeyError):
                raise NNTPDataError("Invalid LIST info")
            yield group, first, last, post

    def list_active(self, pattern=None):

        return [x for x in self.list_active_gen()]

    def list_active_times_gen(self, pattern=None):
    
        raise NotImplementedError()
 
    def list_active_times(self, pattern=None):

        return [x for x in self.list_active_times_gen(pattern)]

    def list_distrib_pats_gen(self):

        raise NotImplementedError()

    def list_distrib_pats(self):

        return [x for x in self.list_distrib_pats_gen()]

    def list_headers_gen(self, arg=None):

        raise NotImplementedError()

    def list_headers(self, arg=None):

        return [x for x in self.list_headers_gen(arg)]

    def list_newsgroups_gen(self, pattern=None):

        args = pattern

        code, message = self.__command("LIST NEWSGROUPS", args)
        if code != 215:
            raise NNTPReplyError(code, message)

        return self.__info_gen()
    
    def list_newsgroups(self, pattern=None):

        return [x for x in self.list_newsgroups_gen(pattern)]

    def list_overview_fmt_gen(self):
        """Generator for LIST OVERVIEW.FMT

        This is not really appropriate as a generator it is just implemented in
        this way to maintain consistency with other LIST commands.
        """

        code, message = self.__command("LIST OVERVIEW.FMT")
        if code != 215:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen():
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

        code, message = self.__command("LIST EXTENSIONS")
        if code != 202:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen():
            yield line.strip()

    def list_extensions(self):

        return [x for x in self.list_extensions_gen()]

    def list_gen(self, keyword=None, pattern=None):
        """Generator for LIST command.

        For each element in the list a result is yielded. The format of this
        result is determined by the keyword that is used. For more details on
        the return formats see the direct call version of the keyword you are
        using.

        Args:
            keyword: Information requested.
            pattern: Pattern to match group or keyword specific argument.

        Note: Keywords supported by this function are include ACTIVE,
            ACTIVE.TIMES, DISTRIB.PATS, HEADERS, NEWSGROUPS and OVERVIEW.FMT.

        Raises:
            NotImplementedError: For unsupported keywords.
        """

        if keyword:
            keyword = keyword.upper()

        if keyword is None or keyword == "ACTIVE":
            return self.list_active_gen(pattern)
        if keyword == "ACTIVE.TIMES":
            return self.list_active_times_gen(pattern)
        if keyword == "DISTRIB.PATS":
            return self.list_distrib_pats_gen()
        if keyword == "HEADERS":
            return self.list_headers_gen(pattern)
        if keyword == "NEWSGROUPS":
            return self.list_newsgroups_gen(pattern)
        if keyword == "OVERVIEW.FMT":
            return self.list_overview_fmt_gen()
        if keyword == "EXTENSIONS":
            return self.list_extensions_gen()

        raise NotImplementedError()

    def list(self, keyword=None, pattern=None):
        """LIST command.
        """

        return [x for x in self.list_gen(keyword, pattern)]

    def group(self, name):

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

    def article(self, ident=None):

        args = ident
        if isinstance(args, int):
            args = str(args)

        code, message = self.__command("ARTICLE", args)
        if code != 221:
            raise NNTReplyError(code, message)

        return self.__info()

    def head(self, ident=None):

        args = ident
        if isinstance(args, int):
            args = str(args)

        code, message = self.__command("HEAD", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info()

    def xgtitle(self, pattern=None):

        args = pattern

        code, message = self.__command("XGTITLE", args)
        if code != 282:
            raise NNTPReplyError(code, message)

        return self.__info()

    def xhdr(self, header, first=None, last=None):

        args = header
        if isinstance(first, (int, long)):
            args += " " + str(first) + "-"
            if last is not None:
                args += str(last)
        elif isinstance(first, str):
            args += " " + first

        code, message = self.__command("XHDR", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info()
    
    def xzhdr(self, header, first=None, last=None):

        args = header
        if isinstance(first, (int, long)):
            args += " " + str(first) + "-"
            if last is not None:
                args += str(last)
        elif isinstance(first, str):
            args += " " + first

        code, message = self.__command("XZHDR", args)
        if code != 221:
            raise NNTPReplyError(code, message)

        return self.__info_compressed()

    def xover_gen(self, first=None, last=None):
        """Generator for the XOVER command.

        The XOVER command returns information from the overview database for
        the article(s) specified.

        <http://tools.ietf.org/html/rfc2980#section-2.8>

        Args:
            first (int): First article number in range. If not supplied the
                current article is retrieved.
            last (int): Last article number in range. If not supplied all
                articles following the specified first article are retrieved.

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
        if first is not None:
            args = str(first) + "-"
            if last is not None:
                args += str(last)

        code, message = self.__command("XOVER", args)
        if code != 224:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen():
            yield line.rstrip().split("\t")

    def xover(self, first=None, last=None):
        """The XOVER command.

        The XOVER command returns information from the overview database for
        the article(s) specified.

        <http://tools.ietf.org/html/rfc2980#section-2.8>

        Args:
            first (int): First article number in range. If not supplied the
                current article is retrieved.
            last (int): Last article number in range. If not supplied all
                articles following the specified first article are retrieved.

        Returns:
            A table (list of lists) of articles and their fields as given by the
            overview database for each available article in the specified range.
            The fields that are given can be determined using the LIST
            OVERVIEW.FMT command if the server supports it.

        Raises:
            NNTPReplyError: If no such article exists or the currently selected
                newsgroup is invalid.
        """

        return [x for x in self.xover_gen(first, last)]

    def xzver_gen(self, first=None, last=None):
        """Generator for the XZVER command.

        The XZVER command returns information from the overview database for
        the article(s) specified. It is part of the compressed headers
        extensions that are supported by some usenet servers. It is the
        compressed version of the XOVER command.

        <http://helpdesk.astraweb.com/index.php?_m=news&_a=viewnews&newsid=9>

        Args:
            first (int): First article number in range. If not supplied the
                current article is retrieved.
            last (int): Last article number in range. If not supplied all
                articles following the specified first article are retrieved.

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
        if first is not None:
            args = str(first) + "-"
            if last is not None:
                args += str(last)

        code, message = self.__command("XZVER", args)
        if code != 224:
            raise NNTPReplyError(code, message)

        for line in self.__info_compressed_gen():
            yield line.split("\t")

    def xzver(self, first=None, last=None):

        return [x for x in self.xzver_gen(first, last)]
        
if __name__ == "__main__":

    import sys

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
    print r.help()
    print

    print "DATE"
    print r.date()
    print

    print "NEWGROUPS"
    print r.newgroups(datetime.datetime.utcnow() - datetime.timedelta(days=50))
    print

    print "LIST"
    print "LIST", len(r.list())
    print "LIST ACTIVE", len(r.list("ACTIVE"))
    print "LIST ACTIVE alt.binaries.*", len(r.list("ACTIVE", "alt.binaries.*"))
    print "LIST NEWSGROUPS", len(r.list("NEWSGROUPS"))
    print "LIST NEWSGROUPS alt.binaries.*", len(r.list("NEWSGROUPS", "alt.binaries.*"))
    print "LIST OVERVIEW.FMT", len(r.list("OVERVIEW.FMT"))
    print "LIST EXTENSIONS", r.list("EXTENSIONS")
    print

    #print "CAPABILITIES"
    #print r.capabilities()
    #print

    print "GROUP alt.binaries.boneless"
    total, first, last, name = r.group("alt.binaries.boneless")
    print total, first, last, name
    print

    print "HEAD"
    print r.head()
    print

    print "XHDR Date", "%d-%d" % (first, first + 10)
    print r.xhdr("Date", first, first + 10)
    print

    print "XZHDR Date", "%d-%d" % (first, first + 10)
    print r.xzhdr("Date", first, first + 10)
    print

    print "XOVER" , "%d-%d" % (first, first + 10)
    print r.xover(first, first + 10)
    print

    print "XZVER" , "%d-%d" % (first, first + 10)
    print r.xzver(first, first + 10)
    print
