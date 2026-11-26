"""The maplestory.io disk cache: bad replies and bad files never stick, failures are never cached (and pause
requests for a while), and the GUI-side item lookup never touches the network. ``httpx.get`` and the clock
are faked throughout: no real network, no real waiting."""

import json
import os
import threading
import time

import httpx
import pytest

from mctracker import gamedata


class FakeAPI:
    """Stands in for ``httpx.get``: answers by URL substring. An unrouted URL, or any URL while ``offline``,
    fails as a dropped connection would (a 404 is only ever an explicit route)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, str]] = {}
        self.offline, self.calls, self.timeouts = False, [], []

    def __call__(self, url, **kw):
        self.calls.append(url)
        self.timeouts.append(kw.get("timeout"))
        req = httpx.Request("GET", url)
        if not self.offline:
            for part, (status, body) in self.routes.items():
                if part in url:
                    return httpx.Response(status, text=body, request=req)
        raise httpx.ConnectError("offline", request=req)


class FakeClock:
    """gamedata's ``time``: a monotonic clock the test moves, the real wall clock, and no real sleeping."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def time(self):
        return time.time()

    def sleep(self, s):
        self.slept.append(s)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(gamedata, "time", c)
    return c


@pytest.fixture
def api(monkeypatch, tmp_path, clock):
    fake = FakeAPI()
    monkeypatch.setattr(gamedata, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(gamedata.httpx, "get", fake)

    def reset():
        gamedata.reset_backoff()
        gamedata.item_id.cache_clear()
        gamedata.mob.cache_clear()
        gamedata.map_mobs.cache_clear()
        gamedata.maps.cache_clear()

    reset()
    yield fake
    reset()


def _wait_out_backoff(clock):
    clock.now += gamedata.RETRY_S + 1


SNAIL = json.dumps({"name": "Snail", "meta": {"exp": 3, "level": 1}})
BLUE = json.dumps({"name": "Blue Snail", "meta": {"exp": 4}})
SILVER = json.dumps([{"id": 4010004, "name": "Silver Ore"}])


def _map(*mob_ids):
    return json.dumps({"name": "Snail Garden", "mobs": [{"id": i} for i in mob_ids]})


# --- bad replies, bad files ---------------------------------------------------------------------------


def test_corrupt_cache_file_is_a_miss_and_refetched(api, tmp_path):
    (tmp_path / "mob_2.json").write_text('{"name": "Sna', encoding="utf-8")  # half a file
    api.routes["/mob/2"] = (200, SNAIL)
    assert gamedata.mob(2).name == "Snail"
    assert json.loads((tmp_path / "mob_2.json").read_text(encoding="utf-8"))["name"] == "Snail"


def test_wrong_shape_cache_files_are_misses(api, tmp_path):
    (tmp_path / "itemsearch_silverore.json").write_text('{"error": "x"}', encoding="utf-8")
    (tmp_path / "mob_2.json").write_text('{"message": "rate limited"}', encoding="utf-8")
    api.routes["searchFor=Silver"] = (200, SILVER)
    api.routes["/mob/2"] = (200, SNAIL)
    assert gamedata.item_id("Silver Ore") == 4010004
    assert gamedata.mob(2).name == "Snail"


def test_non_json_200_is_not_cached_and_does_not_raise(api, tmp_path, clock):
    api.routes["/mob/2"] = (200, "<html>Cloudflare says hold on</html>")
    assert gamedata.mob(2) is None
    assert not (tmp_path / "mob_2.json").exists()
    api.routes["/mob/2"] = (200, SNAIL)
    _wait_out_backoff(clock)
    assert gamedata.mob(2).name == "Snail"  # the bad reply wasn't kept, in memory or on disk


def test_wrong_shape_mob_reply_is_not_a_mob(api, tmp_path, clock):
    api.routes["/mob/123"] = (200, '{"message": "rate limited"}')  # a JSON object, but not a monster
    assert gamedata.mob(123) is None
    assert not (tmp_path / "mob_123.json").exists()
    api.routes["/mob/123"] = (200, SNAIL)
    _wait_out_backoff(clock)
    assert gamedata.mob(123).exp == 3


def test_wrong_shape_search_reply_is_not_cached(api, tmp_path, clock):
    api.routes["searchFor="] = (200, '{"message": "rate limited"}')
    assert gamedata.item_id("Silver Ore") is None
    assert not list(tmp_path.glob("itemsearch_*.json"))
    api.routes["searchFor="] = (200, SILVER)
    _wait_out_backoff(clock)
    assert gamedata.item_id("Silver Ore") == 4010004


def test_map_data_non_json_raises_httperror_not_valueerror(api):
    api.routes["/map/100"] = (200, "oops")
    with pytest.raises(httpx.HTTPError):
        gamedata.map_mobs(100)


def test_empty_map_list_is_not_cached(api, clock):
    api.routes["/map"] = (200, "[]")
    with pytest.raises(httpx.HTTPError):
        gamedata.maps()
    api.routes["/map"] = (200, json.dumps([{"id": 1, "name": "Snail Garden", "streetName": "Maple Road"}]))
    _wait_out_backoff(clock)
    assert gamedata.maps()[0]["id"] == 1


# --- atomic writes --------------------------------------------------------------------------------------


def test_no_temp_files_left_behind(api, tmp_path):
    api.routes["/mob/2"] = (200, SNAIL)
    gamedata.mob(2)
    assert [f.name for f in tmp_path.iterdir()] == ["mob_2.json"]


def test_old_temp_files_are_cleaned_once(api, tmp_path, monkeypatch):
    old, new = tmp_path / "mob_9.json.1.2.tmp", tmp_path / "mob_8.json.1.2.tmp"
    old.write_text("{", encoding="utf-8")
    new.write_text("{", encoding="utf-8")  # maybe another process's write in progress: kept
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    monkeypatch.setattr(gamedata, "_cleaned", set())
    api.routes["/mob/2"] = (200, SNAIL)
    gamedata.mob(2)
    assert not old.exists() and new.exists()


def test_replace_retries_once_when_the_file_is_held(api, tmp_path, clock, monkeypatch):
    real, tries = os.replace, []

    def held_once(a, b):
        tries.append(1)
        if len(tries) == 1:
            raise PermissionError("held by an antivirus scan")
        real(a, b)

    monkeypatch.setattr(gamedata.os, "replace", held_once)
    api.routes["/mob/2"] = (200, SNAIL)
    assert gamedata.mob(2).name == "Snail"
    assert len(tries) == 2 and clock.slept
    assert [f.name for f in tmp_path.iterdir()] == ["mob_2.json"]


def test_replace_failing_costs_only_the_cache(api, tmp_path, monkeypatch, caplog):
    def held(a, b):
        raise PermissionError("held")

    monkeypatch.setattr(gamedata.os, "replace", held)
    api.routes["/mob/2"] = (200, SNAIL)
    with caplog.at_level("DEBUG", logger="mctracker.gamedata"):
        assert gamedata.mob(2).name == "Snail"  # the answer still comes back
    assert list(tmp_path.iterdir()) == []  # no cache file and no stray temp file
    assert "Couldn't cache mob_2.json" in caplog.text


# --- failures are never cached, and pause requests ----------------------------------------------------


def test_offline_item_lookup_recovers_once_the_pause_is_over(api, clock):
    api.offline = True
    assert gamedata.item_id("Silver Ore") is None
    api.offline = False
    api.routes["searchFor=Silver"] = (200, SILVER)
    asked = len(api.calls)
    assert gamedata.item_id("Silver Ore") is None  # within RETRY_S: not asked
    assert len(api.calls) == asked
    _wait_out_backoff(clock)
    assert gamedata.item_id("Silver Ore") == 4010004


def test_offline_mob_and_map_recover(api, clock):
    api.offline = True
    assert gamedata.mob(2) is None
    with pytest.raises(httpx.HTTPError):
        gamedata.map_mobs(100)
    api.offline = False
    api.routes["/mob/2"] = (200, SNAIL)
    api.routes["/map/100"] = (200, _map(2, 2))
    _wait_out_backoff(clock)
    assert gamedata.mob(2).name == "Snail"
    (snail,) = gamedata.map_mobs(100)
    assert (snail.name, snail.spawns) == ("Snail", 2)


def test_a_mob_failing_mid_map_gives_the_rest_and_is_not_cached(api, clock):
    api.routes["/map/100"] = (200, _map(2, 3))
    api.routes["/mob/2"] = (200, SNAIL)
    api.routes["/mob/3"] = (500, "broken")
    with pytest.raises(gamedata.PartialMap) as e:
        gamedata.map_mobs(100)
    assert [m.name for m in e.value.mobs] == ["Snail"]
    api.routes["/mob/3"] = (200, BLUE)
    _wait_out_backoff(clock)
    assert [m.name for m in gamedata.map_mobs(100)] == ["Snail", "Blue Snail"]


def test_not_found_is_cached_but_failure_is_not(api, clock):
    api.routes["/mob/999"] = (404, "Not Found")
    assert gamedata.mob(999) is None  # the API has no such monster
    asked = len(api.calls)
    assert gamedata.mob(999) is None
    assert len(api.calls) == asked  # remembered
    api.offline = True
    assert gamedata.mob(2) is None
    api.offline = False
    api.routes["/mob/2"] = (200, SNAIL)
    _wait_out_backoff(clock)
    assert gamedata.mob(2) is not None  # the failure wasn't


def test_a_dropped_connection_pauses_every_endpoint(api, clock):
    api.offline = True
    gamedata.mob(2)
    asked = len(api.calls)
    api.offline = False
    api.routes["/mob/3"] = (200, BLUE)
    api.routes["searchFor="] = (200, SILVER)
    assert gamedata.mob(3) is None and gamedata.item_id("Silver Ore") is None
    assert len(api.calls) == asked  # the API is down: nothing asked at all
    clock.now += gamedata.RETRY_S - 1
    assert gamedata.mob(3) is None and len(api.calls) == asked
    clock.now += 2  # the pause is over
    assert gamedata.mob(3).name == "Blue Snail"


def test_a_rate_limit_pauses_every_endpoint(api, clock):
    api.routes["/mob/2"] = (429, "slow down")
    api.routes["/mob/3"] = (200, BLUE)
    gamedata.mob(2)
    asked = len(api.calls)
    assert gamedata.mob(3) is None and len(api.calls) == asked


def test_one_broken_id_pauses_only_itself(api, clock):
    api.routes["/mob/7"] = (500, "broken")
    api.routes["/mob/3"] = (200, BLUE)
    assert gamedata.mob(7) is None
    asked = len(api.calls)
    assert gamedata.mob(3).name == "Blue Snail"  # the rest of the API still answers
    assert gamedata.mob(7) is None and len(api.calls) == asked + 1  # but 7 isn't asked again yet


def test_requests_use_a_short_timeout(api):
    api.routes["/mob/2"] = (200, SNAIL)
    gamedata.mob(2)
    assert api.timeouts == [gamedata.TIMEOUT_S] and gamedata.TIMEOUT_S <= 5


# --- item searches ----------------------------------------------------------------------------------------


def test_empty_search_is_not_persisted_but_remembered_this_run(api, tmp_path):
    api.routes["searchFor="] = (200, "[]")
    assert gamedata.item_id("Zorblax") is None
    assert not list(tmp_path.glob("itemsearch_*.json"))
    asked = len(api.calls)
    assert gamedata.item_id("Zorblax") is None
    assert len(api.calls) == asked


def test_search_404_is_no_hits_and_remembered(api):
    api.routes["searchFor="] = (404, "Not Found")
    assert gamedata.item_id("Zorblax") is None
    asked = len(api.calls)
    assert gamedata.item_id("Zorblax") is None
    assert len(api.calls) == asked  # not asked again every frame


def test_old_empty_search_file_is_asked_again(api, tmp_path):
    (tmp_path / "itemsearch_silverore.json").write_text("[]", encoding="utf-8")  # left by a failure
    (tmp_path / "itemsearch_silver.json").write_text("", encoding="utf-8")
    api.routes["searchFor=Silver"] = (200, SILVER)
    assert gamedata.item_id("Silver Ore") == 4010004
    assert json.loads((tmp_path / "itemsearch_silverore.json").read_text(encoding="utf-8")) == json.loads(SILVER)


def test_concurrent_item_lookups(api, monkeypatch):
    monkeypatch.setattr(gamedata, "ITEM_MEMO_MAX", 5)  # evicting while other threads read and write
    db = [{"id": 100 + i, "name": f"Thing{chr(65 + i)} Ore"} for i in range(12)]
    api.routes["searchFor="] = (200, json.dumps(db))
    names = [h["name"] for h in db]
    errors, results = [], {}

    def work(k):
        try:
            for _ in range(20):
                for n in names[k::3]:
                    results[n] = gamedata.item_id(n)
                    gamedata.known_item_id(n)
        except Exception as e:  # pragma: no cover - the failure being tested for
            errors.append(e)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert results == {h["name"]: h["id"] for h in db}
    assert len(gamedata._item_memo) <= 5


# --- the GUI-side lookup ----------------------------------------------------------------------------------


def test_known_item_id_does_no_network(api):
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


def test_known_item_id_tries_every_cached_term(api, tmp_path):
    # "Silvr Ore" (a misread): its own search and "Silvr" found nothing, so only "Ore" is on disk.
    (tmp_path / "itemsearch_ore.json").write_text(SILVER, encoding="utf-8")
    assert gamedata.known_item_id("Silvr Ore") == 4010004
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


# --- the capture thread's map lookup ------------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch):
    pytest.importorskip("PySide6")
    from mctracker.capture import CaptureWorker

    w = CaptureWorker("MapleStory")
    infos = []
    w.info.connect(infos.append)
    w.infos = infos
    garden = {"id": 100, "name": "Snail Garden", "streetName": "Maple Road"}
    monkeypatch.setattr(gamedata, "match_map", lambda street, name, clipped=False: (garden, "mapleroadsnailgarden"))
    return w


def test_lookup_map_keeps_the_map_when_some_mobs_fail_and_retries(worker, monkeypatch):
    from mctracker.kills import Mob

    snail, calls = Mob(2, "Snail", 3), []

    def partial(map_id):
        calls.append(map_id)
        raise gamedata.PartialMap("monster 3 unavailable", (snail,))

    monkeypatch.setattr(gamedata, "map_mobs", partial)
    m, place, mobs = worker._lookup_map("Maple Road", "Snail Garden", False)
    assert (m["id"], place, mobs) == (100, "mapleroadsnailgarden", (snail,))
    worker._lookup_map("Maple Road", "Snail Garden", False)
    assert len(calls) == 2  # not cached: asked again on the next read
    assert len(worker.infos) == 1 and "retrying" in worker.infos[0]  # said once, not every read
    monkeypatch.setattr(gamedata, "map_mobs", lambda map_id: calls.append(map_id) or (snail, snail))
    assert worker._lookup_map("Maple Road", "Snail Garden", False)[2] == (snail, snail)
    worker._lookup_map("Maple Road", "Snail Garden", False)
    assert len(calls) == 3  # complete now: cached for the unchanged read


def test_lookup_map_keeps_the_map_when_its_data_fails(worker, monkeypatch):
    def down(map_id):
        raise gamedata.Unavailable("offline")

    monkeypatch.setattr(gamedata, "map_mobs", down)
    m, place, mobs = worker._lookup_map("Maple Road", "Snail Garden", False)
    assert (m["id"], place, mobs) == (100, "mapleroadsnailgarden", ())
    assert worker._lookup_key is None


def test_no_map_list_is_reported_not_a_crash(worker, monkeypatch):
    def down(street, name, clipped=False):
        raise gamedata.Unavailable("offline")

    monkeypatch.setattr(gamedata, "match_map", down)
    assert worker._resolve_map("Maple Road", "Snail Garden") is None
    assert any("Map lookup failed" in i for i in worker.infos)
