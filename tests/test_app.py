import os

from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def test_app_renders_with_empty_queue():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Verify a resume", "Human-review queue"]
    assert at.button[0].label == "Run verification" and at.button[0].disabled  # nothing uploaded yet
    assert any("Nothing waiting" in i.value for i in at.info)
