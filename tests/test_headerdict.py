import copy
import pickle
from collections.abc import Iterable

import pytest

from nntp.headerdict import HeaderDict


def test_init_empty() -> None:
    header_dict = HeaderDict()
    assert header_dict == {}


def test_init_with_list() -> None:
    header_dict = HeaderDict([("key2", "value2")])
    assert header_dict == {"key2": "value2"}


def test_get_item() -> None:
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    assert header_dict["keylower"] == "value"
    assert header_dict["KeYlOwer"] == "value"
    assert header_dict["keymixed"] == "value1"
    assert header_dict["KeYMIXED"] == "value1"


def test_delete() -> None:
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    del header_dict["keyLOWER"]
    del header_dict["keymixed"]
    assert header_dict == {}


def test_iter() -> None:
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    assert list(header_dict) == ["keylower", "KeYMiXeD"]


def test_len() -> None:
    header_dict = HeaderDict()
    header_dict["key"] = "value"
    assert len(header_dict) == 1


def test_eq() -> None:
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    assert header_dict == {"keyLoWer": "value", "keymixed": "value1"}


def test_repr() -> None:
    header_dict = HeaderDict()
    header_dict["key"] = "value"
    assert repr(header_dict) == "HeaderDict([('key', 'value')])"


def test_init_mapping_and_kwargs() -> None:
    header_dict = HeaderDict({"A": "1"}, b="2")
    assert list(header_dict.items()) == [("A", "1"), ("b", "2")]


def test_init_duplicates_last_value_first_case() -> None:
    header_dict = HeaderDict([("Subject", "one"), ("SUBJECT", "two")])
    assert list(header_dict.items()) == [("Subject", "two")]


def test_overwrite_keeps_first_case() -> None:
    header_dict = HeaderDict(Subject="one")
    header_dict["SUBJECT"] = "two"
    assert list(header_dict) == ["Subject"]
    assert header_dict["subject"] == "two"


def test_iter_yields_plain_names() -> None:
    header_dict = HeaderDict(Subject="x")
    name = next(iter(header_dict))
    assert type(name) is str
    assert name == "Subject"


def test_contains() -> None:
    header_dict = HeaderDict(Subject="x")
    assert "SUBJECT" in header_dict
    assert "from" not in header_dict
    not_a_name: object = 1
    assert not_a_name not in header_dict


def test_missing_key() -> None:
    header_dict = HeaderDict(Subject="x")
    assert header_dict.get("From") is None
    with pytest.raises(KeyError):
        header_dict["From"]
    with pytest.raises(KeyError):
        del header_dict["From"]


def test_mutable_mapping_methods() -> None:
    header_dict = HeaderDict(Subject="x")
    header_dict.update({"SUBJECT": "y"}, FROM="me")
    assert header_dict.setdefault("from", "other") == "me"
    assert header_dict.pop("subject") == "y"
    assert list(header_dict.items()) == [("FROM", "me")]


def test_eq_ignores_order() -> None:
    a = HeaderDict([("A", "1"), ("B", "2")])
    assert a == HeaderDict([("b", "2"), ("a", "1")])
    assert a == {"B": "2", "A": "1"}


def test_eq_values_case_sensitive() -> None:
    assert HeaderDict(A="x") != {"A": "X"}


@pytest.mark.parametrize(
    "other", [[("A", "1")], "A", None, 1, {1: "1"}, {"A": "1", "B": "2"}]
)
def test_not_equal(other: object) -> None:
    assert HeaderDict(A="1") != other


def test_copy_module_is_independent() -> None:
    original = HeaderDict(Subject="x")
    dup = copy.copy(original)
    dup["SUBJECT"] = "y"
    dup["From"] = "me"
    assert original == {"Subject": "x"}
    assert dup == {"Subject": "y", "From": "me"}


def test_copy_method_is_independent() -> None:
    original = HeaderDict(Subject="x")
    dup = original.copy()
    assert type(dup) is HeaderDict
    del dup["subject"]
    original["From"] = "me"
    assert dup == {}
    assert original == {"Subject": "x", "From": "me"}


def test_copy_keeps_names_and_order() -> None:
    original = HeaderDict([("KeYMiXeD", "1"), ("Subject", "2")])
    for dup in (copy.copy(original), original.copy()):
        assert list(dup) == ["KeYMiXeD", "Subject"]
        assert dup["keymixed"] == "1"
        assert "SUBJECT" in dup
        dup["keymixed"] = "3"
        assert list(dup) == ["KeYMiXeD", "Subject"]
        assert original["KEYMIXED"] == "1"


def test_deepcopy_is_independent() -> None:
    original = HeaderDict(Subject="x")
    dup = copy.deepcopy(original)
    assert type(dup) is HeaderDict
    dup["SUBJECT"] = "y"
    assert original == {"Subject": "x"}
    assert dup == {"Subject": "y"}


def test_pickle_round_trip() -> None:
    original = HeaderDict([("KeYMiXeD", "1"), ("Subject", "2")])
    restored = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own data
    assert type(restored) is HeaderDict
    assert list(restored) == ["KeYMiXeD", "Subject"]
    assert restored["keymixed"] == "1"
    assert restored == original


class _Sub(HeaderDict):
    note = "class attribute"

    def __init__(self, other: Iterable[tuple[str, str]] = ()) -> None:
        super().__init__(other)
        self.tag = "instance attribute"


def test_copy_keeps_subclass_type() -> None:
    original = _Sub([("KeYMiXeD", "1"), ("Subject", "2")])
    for dup in (copy.copy(original), original.copy(), copy.deepcopy(original)):
        assert type(dup) is _Sub
        assert dup == original
        assert dup.tag == "instance attribute"
        assert list(dup) == ["KeYMiXeD", "Subject"]
        assert dup["keymixed"] == "1"
        assert "SUBJECT" in dup
        dup["keymixed"] = "3"
        dup["From"] = "me"
        del dup["subject"]
        assert list(dup) == ["KeYMiXeD", "From"]
        assert list(original) == ["KeYMiXeD", "Subject"]
        assert original["KEYMIXED"] == "1"
        assert original["subject"] == "2"


def test_pickle_round_trip_subclass() -> None:
    original = _Sub([("KeYMiXeD", "1")])
    restored = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own data
    assert type(restored) is _Sub
    assert list(restored) == ["KeYMiXeD"]
    assert restored == original


class _Slotted(HeaderDict):
    __slots__ = ("article_number", "extra")
    article_number: int
    extra: list[str]


def test_copy_keeps_slot_values() -> None:
    original = _Slotted([("Subject", "x")])
    original.article_number = 42
    original.extra = ["a"]
    for dup in (copy.copy(original), original.copy(), copy.deepcopy(original)):
        assert type(dup) is _Slotted
        assert dup.article_number == 42
        assert dup.extra == ["a"]
        assert dup == original
        dup.article_number = 43
        dup["Subject"] = "y"
        dup["From"] = "me"
        assert original.article_number == 42
        assert original == {"Subject": "x"}
        assert dup == {"Subject": "y", "From": "me"}


def test_deepcopy_slot_values_are_independent() -> None:
    original = _Slotted()
    original.extra = ["a"]
    dup = copy.deepcopy(original)
    dup.extra.append("b")
    assert original.extra == ["a"]


def test_copy_with_unset_slots() -> None:
    original = _Slotted([("Subject", "x")])
    for dup in (copy.copy(original), original.copy()):
        assert type(dup) is _Slotted
        assert dup == original
        assert not hasattr(dup, "article_number")
