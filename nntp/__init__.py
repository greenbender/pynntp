"""NNTP client library with streaming, compressed and yEnc responses."""

from .headerdict import HeaderDict
from .nntp import (
    BaseNNTPClient,
    NNTPClient,
    NNTPDataError,
    NNTPError,
    NNTPPermanentError,
    NNTPProtocolError,
    NNTPReplyError,
    NNTPSyncError,
    NNTPTemporaryError,
    NNTPYEncError,
)
from .types import Newsgroup, Range, SSLMode

__all__ = [
    "BaseNNTPClient",
    "HeaderDict",
    "NNTPClient",
    "NNTPDataError",
    "NNTPError",
    "NNTPPermanentError",
    "NNTPProtocolError",
    "NNTPReplyError",
    "NNTPSyncError",
    "NNTPTemporaryError",
    "NNTPYEncError",
    "Newsgroup",
    "Range",
    "SSLMode",
]
