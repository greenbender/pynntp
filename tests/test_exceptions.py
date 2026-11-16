"""Exceptions survive pickle and copy, e.g. when sent between processes."""

from __future__ import annotations

import copy
import pickle
from typing import TYPE_CHECKING

import pytest

import nntp

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: nntp.NNTPError("boom"), id="NNTPError"),
        pytest.param(lambda: nntp.NNTPSyncError("busy"), id="NNTPSyncError"),
        pytest.param(
            lambda: nntp.NNTPReplyError(440, "no posting"), id="NNTPReplyError"
        ),
        pytest.param(
            lambda: nntp.NNTPTemporaryError(400, "try later"),
            id="NNTPTemporaryError",
        ),
        pytest.param(
            lambda: nntp.NNTPPermanentError(502, "no"), id="NNTPPermanentError"
        ),
        pytest.param(
            lambda: nntp.NNTPProtocolError("bad status"), id="NNTPProtocolError"
        ),
        pytest.param(lambda: nntp.NNTPDataError("bad data"), id="NNTPDataError"),
        pytest.param(
            lambda: nntp.NNTPYEncError("Bad yEnc data: crc", b"\x00\x01\xff"),
            id="NNTPYEncError",
        ),
    ],
)
def test_error_round_trips(make: Callable[[], Exception]) -> None:
    err = make()
    err.add_note("note")
    vars(err)["extra"] = "kept"
    pickled = pickle.loads(pickle.dumps(err))  # noqa: S301 - own output
    for clone in (pickled, copy.copy(err), copy.deepcopy(err)):
        assert type(clone) is type(err)
        assert str(clone) == str(err)
        assert clone.args == err.args
        assert vars(clone) == vars(err)
        assert vars(clone)["extra"] == "kept"
        assert vars(clone)["__notes__"] == ["note"]


def test_yenc_error_keeps_data_after_pickle() -> None:
    err = nntp.NNTPYEncError("Bad yEnc data: crc", b"\x00\x01\xff")
    clone = pickle.loads(pickle.dumps(err))  # noqa: S301 - own output
    assert clone.data == b"\x00\x01\xff"
    assert str(clone) == "Bad yEnc data: crc"
    assert isinstance(clone, nntp.NNTPDataError)
