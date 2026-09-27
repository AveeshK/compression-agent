import pytest

from compression_agent.cli import parse_selection


def test_parse_selection():
    assert parse_selection("1,3,5-7", 8) == [0, 2, 4, 5, 6]
    assert parse_selection(" 2 , 2,1 ", 3) == [0, 1]
    assert parse_selection("all", 3) == [0, 1, 2]


@pytest.mark.parametrize("text", ["", "0", "4", "3-1", "x", "1-9"])
def test_parse_selection_rejects(text):
    with pytest.raises(ValueError):
        parse_selection(text, 3)
