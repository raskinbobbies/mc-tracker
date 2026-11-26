"""The maplestory.io disk cache: bad replies and bad files never stick, failures are never cached, and the
GUI-side item lookup never touches the network. ``httpx.get`` is faked throughout: no real network."""

import json

import httpx
import pytest

from mctracker import gamedata


class FakeAPI:
    """Stands in for ``httpx.get``: answers by URL substring, or fails every request while ``offline``."""

    def __init__(self, routes: dict[str, tuple[int, str]]):
        self.routes, self.offline, self.calls = routes, False, []

    def __call__(self, url, **kw):
        self.calls.append(url)
        req = httpx.Request("GET", url)
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        for part, (status, body) in self.routes.items():
            if part in url:
                return httpx.Response(status, text=body, request=req)
        return httpx.Response(404, text="Not Found", request=req)


@pytest.fixture
def api(monkeypatch, tmp_path):
    fake = FakeAPI({})
    monkeypatch.setattr(gamedata, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(gamedata.httpx, "get", fake)
    gamedata._fail_until.clear()
    gamedata.item_id.cache_clear()
    gamedata.mob.cache_clear()
    gamedata.map_mobs.cache_clear()
    yield fake
    gamedata._fail_until.clear()
    gamedata.item_id.cache_clear()
    gamedata.mob.cache_clear()
    gamedata.map_mobs.cache_clear()


def _back_online(api):
    api.offline = False
    gamedata._fail_until.clear()  # the retry pause has passed


SNAIL = json.dumps({"name": "Snail", "meta": {"exp": 3, "level": 1}})
SILVER = json.dumps([{"id": 4010004, "name": "Silver Ore"}])


def test_corrupt_cache_file_is_a_miss_and_refetched(api, tmp_path):
    (tmp_path / "mob_2.json").write_text('{"name": "Sna', encoding="utf-8")  # half a file
    api.routes["/mob/2"] = (200, SNAIL)
    assert gamedata.mob(2).name == "Snail"
    assert json.loads((tmp_path / "mob_2.json").read_text(encoding="utf-8"))["name"] == "Snail"


def test_wrong_shape_cache_file_is_a_miss(api, tmp_path):
    (tmp_path / "itemsearch_silverore.json").write_text('{"error": "x"}', encoding="utf-8")
    api.routes["searchFor=Silver"] = (200, SILVER)
    assert gamedata.item_id("Silver Ore") == 4010004


def test_non_json_200_is_not_cached_and_does_not_raise(api, tmp_path):
    api.routes["/mob/2"] = (200, "<html>Cloudflare says hold on</html>")
    assert gamedata.mob(2) is None
    assert not (tmp_path / "mob_2.json").exists()
    api.routes["/mob/2"] = (200, SNAIL)
    _back_online(api)
    assert gamedata.mob(2).name == "Snail"  # the bad reply wasn't kept, in memory or on disk


def test_wrong_shape_200_is_not_cached(api, tmp_path):
    api.routes["searchFor=Silver"] = (200, '{"message": "rate limited"}')
    assert gamedata.item_id("Silver Ore") is None
    assert not list(tmp_path.glob("itemsearch_*.json"))
    api.routes["searchFor=Silver"] = (200, SILVER)
    _back_online(api)
    assert gamedata.item_id("Silver Ore") == 4010004


def test_map_data_non_json_raises_httperror_not_valueerror(api):
    api.routes["/map/100"] = (200, "oops")
    with pytest.raises(httpx.HTTPError):
        gamedata.map_mobs(100)


def test_no_temp_files_left_behind(api, tmp_path):
    api.routes["/mob/2"] = (200, SNAIL)
    gamedata.mob(2)
    assert [f.name for f in tmp_path.iterdir()] == ["mob_2.json"]


def test_offline_item_lookup_recovers_when_back_online(api):
    api.offline = True
    assert gamedata.item_id("Silver Ore") is None
    api.routes["searchFor=Silver"] = (200, SILVER)
    _back_online(api)
    assert gamedata.item_id("Silver Ore") == 4010004


def test_offline_mob_and_map_recover_when_back_online(api):
    api.offline = True
    assert gamedata.mob(2) is None
    with pytest.raises(httpx.HTTPError):
        gamedata.map_mobs(100)
    api.routes["/mob/2"] = (200, SNAIL)
    api.routes["/map/100"] = (200, json.dumps({"mobs": [{"id": 2}, {"id": 2}]}))
    _back_online(api)
    assert gamedata.mob(2).name == "Snail"
    (snail,) = gamedata.map_mobs(100)
    assert (snail.name, snail.spawns) == ("Snail", 2)


def test_a_mob_failing_mid_map_does_not_cache_a_short_list(api):
    api.routes["/map/100"] = (200, json.dumps({"mobs": [{"id": 2}, {"id": 3}]}))
    api.routes["/mob/2"] = (200, SNAIL)
    api.routes["/mob/3"] = (503, "busy")
    with pytest.raises(httpx.HTTPError):
        gamedata.map_mobs(100)
    api.routes["/mob/3"] = (200, json.dumps({"name": "Blue Snail", "meta": {"exp": 4}}))
    _back_online(api)
    assert [m.name for m in gamedata.map_mobs(100)] == ["Snail", "Blue Snail"]


def test_not_found_is_cached_but_failure_is_not(api):
    assert gamedata.mob(999) is None  # 404: the API has no such monster
    asked = len(api.calls)
    assert gamedata.mob(999) is None
    assert len(api.calls) == asked  # remembered
    api.offline = True
    assert gamedata.mob(2) is None
    _back_online(api)
    api.routes["/mob/2"] = (200, SNAIL)
    assert gamedata.mob(2) is not None  # the failure wasn't


def test_failed_request_is_not_retried_at_once(api):
    api.offline = True
    gamedata.mob(2)
    asked = len(api.calls)
    gamedata.mob(2)
    assert len(api.calls) == asked  # within RETRY_S: no request


def test_empty_search_is_not_persisted(api, tmp_path):
    api.routes["searchFor="] = (200, "[]")
    assert gamedata.item_id("Zorblax") is None
    assert not list(tmp_path.glob("itemsearch_*.json"))


def test_old_empty_search_file_is_asked_again(api, tmp_path):
    (tmp_path / "itemsearch_silverore.json").write_text("[]", encoding="utf-8")  # left by a failure
    (tmp_path / "itemsearch_silver.json").write_text("", encoding="utf-8")
    api.routes["searchFor=Silver"] = (200, SILVER)
    assert gamedata.item_id("Silver Ore") == 4010004
    assert json.loads((tmp_path / "itemsearch_silverore.json").read_text(encoding="utf-8")) == json.loads(SILVER)


def test_known_item_id_does_no_network(api, tmp_path):
    api.routes["searchFor="] = (200, SILVER)
    assert gamedata.known_item_id("Silver Ore") is None  # nothing cached yet
    assert gamedata.known_item_id("Zorblax") is None
    assert api.calls == []
    assert gamedata.item_id("Silver Ore") == 4010004  # the capture thread warms it
    api.calls.clear()
    assert gamedata.known_item_id("Silver Ore") == 4010004
    gamedata.item_id.cache_clear()  # a fresh run: only the disk cache
    assert gamedata.known_item_id("Silver Ore") == 4010004
    assert api.calls == []


def test_known_item_id_does_no_network_when_offline(api):
    api.offline = True
    assert gamedata.known_item_id("Silver Ore") is None
    assert api.calls == []


def test_capture_warms_items_from_joined_rows(api):
    """The capture thread warms the rows the tracker parses: a loot line OCR split in two is warmed as the
    whole name, which is what the session's cache-only resolver then asks for."""
    from mctracker.capture import warm_items

    class L:
        def __init__(self, text, x0):
            self.text, self.y, self.height, self.x0, self.colour = text, 100.0, 12.0, x0, None

    api.routes["searchFor=Silver"] = (200, SILVER)
    warm_items([L("Silver", 0), L("Ore x1 earned. (Etc)", 60)])
    api.calls.clear()
    assert gamedata.known_item_id("Silver Ore") == 4010004
    assert api.calls == []
