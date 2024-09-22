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
