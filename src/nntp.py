#!/usr/bin/python
"""
An NNTP library - a bit more useful than the lib nntp one (hopefully).
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
import iodict
import stringbuffer
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

class Reader(object):

    def __init__(self, host, port=119, username="anonymous", password="anonymous", timeout=30, tls=False):

        self.socket = socket.socket()
        self.socket.settimeout(timeout)
        if tls:
            self.socket = ssl.wrap_socket(self.socket)

        self.__buffer = stringbuffer.StringBuffer()

        self.username = username
        self.password = password

        # connect
        self.socket.connect((host, port))
        code, message = self.__status()
        if not code in [200, 201]:
            raise NNTPReplyError(code, message)

        # reader
        code, message = self.__command("MODE READER")
        if not code in [200, 201]:
            raise NNTPReplyError(code, message)

    def __line_gen(self):

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

        for line in self.__line_gen():
            pass

    def __status(self):

        line = next(self.__line_gen()).rstrip()
        parts = line.split(None, 1)

        try:
            code, message = int(parts[0]), ""
        except ValueError:
            raise NNTPProtocolError(line)

        if len(parts) > 1:
            message = parts[1]

        if 400 <= code <= 499:
            raise NNTPTemporaryError(code, message)

        if 500 <= code <= 599:
            raise NNTPPermanentError(code, message)

        return code, message

    def __info_gen(self):

        return self.__line_gen()

    def __info(self):

        return "".join([x for x in self.__info_gen()])
    
    def __info_compressed_gen(self):

        escape = 0
        dcrc32 = 0
        inflate = zlib.decompressobj(-15)

        # header
        header = next(self.__line_gen())
        if not header.startswith("=ybegin"):
            self.__drain()
            raise NNTPDataError("Bad yEnc header")

        # data
        buf, trailer = stringbuffer.StringBuffer(), ""
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

        return "".join([x for x in self.__info_compressed_gen()])

    def __command(self, verb, args=None):

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

    def help(self):

        code, message = self.__command("HELP")
        if code != 100:
            raise NNTPReplyError(code, message)
        
        return self.__info()

    def capabilities(self, keyword=None):

        args = keyword

        code, message = self.__command("CAPABILITIES", args)
        if code != 101:
            raise NNTPReplyError(code, message)

        return self.__info()

    def quit(self):

        code, message = self.__command("QUIT")
        if code != 205:
            raise NNTPReplyError(code, message)

    def group(self, name):

        args = name

        code, message = self.__command("GROUP", args)
        if code != 211:
            raise NNTPReplyError(code, message)

        parts = message.split(None, 4)
        if len(parts) < 4:
            raise NNTPDataError("Invalid GROUP status '%s'" % message)
        
        try:
            total = int(parts[0])
            first = int(parts[1])
            last  = int(parts[2])
        except ValueError:
            raise NNTPDataError("Invalid GROUP status '%s'" % message)
        group = parts[3]

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

    def list_gen(self):

        code, message = self.__command("LIST")
        if code != 215:
            raise NNTPReplyError(code, message)
        
        for line in self.__info_gen():
            parts = line.rstrip().split()
            if len(parts) != 4:
                raise NNTPDataError("Invalid LIST info")
            try:
                group = parts[0]
                first = int(parts[1])
                last  = int(parts[2])
                post  = {"y": True, "n": False, "m": None}[parts[3]]
            except (ValueError, KeyError):
                raise NNTPDataError("Invalid LIST info")
            yield group, first, last, post

    def list(self):

        return [x for x in self.list_gen()]

    def list_newsgroups_gen(self, pattern=None):

        args = pattern

        code, message = self.__command("LIST NEWSGROUPS", args)
        if code != 215:
            raise NNTPReplyError(code, message)

        return self.__info_gen()
    
    def list_newsgroups(self, pattern=None):

        return "".join([x for x in self.list_newsgroups_gen(pattern)])

    def list_overview_fmt(self):

        if hasattr(self, "__overview_fmt"):
            return self.__overview_fmt

        code, message = self.__command("LIST OVERVIEW.FMT")
        if code != 215:
            raise NNTPReplyError(code, message)

        self.__overview_fmt = []
        for line in self.__info_gen():
            parts = line.rstrip().split(":")
            if len(parts) != 2:
                raise NNTPDataError("Overview format missing colon")
            name, suffix = parts
            if suffix and not name:
                name, suffix = suffix, name
            if suffix and suffix != "full":
                raise NNTPDataError("Invalid overview format suffix")
            self.__overview_fmt.append((name, suffix == "full"))

        return self.__overview_fmt

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

        args = None
        if first is not None:
            args = str(first) + "-"
            if last is not None:
                args += str(last)

        code, message = self.__command("XOVER", args)
        if code != 224:
            raise NNTPReplyError(code, message)

        for line in self.__info_gen():
            yield line.split("\t")

    def xover(self, first=None, last=None):

        return [x for x in self.xover_gen(first, last)]

    def xzver_gen(self, first=None, last=None):

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
        host = sys.argv[0]
        port = int(sys.argv[1])
        usename = sys.argv[2]
        password = sys.argv[3]
        tls = int(sys.argv[4])
    except:
        print "%s <host> <port> <username> <password> <ssl(0|1)>"
        sys.exit(1)

    r = Reader(host, port, username, password, tls)

    print "HELP"
    print r.help()
    print

    print "LIST OVERVIEW.FMT"
    print r.list_overview_fmt()
    print

    #print "CAPABILITIES"
    #print r.capabilities()
    #print

    print "GROUP alt.binaries.boneless"
    total, first, last, name = r.group("alt.binaries.boneless")
    print total, first, last, name
    print

#    print "HEAD"
#    print len(r.head())
#    print

    print "LIST"
    print len(r.list())
    print

#    print "LIST NEWSGROUPS"
#    print len(r.list_newsgroups())
#    print
#
#    print "XHDR Date", "%d-%d" % (first, first + 100)
#    print len(r.xhdr("Date", first, first + 100))
#    print
#
#    print "XZHDR Date", "%d-%d" % (first, first + 100)
#    print len(r.xzhdr("Date", first, first + 100))
#    print

    print "XOVER" , "%d-%d" % (first, first + 10000)
    print len(r.xover(first, first + 10000))
    print

    print "XZVER" , "%d-%d" % (first, first + 10000)
    print len(r.xzver(first, first + 10000))
    print
