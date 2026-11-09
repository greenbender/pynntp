"""NNTP client library with streaming, compressed and yEnc responses."""

from .headerdict import HeaderDict
from .nntp import (
    BaseNNTPClient,
    FromURLOptions,
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
    "FromURLOptions",
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
