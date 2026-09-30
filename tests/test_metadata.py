import copy

import pytest

from p4_history.codec import encode
from p4_history.errors import HistoryError
from p4_history.models import Snapshot, decode


@pytest.mark.parametrize("mutation", ["version", "duplicate", "traversal", "blob", "size", "extra"])
def test_invalid_snapshot_metadata_is_rejected(history, mutation):
    history.p4.add("file.txt")
    history.begin("123")
    value = history.store.manifest("123")
    if mutation == "version":
        value["format"] = 2
    elif mutation == "duplicate":
        value["files"].append(copy.deepcopy(value["files"][0]))
    elif mutation == "traversal":
        value["files"][0]["path"] = "../outside"
    elif mutation == "blob":
        value["files"][0]["blob"] = "not-an-object"
    elif mutation == "size":
        value["files"][0]["size"] = -1
    else:
        value["unknown"] = True
    with pytest.raises(HistoryError):
        decode(Snapshot, encode(value))


@pytest.mark.parametrize("data", [b"null", b"[1,2]", b"{}", b"{broken", b"\xff"])
def test_malformed_metadata_is_an_actionable_error(data):
    with pytest.raises(HistoryError, match="Invalid Snapshot"):
        decode(Snapshot, data)
