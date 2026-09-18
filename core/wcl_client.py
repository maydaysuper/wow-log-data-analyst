from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
GRAPHQL_URL = "https://www.warcraftlogs.com/api/v2/client"


def extract_report_code(value: str) -> str:
    value = value.strip()
    m = re.search(r"/reports/([A-Za-z0-9]+)", value)
    return m.group(1) if m else value


class WCLClient:
    """Small, cache-friendly Warcraft Logs v2 client.

    The desktop app uses the public client-credentials endpoint. User/private reports
    should only be added later through an explicit user OAuth flow.
    """

    def __init__(self, client_id: str, client_secret: str, timeout: int = 30):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        # Compatibility mirrors kept for older tests/debugging; authoritative OAuth
        # state lives in _auth_state so forked workers see refreshes immediately.
        self._token = None
        self._expires_at = 0.0
        self._auth_lock = threading.RLock()
        self._auth_state: dict[str, Any] = {"token": None, "expires_at": 0.0}

        self._session = requests.Session()
        self._session.headers.update({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
        retry = Retry(
            total=3, connect=3, read=3, status=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._cache_lock = threading.RLock()
        self._short_cache: dict[str, tuple[float, Any]] = {}
        # Single-flight prevents two concurrent workers from spending WCL points on
        # the same expensive report/event query after a simultaneous cache miss.
        self._inflight_lock = threading.RLock()
        self._inflight: dict[str, threading.Event] = {}
        self._stats_lock = threading.RLock()
        self._stats: dict[str, int] = {
            "network_queries": 0,
            "token_refreshes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "singleflight_waits": 0,
            "auth_retries": 0,
        }

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def fork(self) -> "WCLClient":
        """Create a parallel HTTP worker sharing auth/cache/single-flight state.

        The requests.Session itself remains per-worker because it owns connection-pool
        state, while OAuth credentials, short-lived payload cache and in-flight gates
        are shared. This avoids token-refresh storms and duplicate event downloads.
        """
        child = WCLClient(self.client_id, self.client_secret, timeout=self.timeout)
        with self._auth_lock:
            # Honor legacy direct assignment used by older callers/tests.
            if self._token and not self._auth_state.get("token"):
                self._auth_state["token"] = self._token
                self._auth_state["expires_at"] = float(self._expires_at or 0.0)
            child._auth_lock = self._auth_lock
            child._auth_state = self._auth_state
            child._token = self._auth_state.get("token")
            child._expires_at = float(self._auth_state.get("expires_at") or 0.0)
        child._cache_lock = self._cache_lock
        child._short_cache = self._short_cache
        child._inflight_lock = self._inflight_lock
        child._inflight = self._inflight
        child._stats_lock = self._stats_lock
        child._stats = self._stats
        return child

    def _inc_stat(self, key: str, amount: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = int(self._stats.get(key, 0)) + int(amount)

    def diagnostics(self) -> dict[str, Any]:
        """Return lightweight shared-client diagnostics for UI/support logs.

        Never hold more than one internal lock at a time. Network workers can
        update counters while they populate cache/single-flight state, so nested
        locks here would create an avoidable lock-order deadlock risk.
        """
        cache_entries = 0
        inflight_queries = 0
        if hasattr(self, "_cache_lock") and hasattr(self, "_short_cache"):
            with self._cache_lock:
                cache_entries = len(self._short_cache)
        if hasattr(self, "_inflight_lock") and hasattr(self, "_inflight"):
            with self._inflight_lock:
                inflight_queries = len(self._inflight)
        with self._stats_lock:
            out = dict(self._stats)
        out["cache_entries"] = cache_entries
        out["inflight_queries"] = inflight_queries
        return out

    def _cache_key(self, label: str, payload: dict[str, Any]) -> str:
        return label + ":" + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _cache_get(self, key: str) -> Any | None:
        value = None
        hit = False
        with self._cache_lock:
            item = self._short_cache.get(key)
            if item:
                expires, candidate = item
                if time.time() >= expires:
                    self._short_cache.pop(key, None)
                else:
                    value = candidate
                    hit = True
        # Keep statistics outside the cache lock. This matches diagnostics() and
        # prevents cache-lock -> stats-lock inversion under concurrent workers.
        self._inc_stat("cache_hits" if hit else "cache_misses")
        return value

    def _cache_put(self, key: str, value: Any, ttl: int) -> Any:
        with self._cache_lock:
            # Bound memory usage; event payloads can be large. Expiry time is a useful
            # eviction proxy here because entries use fixed short TTLs per data class.
            if len(self._short_cache) >= 48:
                oldest = min(self._short_cache.items(), key=lambda kv: kv[1][0])[0]
                self._short_cache.pop(oldest, None)
            self._short_cache[key] = (time.time() + max(1, int(ttl)), value)
        return value

    def _cached_load(self, key: str, ttl: int, loader) -> Any:
        """Return cached value or let exactly one worker perform the network load.

        Lightweight test doubles and third-party subclasses from older releases may
        intentionally skip ``WCLClient.__init__``. In that case preserve the old
        uncached behavior instead of failing on missing synchronization attributes.
        """
        required = ("_cache_lock", "_short_cache", "_inflight_lock", "_inflight")
        if any(not hasattr(self, attr) for attr in required):
            return loader()
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        owner = False
        waited = False
        with self._inflight_lock:
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                owner = True
            else:
                waited = True
        if waited:
            self._inc_stat("singleflight_waits")

        if not owner:
            # Event payload queries can legitimately take longer than the HTTP read
            # timeout because they paginate. Wait generously; if the owner fails, the
            # waiter retries through the same gate instead of stampeding the API.
            event.wait(timeout=max(60.0, float(self.timeout) * 4.0))
            cached = self._cache_get(key)
            if cached is not None:
                return cached
            return self._cached_load(key, ttl, loader)

        try:
            return self._cache_put(key, loader(), ttl)
        finally:
            with self._inflight_lock:
                done = self._inflight.pop(key, None)
                if done is not None:
                    done.set()

    def _sync_legacy_auth_into_shared(self) -> None:
        # Older tests/integrations may assign _token/_expires_at directly.
        if self._token and (
            not self._auth_state.get("token")
            or float(self._expires_at or 0.0) > float(self._auth_state.get("expires_at") or 0.0)
        ):
            self._auth_state["token"] = self._token
            self._auth_state["expires_at"] = float(self._expires_at or 0.0)

    def _invalidate_token(self) -> None:
        with self._auth_lock:
            self._auth_state["token"] = None
            self._auth_state["expires_at"] = 0.0
            self._token = None
            self._expires_at = 0.0

    def _get_token(self, force_refresh: bool = False) -> str:
        # Serializing refresh is intentional: all forks share the same client-credential
        # token, and a refresh storm wastes requests without improving throughput.
        with self._auth_lock:
            self._sync_legacy_auth_into_shared()
            token = self._auth_state.get("token")
            expires_at = float(self._auth_state.get("expires_at") or 0.0)
            if not force_refresh and token and time.time() < expires_at - 30:
                self._token = token
                self._expires_at = expires_at
                return str(token)

            r = self._session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                timeout=(8, self.timeout),
            )
            r.raise_for_status()
            data = r.json()
            token = data["access_token"]
            expires_at = time.time() + int(data.get("expires_in", 3600))
            self._auth_state["token"] = token
            self._auth_state["expires_at"] = expires_at
            self._token = token
            self._expires_at = expires_at
            self._inc_stat("token_refreshes")
            return str(token)

    def query(self, query: str, variables: dict | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}

        def send(token: str):
            self._inc_stat("network_queries")
            return self._session.post(
                GRAPHQL_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(8, self.timeout),
            )

        token = self._get_token()
        r = send(token)
        # Retry auth exactly once outside urllib3's status retry policy. This handles
        # revoked/expired tokens and clock skew without replaying every non-auth error.
        if r.status_code == 401:
            self._inc_stat("auth_retries")
            # Several forked workers can receive a 401 for the same stale token at
            # nearly the same time. Serialize the compare-and-refresh step: once one
            # worker has refreshed, later workers reuse that newer token instead of
            # invalidating it and causing a refresh storm.
            with self._auth_lock:
                current = self._auth_state.get("token")
                expires_at = float(self._auth_state.get("expires_at") or 0.0)
                if current and current != token and time.time() < expires_at - 5:
                    retry_token = str(current)
                    self._token = current
                    self._expires_at = expires_at
                else:
                    self._auth_state["token"] = None
                    self._auth_state["expires_at"] = 0.0
                    self._token = None
                    self._expires_at = 0.0
                    retry_token = self._get_token(force_refresh=True)
            r = send(retry_token)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        result = data.get("data")
        if not isinstance(result, dict):
            raise RuntimeError("WCL GraphQL response missing data object")
        return result

    def rate_limit(self) -> dict[str, Any]:
        q = """
        query {
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self.query(q).get("rateLimitData") or {}

    def report_summary(self, report_code: str) -> dict:
        code = extract_report_code(report_code)
        key = self._cache_key("report_summary", {"code": code})
        q = """
        query($code: String!) {
          reportData {
            report(code: $code) {
              code title startTime endTime visibility revision
              zone { id name frozen }
              fights(translate: true) {
                id name startTime endTime kill difficulty encounterID
                keystoneLevel keystoneTime keystoneBonus keystoneAffixes
                friendlyPlayers friendlySpecs
              }
              masterData(translate: true) {
                logVersion gameVersion lang
                actors { id name type subType gameID petOwner server }
                abilities { gameID name type icon }
              }
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self._cached_load(key, 180, lambda: self.query(q, {"code": code}))

    def report_learning_summary(self, report_code: str) -> dict:
        """Report metadata plus WCL-native dungeon pulls for offline learning."""
        code = extract_report_code(report_code)
        key = self._cache_key("report_learning_summary", {"code": code})
        q = """
        query($code: String!) {
          reportData {
            report(code: $code) {
              code title startTime endTime visibility revision
              zone { id name frozen }
              fights(translate: true) {
                id name startTime endTime kill difficulty encounterID
                averageItemLevel countReached countRequired
                keystoneLevel keystoneTime keystoneBonus keystoneAffixes
                friendlyPlayers friendlySpecs
                dungeonPulls {
                  id name startTime endTime kill encounterID x y
                  enemyNPCs { id gameID minimumInstanceID maximumInstanceID minimumInstanceGroupID maximumInstanceGroupID }
                  maps { id }
                }
              }
              masterData(translate: true) {
                logVersion gameVersion lang
                actors { id name type subType gameID petOwner server }
                abilities { gameID name type icon }
              }
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self._cached_load(key, 600, lambda: self.query(q, {"code": code}))

    def report_player_details(self, report_code: str, fight_id: int, include_combatant_info: bool = True) -> dict[str, Any]:
        code = extract_report_code(report_code)
        key = self._cache_key("report_player_details", {"code": code, "fight": int(fight_id), "combatant": bool(include_combatant_info)})
        q = """
        query($code: String!, $fight: Int!, $combatant: Boolean!) {
          reportData {
            report(code: $code) {
              playerDetails(fightIDs: [$fight], translate: true, includeCombatantInfo: $combatant)
            }
          }
        }
        """
        variables = {"code": code, "fight": int(fight_id), "combatant": bool(include_combatant_info)}
        return self._cached_load(key, 180, lambda: self.query(q, variables))

    def report_table(
        self,
        report_code: str,
        fight_id: int,
        data_type: str = "DamageDone",
        source_id: int | None = None,
        target_id: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> dict:
        code = extract_report_code(report_code)
        allowed = {
            "Summary", "DamageDone", "DamageTaken", "Healing", "Casts", "Deaths",
            "Interrupts", "Buffs", "Debuffs", "Resources", "Threat", "Dispels",
            "Summons", "Survivability",
        }
        if data_type not in allowed:
            raise ValueError(f"Unsupported WCL table type: {data_type}")
        q = f"""
        query($code: String!, $fight: Int!, $source: Int, $target: Int, $start: Float, $end: Float) {{
          reportData {{
            report(code: $code) {{
              table(
                dataType: {data_type}, fightIDs: [$fight], sourceID: $source, targetID: $target,
                startTime: $start, endTime: $end, translate: true
              )
            }}
          }}
        }}
        """
        return self.query(q, {
            "code": code,
            "fight": int(fight_id),
            "source": int(source_id) if source_id is not None else None,
            "target": int(target_id) if target_id is not None else None,
            "start": float(start_time) if start_time is not None else None,
            "end": float(end_time) if end_time is not None else None,
        })

    def report_events_page(
        self,
        report_code: str,
        fight_id: int,
        data_type: str = "Casts",
        source_id: int | None = None,
        target_id: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        include_resources: bool = False,
        limit: int = 10000,
    ) -> dict[str, Any]:
        code = extract_report_code(report_code)
        allowed = {
            "All", "Buffs", "Casts", "CombatantInfo", "DamageDone", "DamageTaken",
            "Deaths", "Debuffs", "Dispels", "Healing", "Interrupts", "Resources",
            "Summons", "Threat",
        }
        if data_type not in allowed:
            raise ValueError(f"Unsupported WCL event type: {data_type}")
        limit = max(100, min(10000, int(limit)))
        q = f"""
        query($code: String!, $fight: Int!, $source: Int, $target: Int, $start: Float, $end: Float, $resources: Boolean!, $limit: Int!) {{
          reportData {{
            report(code: $code) {{
              events(
                dataType: {data_type}, fightIDs: [$fight], sourceID: $source, targetID: $target,
                startTime: $start, endTime: $end, includeResources: $resources,
                translate: false, useAbilityIDs: true, useActorIDs: true, limit: $limit
              ) {{ data nextPageTimestamp }}
            }}
          }}
        }}
        """
        return self.query(q, {
            "code": code,
            "fight": int(fight_id),
            "source": int(source_id) if source_id is not None else None,
            "target": int(target_id) if target_id is not None else None,
            "start": float(start_time) if start_time is not None else None,
            "end": float(end_time) if end_time is not None else None,
            "resources": bool(include_resources),
            "limit": limit,
        })

    def report_events(
        self,
        report_code: str,
        fight_id: int,
        data_type: str = "Casts",
        source_id: int | None = None,
        target_id: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        include_resources: bool = False,
        limit: int = 10000,
        max_pages: int = 12,
    ) -> list[dict[str, Any]]:
        """Page event data using nextPageTimestamp with a hard safety bound."""
        event_cache_key = self._cache_key("report_events", {
            "code": extract_report_code(report_code), "fight": int(fight_id), "type": data_type,
            "source": source_id, "target": target_id, "start": start_time, "end": end_time,
            "resources": bool(include_resources), "limit": int(limit), "pages": int(max_pages),
        })
        def load_pages() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            cursor = start_time
            for _ in range(max(1, int(max_pages))):
                raw = self.report_events_page(
                    report_code, fight_id, data_type, source_id, target_id,
                    cursor, end_time, include_resources, limit,
                )
                paginator = (((raw.get("reportData") or {}).get("report") or {}).get("events") or {})
                data = paginator.get("data") or []
                if isinstance(data, list):
                    out.extend(x for x in data if isinstance(x, dict))
                nxt = paginator.get("nextPageTimestamp")
                if nxt is None:
                    break
                try:
                    nxt_f = float(nxt)
                except Exception:
                    break
                if cursor is not None and nxt_f <= float(cursor):
                    break
                cursor = nxt_f
            return out

        return list(self._cached_load(event_cache_key, 180, load_pages))

    def character_recent_reports(
        self,
        *,
        character_id: int | None = None,
        name: str = "",
        server_slug: str = "",
        server_region: str = "",
        limit: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        """Resolve a character and return its recent public reports.

        WCL validates GraphQL variables strictly. Keep ID lookup and
        name/server/region lookup as separate documents so no declared variable is
        unused (the old combined query triggered ``Variable "$id" is never used``).
        """
        limit = max(1, min(100, int(limit)))
        page = max(1, int(page))

        fields = """
              id canonicalID name classID level hidden
              server { id name slug region { id name slug compactName } }
              recentReports(limit: $limit, page: $page) {
                total per_page current_page last_page has_more_pages
                data {
                  code title startTime endTime visibility revision
                  zone { id name frozen }
                }
              }
        """

        if character_id:
            q = f"""
            query CharacterById($id: Int!, $limit: Int!, $page: Int!) {{
              characterData {{
                character(id: $id) {{
                  {fields}
                }}
              }}
              rateLimitData {{ limitPerHour pointsSpentThisHour pointsResetIn }}
            }}
            """
            return self.query(q, {
                "id": int(character_id),
                "limit": limit,
                "page": page,
            })

        if not (name and server_slug and server_region):
            raise ValueError("Need character id or name/server/region")

        q = f"""
        query CharacterByName($name: String!, $server: String!, $region: String!, $limit: Int!, $page: Int!) {{
          characterData {{
            character(name: $name, serverSlug: $server, serverRegion: $region) {{
              {fields}
            }}
          }}
          rateLimitData {{ limitPerHour pointsSpentThisHour pointsResetIn }}
        }}
        """
        return self.query(q, {
            "name": name,
            "server": server_slug,
            "region": server_region.lower(),
            "limit": limit,
            "page": page,
        })

    def user_reports(
        self,
        user_id: int,
        *,
        limit: int = 20,
        page: int = 1,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> dict[str, Any]:
        """Return public personal reports uploaded under a WCL user id."""
        q = """
        query($user: Int!, $limit: Int!, $page: Int!, $start: Float, $end: Float) {
          reportData {
            reports(userID: $user, limit: $limit, page: $page, startTime: $start, endTime: $end) {
              total per_page current_page last_page has_more_pages
              data {
                code title startTime endTime visibility revision
                zone { id name frozen }
              }
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self.query(q, {
            "user": int(user_id), "limit": max(1, min(100, int(limit))),
            "page": max(1, int(page)), "start": start_time, "end": end_time,
        })

    def report_with_talents(self, report_code: str) -> dict[str, Any]:
        """Fetch report metadata needed by the targeted desktop analysis flow."""
        code = extract_report_code(report_code)
        key = self._cache_key("report_with_talents", {"code": code})
        q = """
        query($code: String!) {
          reportData {
            report(code: $code) {
              code title startTime endTime visibility revision
              zone { id name frozen }
              fights(translate: true) {
                id name startTime endTime kill difficulty encounterID
                averageItemLevel countReached countRequired
                keystoneLevel keystoneTime keystoneBonus keystoneAffixes
                friendlyPlayers friendlySpecs friendlyItemLevels
                dungeonPulls {
                  id name startTime endTime kill encounterID x y
                  enemyNPCs { id gameID minimumInstanceID maximumInstanceID minimumInstanceGroupID maximumInstanceGroupID }
                  maps { id }
                }
              }
              masterData(translate: true) {
                logVersion gameVersion lang
                actors { id name type subType gameID petOwner server }
                abilities { gameID name type icon }
              }
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self._cached_load(key, 180, lambda: self.query(q, {"code": code}))

    def world_expansions(self) -> dict[str, Any]:
        """Return WCL world expansions for catalog-driven encounter selection."""
        key = self._cache_key("world_expansions", {})
        q = """
        query {
          worldData { expansions { id name } }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self._cached_load(key, 21600, lambda: self.query(q))

    def world_zones(self, expansion_id: int | None = None) -> dict[str, Any]:
        """Return zones/encounters for one expansion (or all when omitted).

        WCL's WorldData is relatively static compared with rankings. The desktop UI
        caches this response in session state so users do not need to know numeric
        Encounter IDs and repeated selections do not waste API points.
        """
        key = self._cache_key("world_zones", {"expansion": int(expansion_id) if expansion_id is not None else None})
        q = """
        query($expansion: Int) {
          worldData {
            zones(expansion_id: $expansion) {
              id name frozen
              brackets { min max bucket type }
              encounters { id name journalID }
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        variables = {"expansion": int(expansion_id) if expansion_id is not None else None}
        return self._cached_load(key, 21600, lambda: self.query(q, variables))

    def game_classes(self, zone_id: int | None = None) -> dict[str, Any]:
        q = """
        query($zone: Int) {
          gameData {
            classes(zone_id: $zone) { id name slug specs { id name slug } }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        return self.query(q, {"zone": int(zone_id) if zone_id is not None else None})

    def game_ability(self, ability_id: int) -> dict[str, Any]:
        q = """
        query($id: Int!) {
          gameData { ability(id: $id) { id name icon } }
        }
        """
        return self.query(q, {"id": int(ability_id)})


    def encounter_bracket_info(self, encounter_id: int) -> dict[str, Any]:
        """Return the zone bracket definition for an encounter, cached for one hour."""
        eid = int(encounter_id)
        key = self._cache_key("encounter_bracket_info", {"encounter": eid})
        q = """
        query($encounter: Int!) {
          worldData {
            encounter(id: $encounter) {
              id
              zone { id name brackets { min max bucket type } }
            }
          }
        }
        """
        def load() -> dict[str, Any]:
            data = self.query(q, {"encounter": eid})
            encounter = (((data.get("worldData") or {}).get("encounter") or {}))
            zone = encounter.get("zone") or {}
            return {"encounter_id": eid, "zone_id": zone.get("id"), "zone_name": zone.get("name"), "brackets": zone.get("brackets") or {}}

        return dict(self._cached_load(key, 3600, load))

    def ranking_bracket_for_value(self, encounter_id: int, value: int | float) -> int | None:
        """Translate a raw keystone/item-level value to WCL's 1-based bracket number.

        WCL exposes bracket definitions as min/max/bucket.  ``characterRankings.bracket``
        expects the bracket number, not necessarily the raw keystone level shown to users.
        """
        try:
            raw = float(value)
            info = self.encounter_bracket_info(int(encounter_id))
            b = info.get("brackets") or {}
            minimum = float(b.get("min"))
            maximum = float(b.get("max"))
            bucket = float(b.get("bucket"))
            if bucket <= 0:
                return None
            raw = min(max(raw, minimum), maximum)
            return max(1, int((raw - minimum) // bucket) + 1)
        except Exception:
            return None

    def character_rankings(
        self,
        encounter_id: int,
        class_name: str,
        spec_name: str,
        bracket: int | None = None,
        page: int = 1,
        server_region: str | None = None,
        include_other_players: bool = True,
        include_combatant_info: bool = True,
    ) -> dict:
        # WCL documents brackets as item-level/keystone buckets. Callers should map raw values through the zone bracket definition first.
        q = """
        query($encounter: Int!, $class: String!, $spec: String!, $bracket: Int, $page: Int!, $region: String, $other: Boolean!, $combatant: Boolean!) {
          worldData {
            encounter(id: $encounter) {
              id name journalID
              characterRankings(
                className: $class, specName: $spec, bracket: $bracket, page: $page,
                serverRegion: $region, includeOtherPlayers: $other, includeCombatantInfo: $combatant
              )
            }
          }
          rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }
        }
        """
        vars = {
            "encounter": int(encounter_id), "class": class_name, "spec": spec_name,
            "bracket": bracket, "page": int(page), "region": server_region,
            "other": bool(include_other_players), "combatant": bool(include_combatant_info),
        }
        return self.query(q, vars)

    def fight_rankings(
        self,
        encounter_id: int,
        bracket: int | None = None,
        page: int = 1,
        server_region: str | None = None,
        metric: str = "score",
        include_other_players: bool = True,
    ) -> dict[str, Any]:
        allowed = {"default", "execution", "feats", "score", "speed", "progress"}
        if metric not in allowed:
            metric = "score"
        q = f"""
        query($encounter: Int!, $bracket: Int, $page: Int!, $region: String, $other: Boolean!) {{
          worldData {{
            encounter(id: $encounter) {{
              id name journalID
              fightRankings(
                bracket: $bracket, page: $page, serverRegion: $region,
                metric: {metric}, includeOtherPlayers: $other
              )
            }}
          }}
          rateLimitData {{ limitPerHour pointsSpentThisHour pointsResetIn }}
        }}
        """
        return self.query(q, {
            "encounter": int(encounter_id), "bracket": bracket, "page": int(page),
            "region": server_region, "other": bool(include_other_players),
        })


def recursively_find_report_codes(value: Any) -> list[str]:
    """Best-effort extractor for WCL JSON ranking payloads.

    WCL ranking fields are JSON scalars and can evolve independently of the GraphQL
    schema. We therefore tolerate common field names instead of binding to one layout.
    """
    found: list[str] = []

    def walk(v: Any, parent_key: str = "") -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                lk = str(k).lower()
                if isinstance(x, str) and (
                    lk in {"reportcode", "report_code", "reportid", "report_id", "code"}
                    or ("report" in lk and "code" in lk)
                ):
                    code = extract_report_code(x)
                    if re.fullmatch(r"[A-Za-z0-9]{6,32}", code or ""):
                        found.append(code)
                elif isinstance(x, str) and "warcraftlogs.com/reports/" in x:
                    code = extract_report_code(x)
                    if code:
                        found.append(code)
                walk(x, lk)
        elif isinstance(v, list):
            for x in v:
                walk(x, parent_key)

    walk(value)
    seen = set()
    return [x for x in found if not (x in seen or seen.add(x))]


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for x in value.values():
            yield from iter_dicts(x)
    elif isinstance(value, list):
        for x in value:
            yield from iter_dicts(x)
