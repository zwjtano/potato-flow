"""Verified current-team and official-event TI player portrait references.

Valve requires TI teams to submit 1024x1024 transparent head-and-chest player
portraits.  Liquipedia Commons mirrors many of those team-provided assets and
records their provenance. Official team roster pages and official tournament
media libraries are accepted as lower-priority fallbacks; search-engine images,
AI faces and old-team portraits are deliberately rejected.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from ti2026_context import normalize_ti2026_team, ti2026_player_portrait_slot


LIQUIPEDIA_DOTA2_API = "https://liquipedia.net/dota2/api.php"
LIQUIPEDIA_COMMONS_API = "https://liquipedia.net/commons/api.php"
USER_AGENT = "PotatoFlow/1.6.102 (+https://github.com/zwjtano/potato-flow)"
MAX_PLAYER_PORTRAIT_BYTES = 12 * 1024 * 1024

# Player handles whose Liquipedia page title is not a punctuation-insensitive
# spelling of the current in-game name.
LIQUIPEDIA_PLAYER_PAGES: dict[str, str] = {
    "No[o]ne-": "Noone",
    "m1CKe": "MiCKe",
    "Cr1t-": "Cr1t",
    "Kiritych~": "Kiritych",
    "gpk~": "Gpk",
    "MieRo`": "MieRo",
    "Save-": "Save",
    "Mirage`": "Mirage",
    "Yopaj-": "Yopaj",
    "not me": "Not me",
    "y`": "Y",
    "Erika": "YSR-04E",
    "Faith_bian": "Bach",
}

PLAYER_MEDIA_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "TEAM VISION": ("TEAM VISION", "PARIVISION"),
    "1win Team": ("1win", "Tundra"),
    "BoomBoys": ("BoomBoys", "BetBoom"),
    "HULIGANI": ("HULIGANI", "L1GA TEAM"),
}

# Provenance was read back from the Commons FileInfo record. Keeping verified
# current assets here avoids repeated MediaWiki calls during a live cover job;
# the dynamic resolver below covers other players and populates the same cache.
VERIFIED_TI2026_PLAYER_PORTRAITS: dict[str, dict[str, str]] = {
    "No[o]ne-": {
        "team_name": "TEAM VISION",
        "image_name": "Noone_2026_PARIVISION.webp",
        "url": "https://liquipedia.net/commons/images/0/02/Noone_2026_PARIVISION.webp",
        "source_page": "https://liquipedia.net/commons/File:Noone_2026_PARIVISION.webp",
        "note": "Provided by representative of PARIVISION",
        "source": "Liquipedia Commons permission record",
        "source_kind": "team_representative",
    },
    "Erika": {
        "team_name": "Team Resilience", "image_name": "YSR-04E_2026_Team_Resilience.jpg",
        "url": "https://liquipedia.net/commons/images/d/d7/YSR-04E_2026_Team_Resilience.jpg",
        "source_page": "https://liquipedia.net/commons/File:YSR-04E_2026_Team_Resilience.jpg",
        "note": "Provided by Galahad, manager of Team Resilience",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "Echozz": {
        "team_name": "Team Resilience", "image_name": "Echo_2026_Team_Resilience.jpg",
        "url": "https://liquipedia.net/commons/images/7/71/Echo_2026_Team_Resilience.jpg",
        "source_page": "https://liquipedia.net/commons/File:Echo_2026_Team_Resilience.jpg",
        "note": "Provided by Galahad, manager of Team Resilience",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "niu": {
        "team_name": "Team Resilience", "image_name": "Niu_2026_Team_Resilience.jpg",
        "url": "https://liquipedia.net/commons/images/d/da/Niu_2026_Team_Resilience.jpg",
        "source_page": "https://liquipedia.net/commons/File:Niu_2026_Team_Resilience.jpg",
        "note": "Provided by Galahad, manager of Team Resilience",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "planet": {
        "team_name": "Team Resilience", "image_name": "Planet_2026_Team_Resilience.jpg",
        "url": "https://liquipedia.net/commons/images/d/d9/Planet_2026_Team_Resilience.jpg",
        "source_page": "https://liquipedia.net/commons/File:Planet_2026_Team_Resilience.jpg",
        "note": "Provided by Galahad, manager of Team Resilience",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "zzq": {
        "team_name": "Team Resilience", "image_name": "Zzq_2026_Team_Resilience.jpg",
        "url": "https://liquipedia.net/commons/images/5/58/Zzq_2026_Team_Resilience.jpg",
        "source_page": "https://liquipedia.net/commons/File:Zzq_2026_Team_Resilience.jpg",
        "note": "Provided by Galahad, manager of Team Resilience",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "sayuw": {
        "team_name": "HULIGANI", "image_name": "Sayuw_2026_L1GA_TEAM.webp",
        "url": "https://liquipedia.net/commons/images/6/60/Sayuw_2026_L1GA_TEAM.webp",
        "source_page": "https://liquipedia.net/commons/File:Sayuw_2026_L1GA_TEAM.webp",
        "note": "Provided by Vladislav, operations director for L1GA TEAM",
        "source": "Liquipedia Commons permission record", "source_kind": "team_representative",
    },
    "Topson": {
        "team_name": "LGD Gaming", "image_name": "Topson_Riyadh_Masters_2024.jpg",
        "url": "https://liquipedia.net/commons/images/e/e1/Topson_Riyadh_Masters_2024.jpg",
        "source_page": "https://liquipedia.net/commons/File:Topson_Riyadh_Masters_2024.jpg",
        "note": "Official EWC event photo used as a temporary identity fallback after the late LGD transfer",
        "source": "Esports World Cup Flickr permission record", "source_kind": "official_event_media_legacy",
    },
}


# The EWC 2026 media lobby supplied current-event photos for these players.
# Store the verified file identities locally so production cover jobs never
# depend on dozens of live MediaWiki lookups (which quickly hit HTTP 429).
EWC2026_PLAYER_FILES: dict[str, str] = {
    "Nightfall": "Nightfall_Esports_World_Cup_2026_Dota_2.jpg",
    "Mikoto": "Mikoto_Esports_World_Cup_2026_Dota_2.jpg",
    "Ws": "Ws_Esports_World_Cup_2026_Dota_2.jpg",
    "Mira": "Mira_Esports_World_Cup_2026_Dota_2.jpg",
    "kaori": "Kaori_Esports_World_Cup_2026_Dota_2.jpg",
    "Kiritych~": "Kiritych_Esports_World_Cup_2026_Dota_2.jpg",
    "gpk~": "Gpk_Esports_World_Cup_2026_Dota_2.jpg",
    "MieRo`": "MieRo_Esports_World_Cup_2026_Dota_2.jpg",
    "Save-": "Save-_Esports_World_Cup_2026_Dota_2.jpg",
    "Kataomi": "Kataomi_Esports_World_Cup_2026_Dota_2.jpg",
    "skiter": "Skiter_Esports_World_Cup_2026_Dota_2.jpg",
    "Malr1ne": "Malr1ne_Esports_World_Cup_2026_Dota_2.jpg",
    "ATF": "ATF_Esports_World_Cup_2026_Dota_2.jpg",
    "Cr1t-": "Cr1t-_Esports_World_Cup_2026_Dota_2.jpg",
    "Sneyking": "Sneyking_Esports_World_Cup_2026_Dota_2.jpg",
    "m1CKe": "MiCKe_Esports_World_Cup_2026_Dota_2.jpg",
    "Nisha": "Nisha_Esports_World_Cup_2026_Dota_2.jpg",
    "Ace": "Ace_Esports_World_Cup_2026_Dota_2.jpg",
    "Boxi": "Boxi_Esports_World_Cup_2026_Dota_2.jpg",
    "tOfu": "TOfu_Esports_World_Cup_2026_Dota_2.jpg",
    "Pure": "Pure_Esports_World_Cup_2026_Dota_2.jpg",
    "bzm": "Bzm_Esports_World_Cup_2026_Dota_2.jpg",
    "33": "33_Esports_World_Cup_2026_Dota_2.jpg",
    "Ari": "Ari_Esports_World_Cup_2026_Dota_2.jpg",
    "Whitemon": "Whitemon_Esports_World_Cup_2026_Dota_2.jpg",
    "Ame": "Ame_Esports_World_Cup_2026_Dota_2.jpg",
    "NothingToSay": "NothingToSay_Esports_World_Cup_2026_Dota_2.jpg",
    "Xxs": "Xxs_Esports_World_Cup_2026_Dota_2.jpg",
    "fy": "Fy_Esports_World_Cup_2026_Dota_2.jpg",
    "xNova": "XNova_Esports_World_Cup_2026_Dota_2.jpg",
    "watson": "Watson_Esports_World_Cup_2026_Dota_2.jpg",
    "CHIRA_JUNIOR": "CHIRA_JUNIOR_Esports_World_Cup_2026_Dota_2.jpg",
    "DM": "DM_Esports_World_Cup_2026_Dota_2.jpg",
    "Saksa": "Saksa_Esports_World_Cup_2026_Dota_2.jpg",
    "Malady": "Malady_Esports_World_Cup_2026_Dota_2.jpg",
    "Yatoro": "Yatoro_Esports_World_Cup_2026_Dota_2.jpg",
    "Larl": "Larl_Esports_World_Cup_2026_Dota_2.jpg",
    "Collapse": "Collapse_Esports_World_Cup_2026_Dota_2.jpg",
    "not me": "Not_me_Esports_World_Cup_2026_Dota_2.jpg",
    "rue": "Rue_Esports_World_Cup_2026_Dota_2.jpg",
    "Satanic": "Satanic_Esports_World_Cup_2026_Dota_2.jpg",
    "No[o]ne-": "Noone_Esports_World_Cup_2026_Dota_2.jpg",
    "Noticed": "Noticed_Esports_World_Cup_2026_Dota_2.jpg",
    "9Class": "9Class_Esports_World_Cup_2026_Dota_2.jpg",
    "Dukalis": "Dukalis_Esports_World_Cup_2026_Dota_2.jpg",
    "SumaiL": "SumaiL_Esports_World_Cup_2026_Dota_2.jpg",
    "lorenof": "Lorenof_Esports_World_Cup_2026_Dota_2.jpg",
    "Davai": "Davai_Esports_World_Cup_2026_Dota_2.jpg",
    "OmaR": "OmaR_Esports_World_Cup_2026_Dota_2.jpg",
    "GH": "GH_Esports_World_Cup_2026_Dota_2.jpg",
    "ssnovv1": "Ssnovv1_Esports_World_Cup_2026_Dota_2.jpg",
    "Mirage`": "Mirage_Esports_World_Cup_2026_Dota_2.jpg",
    "Corrupted": "Corrupted_Esports_World_Cup_2026_Dota_2.jpg",
    "RESPECT": "RESPECT_Esports_World_Cup_2026_Dota_2.jpg",
    "shiro": "Shiro_Esports_World_Cup_2026_Dota_2.jpg",
    "Xm": "Xm_Esports_World_Cup_2026_Dota_2.jpg",
    "Faith_bian": "Bach_Esports_World_Cup_2026_Dota_2.jpg",
    "XinQ": "XinQ_Esports_World_Cup_2026_Dota_2.jpg",
    "y`": "Y`_Esports_World_Cup_2026_Dota_2.jpg",
    "Natsumi": "Natsumi_Esports_World_Cup_2026_Dota_2.jpg",
    "Yopaj-": "Yopaj_Esports_World_Cup_2026_Dota_2.jpg",
    "Yuma": "Yuma_Esports_World_Cup_2026_Dota_2.jpg",
    "Wisper": "Wisper_Esports_World_Cup_2026_Dota_2.jpg",
    "Thiolicor": "Thiolicor_Esports_World_Cup_2026_Dota_2.jpg",
    "KJ": "KJ_Esports_World_Cup_2026_Dota_2.jpg",
    "Ghost": "Ghost_Esports_World_Cup_2026_Dota_2.jpg",
    "RCY": "RCY_Esports_World_Cup_2026_Dota_2.jpg",
    "Fayde": "Fayde_Esports_World_Cup_2026_Dota_2.jpg",
    "Bignum": "Bignum_Esports_World_Cup_2026_Dota_2.jpg",
    "Speeed": "Speeed_Esports_World_Cup_2026_Dota_2.jpg",
}

# Current player pages on the official OG website expose transparent roster
# cutouts. These are preferable to action photography even when the file was
# first published in 2025 because the same player remains on OG's current team.
VERIFIED_OFFICIAL_TEAM_PLAYER_PORTRAITS: dict[str, dict[str, str]] = {
    "SumaiL": {
        "team_name": "Nigma Galaxy", "image_name": "NGX-Sumail-2025.png",
        "url": "https://nigmagalaxy.com/wp-content/uploads/2025/04/NGX-Sumail-2025.png",
        "source_page": "https://nigmagalaxy.com/news/players/sumail/",
        "note": "Current player portrait on the official Nigma Galaxy roster page",
        "source": "Nigma Galaxy", "source_kind": "official_team_website",
    },
    "Natsumi": {
        "team_name": "OG", "image_name": "Natsumi.png",
        "url": "https://ogs.gg/wp-content/uploads/2025/05/Natsumi.png",
        "source_page": "https://ogs.gg/players/natsumi/",
        "note": "Current player portrait on the official OG roster page",
        "source": "OG Esports", "source_kind": "official_team_website",
    },
    "Yopaj-": {
        "team_name": "OG", "image_name": "yopaj.png",
        "url": "https://ogs.gg/wp-content/uploads/2025/05/yopaj.png",
        "source_page": "https://ogs.gg/players/yopaj/",
        "note": "Current player portrait on the official OG roster page",
        "source": "OG Esports", "source_kind": "official_team_website",
    },
    "Raven": {
        "team_name": "OG", "image_name": "Raven.png",
        "url": "https://ogs.gg/wp-content/uploads/2026/06/Raven.png",
        "source_page": "https://ogs.gg/players/raven/",
        "note": "Current player portrait on the official OG roster page",
        "source": "OG Esports", "source_kind": "official_team_website",
    },
    "TIMS": {
        "team_name": "OG", "image_name": "TIMS.png",
        "url": "https://ogs.gg/wp-content/uploads/2025/05/TIMS.png",
        "source_page": "https://ogs.gg/players/tims/",
        "note": "Current player portrait on the official OG roster page",
        "source": "OG Esports", "source_kind": "official_team_website",
    },
    "skem": {
        "team_name": "OG", "image_name": "Skem.png",
        "url": "https://ogs.gg/wp-content/uploads/2025/05/Skem.png",
        "source_page": "https://ogs.gg/players/skem/",
        "note": "Current player portrait on the official OG roster page",
        "source": "OG Esports", "source_kind": "official_team_website",
    },
}


@dataclass(frozen=True)
class Dota2PlayerPortrait:
    player_name: str
    team_name: str
    path: str
    source_url: str
    source_page: str
    source_note: str
    image_name: str
    source_kind: str


def _update_roster_slot(
    portrait: Dota2PlayerPortrait | None,
    player_name: str,
    team_name: str,
    *,
    error: str = "",
) -> None:
    """Keep the v1.6.78 roster slot authoritative for runtime asset state."""
    slot = ti2026_player_portrait_slot(player_name, team_name)
    if slot is None:
        return
    if portrait is None:
        slot.update({"status": "fetch_failed", "error": error})
        return
    slot.update({
        "status": "ready",
        "path": portrait.path,
        "source": portrait.source_url,
        "source_page": portrait.source_page,
        "source_note": portrait.source_note,
        "image_name": portrait.image_name,
    })
    slot.pop("error", None)


def _fetch_json(url: str, timeout: float = 20) -> Any:
    last_error: Exception | None = None
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if str(response.headers.get("Content-Encoding") or "").casefold() == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.5)
    curl = shutil.which("curl")
    if curl:
        completed = subprocess.run(
            [
                curl, "-L", "--compressed", "--fail", "--silent", "--show-error",
                "--max-time", str(max(1, int(timeout))),
                "--user-agent", USER_AGENT, url,
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return json.loads(completed.stdout.decode("utf-8"))
    assert last_error is not None
    raise last_error


def _api_url(base: str, **params: Any) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _cache_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return slug or "player"


def _commons_image_url(image_name: str) -> str:
    digest = hashlib.md5(image_name.encode("utf-8")).hexdigest()
    quoted = urllib.parse.quote(image_name, safe="")
    return f"https://liquipedia.net/commons/images/{digest[0]}/{digest[:2]}/{quoted}"


def _verified_portrait(player_name: str, team_name: str) -> dict[str, str] | None:
    """Resolve punctuation variants such as OpenDota's ``SumaiL-`` safely."""
    identity = _compact(player_name)
    for portraits in (
        VERIFIED_TI2026_PLAYER_PORTRAITS,
        VERIFIED_OFFICIAL_TEAM_PLAYER_PORTRAITS,
    ):
        for known_name, metadata in portraits.items():
            if _compact(known_name) == identity:
                return metadata
    for known_name, image_name in EWC2026_PLAYER_FILES.items():
        if _compact(known_name) != identity:
            continue
        return {
            "team_name": team_name,
            "image_name": image_name,
            "url": _commons_image_url(image_name),
            "source_page": (
                "https://liquipedia.net/commons/File:"
                + urllib.parse.quote(image_name, safe="")
            ),
            "note": "Official Esports World Cup 2026 media-lobby player photo",
            "source": "Esports World Cup media lobby permission record",
            "source_kind": "official_event_media",
        }
    return None


def _image_matches_current_team(image_name: str, team_name: str) -> bool:
    if "2026" not in str(image_name):
        return False
    team_tokens = {
        _compact(alias)
        for alias in PLAYER_MEDIA_TEAM_ALIASES.get(team_name, (team_name,))
        if _compact(alias)
    }
    return not team_tokens or any(token in _compact(image_name) for token in team_tokens)


def _metadata_matches_current_team(metadata: dict[str, Any], team_name: str) -> bool:
    if normalize_ti2026_team(str(metadata.get("team_name") or "")) != normalize_ti2026_team(team_name):
        return False
    if str(metadata.get("source_kind") or "") in {
        "official_team_website", "official_event_media_legacy",
    }:
        return True
    image_name = str(metadata.get("image_name") or "")
    return "2026" in image_name and (
        _image_matches_current_team(image_name, team_name)
        or str(metadata.get("source_kind") or "") == "official_event_media"
    )


def _file_info_value(wikitext: str, key: str) -> str:
    match = re.search(rf"(?mi)^\|{re.escape(key)}\s*=\s*(.+?)\s*$", wikitext)
    return str(match.group(1) or "").strip() if match else ""


def _candidate_images(player_name: str, team_name: str, images: list[str]) -> list[str]:
    identity = _compact(LIQUIPEDIA_PLAYER_PAGES.get(player_name, player_name))
    candidates = []
    for image in images:
        lowered = str(image).casefold()
        if not lowered.endswith((".png", ".webp", ".jpg", ".jpeg")):
            continue
        if any(marker in lowered for marker in ("_hd.", "icon_", "_icon", "allmode", "logo", "mapicon")):
            continue
        if identity and identity not in _compact(image):
            continue
        if "2026" not in image:
            continue
        candidates.append(str(image))
    return sorted(
        candidates,
        key=lambda image: (
            1 if "2026" in image else 0,
            1 if _image_matches_current_team(image, team_name) else 0,
            max((int(year) for year in re.findall(r"20\d{2}", image)), default=0),
            1 if image.casefold().endswith(".webp") else 0,
        ),
        reverse=True,
    )


def _official_commons_metadata(
    image_name: str,
    player_name: str,
    team_name: str,
    timeout: float,
) -> dict[str, str] | None:
    title = f"File:{image_name}"
    parsed = _fetch_json(
        _api_url(
            LIQUIPEDIA_COMMONS_API,
            action="parse",
            page=title,
            prop="wikitext",
            format="json",
            formatversion=2,
        ),
        timeout,
    )
    wikitext = str((parsed.get("parse") or {}).get("wikitext") or "")
    note = _file_info_value(wikitext, "note")
    source = _file_info_value(wikitext, "source")
    copyright_name = _file_info_value(wikitext, "copyright")
    event_name = _file_info_value(wikitext, "event")
    featured_player = _file_info_value(wikitext, "featured")
    featured_team = _file_info_value(wikitext, "featured2")
    license_name = _file_info_value(wikitext, "license").casefold()
    provenance = f"{note}\n{source}".casefold()
    expected_player = _compact(LIQUIPEDIA_PLAYER_PAGES.get(player_name, player_name))
    expected_team_tokens = {
        _compact(alias)
        for alias in PLAYER_MEDIA_TEAM_ALIASES.get(team_name, (team_name,))
    }
    identity_matches = _compact(featured_player) in {expected_player, _compact(player_name)}
    team_matches = _compact(featured_team) in expected_team_tokens
    team_authorized = bool(re.search(
        r"provided\s+by.*(?:representative|manager|coach|owner)|provided\s+by\s+(?:the\s+)?team",
        provenance,
    ))
    official_event_media = (
        "esports world cup/2026" in event_name.casefold()
        and "esports world cup" in copyright_name.casefold()
        and "medialobby.esportsfoundation.com" in source.casefold()
    )
    if (
        license_name != "permission"
        or not identity_matches
        or not team_matches
        or not (team_authorized or official_event_media)
    ):
        return None
    query = _fetch_json(
        _api_url(
            LIQUIPEDIA_COMMONS_API,
            action="query",
            titles=title,
            prop="imageinfo",
            iiprop="url|size|mime",
            format="json",
            formatversion=2,
        ),
        timeout,
    )
    pages = ((query.get("query") or {}).get("pages") or [])
    image_info = ((pages[0].get("imageinfo") or [None])[0] if pages else None)
    if not isinstance(image_info, dict):
        return None
    width = int(image_info.get("width") or 0)
    height = int(image_info.get("height") or 0)
    if width < 512 or height < 512 or width > height * 1.35:
        return None
    return {
        "url": str(image_info.get("url") or ""),
        "source_page": str(image_info.get("descriptionurl") or ""),
        "note": note,
        "source": source,
        "source_kind": "team_representative" if team_authorized else "official_event_media",
    }


def _global_commons_images(player_name: str, timeout: float) -> list[str]:
    """Search the Commons image index when the player page omits new media."""
    prefix = LIQUIPEDIA_PLAYER_PAGES.get(player_name, player_name).strip("`~- ")
    result = _fetch_json(
        _api_url(
            LIQUIPEDIA_COMMONS_API,
            action="query",
            list="allimages",
            aiprefix=prefix,
            ailimit=100,
            format="json",
            formatversion=2,
        ),
        timeout,
    )
    return [
        str(row.get("name") or "")
        for row in ((result.get("query") or {}).get("allimages") or [])
        if isinstance(row, dict) and row.get("name")
    ]


def download_ti_player_portrait(
    player_name: str,
    team_name: str,
    cache_dir: Path,
    *,
    timeout: float = 20,
) -> Dota2PlayerPortrait:
    """Download one provenance-verified current player portrait into a cache."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = _cache_slug(player_name)
    cached_image = cache_dir / f"{slug}.png"
    cached_metadata = cache_dir / f"{slug}.json"
    if cached_image.is_file() and cached_metadata.is_file():
        try:
            metadata = json.loads(cached_metadata.read_text(encoding="utf-8"))
            if not _metadata_matches_current_team(metadata, team_name):
                raise ValueError("cached portrait is not the current 2026 team asset")
            with Image.open(cached_image) as image:
                image.verify()
            portrait = Dota2PlayerPortrait(**metadata)
            _update_roster_slot(portrait, player_name, team_name)
            return portrait
        except (OSError, ValueError, TypeError, json.JSONDecodeError, UnidentifiedImageError):
            cached_image.unlink(missing_ok=True)
            cached_metadata.unlink(missing_ok=True)

    verified = _verified_portrait(player_name, team_name)
    if verified and str(verified.get("team_name") or "") == team_name:
        images = [str(verified["image_name"])]
    else:
        page = LIQUIPEDIA_PLAYER_PAGES.get(player_name, player_name)
        try:
            parsed = _fetch_json(
                _api_url(
                    LIQUIPEDIA_DOTA2_API,
                    action="parse",
                    page=page,
                    prop="images",
                    format="json",
                    formatversion=2,
                ),
                timeout,
            )
        except Exception as exc:
            _update_roster_slot(
                None,
                player_name,
                team_name,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        images = [str(name) for name in (parsed.get("parse") or {}).get("images", [])]
        if not _candidate_images(player_name, team_name, images):
            try:
                images.extend(_global_commons_images(player_name, timeout))
            except (OSError, ValueError, urllib.error.URLError):
                pass
    failures: list[str] = []
    candidates = (
        [str(verified["image_name"])]
        if verified and str(verified.get("team_name") or "") == team_name
        else _candidate_images(player_name, team_name, images)
    )
    for image_name in candidates:
        if verified and image_name == verified.get("image_name"):
            metadata = dict(verified)
        else:
            try:
                metadata = _official_commons_metadata(
                    image_name, player_name, team_name, timeout
                )
            except (OSError, ValueError, urllib.error.URLError):
                failures.append(f"{image_name}: metadata")
                continue
        if not metadata or not metadata["url"]:
            continue
        try:
            raw = b""
            last_download_error: Exception | None = None
            for attempt in range(2):
                request = urllib.request.Request(metadata["url"], headers={"User-Agent": USER_AGENT})
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        raw = response.read(MAX_PLAYER_PORTRAIT_BYTES + 1)
                    break
                except (OSError, urllib.error.URLError) as exc:
                    last_download_error = exc
                    if attempt == 0:
                        time.sleep(0.5)
            if not raw and last_download_error is not None:
                curl = shutil.which("curl")
                if curl:
                    completed = subprocess.run(
                        [
                            curl, "-L", "--compressed", "--fail", "--silent", "--show-error",
                            "--max-time", str(max(1, int(timeout))),
                            "--user-agent", USER_AGENT, metadata["url"],
                        ],
                        capture_output=True,
                        check=False,
                    )
                    if completed.returncode == 0:
                        raw = completed.stdout
                if not raw:
                    raise last_download_error
            if not raw or len(raw) > MAX_PLAYER_PORTRAIT_BYTES:
                raise ValueError("portrait is empty or too large")
            temporary = cached_image.with_suffix(".tmp")
            temporary.write_bytes(raw)
            with Image.open(temporary) as image:
                normalized = image.convert("RGBA")
                if normalized.width < 512 or normalized.height < 512:
                    raise ValueError("portrait is too small")
                normalized.save(cached_image, format="PNG")
            temporary.unlink(missing_ok=True)
        except (OSError, ValueError, urllib.error.URLError, UnidentifiedImageError) as exc:
            cached_image.unlink(missing_ok=True)
            failures.append(f"{image_name}: {type(exc).__name__}")
            continue
        portrait = Dota2PlayerPortrait(
            player_name=player_name,
            team_name=team_name,
            path=str(cached_image),
            source_url=metadata["url"],
            source_page=metadata["source_page"],
            source_note=metadata["note"],
            image_name=image_name,
            source_kind=metadata["source_kind"],
        )
        cached_metadata.write_text(
            json.dumps(asdict(portrait), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _update_roster_slot(portrait, player_name, team_name)
        return portrait
    suffix = f" ({'; '.join(failures[:3])})" if failures else ""
    message = f"未找到可核验的当前 {player_name} 官方选手照{suffix}"
    _update_roster_slot(None, player_name, team_name, error=message)
    raise ValueError(message)
