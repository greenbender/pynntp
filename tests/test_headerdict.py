import pytest

from nntp.headerdict import HeaderDict


def test_init_empty() -> None:
    header_dict = HeaderDict()
    assert header_dict == {}


def test_init_with_dict() -> None:
    header_dict = HeaderDict({"Key1": "value1"})
    assert header_dict == {"Key1": "value1"}


def test_init_with_list() -> None:
    header_dict = HeaderDict([("key2", "value2")])
    assert header_dict == {"key2": "value2"}


def test_init_with_kwargs() -> None:
    header_dict = HeaderDict(key="value")
    assert header_dict == {"key": "value"}


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
