from nntp.headerdict import HeaderDict


def test_init_empty():
    header_dict = HeaderDict()
    assert header_dict == {}


def test_init_with_dict():
    header_dict = HeaderDict({"key1": "value1"})
    assert header_dict == {"key1": "value1"}


def test_init_with_list():
    header_dict = HeaderDict([("key2", "value2")])
    assert header_dict == {"key2": "value2"}


def test_init_with_kwargs():
    header_dict = HeaderDict(key="value")
    assert header_dict == {"key": "value"}


def test_get_item():
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    assert header_dict["keylower"] == "value"
    assert header_dict["KeYlOwer"] == "value"
    assert header_dict["keymixed"] == "value1"
    assert header_dict["KeYMIXED"] == "value1"


def test_delete():
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    del header_dict["keyLOWER"]
    del header_dict["keymixed"]
    assert header_dict == {}


def test_iter():
    header_dict = HeaderDict()
    header_dict["keylower"] = "value"
    header_dict["KeYMiXeD"] = "value1"
    assert list(header_dict) == ["keylower", "KeYMiXeD"]


def test_len():
    header_dict = HeaderDict()
    header_dict["key"] = "value"
    assert len(header_dict) == 1


def test_eq():
    header_dict = HeaderDict()
    header_dict["key"] = "value"
    assert header_dict == {"key": "value"}


def test_repr():
    header_dict = HeaderDict()
    header_dict["key"] = "value"
    assert repr(header_dict) == "HeaderDict([('key', 'value')])"
