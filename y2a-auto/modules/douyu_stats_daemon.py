#!/usr/bin/env python3
"""Collect time-stamped Douyu events for PotatoFlow recordings."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .path_policy import atomic_write_text, ensure_directory, safe_path_component
except ImportError:  # Direct execution by the Docker entrypoint.
    from path_policy import atomic_write_text, ensure_directory, safe_path_component


BRIDGE_CONFIG = os.environ.get("BRIDGE_CONFIG", "/data/config/pipeline.json")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/data/recordings")
DOTA2_HEROES_URL = "https://wconf.douyucdn.cn/resource/node/config/dota2_wiki_new.json"
DOTA2_ITEMS_URL = "https://wconf.douyucdn.cn/resource/node/config/dota2_wiki_items.json"
DOTA2_OFFICIAL_ITEMS_URL = "https://www.dota2.com/datafeed/itemlist?language=schinese"
DOTA2_DATA_URL = "https://www.douyu.com/wgapi/augmentedlive/dota2/data/get"
HIGH_ENERGY_GFID = "24597"
FLUSH_INTERVAL = 30
DOTA2_POLL_INTERVAL = 15
RETENTION_SECONDS = 48 * 60 * 60
STABLE_SNAPSHOT_COUNT = 3
TZ = timezone(timedelta(hours=8))
STREAM_METADATA_DIR = ".potato-flow"
STATS_FILENAME = "douyu-stats.json"

dota_hero_map: dict[str, str] = {}
dota_item_map: dict[str, str] = {}


def stt_decode(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.split("/"):
        if "@=" in item:
            key, value = item.split("@=", 1)
            result[key] = value.replace("@A", "@").replace("@S", "/")
    return result


def encode_packet(payload: str) -> bytes:
    data = payload.encode("utf-8")
    body = data + b"\x00"
    length = 9 + len(data)
    return struct.pack("<III", length, length, 689) + body


def douyu_connect(room_id: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(15)
    sock.connect(("danmuproxy.douyu.com", 8601))
    sock.sendall(encode_packet(f"type@=loginreq/roomid@={room_id}/"))
    sock.sendall(encode_packet(f"type@=joingroup/rid@={room_id}/gid@=-9999/"))
    return sock


def send_heartbeat(sock: socket.socket) -> None:
    sock.sendall(encode_packet("type@=mrkl/"))


def decode_packets(buffer: bytes) -> tuple[list[tuple[str, dict[str, str], str]], bytes]:
    """Decode complete frames and retain a fragmented TCP tail."""
    messages: list[tuple[str, dict[str, str], str]] = []
    offset = 0
    while len(buffer) - offset >= 12:
        length = struct.unpack_from("<I", buffer, offset)[0]
        if length < 9 or length > 8 * 1024 * 1024:
            offset += 1
            continue
        frame_end = offset + 4 + length
        if frame_end > len(buffer):
            break
        body_start = offset + 12
        body_end = frame_end - 1
        text = buffer[body_start:body_end].decode("utf-8", errors="replace")
        message = stt_decode(text)
        messages.append((message.get("type", ""), message, text))
        offset = frame_end
    return messages, buffer[offset:]


def receive_messages(
    sock: socket.socket,
    pending: bytes,
) -> tuple[list[tuple[str, dict[str, str], str]] | None, bytes]:
    try:
        chunk = sock.recv(131072)
    except socket.timeout:
        return [], pending
    except OSError:
        return None, pending
    if not chunk:
        return None, pending
    return decode_packets(pending + chunk)


def _request_json(url: str, referer: str = "") -> dict:
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    return value if isinstance(value, dict) else {}


def load_gift_prices(room_id: str) -> dict[str, dict[str, object]]:
    url = f"https://gift.douyucdn.cn/api/gift/v2/web/list?rid={room_id}"
    try:
        data = _request_json(url, f"https://www.douyu.com/{room_id}")
        prices: dict[str, dict[str, object]] = {}
        for gift in data.get("data", {}).get("giftList", []):
            price = gift.get("priceInfo", {})
            value = float(price.get("price") or 0) / 100 if price.get("priceType") == "YUCHI" else 0
            prices[str(gift.get("id") or 0)] = {
                "name": str(gift.get("name") or ""),
                "price": value,
            }
        return prices
    except Exception as exc:
        print(f"[stats] 礼物配置加载失败({room_id}): {exc}", flush=True)
        return {}


def load_dota2_maps() -> None:
    global dota_hero_map, dota_item_map
    try:
        heroes = _request_json(DOTA2_HEROES_URL).get("heroes", {})
        items = _request_json(DOTA2_ITEMS_URL).get("items", {})
        official_items: dict[str, str] = {}
        try:
            rows = (
                _request_json(DOTA2_OFFICIAL_ITEMS_URL)
                .get("result", {})
                .get("data", {})
                .get("itemabilities", [])
            )
            for row in rows:
                name = str(row.get("name_loc") or "").strip()
                item_id = str(row.get("id") or "")
                item_key = str(row.get("name") or "")
                if not name or name.startswith("item_"):
                    continue
                if item_id:
                    official_items[item_id] = name
                if item_key:
                    official_items[item_key] = name
        except Exception as exc:
            print(f"[stats] DOTA2 官方装备中文名加载失败，继续使用斗鱼映射: {exc}", flush=True)
        dota_hero_map = {
            str(info.get("ID")): str(info.get("Name") or key)
            for key, info in heroes.items()
            if str(info.get("ID") or "")
        }
        item_map: dict[str, str] = {}
        for key, info in items.items():
            item_id = str(info.get("ID") or "")
            item_key = str(info.get("Key") or key)
            douyu_name = str(info.get("Name") or "").strip()
            name = (
                douyu_name
                if douyu_name and not douyu_name.startswith("item_")
                else official_items.get(item_key) or official_items.get(item_id) or item_key
            )
            if item_id:
                item_map[item_id] = name
            if item_key:
                item_map[item_key] = name
        for item_ref, name in official_items.items():
            item_map.setdefault(item_ref, name)
        dota_item_map = item_map
        print(f"[stats] DOTA2 映射: {len(dota_hero_map)} 英雄, {len(dota_item_map)} 装备", flush=True)
    except Exception as exc:
        print(f"[stats] DOTA2 映射加载失败: {exc}", flush=True)


def load_streamers_from_config() -> list[dict[str, str]]:
    """Read valid numeric Douyu rooms; configuration errors fail closed."""
    try:
        with open(BRIDGE_CONFIG, "r", encoding="utf-8") as file:
            config = json.load(file)
    except Exception as exc:
        print(f"[stats] 读取 bridge.config.json 失败: {exc}", flush=True)
        return []
    if not bool(config.get("douyu_stats_enabled", True)):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for profile in config.get("profiles", []):
        source = str(profile.get("source_url") or "")
        if "douyu.com" not in source:
            continue
        room_id = source.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit() or room_id in seen:
            continue
        seen.add(room_id)
        result.append({
            "room_id": room_id,
            "streamer": str(profile.get("streamer_name") or room_id).strip() or room_id,
        })
    return result


class RoomMonitor(threading.Thread):
    def __init__(self, room_id: str, streamer: str, prices: dict[str, dict[str, object]]):
        super().__init__(daemon=True, name=f"monitor-{room_id}")
        self.room_id = room_id
        self.streamer = streamer
        self.prices = prices
        self.streamer_dir = os.path.join(
            RECORDINGS_DIR,
            safe_path_component(streamer, fallback=room_id),
        )
        self.output_dir = os.path.join(self.streamer_dir, STREAM_METADATA_DIR)
        self.state: dict[str, object] = {
            "schema_version": 2,
            "room_id": room_id,
            "streamer": streamer,
            "started_at": datetime.now(TZ).isoformat(),
            "gift_events": [],
            "high_energy": {"details": []},
            "online_samples": [],
            "games": [],
            "active_game": None,
            "tooltip_diagnostics": {
                "messages": 0,
                "valid_snapshots": 0,
                "invalid_snapshots": 0,
                "last_raw_player_count": 0,
                "last_nonzero_player_count": 0,
                "last_seen_unix_ts": None,
            },
        }
        self._stop_event = threading.Event()
        self._accepted_fingerprint: tuple[str, ...] | None = None
        self._pending_fingerprint: tuple[str, ...] | None = None
        self._pending_players: list[dict] = []
        self._pending_anchor: dict | None = None
        self._pending_anchor_source = ""
        self._pending_count = 0
        self._restore_snapshot()
        diagnostics = self.state.setdefault("tooltip_diagnostics", {})
        if isinstance(diagnostics, dict):
            for key, value in {
                "messages": 0,
                "http_polls": 0,
                "http_snapshots": 0,
                "valid_snapshots": 0,
                "invalid_snapshots": 0,
                "last_raw_player_count": 0,
                "last_nonzero_player_count": 0,
                "last_seen_unix_ts": None,
                "last_source": "",
                "streamer_anchor_snapshots": 0,
                "streamer_anchor_last_seen_unix_ts": None,
            }.items():
                diagnostics.setdefault(key, value)

    def _restore_snapshot(self) -> None:
        """Continue a recording window across safe container restarts."""
        candidates = (
            os.path.join(self.output_dir, STATS_FILENAME),
            os.path.join(self.streamer_dir, "stats_current.json"),
        )
        previous = None
        for output in candidates:
            try:
                with open(output, "r", encoding="utf-8") as file:
                    previous = json.load(file)
                break
            except (OSError, ValueError, TypeError):
                continue
        if not isinstance(previous, dict):
            return
        if previous.get("schema_version") != 2 or str(previous.get("room_id")) != self.room_id:
            return
        for key in (
            "started_at", "gift_events", "high_energy", "online_samples",
            "games", "active_game", "tooltip_diagnostics",
        ):
            if key in previous:
                self.state[key] = previous[key]
        active = self.state.get("active_game")
        if not isinstance(active, dict):
            return
        players = active.get("players", [])
        hero_ids = [str(player.get("id") or "") for player in players if isinstance(player, dict)]
        if len(hero_ids) == 10 and len(set(hero_ids)) == 10 and "0" not in hero_ids:
            self._accepted_fingerprint = tuple(sorted(hero_ids))
        else:
            self.state["active_game"] = None

    def stop(self) -> None:
        self._stop_event.set()

    def handle_dgb(self, message: dict[str, str]) -> None:
        now = time.time()
        gift_id = message.get("gfid", "0")
        count = max(1, int(message.get("gfcnt", "1") or 1))
        user = message.get("nn", "")
        if gift_id == HIGH_ENERGY_GFID:
            details = self.state["high_energy"]["details"]  # type: ignore[index]
            details.append({
                "unix_ts": now,
                "ts": datetime.now(TZ).strftime("%H:%M:%S"),
                "user": user,
                "amount": count,
            })
            return
        info = self.prices.get(gift_id)
        if not info or float(info.get("price") or 0) < 100:
            return
        unit_price = int(float(info.get("price") or 0))
        self.state["gift_events"].append({  # type: ignore[union-attr]
            "unix_ts": now,
            "gift_id": gift_id,
            "name": str(message.get("gfn") or info.get("name") or "未知礼物"),
            "unit_price": unit_price,
            "count": count,
            "total_value": unit_price * count,
        })

    def handle_oni(self, message: dict[str, str]) -> None:
        raw = message.get("un", "")
        try:
            value = int(float(raw.replace("万", "")) * 10000) if "万" in raw else int(raw)
        except (TypeError, ValueError):
            return
        self.state["online_samples"].append({"unix_ts": time.time(), "value": value})  # type: ignore[union-attr]

    @staticmethod
    def _raw_players_from_tooltip(data: dict) -> list[dict]:
        top = data.get("top", [])
        bottom = data.get("bottom", [])
        if not isinstance(top, list) or not isinstance(bottom, list):
            return []
        # Current Douyu GSI puts all ten heroes in ``top``. Older
        # type_tooltips payloads split the line-up into top/bottom teams.
        candidates = top if len(top) == 10 and not bottom else top + bottom
        return [player for player in candidates if isinstance(player, dict)]

    @classmethod
    def _item_name(cls, raw_item: object) -> str:
        item_key = str(raw_item or "")
        if item_key.casefold() in {"", "0", "empty", "item_empty", "unknown"}:
            return ""
        mapped = str(dota_item_map.get(item_key) or "")
        if mapped and not mapped.startswith("item_"):
            return mapped
        if item_key.startswith("item_"):
            # New Dota items can arrive before Douyu's Chinese wiki map is
            # updated. A readable official key is better model context than
            # an opaque "unknown" marker and is never presented as Chinese.
            return item_key.removeprefix("item_").replace("_", " ").title()
        return f"未知({item_key})"

    @classmethod
    def _player_from_raw(cls, raw_player: dict) -> dict | None:
        hero_id = str(raw_player.get("id") or "")
        if hero_id in {"", "0"}:
            return None
        player = {
            "id": hero_id,
            "hero": dota_hero_map.get(hero_id, f"未知({hero_id})"),
            "items": [
                item_name
                # Douyu's player renders the six main inventory slots
                # with items.slice(0, 6). Backpack slots are deliberately
                # excluded from the final equipment snapshot.
                for item in raw_player.get("items", [])[:6]
                if (item_name := cls._item_name(item))
            ],
            "neutral": cls._item_name(raw_player.get("neutral")),
            "scepter": bool(raw_player.get("aghanims_scepter", False)),
            "shard": bool(raw_player.get("aghanims_shard", False)),
            "facet": raw_player.get("facet", 0),
            "talents": raw_player.get("talents", []),
        }
        aliases = (
            ("kills", "kills", "kill", "k"),
            ("deaths", "deaths", "death", "d"),
            ("assists", "assists", "assist", "a"),
        )
        values: dict[str, int] = {}
        for output_key, *input_keys in aliases:
            raw_value = next((raw_player.get(key) for key in input_keys if key in raw_player), None)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                values[output_key] = value
        if len(values) == 3:
            player.update(values)
            player["kda"] = round(
                (values["kills"] + values["assists"]) / max(1, values["deaths"]),
                2,
            )
        return player

    @classmethod
    def _players_from_tooltip(cls, data: dict) -> list[dict]:
        players: list[dict] = []
        seen: set[str] = set()
        for raw_player in cls._raw_players_from_tooltip(data):
            player = cls._player_from_raw(raw_player)
            hero_id = str((player or {}).get("id") or "")
            if not player or hero_id in seen:
                return []
            seen.add(hero_id)
            players.append(player)
        return players if len(players) == 10 else []

    @classmethod
    def _streamer_anchor(cls, data: dict, players: list[dict]) -> dict | None:
        """Use Douyu's explicit streamer-view hero, never a fixed lineup slot."""
        raw_anchor = data.get("hero")
        if not isinstance(raw_anchor, dict):
            return None
        anchor = cls._player_from_raw(raw_anchor)
        lineup_ids = {str(player.get("id") or "") for player in players}
        return anchor if anchor and anchor["id"] in lineup_ids else None

    @staticmethod
    def _anchor_signature(player: dict) -> tuple:
        return (
            str(player.get("id") or ""),
            tuple(player.get("items", [])),
            str(player.get("neutral") or ""),
            bool(player.get("scepter")),
            bool(player.get("shard")),
            player.get("kills"), player.get("deaths"), player.get("assists"),
        )

    def _update_anchor_history(
        self,
        game: dict,
        anchor: dict | None,
        source: str,
        now: float,
    ) -> None:
        if not anchor:
            return
        player = dict(anchor)
        game["anchor_player"] = player
        game["anchor_source"] = source
        game["anchor_last_seen_unix_ts"] = now
        history = game.setdefault("anchor_history", [])
        if not isinstance(history, list):
            history = []
            game["anchor_history"] = history
        if history and self._anchor_signature(history[-1].get("player", {})) == self._anchor_signature(player):
            history[-1]["last_seen_unix_ts"] = now
            history[-1]["player"] = player
            history[-1]["source"] = source
        else:
            history.append({
                "start_unix_ts": now,
                "last_seen_unix_ts": now,
                "source": source,
                "player": player,
            })
            del history[:-200]

    def handle_dota2_snapshot(self, data: dict, source: str) -> None:
        diagnostics = self.state["tooltip_diagnostics"]
        raw_players = self._raw_players_from_tooltip(data)
        if source == "type_tooltips":
            diagnostics["messages"] += 1
        else:
            diagnostics["http_snapshots"] += 1
        diagnostics["last_raw_player_count"] = len(raw_players)
        diagnostics["last_nonzero_player_count"] = sum(
            str(player.get("id") or "") not in {"", "0"}
            for player in raw_players
        )
        diagnostics["last_seen_unix_ts"] = time.time()
        diagnostics["last_source"] = source
        players = self._players_from_tooltip(data)
        if not players:
            diagnostics["invalid_snapshots"] += 1
            return
        diagnostics["valid_snapshots"] += 1
        anchor = self._streamer_anchor(data, players)
        if anchor:
            diagnostics["streamer_anchor_snapshots"] = int(
                diagnostics.get("streamer_anchor_snapshots") or 0
            ) + 1
            diagnostics["streamer_anchor_last_seen_unix_ts"] = time.time()
        fingerprint = tuple(sorted(player["id"] for player in players))
        now = time.time()
        if fingerprint == self._accepted_fingerprint:
            active = self.state.get("active_game")
            if isinstance(active, dict):
                active["players"] = players
                active["last_seen_unix_ts"] = now
                self._update_anchor_history(active, anchor, source, now)
            self._pending_fingerprint = None
            self._pending_players = []
            self._pending_anchor = None
            self._pending_anchor_source = ""
            self._pending_count = 0
            return
        if fingerprint == self._pending_fingerprint:
            self._pending_count += 1
            self._pending_players = players
            self._pending_anchor = anchor
            self._pending_anchor_source = source
        else:
            self._pending_fingerprint = fingerprint
            self._pending_players = players
            self._pending_anchor = anchor
            self._pending_anchor_source = source
            self._pending_count = 1
        if self._pending_count < STABLE_SNAPSHOT_COUNT:
            return
        previous = self.state.get("active_game")
        if isinstance(previous, dict):
            archived = dict(previous)
            archived["end_unix_ts"] = now
            archived["end_ts"] = datetime.now(TZ).strftime("%H:%M:%S")
            self.state["games"].append(archived)  # type: ignore[union-attr]
            print(f"[stats:{self.streamer}] 稳定阵容切换，归档上一局", flush=True)
        self.state["active_game"] = {
            "start_unix_ts": now,
            "last_seen_unix_ts": now,
            "players": self._pending_players,
        }
        self._update_anchor_history(
            self.state["active_game"],  # type: ignore[arg-type]
            self._pending_anchor,
            self._pending_anchor_source,
            now,
        )
        self._accepted_fingerprint = fingerprint
        self._pending_fingerprint = None
        self._pending_players = []
        self._pending_anchor = None
        self._pending_anchor_source = ""
        self._pending_count = 0

    def handle_tooltips(self, message: dict[str, str]) -> None:
        try:
            data = json.loads(message.get("content", ""))
        except (TypeError, ValueError):
            return
        if isinstance(data, dict):
            self.handle_dota2_snapshot(data, "type_tooltips")

    def poll_dota2_data(self) -> None:
        diagnostics = self.state["tooltip_diagnostics"]
        diagnostics["http_polls"] += 1
        response = _request_json(
            f"{DOTA2_DATA_URL}?rid={self.room_id}",
            f"https://www.douyu.com/{self.room_id}",
        )
        data = response.get("data")
        if isinstance(data, dict) and data:
            self.handle_dota2_snapshot(data, "http")

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        self.state["gift_events"] = [  # type: ignore[index]
            item for item in self.state.get("gift_events", [])
            if float(item.get("unix_ts") or 0) >= cutoff
        ]
        high = self.state.get("high_energy", {})
        if isinstance(high, dict):
            high["details"] = [
                item for item in high.get("details", [])
                if float(item.get("unix_ts") or 0) >= cutoff
            ]
        self.state["online_samples"] = [  # type: ignore[index]
            item for item in self.state.get("online_samples", [])
            if float(item.get("unix_ts") or 0) >= cutoff
        ][-10000:]
        self.state["games"] = [  # type: ignore[index]
            item for item in self.state.get("games", [])
            if float(item.get("end_unix_ts") or item.get("last_seen_unix_ts") or 0) >= cutoff
        ][-50:]

    def flush(self) -> None:
        try:
            self._prune()
            ensure_directory(Path(self.streamer_dir))
            ensure_directory(Path(self.output_dir))
            output = Path(self.output_dir) / STATS_FILENAME
            atomic_write_text(
                output,
                json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            )
            # Remove the old visible filename only after its replacement has
            # been written successfully.
            (Path(self.streamer_dir) / "stats_current.json").unlink(missing_ok=True)
        except Exception as exc:
            print(f"[stats:{self.streamer}] 落盘失败: {exc}", flush=True)

    def run(self) -> None:
        last_flush = 0.0
        heartbeat_sent = 0.0
        print(f"[stats:{self.streamer}] 监控启动 (房间 {self.room_id})", flush=True)
        while not self._stop_event.is_set():
            sock: socket.socket | None = None
            try:
                sock = douyu_connect(self.room_id)
                sock.settimeout(15)
                pending = b""
                last_message = time.time()
                last_dota_poll = 0.0
                while not self._stop_event.is_set():
                    if time.time() - heartbeat_sent > 40:
                        send_heartbeat(sock)
                        heartbeat_sent = time.time()
                    messages, pending = receive_messages(sock, pending)
                    if messages is None:
                        raise ConnectionError("弹幕连接断开")
                    if messages:
                        last_message = time.time()
                    elif time.time() - last_message > 120:
                        raise ConnectionError("120s 无消息，主动重连")
                    for message_type, message, _raw in messages:
                        if message_type == "dgb":
                            self.handle_dgb(message)
                        elif message_type == "oni":
                            self.handle_oni(message)
                        elif message_type == "type_tooltips":
                            self.handle_tooltips(message)
                    if time.time() - last_dota_poll >= DOTA2_POLL_INTERVAL:
                        self.poll_dota2_data()
                        last_dota_poll = time.time()
                    if time.time() - last_flush >= FLUSH_INTERVAL:
                        self.flush()
                        last_flush = time.time()
            except Exception as exc:
                print(f"[stats:{self.streamer}] 连接断开: {exc}, 3s 重连", flush=True)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self.flush()
            self._stop_event.wait(3)


def _start_monitor(room: dict[str, str]) -> RoomMonitor:
    prices = load_gift_prices(room["room_id"])
    print(f"[stats:{room['streamer']}] 加载 {len(prices)} 个礼物配置", flush=True)
    monitor = RoomMonitor(room["room_id"], room["streamer"], prices)
    monitor.start()
    return monitor


def run() -> None:
    print("[stats] === 斗鱼数据监控启动 (多直播间) ===", flush=True)
    load_dota2_maps()
    monitors: dict[str, RoomMonitor] = {}
    while True:
        rooms = {room["room_id"]: room for room in load_streamers_from_config()}
        for room_id, monitor in list(monitors.items()):
            room = rooms.get(room_id)
            if room is None or room["streamer"] != monitor.streamer or not monitor.is_alive():
                monitor.stop()
                monitors.pop(room_id, None)
        for room_id, room in rooms.items():
            if room_id not in monitors:
                monitors[room_id] = _start_monitor(room)
                time.sleep(1)
        time.sleep(30)


if __name__ == "__main__":
    run()
