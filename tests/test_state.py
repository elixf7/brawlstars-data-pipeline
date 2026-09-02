"""Working state has to survive an ephemeral runner, or a bounded crawl is
pointless: the next run would restart from seeds every time."""
import pytest

from bsetl.publish.hub import PublishError
from bsetl.publish.state import STATE_PREFIX, pull_state, push_state, state_path


def test_state_lives_outside_the_published_data_prefix():
    """The dataset config globs data/**; state must not be loaded as data."""
    p = state_path("season50")
    assert p == "state/season50.db"
    assert not p.startswith("data/")
    assert STATE_PREFIX == "state"


def test_pushing_a_missing_database_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    with pytest.raises(PublishError, match="No database to store"):
        push_state("me/ds", "season50", str(tmp_path / "absent.db"))


def test_first_run_of_a_season_has_no_state_and_that_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    from huggingface_hub.errors import EntryNotFoundError

    def missing(**kwargs):
        raise EntryNotFoundError("nothing here")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", missing)
    assert pull_state("me/ds", "season50", str(tmp_path / "out.db")) is False
    assert not (tmp_path / "out.db").exists()


def test_pulled_state_is_copied_out_of_the_cache(tmp_path, monkeypatch):
    """The crawl writes to this file; mutating a cached blob in place would
    corrupt the cache for every later download."""
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    cached = tmp_path / "cache" / "blob.db"
    cached.parent.mkdir()
    cached.write_bytes(b"SQLite format 3\x00payload")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", lambda **kw: str(cached))

    dest = tmp_path / "work" / "season.db"
    assert pull_state("me/ds", "season50", str(dest)) is True
    assert dest.read_bytes() == cached.read_bytes()
    assert dest.resolve() != cached.resolve()

    dest.write_bytes(b"mutated by the crawl")
    assert cached.read_bytes() == b"SQLite format 3\x00payload"


def test_push_uploads_to_the_state_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    db = tmp_path / "s.db"
    db.write_bytes(b"x" * 32)
    seen = {}

    class FakeApi:
        def __init__(self, token=None):
            seen["token"] = token

        def create_repo(self, **kw):
            seen["create"] = kw

        def upload_file(self, **kw):
            seen["upload"] = kw

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    assert push_state("me/ds", "season50", str(db)) == "state/season50.db"
    assert seen["upload"]["path_in_repo"] == "state/season50.db"
    assert seen["upload"]["repo_type"] == "dataset"
    assert seen["create"]["exist_ok"] is True
