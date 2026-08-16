"""Deterministic broadcast-style renderer for TI recording covers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


PHASE_LABELS = {
    "group_stage": "瑞士轮",
    "elimination_round": "淘汰轮",
    "intermission": "休赛日",
    "main_event": "主赛事",
    "grand_final": "总决赛",
}


def _font(root: Path, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        root / "fonts" / ("NotoSansCJKsc-Bold.otf" if bold else "NotoSansCJKsc-Regular.otf"),
        root / "fonts" / "NotoSansCJKsc-Regular.otf",
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, root: Path, max_width: int, start: int) -> ImageFont.ImageFont:
    size = start
    while size > 20:
        font = _font(root, size, bold=True)
        if draw.textbbox((0, 0), text, font=font, stroke_width=1)[2] <= max_width:
            return font
        size -= 2
    return _font(root, 20, bold=True)


def _center_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.ImageFont,
                 fill: str, *, stroke_width: int = 0, stroke_fill: str = "#000000") -> None:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    draw.text((xy[0] - (box[2] - box[0]) / 2, xy[1] - (box[3] - box[1]) / 2), text,
              font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def _team_logo(root: Path, team: str) -> Path | None:
    try:
        from ti2026_context import TI2026_TEAMS
        row = next((item for item in TI2026_TEAMS if item["name"] == team), None)
        path = root / str((row or {}).get("logo_path") or "")
        return path if row and path.is_file() else None
    except Exception:
        return None


def _paste_contain(canvas: Image.Image, path: Path, box: tuple[int, int, int, int]) -> None:
    with Image.open(path) as source:
        layer = source.convert("RGBA")
        layer.thumbnail((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
        x = box[0] + (box[2] - box[0] - layer.width) // 2
        y = box[1] + (box[3] - box[1] - layer.height) // 2
        canvas.alpha_composite(layer, (x, y))


def _lineups(match: dict[str, Any], opponents: list[str]) -> tuple[dict[str, list[str]], dict[str, Any] | None]:
    games = [row for row in match.get("games", []) if isinstance(row, dict)]
    if games:
        game = games[-1]
        result = {team: [] for team in opponents}
        for player in game.get("players", []):
            team = str(player.get("team") or "")
            hero = str(player.get("hero_name") or player.get("hero_internal_name") or "")
            if team in result and hero:
                result[team].append(hero)
        return result, game
    maps = [row for row in match.get("liquipedia_maps", []) if isinstance(row, dict)]
    picked = next((row for row in reversed(maps) if row.get("team1_heroes") or row.get("team2_heroes")), {})
    return {
        opponents[0]: list(picked.get("team1_heroes") or []),
        opponents[1]: list(picked.get("team2_heroes") or []),
    }, None


def render_ti_cover(
    background_path: Path,
    output_path: Path,
    *,
    app_root: Path,
    tournament_context: dict[str, Any],
    tournament_match: dict[str, Any],
    headline: str,
    hero_cache_dir: Path,
) -> dict[str, Any]:
    """Overlay the fixed TI layout on an AI-created textless background."""
    from dota2_heroes import download_dota2_hero_image, find_official_dota2_hero
    from ti2026_context import ti2026_match_round_label, ti2026_match_series_format

    with Image.open(background_path) as source:
        base = source.convert("RGB")
    width, height = base.size
    scale = width / 1600
    base = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(max(1, int(2 * scale))))).enhance(0.42)
    canvas = base.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    opponents = [str(v) for v in tournament_match.get("opponents", []) if str(v)][:2]
    if len(opponents) != 2:
        raise ValueError("TI cover requires exactly two verified opponents")
    lineups, game = _lineups(tournament_match, opponents)
    confirmed = tournament_match.get("status") == "confirmed" and game is not None

    # Red/green atmospheric halves and dark readability veil.
    draw.rectangle((0, 0, width // 2, height), fill=(100, 0, 0, 52))
    draw.rectangle((width // 2, 0, width, height), fill=(0, 84, 54, 52))
    draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 32))

    banner = (int(390*scale), int(28*scale), int(1210*scale), int(92*scale))
    draw.polygon([(banner[0], banner[1]), (banner[2], banner[1]), (banner[2]+45*scale, (banner[1]+banner[3])//2),
                  (banner[2], banner[3]), (banner[0], banner[3]), (banner[0]-45*scale, (banner[1]+banner[3])//2)],
                 fill=(70, 4, 2, 235), outline=(218, 166, 75, 255), width=max(2, int(3*scale)))
    phase = PHASE_LABELS.get(str(tournament_context.get("phase") or ""), "瑞士轮")
    round_label = ti2026_match_round_label(tournament_match.get("round_label")) or phase
    series_format = ti2026_match_series_format(
        tournament_context, tournament_match
    ).upper()
    _center_text(draw, (width//2, int(60*scale)), f"TI 2026 · {round_label} · {series_format}",
                 _font(app_root, int(34*scale), bold=True), "#F8E5B5", stroke_width=max(1, int(scale)), stroke_fill="#2B0903")

    clean_headline = " ".join(str(headline or "TI 赛场焦点").split())
    title_font = _fit_font(draw, clean_headline, app_root, int(width*0.82), int(83*scale))
    _center_text(draw, (width//2, int(170*scale)), clean_headline, title_font, "#F8E1AD",
                 stroke_width=max(2, int(4*scale)), stroke_fill="#160A03")

    # Featured official hero portrait, never a generated human face.
    standout = (game.get("performance_candidates") or [None])[0] if game else None
    # Pending matches may still use one hero from the verified BP as neutral
    # key art. This does not attribute the hero to a player or imply a result.
    featured_name = str(
        (standout or {}).get("hero_name")
        or next(iter(lineups.get(opponents[0], [])), "")
    )
    featured = find_official_dota2_hero(featured_name) if featured_name else None
    if featured:
        try:
            portrait_path = download_dota2_hero_image(featured, hero_cache_dir)
            with Image.open(portrait_path) as hero_source:
                hero = ImageOps.fit(hero_source.convert("RGBA"), (int(520*scale), int(300*scale)), Image.Resampling.LANCZOS)
                hero.putalpha(ImageEnhance.Contrast(hero.getchannel("A")).enhance(1.3))
                canvas.alpha_composite(hero, (int(540*scale), int(270*scale)))
        except Exception:
            pass

    for index, team in enumerate(opponents):
        x0 = int((105 if index == 0 else 1280)*scale)
        logo = _team_logo(app_root, team)
        if logo:
            _paste_contain(canvas, logo, (x0, int(285*scale), x0+int(215*scale), int(500*scale)))

    game_number = int((game or {}).get("game_number") or tournament_match.get("game_number") or 1)
    score = tournament_match.get("series_score") or {}
    score_text = f"{int(score.get(opponents[0], 0))}:{int(score.get(opponents[1], 0))}" if confirmed else "进行中"
    draw.rounded_rectangle((int(425*scale), int(550*scale), int(1175*scale), int(706*scale)), radius=int(22*scale),
                           fill=(8, 11, 10, 238), outline=(206, 158, 68, 255), width=max(2, int(4*scale)))
    _center_text(draw, (width//2, int(570*scale)), f"GAME {game_number}", _font(app_root, int(28*scale), bold=True), "#F2D99B")
    _center_text(draw, (int(570*scale), int(635*scale)), opponents[0], _fit_font(draw, opponents[0], app_root, int(240*scale), int(42*scale)), "white")
    score_size = 68 if confirmed else 42
    _center_text(draw, (width//2, int(642*scale)), score_text, _font(app_root, int(score_size*scale), bold=True), "#F5D797")
    _center_text(draw, (int(1030*scale), int(635*scale)), opponents[1], _fit_font(draw, opponents[1], app_root, int(240*scale), int(42*scale)), "white")

    # Exact official 5+5 portraits in the lower strip.
    strip_top, strip_bottom = int(720*scale), height-int(24*scale)
    center_gap = int(190*scale)
    half_width = (width-center_gap)//2
    cell_width = half_width//5
    rendered: dict[str, list[str]] = {team: [] for team in opponents}
    for side, team in enumerate(opponents):
        start_x = 0 if side == 0 else half_width+center_gap
        for col, name in enumerate(lineups.get(team, [])[:5]):
            hero = find_official_dota2_hero(str(name))
            if not hero:
                continue
            try:
                path = download_dota2_hero_image(hero, hero_cache_dir)
                with Image.open(path) as portrait_source:
                    portrait = ImageOps.fit(portrait_source.convert("RGBA"), (cell_width, strip_bottom-strip_top), Image.Resampling.LANCZOS)
                    canvas.alpha_composite(portrait, (start_x+col*cell_width, strip_top))
                rendered[team].append(hero.english_name)
            except Exception:
                continue
        color = (239, 67, 30, 255) if side == 0 else (42, 226, 147, 255)
        draw.rectangle((start_x, strip_top, start_x+half_width, strip_bottom), outline=color, width=max(2, int(4*scale)))

    kills = "待确认"
    if confirmed:
        left = int(game.get("radiant_score") or 0) if game.get("radiant") == opponents[0] else int(game.get("dire_score") or 0)
        right = int(game.get("radiant_score") or 0) if game.get("radiant") == opponents[1] else int(game.get("dire_score") or 0)
        kills = f"{left}:{right}"
    cx0, cx1 = half_width, half_width+center_gap
    draw.rounded_rectangle((cx0+int(8*scale), strip_top+int(25*scale), cx1-int(8*scale), strip_bottom-int(10*scale)),
                           radius=int(12*scale), fill=(4, 7, 7, 245), outline=(217, 169, 78, 255), width=max(2, int(3*scale)))
    strip_height = strip_bottom - strip_top
    _center_text(draw, (width//2, int(strip_top + strip_height * 0.30)), "本局击杀", _font(app_root, int(24*scale), bold=True), "#F1D797")
    _center_text(draw, (width//2, int(strip_top + strip_height * 0.69)), kills, _font(app_root, int(42*scale), bold=True), "#FFFFFF")

    if confirmed and standout:
        kda = f"KDA {standout.get('kills', 0)}/{standout.get('deaths', 0)}/{standout.get('assists', 0)}"
        draw.rounded_rectangle((int(610*scale), int(495*scale), int(990*scale), int(540*scale)), radius=int(14*scale), fill=(0, 0, 0, 190))
        _center_text(draw, (width//2, int(515*scale)), f"{standout.get('name') or '焦点选手'} · {kda}",
                     _font(app_root, int(23*scale), bold=True), "#F7E3AC")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, quality=94, subsampling=0)
    return {"confirmed": confirmed, "game_number": game_number, "kills": kills, "lineups": rendered,
            "featured_hero": featured.english_name if featured else ""}
