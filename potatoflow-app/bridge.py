#!/usr/bin/env python3
"""Bridge finalized recorder segments to PotatoFlow uploaders."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from danmaku_pipeline import (
    batch_summary_comments,
    build_ass,
    burn_ass,
    inspect_danmaku_xml,
    parse_danmaku_xml,
    probe_video_size,
    select_summary_comments,
)
from dota2_abilities import (
    build_dota2_ability_reference_sheet,
    dota2_ability_prompt_instruction,
    match_dota2_abilities,
)
from dota2_items import (
    build_dota2_item_reference_sheet,
    dota2_item_prompt_instruction,
    match_dota2_items,
)
from dota2_heroes import build_dota2_hero_reference, find_official_dota2_hero
from runtime_environment import configure_linux_ca_environment

VIDEO_EXTENSIONS = {".mp4", ".flv", ".mkv", ".webm", ".ts", ".m2ts", ".mov"}
_IMAGE_GENERATION_THREAD_LOCK = threading.Lock()
_MULTIPART_SESSION_THREAD_LOCKS = tuple(threading.Lock() for _ in range(64))


def _queue_lock_is_busy(exc: OSError) -> bool:
    """Return true only for the lock-contention errors worth retrying."""
    return (
        exc.errno in {11, 13, 35, 36}
        or getattr(exc, "winerror", None) in {33, 36}
    )


def safe_task_error_detail(error: Any, limit: int = 800) -> str:
    """Keep actionable provider errors while redacting credentials."""
    text = re.sub(r"\s+", " ", str(error or "")).strip()
    if not text:
        return "未知错误"
    redactions = (
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[redacted]"),
        (r"(?i)((?:api[_ -]?key|token|cookie)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]"),
        (r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]"),
    )
    for pattern, replacement in redactions:
        text = re.sub(pattern, replacement, text)
    return text[: max(80, int(limit))]


def ai_batch_error_summary(errors: Any) -> str:
    """Format every failed AI batch for task details without exposing secrets."""
    items = errors if isinstance(errors, list) else []
    summaries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        prefix = f"批次 {index}" if index not in (None, "") else "批次"
        summaries.append(f"{prefix}: {safe_task_error_detail(item.get('error'), 320)}")
    return "；".join(summaries)[:800]


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    """Prevent FFmpeg/ffprobe helper consoles in the Windows desktop build."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
DEFAULT_TITLE_TEMPLATE = "{streamer}｜{ai_topic}｜{date}"
DEFAULT_DESCRIPTION_TEMPLATE = "{recording_intro}"
RECORDING_TITLE_TOPIC_LIMIT = 48
RECORDING_COVER_TEXT_PREFERRED_LIMIT = 24
RECORDING_COVER_TEXT_HARD_LIMIT = 28
DEFAULT_RECORDING_TITLE_AI_PROMPT = (
    "根据本段直播的实际内容和弹幕反应，从有精确证据的重要事件中提炼一个自然、"
    "有信息量、语义完整的标题。默认选择一个最强事件；若最终简介中确有两个同等重要、彼此独立且都值得展示的事件，"
    "允许在48字内写入两个，并按重要性排序、使用中文分号“；”明确分隔。不得加入第三个事件，不得堆砌无关关键词。"
    "标题优先写清具体人物、动作、对象、变化、反差或结果；完整信息优先，不追求越短越好，普通录播通常用22至44字，"
    "一小时长录播在证据充足时通常用30至46字保留主线、转折和结果；只有有效内容确实稀疏时才可更短，"
    "绝不能为凑字补猜，无论长短都要把事件说完整。主播确实参与、观战、评价或本身是话题对象时，才将其标题首选名自然融入事件句；"
    "主播只是房间归属或背景时不要写，不能为统一格式强塞主播名。"
    "不得使用“主播名｜事件”或“主播名：事件”这种标签格式。突出关键对局、英雄、事件或节目效果，"
    "不使用夸张的虚假结论；标题中的核心事件必须同时进入重要时间点。"
    "禁止用“引发热议”“引起争议”“出装引争议”“被指”“被曝”“据称”“被吐槽”“被喷”“被赞完美适配”"
    "“争议话题”“直播精彩内容”等空泛、营销式评价代替具体事件；"
    "必须直接写清具体动作、选择、结果或节目效果。游戏内容应让读者自然看出游戏语境："
    "DOTA2 默认通过英雄、装备、模式、比赛或选手体现，不机械写“DOTA2”；其他游戏只有可靠识别且"
    "不写名称就难以理解事件时，才自然带上游戏名，无法可靠识别时不得猜测。"
    "不要包含日期、时间和“直播回放”，完整标题最多48个字符；超长时重新改写，绝不能直接截断半句话。"
    "选题优先级依次为：明确结果或反差、关键操作或决定、阶段性转折、可复述的节目效果、信息明确的重要讨论。"
    "当标题选中的游戏时间点能唯一落入 verified_live_context.game_segments，且该时间点描述的正是主播当局操作、"
    "出装、团战、推进或结果时，必须把主播标题名和该段已确认英雄自然写入标题，至少一次交代清楚‘谁用什么英雄’；"
    "这属于人物事实，不是机械添加房间名。"
    "若结构化 GSI 只确认某局已结束，它只用于切开前后对局，不能单独生成‘本局结束/转入下一局’这类低信息标题；"
    "只有已核验时间点同时明确胜负、翻盘、基地告破或决定性收尾时，才把具体结束结果提升为标题优先项。"
    "不要仅因弹幕数量多就选择缺少具体事件的情绪词；不要把简介中两个相隔较远的时间点拼成一句；"
    "不要用直播间默认标题补充简介没有的游戏、人物或结论。若多个事件强度接近，选择人物动作和结果最完整、"
    "证据最集中、读者脱离上下文也能理解的一项。本录播虽然以弹幕观看体验为重点，但标题不需要每条都出现"
    "“弹幕”或“观众”；先写完整事件，删除观众反应后仍不影响主线时，优先不写。只有观众反应本身推动剧情、"
    "形成明显反差或成为核心笑点时，才在事件之后自然写出“弹幕提醒/催促/刷屏/调侃”的具体内容；"
    "同一标题最多保留一处观众反应，不得只写没有对象的“弹幕热议/讨论”。"
    "简介能用连续明确证据确认一起玩、组队或对战关系时，优先写清“谁和谁做了什么”；只因两个人名同时出现，"
    "或其中一人只是观战、被提及，不得猜测为一起玩。"
    "现实人物状态、签约、收入、婚恋、疾病、违法等高风险内容中，较负面、未经证实、可能损害人物"
    "名誉的现实传言不得入题，不能通过添加“弹幕称”继续保留；中性现实消息使用“观众讨论”，"
    "明显玩笑使用“直播间调侃”。"
    "现场可见的游戏表现和比赛结果直接陈述，不得套用“直播间热议/讨论”等房间前缀。"
    "结构化 GSI 已确认主播操作时，标题不得以“观众讨论、弹幕认为、直播间质疑”开头；"
    "不得把“结束后转入下一局、进入某英雄对局”写进标题占用篇幅，应直接写各局具体动作、转折或结果。"
    "同时给出 cover_text：只压缩标题中排序第一的核心事件，证据充足时优先16至24字、最多28字，可比投稿标题短；"
    "应保留人物或英雄、关键装备或阶段、核心动作、转折或结果中至少两类有区分度的信息，不要只剩一个泛化动作；"
    "有效内容确实稀疏时可用8至15字。不得新增人物、动作、结果或来源，不得强塞主播名，"
    "负面现实传言不得进入封面文案。无法安全压缩时返回空字符串。"
)
DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT = (
    "生成尽可能完整、可直接用于哔哩哔哩投稿的时间点式中文简介。最终正文只保留程序核验并格式化后的"
    "“时间 + 事件”行，不添加开场白、总结段、栏目标题或营销套话。详细是指覆盖更多有独立信息增量的可靠看点，"
    "不是把单条写得冗长，也不是为了达到数量重复同一事件或收录普通闲聊。候选事件必须按直播时间向前推进："
    "覆盖开场状态、中段关键变化、重要互动或节目效果，以及后段发展、结果或复盘；不要为了突出标题打乱顺序，"
    "也不要忽略真正发生的话题、游戏、节目环节或参与人物转换。"
    "每条事件使用一到两句完整中文，优先写成“明确人物或对象 + 具体动作/选择 + 变化、结果或观众反应”；"
    "只写证据能支持的最小完整事实，但在证据同时支持时应保留有助理解的英雄、装备、模式、对阵、阶段、数字和结果。"
    "叙述中要写清人物主语：包括当前直播间主播，以及弹幕内容确实提到的其他主播、选手或嘉宾。"
    "其他人物必须有可靠原文证据并能明确消歧；不得把弹幕用户名、模糊外号或同名对象写成视频人物。"
    "按5W1H检查每个关键事件：何时由程序定位，正文写清谁、做了什么、处于什么场景或游戏阶段、为何发生、"
    "如何发展以及结果；输入没有地点、原因或结果证据时宁可省略，绝不能用常识补齐。"
    "若连续明确证据能够确认人物之间是一起玩、同队配合、互为对手、接力或只是观战，简介必须写清这种关系，"
    "并在关系或参与者变化时另列时间点；不能只列多个人名，也不能把同时出现误写成共同游玩。"
    "装备事实与装备归属必须分开判断：弹幕明确提到购买、未购买、替换、攻击或使用某件 Dota 2 装备时，"
    "应在对应时间点保留装备名；证据不能确认属于谁时写成本局、某英雄或某一方，不能为保守而把装备事实整条删掉，"
    "也不能反过来把装备强行归到当前主播。"
    "确实发生的赛后复盘、回看失误或自我调侃可以作为节目效果写入，但必须说明复盘了什么，不得将“赛后复盘”"
    "当成空泛栏目词。避免“直播精彩内容、引发热议、争议话题、气氛热烈、节目效果拉满”等不说具体事件的句子。"
    "不要在简介正文中手写时间点；程序会回到完整 XML 定位最早证据、补偿反应延迟并统一格式化。"
    "重要事件必须有可在完整 XML 定位的弹幕原文，或同一时间围绕事件关键词的集中刷屏；"
    "不要求多条引用全部逐字一致，但不得编造时间或事件。"
    "仅由弹幕支持的评价、传闻和现实状态必须保留来源限定；较负面、未经证实、可能损害人物名誉的"
    "现实传言直接删除，不得通过添加“弹幕称”继续保留；中性现实消息使用“观众讨论”，明显玩笑使用“直播间调侃”；"
    "可靠上下文已经直接确认的画面事件、结构化游戏事实和明确结果则直接陈述，不要一律退化成“弹幕讨论”。"
    "无法确认人物时使用“队伍、本局、画面中的角色”等中性主语；不能把队友、对手、观战对象或第三方选手的动作"
    "写到当前主播身上。无法可靠识别的游戏名、英雄、装备、Role 编号、内部 ID、未知占位符和解析失败数据不得输出。"
    "重要时间点必须覆盖简介中的关键事件；若简介包含两个先后发生的独立转折，必须分别收录，不得拼成一条虚假因果链。"
    "事件文案只做证据的最小忠实改写，不得增加原文没有的人物、动作、数字、原因或结果；"
    "只有证据的绝对时间确实位于整段录制最初一分钟，且内容明显承接此前进程时，才可写“开场承接”；"
    "分析批次的第一条不等于录播开场，后续批次严禁使用“开场”措辞。"
    "时间戳已经能表示阶段，事件正文默认不写“前段/中段/后段”；确有必要时只能依据整段录制的绝对时间三等分，"
    "不得依据当前分析批次内的相对位置判断。"
    "只使用输入能够支持的事实，不虚构主播"
    "原话、比赛结果或人物。不要出现文件名、任务编号、内部路径和机械化套话，不超过1800字。"
)
DOTA2_METADATA_DISAMBIGUATION = (
    "Dota 2 术语消歧：弹幕或直播内容中的“老奶奶”指英雄"
    "电炎绝手（Snapfire），不得理解为普通老年女性。"
)
DEFAULT_RECORDING_COVER_AI_PROMPT = (
    "画面精致、主体明确、对比强烈，在缩略图尺寸下仍清晰。"
    "将指定标题作为唯一封面短文案。必须逐字保留，不得改写、重复、漏字。"
    "不得增加副标题、日期、栏目名或宣传语。根据标题长度使用一至三行。"
    "文字区最多占画面约四成。文字不得遮挡人物、贴边或被裁切。"
    "自定义要求可以补充画风、配色和氛围。"
)
APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
YYF_STREAMER_ALIASES = {
    "yyf", "yyfyyf", "月夜枫", "枫哥", "峰哥", "姜岑", "FG", "胖头", "胖头鱼"
}
GUOXIAOGUO_COVER_REFERENCE = (
    APP_ROOT / "assets" / "streamer-references" / "guoxiaoguo.png"
)
GUOXIAOGUO_STREAMER_ALIASES = {"果小果", "果小果是个弟弟"}
XIEBIN_DD_COVER_REFERENCE = (
    APP_ROOT / "assets" / "streamer-references" / "xiebin-dd.png"
)
GUOMIN_DAJIUGE_COVER_REFERENCE = (
    APP_ROOT / "assets" / "streamer-references" / "guomin-dajiuge.png"
)
GUOMIN_DAJIUGE_STREAMER_ALIASES = {"国民大舅哥", "大舅哥", "182102"}
DOTA2_STREAMER_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "YYF",
        (
            "YYF",
            "yyfyyf",
            "月夜枫",
            "枫哥",
            "峰哥",
            "姜岑",
            "FG",
            "胖头",
            "胖头鱼",
            "石佛",
            "僵尸王",
            "毒瘤枫",
            "吃人枫",
            "姜瘤儿",
        ),
    ),
    ("BurNIng", ("BurNIng", "B神", "徐志雷")),
    ("xiao8", ("xiao8", "小八", "八师傅", "张宁")),
    ("Zhou", ("Zhou", "Zhou陈尧", "周神", "雕哥", "鲷哥", "陈尧")),
    ("Hao", ("Hao", "豪哥", "猴", "HAOB", "陈智豪")),
    ("Mu", ("Mu", "Mu神", "张盼")),
    ("Faith_bian", ("Faith_bian", "faithbian", "小明鞭", "张睿达")),
    ("Somnus", ("Somnus", "Maybe", "超哥", "路垚")),
    ("Chalice", ("Chalice", "查理斯", "查猪", "杨沈仪")),
    ("fy", ("fy", "fy神", "徐林森")),
    ("Ame", ("Ame", "萧瑟", "王淳煜")),
    ("XinQ", ("XinQ", "赵子星")),
    ("Sccc", ("Sccc", "军体拳", "宋淳")),
    ("Paparazi", ("Paparazi", "Eurus", "拒绝者", "张成俊")),
    ("Monet", ("Monet", "圣子华炼", "杜鹏")),
    ("Ori", ("Ori", "曾焦阳")),
    ("Dy", ("Dy", "丁聪")),
    ("Kaka", ("Kaka", "卡卡", "胡良智")),
    ("LaNm", ("LaNm", "国土", "张志成")),
    ("LongDD", ("LongDD", "龙神", "龙弟弟", "黄翔")),
    ("820", ("820", "八二零", "邹倚天")),
    ("DDC", ("DDC", "大狗", "梁发")),
    ("PIS", ("PIS", "Pis", "姚羿成")),
    ("Inflame", ("Inflame", "小书童", "何雍正")),
    (
        "国民大舅哥",
        ("国民大舅哥", "国名大舅哥", "白毛", "大舅哥"),
    ),
    (
        "川神",
        (
            "川神",
            "老菜",
            "老蔡",
            "蔡哥",
            "菜哥",
            "老陈",
            "叫我老陈就好了",
        ),
    ),
    (
        "DD",
        (
            "DD",
            "谢彬DD",
            "谢彬",
            "谢斌",
            "奶哥",
            "奶哥哥",
            "奶子D",
            "奶D",
            "彬子",
        ),
    ),
    (
        "Sylar",
        (
            "Sylar",
            "刘嘉俊Sylar1",
            "刘嘉俊",
            "塞拉",
            "眼哥",
            "眼少",
            "眼神",
            "眼子",
            "眼醋",
            "小眼哥",
            "0.0",
        ),
    ),
    (
        "ZSMJ",
        (
            "ZSMJ",
            "龚建ZSMJ",
            "龚建",
            "诸司马技",
            "马甲哥",
            "马甲",
            "甲哥",
            "这是马甲",
            "左手摸鸡",
            "蛛丝马迹",
        ),
    ),
    (
        "Doinb",
        ("Doinb", "doinb", "金泰相", "金咕咕", "硬币哥", "毒硬币"),
    ),
    (
        "icon",
        ("icon", "冷少icon", "冷少", "谢天宇", "葬爱冷少", "峡谷天天"),
    ),
    ("H4cker", ("H4cker", "骇客H4cker", "骇客", "杨志浩", "MopPeT")),
    ("MacSed", ("MacSed", "igmacsed", "sed")),
    ("ZippO", ("ZippO", "ZippO宝哥", "宝哥")),
)

# 斗鱼 DOTA2 宝可梦 S7（2026-07-25 至 2026-08-04）页面列出的成员。
# 用户确认这些活动成员昵称无需赛事语境；标题、简介或其他作图文案提到即可触发。
DOTA2_POKEMON_PARTICIPANT_ALIAS_GROUPS: tuple[
    tuple[str, tuple[str, ...]], ...
] = (
    ("SupKing", ("右手supking", "SupKing", "supking", "右手")),
    ("石页", ("石页的第一根矛s", "石页", "石业", "第一根矛")),
    (
        "叁肆叁肆",
        (
            "叁肆叁肆",
            "3434",
            "狗哥",
            "三生三世",
            "三生三生",
            "王兆辉",
            "狗妹",
        ),
    ),
    (
        "果小果",
        (
            "果小果是个弟弟",
            "果小果",
            "果神",
            "果宝",
            "狗小狗",
            "烈焰杀神",
        ),
    ),
    ("蛋饼", ("蛋饼", "饼子")),
    ("霸气", ("霸气", "霸气虚幻哥", "霸气虚幻哥1991", "虚幻哥")),
    ("aq", ("aq",)),
    ("阿雅Midori", ("阿雅Midori", "Midori", "阿雅", "雅醋")),
    ("奥特慢", ("奥特慢", "奥特曼")),
    ("是希文吖", ("是希文吖", "希文")),
    ("艾琳", ("艾琳bigbaby", "艾琳", "bigbaby")),
    ("哎呀朝朝", ("哎呀朝朝", "朝朝")),
    ("三酒", ("三酒", "三九")),
    ("塔莉娅", ("塔莉娅", "塔利亚", "塔醋", "塔宝")),
    ("南枫", ("南枫",)),
    ("憨憨", ("憨憨",)),
    ("Spirit小蝴蝶", ("Spirit小蝴蝶", "Spirit", "小蝴蝶")),
    ("小芳FLo", ("小芳FLo", "FLo")),
    ("牙牙OMO", ("牙牙OMO", "OMO")),
    ("蛋糕", ("蛋糕", "糕神")),
    ("炸毛张", ("炸毛张", "毛张")),
    ("小刘", ("小刘", "刘神")),
    ("林仔", ("林仔",)),
    ("白仔啊", ("白仔啊", "白仔")),
    ("顾非池", ("顾非池",)),
    ("甜瓜", ("甜瓜",)),
    ("林九鸽", ("林九鸽", "林九哥", "九鸽", "零九鸽")),
    ("小300TwT", ("小300TwT", "小300")),
    ("哈哈明", ("faith", "哈哈明")),
    ("艾斯yoona", ("艾斯yoona", "yoona", "大猛一")),
    ("moon", ("moon",)),
    ("煊宝", ("煊宝",)),
    ("一只蘇I", ("一只蘇I", "一只苏I", "一只苏")),
    ("小琳达Linda", ("小琳达Linda", "小琳达", "小linda", "Linda")),
)

DOTA2_STREAMER_AVATAR_SEARCH_NAMES: dict[str, str] = {
    "YYF": "yyfyyf",
    "Zhou": "Zhou陈尧",
    "DD": "谢彬DD",
    "川神": "叫我老陈就好了",
    "Sylar": "刘嘉俊Sylar1",
    "ZSMJ": "龚建ZSMJ",
    "Doinb": "doinb",
    "icon": "冷少icon",
    "Chalice": "chalice",
    "MacSed": "igmacsed",
    "H4cker": "骇客H4cker",
    "SupKing": "右手supking",
    "ZippO": "ZippO宝哥",
    "Hao": "hao",
    "哈哈明": "faith",
    **{
        canonical_name: aliases[0]
        for canonical_name, aliases in DOTA2_POKEMON_PARTICIPANT_ALIAS_GROUPS
    },
    "蛋饼": "保护我方蛋饼",
}

# Room 9999 activity page (`DOTA2BKMS7`, pageId 58321) publishes these room IDs
# in the same order as the participant grid. “Spirit小蝴蝶”和“小蝴蝶”是同一人，
# 活动页重复展示且两个入口都指向房间 448014，因此这里只保留一个身份。
DOTA2_POKEMON_PARTICIPANT_ROOM_IDS: dict[str, str] = {
    "YYF": "9999",
    "Zhou": "88660",
    "DD": "110",
    "川神": "74960",
    "Sylar": "762484",
    "ZSMJ": "52876",
    "Doinb": "252140",
    "石页": "593392",
    "icon": "8682569",
    "Chalice": "5135383",
    "果小果": "6558897",
    "MacSed": "7546",
    "H4cker": "7314971",
    "SupKing": "316022",
    "叁肆叁肆": "312407",
    "ZippO": "67554",
    "蛋饼": "8758901",
    "霸气": "73965",
    "aq": "7718843",
    "阿雅Midori": "9667590",
    "奥特慢": "10198618",
    "是希文吖": "1334765",
    "艾琳": "10639765",
    "哎呀朝朝": "9105451",
    "三酒": "2421040",
    "塔莉娅": "6770423",
    "南枫": "3436094",
    "Hao": "8445951",
    "憨憨": "11180817",
    "Spirit小蝴蝶": "448014",
    "小芳FLo": "6752",
    "牙牙OMO": "10577834",
    "蛋糕": "4067868",
    "炸毛张": "209737",
    "小刘": "52887",
    "林仔": "500269",
    "白仔啊": "1759181",
    "顾非池": "5315665",
    "甜瓜": "8702345",
    "林九鸽": "10970886",
    "小300TwT": "8489391",
    "哈哈明": "331437",
    "艾斯yoona": "10229065",
    "moon": "12874381",
    "煊宝": "7828414",
    "一只蘇I": "895712",
    "小琳达Linda": "6188551",
}


def _all_dota2_streamer_alias_groups() -> tuple[
    tuple[str, tuple[str, ...]], ...
]:
    return DOTA2_STREAMER_ALIAS_GROUPS + DOTA2_POKEMON_PARTICIPANT_ALIAS_GROUPS


def _dota2_streamer_alias_groups_for_content(
    *content: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return _all_dota2_streamer_alias_groups()


def _compact_alias(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def normalize_dota2_streamer_name(streamer: str) -> str:
    """Return a stable public name for known Dota 2 streamer aliases."""
    original = str(streamer or "").strip()
    normalized = _compact_alias(original)
    if re.fullmatch(r"yyf(?:yyf)?\d*", normalized):
        return "YYF"
    if normalized.startswith("果小果"):
        return "果小果"
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        if any(normalized == _compact_alias(alias) for alias in aliases):
            return canonical_name
    return original


def preferred_recording_title_name(streamer: str, *verified_content: str) -> str:
    """Choose YYF's editorial nickname only from owner-specific verified context."""
    normalized = normalize_dota2_streamer_name(streamer)
    if normalized == "DD":
        return "奶哥"
    if normalized != "YYF":
        return normalized
    poor_play = re.compile(
        r"(?:暴毙|白给|送了|送掉|阵亡|连续失误|操作失误|空大|刮痧|卡关|"
        r"(?:零|0)输出|打不过|没打过|乱拿|乱选|没选职业|没有破盾)",
        re.IGNORECASE,
    )
    yyf_alias = re.compile(r"(?:YYF|yyfyyf|枫哥|月夜枫|胖头|胖头鱼|姜岑)", re.I)
    for value in verified_content:
        for line in str(value or "").splitlines():
            owner_match = yyf_alias.search(line)
            poor_match = poor_play.search(line)
            if not owner_match or not poor_match:
                continue
            relation_span = line[
                min(owner_match.start(), poor_match.start()):
                max(owner_match.end(), poor_match.end())
            ]
            if len(relation_span) <= 40 and not re.search(
                r"(?:观战|观赛|旁观|OB|解说|点评)", relation_span, re.I
            ):
                return "胖头"
    return "枫哥"


def recording_cover_subject_name(streamer: str, *content: str) -> str:
    """Return the room owner's editorial name used by this segment's cover."""
    normalized = normalize_dota2_streamer_name(streamer)
    if not normalized or normalized == "主播":
        return ""

    owner_aliases: tuple[str, ...] = ()
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        if canonical_name == normalized:
            owner_aliases = aliases
            break

    for value in content:
        text = str(value or "")
        folded = text.casefold()
        matches: list[tuple[int, str]] = []
        for alias in owner_aliases:
            alias_folded = alias.casefold()
            if re.fullmatch(r"[a-z][a-z0-9_ -]*", alias_folded):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                    folded,
                )
                if found:
                    matches.append((found.start(), alias))
            else:
                position = folded.find(alias_folded)
                if position >= 0:
                    matches.append((position, alias))
        if matches:
            matched_alias = min(matches, key=lambda item: item[0])[1]
            # The room name is too long for cover copy; use its stable public name.
            if matched_alias == "叫我老陈就好了":
                return "川神"
            return matched_alias
    return normalized


def _text_name_match_spans(text: str, name: str) -> list[tuple[int, int]]:
    folded_name = str(name or "").strip().casefold()
    if not folded_name:
        return []
    folded_text = str(text or "").casefold()
    if re.fullmatch(r"[a-z][a-z0-9_ -]*", folded_name):
        return [
            match.span()
            for match in re.finditer(
                rf"(?<![a-z0-9]){re.escape(folded_name)}(?![a-z0-9])",
                folded_text,
            )
        ]
    return [match.span() for match in re.finditer(re.escape(folded_name), folded_text)]


def _text_mentions_name(text: str, name: str) -> bool:
    return bool(_text_name_match_spans(text, name))


def _guest_alias_is_numeric_value(text: str, end: int, alias: str) -> bool:
    """Reject numeric nicknames when the match is plainly a measured value."""
    if not re.fullmatch(r"[\d.]+", str(alias or "").strip()):
        return False
    suffix = str(text or "")[end:].lstrip()
    return suffix.startswith(("%", "％"))


def recording_cover_guest_candidates(
    streamer: str,
    *content: str,
) -> list[dict[str, str]]:
    """Return known non-owner streamer aliases explicitly present in the content."""
    current_name = normalize_dota2_streamer_name(streamer)
    combined = "\n".join(str(value or "") for value in content)
    matches: list[tuple[int, int, str, str]] = []
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        if canonical_name == current_name:
            continue
        for alias in aliases:
            for start, end in _text_name_match_spans(combined, alias):
                if _guest_alias_is_numeric_value(combined, end, alias):
                    continue
                matches.append((start, end, canonical_name, alias))

    # Prefer the longest alias at an overlapping location. This keeps page names
    # such as “Spirit小蝴蝶” from also selecting the separate participant “小蝴蝶”.
    selected_spans: list[tuple[int, int]] = []
    selected_names: set[str] = set()
    guests: list[dict[str, str]] = []
    for start, end, canonical_name, alias in sorted(
        matches,
        key=lambda item: (item[0], -(item[1] - item[0])),
    ):
        if canonical_name in selected_names:
            continue
        if any(start < selected_end and end > selected_start
               for selected_start, selected_end in selected_spans):
            continue
        selected_spans.append((start, end))
        selected_names.add(canonical_name)
        guests.append({"name": canonical_name, "mentioned_as": alias})
        if len(guests) >= 3:
            break
    return guests


def resolve_recording_guest_avatar(
    guest: dict[str, str],
    cfg: dict[str, Any],
) -> dict[str, str] | None:
    """Resolve one mentioned streamer to a unique saved profile or Douyu result."""
    guest_name = str(guest.get("name") or "").strip()
    mentioned_as = str(guest.get("mentioned_as") or guest_name).strip()
    for profile in cfg.get("_recording_profiles", []) or []:
        if not isinstance(profile, dict):
            continue
        profile_name = str(profile.get("streamer_name") or "").strip()
        if normalize_dota2_streamer_name(profile_name) != guest_name:
            continue
        avatar_url = str(profile.get("streamer_avatar_url") or "").strip()
        if re.match(r"^https?://", avatar_url, re.IGNORECASE):
            return {
                "name": guest_name,
                "mentioned_as": mentioned_as,
                "avatar_url": avatar_url,
                "source": "saved_room",
            }

    from modules.live_recorder_manager import (  # type: ignore
        RecorderConfigError,
        live_recorder_manager,
    )

    event_room_id = DOTA2_POKEMON_PARTICIPANT_ROOM_IDS.get(guest_name)
    if event_room_id:
        try:
            event_rooms = live_recorder_manager._search_douyu_rooms(event_room_id, 1)
        except RecorderConfigError:
            event_rooms = []
        for event_room in event_rooms:
            avatar_url = str(event_room.get("avatar_url") or "").strip()
            if (
                str(event_room.get("room_id") or "").strip() == event_room_id
                and re.match(r"^https?://", avatar_url, re.IGNORECASE)
            ):
                return {
                    "name": guest_name,
                    "mentioned_as": mentioned_as,
                    "avatar_url": avatar_url,
                    "room_id": event_room_id,
                    "source": "douyu_event_room",
                    "search_name": event_room_id,
                }

    identity_aliases = {guest_name, mentioned_as}
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        if canonical_name == guest_name:
            identity_aliases.update(aliases)
            break

    def exact_candidates(query: str) -> list[dict[str, Any]]:
        exact: list[dict[str, Any]] = []
        seen_room_ids: set[str] = set()
        for candidate in live_recorder_manager._search_douyu_rooms(query, 10):
            room_id = str(candidate.get("room_id") or "").strip()
            candidate_name = str(candidate.get("name") or "").strip()
            avatar_url = str(candidate.get("avatar_url") or "").strip()
            if (
                not room_id
                or room_id in seen_room_ids
                or not re.match(r"^https?://", avatar_url, re.IGNORECASE)
            ):
                continue
            matched_identity_aliases = {
                _compact_alias(alias)
                for alias in identity_aliases
                if _text_mentions_name(candidate_name, alias)
            }
            if not (
                normalize_dota2_streamer_name(candidate_name) == guest_name
                or _compact_alias(candidate_name) == _compact_alias(query)
                or len(matched_identity_aliases) >= 2
            ):
                continue
            seen_room_ids.add(room_id)
            exact.append(candidate)
        exact_name_matches = [
            candidate
            for candidate in exact
            if _compact_alias(candidate.get("name")) == _compact_alias(query)
        ]
        if len(exact_name_matches) == 1:
            return exact_name_matches
        return exact

    preferred_search_name = str(
        DOTA2_STREAMER_AVATAR_SEARCH_NAMES.get(guest_name) or ""
    ).strip()
    for query in dict.fromkeys((mentioned_as, preferred_search_name)):
        if not query:
            continue
        exact = exact_candidates(query)
        if len(exact) == 1:
            return {
                "name": guest_name,
                "mentioned_as": mentioned_as,
                "avatar_url": str(exact[0]["avatar_url"]),
                "room_id": str(exact[0]["room_id"]),
                "source": "douyu_api",
                "search_name": query,
            }
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_pipeline_process_group() -> None:
    """Give one bridge task its own process group so it can be stopped safely."""
    if os.name != "posix":
        return
    try:
        if os.getpgrp() != os.getpid():
            os.setsid()
    except OSError:
        # Retry workers are already session leaders because they are spawned
        # with ``start_new_session=True``.
        pass


def load_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg, dict):
        raise ValueError("配置文件根节点必须是 JSON object")
    cfg["_config_dir"] = str(path.parent)
    return cfg


def resolve_path(value: str | os.PathLike[str], cfg: dict[str, Any]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(cfg["_config_dir"]) / path
    return path.resolve()


def resolve_app_root(cfg: dict[str, Any]) -> Path:
    """Resolve the canonical app path while accepting the legacy config key."""
    configured = cfg.get("app_root") or cfg.get("y2a_root") or "potatoflow-app"
    resolved = resolve_path(str(configured), cfg)
    if not (resolved / "modules").is_dir():
        if str(configured) == "y2a-auto":
            canonical = resolve_path("potatoflow-app", cfg)
            if (canonical / "modules").is_dir():
                return canonical
        if str(configured) in {"y2a-auto", "potatoflow-app"}:
            return APP_ROOT
    return resolved


def effective_config(base: dict[str, Any], video: Path) -> dict[str, Any]:
    cfg = dict(base)
    cfg["_recording_profiles"] = [
        dict(profile)
        for profile in base.get("profiles", []) or []
        if isinstance(profile, dict)
    ]
    cfg.pop("profiles", None)
    for profile in base.get("profiles", []) or []:
        if isinstance(profile, dict) and fnmatch.fnmatch(video.name, str(profile.get("match", ""))):
            cfg.update({key: value for key, value in profile.items() if key != "match"})
            break
    return cfg


def emit_recording_task_added_notification(
    cfg: dict[str, Any],
    *,
    fingerprint_value: str,
    video: Path,
    task_kind: str,
) -> None:
    """Queue a TASK_ADDED notification for a newly claimed recording job."""
    try:
        app_root = resolve_app_root(cfg)
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from modules.notifications import (
            EVENT_TASK_ADDED,
            NotificationEvent,
            emit_notification_event,
        )

        emit_notification_event(
            NotificationEvent(
                event_type=EVENT_TASK_ADDED,
                payload={
                    "task_id": fingerprint_value,
                    "task_kind": task_kind,
                    "video_path": str(video),
                    "video_file": video.name,
                    "streamer": normalize_dota2_streamer_name(
                        str(cfg.get("streamer_name") or "")
                    ),
                    "source_url": str(cfg.get("source_url") or ""),
                    "upload_target": (
                        "local"
                        if task_kind == "record_only"
                        else "bilibili"
                    ),
                },
            )
        )
    except Exception as exc:
        # 通知失败不能阻塞 ASS、AI 或投稿流水线。
        print(f"WARN 录播任务新增通知写入失败: {exc}", file=sys.stderr)


def emit_recording_task_result_notification(
    cfg: dict[str, Any],
    *,
    fingerprint_value: str,
    video: Path,
    task_kind: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    stage: str = "",
    title: str = "",
) -> None:
    """Queue a completion/failure notification for a recording job."""
    if status not in {"completed", "failed"}:
        return
    try:
        app_root = resolve_app_root(cfg)
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        from modules.notifications import (
            EVENT_TASK_COMPLETED,
            EVENT_TASK_FAILED,
            NotificationEvent,
            emit_notification_event,
        )

        normalized_result = dict(result or {})
        bilibili_result = normalized_result.get("bilibili")
        bilibili_result = bilibili_result if isinstance(bilibili_result, dict) else {}
        emit_notification_event(
            NotificationEvent(
                event_type=(
                    EVENT_TASK_COMPLETED
                    if status == "completed"
                    else EVENT_TASK_FAILED
                ),
                payload={
                    "task_id": fingerprint_value,
                    "task_kind": task_kind,
                    "video_path": str(video),
                    "video_file": video.name,
                    "streamer": normalize_dota2_streamer_name(
                        str(cfg.get("streamer_name") or "")
                    ),
                    "source_url": str(cfg.get("source_url") or ""),
                    "upload_target": (
                        "local"
                        if task_kind == "record_only"
                        else "bilibili"
                    ),
                    "status": status,
                    "stage": str(stage or ""),
                    "error_message": str(error or ""),
                    "bvid": str(bilibili_result.get("bvid") or ""),
                    "bilibili_url": str(
                        bilibili_result.get("url")
                        or (
                            f"https://www.bilibili.com/video/{bilibili_result.get('bvid')}"
                            if bilibili_result.get("bvid")
                            else ""
                        )
                    ),
                    "title": str(title or ""),
                    "final_video_path": str(
                        normalized_result.get("final_video_path") or ""
                    ),
                },
            )
        )
    except Exception as exc:
        # 通知失败不能改变已经落库的流水线结果。
        print(f"WARN 录播任务结果通知写入失败: {exc}", file=sys.stderr)


def stdin_paths() -> list[Path]:
    if sys.stdin.isatty():
        return []
    # Rust hooks always write UTF-8 bytes.  A windowed Python executable on a
    # Chinese Windows locale otherwise wraps stdin with the active ANSI code
    # page and corrupts non-ASCII recording paths before Path sees them.
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is not None:
        text = binary_stream.read().decode("utf-8")
    else:
        text = sys.stdin.read()
    return [Path(line.strip()).expanduser() for line in text.splitlines() if line.strip()]


def input_paths(values: list[str], include_stdin: bool = True) -> list[Path]:
    raw = [Path(value).expanduser() for value in values]
    if include_stdin:
        raw.extend(stdin_paths())
    result: list[Path] = []
    seen: set[Path] = set()
    for path in raw:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def find_danmaku_xml(video: Path, paths: list[Path] | None = None) -> Path | None:
    candidates = [path for path in (paths or []) if path.suffix.lower() == ".xml"]
    candidates.extend((video.with_suffix(".xml"), video.parent / "danmaku" / f"{video.stem}.xml"))
    for candidate in candidates:
        if candidate.stem == video.stem and candidate.is_file():
            return candidate.resolve()
    # Older recorder builds could finalize a manually stopped XML with the
    # stop timestamp instead of the video's start timestamp.  A session has
    # its own directory, so the closest recently-written XML is a safe
    # fallback when the exact sidecar name is missing.
    try:
        video_mtime = video.stat().st_mtime
        session_xml = [
            candidate
            for candidate in video.parent.glob("*.xml")
            if candidate.is_file()
        ]
        if session_xml:
            closest = min(
                session_xml,
                key=lambda candidate: abs(candidate.stat().st_mtime - video_mtime),
            )
            if abs(closest.stat().st_mtime - video_mtime) <= 120:
                return closest.resolve()
    except OSError:
        pass
    return None


def wait_for_danmaku_xml(
    video: Path,
    paths: list[Path] | None = None,
    *,
    timeout: float = 8.0,
    interval: float = 0.25,
) -> Path | None:
    """Wait for the recorder to finish rolling the XML before ASS generation."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        danmaku_xml = find_danmaku_xml(video, paths)
        if danmaku_xml is not None:
            try:
                wait_until_stable(danmaku_xml, checks=2, interval=interval)
            except (FileNotFoundError, OSError):
                pass
            else:
                return danmaku_xml
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.05, interval))


def wait_until_stable(path: Path, checks: int, interval: float) -> None:
    previous: tuple[int, int] | None = None
    stable = 0
    while stable < max(1, checks):
        stat = path.stat()
        current = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size <= 0:
            stable = 0
        elif current == previous:
            stable += 1
        else:
            stable = 0
        previous = current
        if stable < max(1, checks):
            time.sleep(max(0.1, interval))


def reusable_burned_video(
    candidate: Path,
    source_video: Path,
    ffprobe: str = "ffprobe",
) -> tuple[bool, dict[str, Any]]:
    """Validate a completed burn before reusing it for another upload attempt."""
    details: dict[str, Any] = {"burned_video_path": str(candidate)}
    try:
        details["burned_video_size_bytes"] = candidate.stat().st_size
    except OSError as exc:
        details["burned_video_reuse_error"] = str(exc)
        return False, details
    if int(details["burned_video_size_bytes"]) <= 0:
        details["burned_video_reuse_error"] = "烧录文件为空"
        return False, details
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
                str(candidate),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            **_hidden_subprocess_kwargs(),
        )
        payload = json.loads(completed.stdout or "{}")
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        details["burned_video_reuse_error"] = str(exc)
        return False, details
    streams = payload.get("streams") if isinstance(payload, dict) else []
    if completed.returncode != 0 or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in (streams if isinstance(streams, list) else [])
    ):
        details["burned_video_reuse_error"] = "烧录文件未检测到有效视频流"
        return False, details
    try:
        burned_duration = float((payload.get("format") or {}).get("duration") or 0)
    except (AttributeError, TypeError, ValueError):
        burned_duration = 0.0
    source_duration = recording_effective_duration_seconds(source_video, ffprobe)
    details.update({
        "burned_video_duration_seconds": burned_duration,
        "source_video_duration_seconds": source_duration,
    })
    if burned_duration <= 0:
        details["burned_video_reuse_error"] = "烧录文件时长无效"
        return False, details
    if source_duration is not None:
        tolerance = max(3.0, float(source_duration) * 0.02)
        details["burned_video_duration_tolerance_seconds"] = tolerance
        if abs(burned_duration - float(source_duration)) > tolerance:
            details["burned_video_reuse_error"] = "烧录文件与原视频时长不一致"
            return False, details
    details["burned_video_reuse_validated"] = True
    return True, details


def reusable_burned_video_for_retry(
    video: Path,
    prior_burn_stage: dict[str, Any],
    ffprobe: str = "ffprobe",
) -> tuple[Path | None, dict[str, Any]]:
    """Find and move a valid prior burn beside its recording source."""
    expected = video.with_name(f"{video.stem}.danmaku.mp4")
    prior_details = (
        prior_burn_stage.get("details")
        if isinstance(prior_burn_stage.get("details"), dict)
        else {}
    )
    candidates = [expected]
    prior_path = str(prior_details.get("burned_video_path") or "").strip()
    if prior_path:
        candidates.append(Path(prior_path))
    seen: set[Path] = set()
    last_details: dict[str, Any] = {}
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        valid, validation = reusable_burned_video(resolved, video, ffprobe)
        last_details = validation
        if not valid:
            continue
        expected.parent.mkdir(parents=True, exist_ok=True)
        if resolved != expected.resolve():
            expected.unlink(missing_ok=True)
            shutil.move(str(resolved), str(expected))
        return expected.resolve(), {
            **prior_details,
            **validation,
            "burned_video_path": str(expected.resolve()),
            "burned_video_location": "recording_directory",
            "reused_on_retry": True,
        }
    return None, last_details


def fingerprint(path: Path, sidecar: Path | None = None) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            handle.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    if sidecar and sidecar.is_file():
        digest.update(sidecar.read_bytes())
    return digest.hexdigest()


def recording_part_title(video: Path, index: int, topic: str = "") -> str:
    match = re.search(r"20\d{2}-\d{2}-\d{2}_(\d{2})-(\d{2})", video.stem)
    clock = f"{match.group(1)}:{match.group(2)}" if match else f"{max(1, index):02d}"
    clean_topic = re.sub(r"[\r\n｜|]+", " ", str(topic or "")).strip()
    clean_topic = clean_topic or "直播精彩内容"
    return f"{clock} {clean_topic[:60]}"[:80]


def strip_recording_intro(description: str) -> str:
    """Remove the generic AI/template lead-in from a recording summary."""
    text = str(description or "").strip()
    if re.match(r"^直播录播[：:]", text):
        quoted_end = text.find("》。")
        if quoted_end >= 0:
            return text[quoted_end + len("》。"):].lstrip()
    return re.sub(
        r"^直播录播[：:].*?[。.!！]\s*",
        "",
        text,
        count=1,
    ).strip()


def strip_ai_timeline_lines(description: str) -> str:
    """Keep AI prose but discard model-formatted timestamps and headings."""
    lines = str(description or "").splitlines()
    timestamp_line = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+")
    section_heading = re.compile(r"\s*重要(?:时间点|事件)\s*[：:]?\s*")
    kept: list[str] = []
    for line in lines:
        if section_heading.fullmatch(line):
            break
        if not timestamp_line.match(line):
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def timeline_target_range(duration_seconds: float | None) -> tuple[int, int]:
    """Return a flexible highlight range that expands with evidence density."""
    if duration_seconds is None or float(duration_seconds) <= 0:
        return 4, 10
    duration = float(duration_seconds)
    minimum = max(3, min(8, int((duration + 899) // 900) + 2))
    maximum = max(minimum + 3, int((duration + 449) // 450) + 4)
    return minimum, min(16, maximum)


_TIMELINE_HEADING = "重要时间点"
_TIMELINE_LINE_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s+\S.*$")
_VAGUE_RECORDING_TITLE_RE = re.compile(
    r"(?:引发|引起|掀起|造成|导致|备受|引)(?:热议|争议)(?:不断)?$"
    r"|(?:被|遭)(?:弹幕)?(?:狂)?(?:吐槽|质疑|批评|喷|怒喷)$"
    r"|(?:可能|疑似|似乎).{0,12}(?:被|遭).{0,8}(?:质疑|吐槽|批评|喷)"
    r"|(?:被赞|获赞)(?:完美)?(?:适配|契合)$"
    r"|(?:争议|热门)话题$"
    r"|^(?:直播精彩内容|精彩内容|精彩直播|直播录像)$"
)
_WEAK_RECORDING_TITLE_RE = re.compile(
    r"(?:进入|来到|进行到|进行至).{0,10}(?:段|阶段|环节)"
    r"(?:[，,]?(?:弹幕|观众|直播间).*)?$"
    r"|第[一二三四五六七八九十百零0-9]+圈左右$"
    r"|(?:继续|持续|仍在)(?:进行|推进|游戏|比赛|挑战)$"
    r"|弹幕(?:调侃|讨论|热议|关注|吐槽|刷屏)$"
    r"|(?:弹幕|观众)(?:围绕|关于).{1,16}$"
    r"|(?:转入|进入|开始)(?:下一|新|[0-9A-Za-z\u4e00-\u9fff]{1,8})"
    r"(?:局|场|把|对局)(?:后|；|，|,|$)"
)
_OPAQUE_RECORDING_TITLE_ATTRIBUTION_RE = re.compile(r"(?:被指|被曝|据称)")
_TITLE_ROOM_DISCUSSION_PREFIX_RE = re.compile(
    r"^(?:[0-9A-Za-z\u4e00-\u9fff·]{1,16})?直播间"
    r"(?:(?:热议|讨论|关注)(?:直播间)?)+[\s：:，,]*"
)
_DANMAKU_ONLY_REAL_WORLD_CLAIM_RE = re.compile(
    r"(?:签约|解约|加入|归入|转入|转会|退役|官宣|开除|辞职|封禁|处罚|结婚|恋爱|"
    r"分手|怀孕|患病|去世|违法|被捕|收入|欠款|诈骗|出轨|作弊|假赛|涉赌|吸毒|"
    r"受伤|出血|流血|被打哭|打伤|挨打|退货(?:成功|失败|没成功))"
)
_NEGATIVE_REAL_WORLD_RUMOR_RE = re.compile(
    r"(?:开除|踢出|赶走|封禁|处罚|分手|怀孕|患病|去世|违法|被捕|欠款|诈骗|"
    r"出轨|作弊|假赛|涉赌|吸毒|被迫.{0,4}(?:解约|退役|辞职)|"
    r"强制.{0,4}(?:解约|退役|辞职)|遭.{0,4}(?:解约|退役|辞职)|"
    r"受伤|出血|流血|被打哭|打伤|挨打)"
)
_LIGHTHEARTED_REAL_WORLD_CLAIM_RE = re.compile(
    r"(?:退货|催婚|相亲|玩梗)"
)
_DANMAKU_CLAIM_ATTRIBUTION_RE = re.compile(
    r"(?:弹幕|观众|直播间)(?:称|说|猜|传|热议|讨论|刷屏|调侃|质疑|认为)|"
    r"(?:据弹幕|传闻|疑似)"
)
_MULTIPART_HEADING_RE = re.compile(r"^【P\d+(?:｜[^\n]*)?】$")
_TIMELINE_SPAM_WINDOW_SECONDS = 60.0
_TIMELINE_MIN_SPAM_MESSAGES = 3


def timeline_lines(description: str) -> list[str]:
    """Return complete, program-rendered timeline lines from a description."""
    lines = str(description or "").splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines)
            if line.strip() == _TIMELINE_HEADING
        )
    except StopIteration:
        return [
            line.strip() for line in lines
            if _TIMELINE_LINE_RE.match(line.strip())
        ]
    return [line.strip() for line in lines[start + 1:] if _TIMELINE_LINE_RE.match(line.strip())]


def recording_title_topic_is_vague(topic: str) -> bool:
    """Reject empty promotional conclusions that do not name the actual event."""
    normalized = normalize_recording_title_filler(topic)
    clean = re.sub(r"[\s｜|：:，,。.!！]+", "", normalized)
    return not clean or bool(
        _VAGUE_RECORDING_TITLE_RE.search(clean)
        or _WEAK_RECORDING_TITLE_RE.search(clean)
    )


def recording_title_uses_opaque_attribution(topic: str) -> bool:
    """Reject source-hiding attribution that obscures who observed the event."""
    return bool(_OPAQUE_RECORDING_TITLE_ATTRIBUTION_RE.search(str(topic or "")))


def normalize_recording_title_filler(topic: str) -> str:
    """Remove room-label filler while keeping the actual event sentence intact."""
    clean = re.sub(r"[\r\n｜|]+", " ", str(topic or "")).strip()
    previous = ""
    while clean and clean != previous:
        previous = clean
        clean = _TITLE_ROOM_DISCUSSION_PREFIX_RE.sub("", clean).strip()
        clean = re.sub(r"^直播中[\s：:，,]*", "", clean).strip()
    return clean


def _strip_danmaku_claim_attribution(topic: str) -> str:
    return re.sub(
        r"^(?:弹幕称|据弹幕|观众(?:讨论|称|认为)|"
        r"直播间(?:调侃|讨论|热议|刷屏调侃)|传闻称?)[\s：:，,]*",
        "",
        str(topic or ""),
    ).strip()


def qualify_danmaku_only_real_world_claim(topic: str) -> str:
    """Keep viewer-reported offline claims attributed instead of stating them as facts."""
    clean = normalize_recording_title_filler(topic)
    real_world_claim = bool(clean and _DANMAKU_ONLY_REAL_WORLD_CLAIM_RE.search(clean))
    if real_world_claim:
        claim = _strip_danmaku_claim_attribution(clean)
        if recording_text_contains_negative_rumor(claim):
            return ""
        if _LIGHTHEARTED_REAL_WORLD_CLAIM_RE.search(claim):
            prefix = "直播间调侃"
        else:
            prefix = "观众讨论"
        clean = f"{prefix}{claim}"
    else:
        # Verified gameplay/timeline events should read as events, not as a
        # recurring viewer-source template.
        clean = _strip_danmaku_claim_attribution(clean)
    # An over-limit title must be rewritten or reviewed. Slicing here can turn
    # a supported event into an incomplete or materially different claim.
    return clean if len(clean) <= RECORDING_TITLE_TOPIC_LIMIT else ""


def recording_text_contains_negative_rumor(text: str) -> bool:
    """Return whether danmaku-only text contains a reputation-sensitive rumor."""
    return bool(_NEGATIVE_REAL_WORLD_RUMOR_RE.search(str(text or "")))


def remove_negative_rumor_text(text: str) -> str:
    """Drop rumor-bearing sentences while retaining unrelated grounded prose."""
    kept_lines: list[str] = []
    for line in str(text or "").splitlines():
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[。！？!?；;])", line)
            if sentence.strip()
        ]
        safe = [
            sentence for sentence in sentences
            if not recording_text_contains_negative_rumor(sentence)
        ]
        if safe:
            kept_lines.append("".join(safe))
    return "\n".join(kept_lines).strip()


def recording_title_topic_from_timeline(
    topic: str,
    timeline_text: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    """Replace a weak title with the strongest complete verified timeline event."""
    clean_topic = normalize_recording_title_filler(topic)
    if not recording_title_topic_is_vague(clean_topic):
        if len(clean_topic) <= RECORDING_TITLE_TOPIC_LIMIT:
            return clean_topic
        if diagnostics is not None:
            diagnostics.update({
                "title_topic_postprocess_over_limit": True,
                "title_topic_postprocess_over_limit_original": clean_topic,
            })
        return ""

    topic_key = re.sub(_VAGUE_RECORDING_TITLE_RE, "", _compact_alias(clean_topic))
    topic_pairs = {
        topic_key[index:index + 2]
        for index in range(max(0, len(topic_key) - 1))
    }
    candidates: list[tuple[int, int, int, int, str]] = []
    over_limit_candidates: list[str] = []
    for position, line in enumerate(timeline_lines(timeline_text)):
        raw_event = normalize_recording_title_filler(
            re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+", "", line).strip()
        ).rstrip("。.!！；; ")
        if not raw_event:
            continue
        sentence = re.split(r"[。；;]", raw_event, maxsplit=1)[0].strip()
        clauses = [
            clause.strip()
            for clause in re.split(r"[，,]", sentence)
            if clause.strip()
        ]
        variants: list[str] = []
        if sentence:
            variants.append(sentence)
        combined: list[str] = []
        for clause in clauses[:3]:
            combined.append(clause)
            variants.append("，".join(combined))
        seen: set[str] = set()
        for event in variants:
            event = normalize_recording_title_filler(event)
            if not event or event in seen:
                continue
            seen.add(event)
            if recording_title_topic_is_vague(event):
                continue
            if len(event) > RECORDING_TITLE_TOPIC_LIMIT:
                over_limit_candidates.append(event)
                continue
            event_key = _compact_alias(event)
            event_pairs = {
                event_key[index:index + 2]
                for index in range(max(0, len(event_key) - 1))
            }
            strength = recording_title_event_strength(event)
            overlap = len(topic_pairs & event_pairs)
            detail = min(len(event), 26)
            candidates.append((overlap * 3 + strength, strength, detail, position, event))
    if diagnostics is not None and over_limit_candidates:
        diagnostics.update({
            "title_topic_timeline_over_limit_count": len(over_limit_candidates),
            "title_topic_timeline_over_limit_candidates": over_limit_candidates[:3],
        })
    if candidates:
        return max(candidates)[4]

    fallback = re.sub(_VAGUE_RECORDING_TITLE_RE, "", clean_topic).strip(" -_｜|：:，,。.!！")
    if len(fallback) <= RECORDING_TITLE_TOPIC_LIMIT:
        return fallback
    if diagnostics is not None:
        diagnostics.update({
            "title_topic_fallback_over_limit": True,
            "title_topic_fallback_over_limit_original": fallback,
        })
    return ""


def recording_title_event_strength(event: str) -> int:
    """Score concrete outcomes and contrasts above mere in-progress states."""
    clean = normalize_recording_title_filler(event)
    score = 0
    if re.search(
        r"(?:第一|夺冠|晋级|淘汰|获胜|落败|击败|翻盘|逆转|反超|追平|团灭|"
        r"0分|零分|拿下|完成|成功|失败|套圈|三杀|五杀|保底|登顶|守住)",
        clean,
    ):
        score += 7
    if re.search(r"(?:却|但|反而|互超|多次超越|追上|从.+到|由.+转)", clean):
        score += 4
    if re.search(r"\d|[一二三四五六七八九十百千]+(?:抽|分|公里|圈|杀|连胜)", clean):
        score += 2
    if re.search(r"(?:打出|升至|冲上|降至|触发|抽到|选出|换上)", clean):
        score += 2
    if len(clean) < 10:
        score -= 3
    if _WEAK_RECORDING_TITLE_RE.search(
        re.sub(r"[\s｜|：:，,。.!！]+", "", clean)
    ):
        score -= 10
    return score


def recording_title_topic_is_underfilled(
    topic: str,
    duration_seconds: float | None,
    verified_timeline_count: int,
) -> bool:
    """Flag tiny single-moment titles only when a long recording has rich evidence."""
    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    clean = normalize_recording_title_filler(topic)
    return bool(
        duration >= 45 * 60
        and int(verified_timeline_count or 0) >= 6
        and len(clean) < 24
        and "；" not in clean
    )


def recording_title_timeline_coverage_is_sufficient(
    selected_indexes: Any,
    duration_seconds: float | None,
    verified_timeline_count: int,
    verified_timeline: list[str] | None = None,
) -> bool:
    """Require long-recording titles to cite evidence from distinct stages."""
    try:
        duration = float(duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    count = max(0, int(verified_timeline_count or 0))
    if duration < 45 * 60 or count < 6:
        return True
    if not isinstance(selected_indexes, list):
        return False
    indexes = sorted({
        int(index)
        for index in selected_indexes
        if str(index).strip().lstrip("-").isdigit()
        and 0 <= int(index) < count
    })
    minimum_span = max(2, count // 3)
    if len(indexes) < 2 or indexes[-1] - indexes[0] < minimum_span:
        return False
    if verified_timeline:
        selected_seconds: list[int] = []
        for index in indexes:
            if index >= len(verified_timeline):
                continue
            match = re.match(
                r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s+",
                str(verified_timeline[index] or "").strip(),
            )
            if not match:
                continue
            first = int(match.group(1))
            second = int(match.group(2))
            third = match.group(3)
            selected_seconds.append(
                first * 3600 + second * 60 + int(third)
                if third is not None
                else first * 60 + second
            )
        minimum_time_span = max(15 * 60, int(duration * 0.4))
        if (
            len(selected_seconds) < 2
            or max(selected_seconds) - min(selected_seconds) < minimum_time_span
        ):
            return False
    return True


def recording_title_missing_selected_gsi_heroes(
    title: str,
    selected_indexes: Any,
    verified_timeline: list[str],
    game_segments: Any,
) -> list[str]:
    """Return event-matched streamer heroes omitted from a gameplay title."""
    if not isinstance(selected_indexes, list) or not isinstance(game_segments, list):
        return []
    missing: list[str] = []
    gameplay_pattern = re.compile(
        r"(?:对局|本局|团战|高地|基地|推进|追击|击杀|阵亡|买活|出装|装备|技能|"
        r"空大|大招|翻盘|团灭|反伤|打盾|控盾|守高|拆塔|开雾|开团|收尾|GG)",
        re.IGNORECASE,
    )
    whole_game_outcome_pattern = re.compile(
        r"(?:第[一二三四五六七八九十\d]+局|本局|对局|高地|基地|推进|守高|拆塔|"
        r"翻盘|团灭|GG|获胜|落败|拿下|输掉|赢下|收尾)",
        re.IGNORECASE,
    )
    indexes = sorted({
        int(index)
        for index in selected_indexes
        if str(index).strip().lstrip("-").isdigit()
        and 0 <= int(index) < len(verified_timeline)
    })
    for index in indexes:
        line = str(verified_timeline[index] or "").strip()
        match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+)$", line)
        if not match:
            continue
        hours_or_minutes = int(match.group(1))
        minutes_or_seconds = int(match.group(2))
        seconds = int(match.group(3) or 0)
        event_seconds = (
            hours_or_minutes * 3600 + minutes_or_seconds * 60 + seconds
            if match.group(3) is not None
            else hours_or_minutes * 60 + minutes_or_seconds
        )
        event = match.group(4).strip()
        matching_segments = [
            segment
            for segment in game_segments
            if isinstance(segment, dict)
            and float(segment.get("start_seconds") or 0) <= event_seconds
            <= float(segment.get("end_seconds") or 0)
            and str(segment.get("hero") or "").strip()
        ]
        if len(matching_segments) != 1:
            continue
        hero = str(matching_segments[0].get("hero") or "").strip()
        title_mentions_hero = recording_text_mentions_specific_dota2_hero(title, hero)
        event_mentions_hero = recording_text_mentions_specific_dota2_hero(event, hero)
        event_hero_keys = _dota2_hero_identity_keys(event)
        # A line about another hero alone may describe a teammate or opponent.
        # Whole-game outcomes still need the verified player's identity, while
        # an isolated third-party action does not.
        if not event_mentions_hero and event_hero_keys:
            if not whole_game_outcome_pattern.search(event):
                continue
        elif not event_mentions_hero and not gameplay_pattern.search(event):
            continue
        if not title_mentions_hero and hero not in missing:
            missing.append(hero)
    return missing


def recording_title_missing_selected_gsi_streamer(
    title: str,
    selected_indexes: Any,
    verified_timeline: list[str],
    game_segments: Any,
    streamer: str,
) -> bool:
    """Require the verified player name when selected events are their gameplay."""
    if not str(streamer or "").strip() or topic_mentions_streamer(title, streamer):
        return False
    # An empty title forces all event-matched heroes to be reported as missing;
    # a non-empty result means at least one selected event is verified gameplay.
    return bool(recording_title_missing_selected_gsi_heroes(
        "",
        selected_indexes,
        verified_timeline,
        game_segments,
    ))


def recording_title_audience_prefix_obscures_selected_gsi_gameplay(
    title: str,
    selected_indexes: Any,
    verified_timeline: list[str],
    game_segments: Any,
) -> bool:
    """Reject audience-label prefixes when GSI already proves the gameplay."""
    if not re.match(
        r"^(?:观众|弹幕|直播间)(?:讨论|称|认为|质疑|调侃|吐槽|关注)",
        normalize_recording_title_filler(title),
    ):
        return False
    return bool(recording_title_missing_selected_gsi_heroes(
        "",
        selected_indexes,
        verified_timeline,
        game_segments,
    ))


def fit_description_preserving_timeline(description: str, limit: int) -> str:
    """Fit text without cutting timestamp lines, keeping verified highlights first."""
    text = str(description or "").strip()
    budget = max(0, int(limit or 0))
    if len(text) <= budget:
        return text
    if budget <= 0:
        return ""

    points = timeline_lines(text)
    if not points:
        return text[:budget].rstrip()
    text_lines = text.splitlines()
    heading_index = next((
        index for index, line in enumerate(text_lines)
        if line.strip() == _TIMELINE_HEADING
    ), None)
    has_heading = heading_index is not None
    prose = "\n".join(
        text_lines[:heading_index]
        if heading_index is not None
        else [line for line in text_lines if not _TIMELINE_LINE_RE.match(line.strip())]
    ).strip()
    kept: list[str] = []
    for point in points:
        candidate = "\n".join((
            *((_TIMELINE_HEADING,) if has_heading else ()),
            *kept,
            point,
        ))
        if len(candidate) > budget:
            break
        kept.append(point)
    timeline = "\n".join((
        *((_TIMELINE_HEADING,) if has_heading else ()),
        *kept,
    )).rstrip()
    if not prose or len(timeline) >= budget:
        return timeline[:budget].rstrip()
    separator = "\n\n"
    prose_budget = max(0, budget - len(timeline) - len(separator))
    fitted_prose = prose[:prose_budget].rstrip()
    return f"{fitted_prose}{separator if fitted_prose else ''}{timeline}".rstrip()


def fit_multipart_description_preserving_sections(description: str, limit: int) -> str:
    """Fit a multipart description while retaining every part heading."""
    text = str(description or "").strip()
    budget = max(0, int(limit or 0))
    if len(text) <= budget:
        return text
    lines = text.splitlines()
    heading_indexes = [
        index for index, line in enumerate(lines)
        if _MULTIPART_HEADING_RE.fullmatch(line.strip())
    ]
    if not heading_indexes:
        return fit_description_preserving_timeline(text, budget)

    intro = "\n".join(lines[:heading_indexes[0]]).strip()
    sections: list[tuple[str, str]] = []
    for position, start in enumerate(heading_indexes):
        end = (
            heading_indexes[position + 1]
            if position + 1 < len(heading_indexes)
            else len(lines)
        )
        sections.append((lines[start].strip(), "\n".join(lines[start + 1:end]).strip()))

    # Headings are structural and must survive. Keep a short intro when room
    # permits, then distribute the remaining body budget fairly; unused space
    # from a short part is automatically available to longer parts.
    intro = intro[: min(len(intro), 200)].rstrip()
    piece_count = len(sections) + (1 if intro else 0)
    structural = sum(len(heading) + 1 for heading, _body in sections)
    structural += 2 * max(0, piece_count - 1)
    if intro:
        structural += len(intro)
    body_budget = max(0, budget - structural)
    remaining = list(range(len(sections)))
    allocations = [0] * len(sections)
    while remaining:
        share = body_budget // len(remaining)
        completed = [
            index for index in remaining
            if len(sections[index][1]) <= share
        ]
        if not completed:
            for index in remaining:
                allocations[index] = share
            break
        for index in completed:
            allocations[index] = len(sections[index][1])
            body_budget -= allocations[index]
            remaining.remove(index)

    pieces = [intro] if intro else []
    for (heading, body), allocation in zip(sections, allocations):
        fitted = fit_description_preserving_timeline(body, allocation)
        pieces.append(f"{heading}\n{fitted}".rstrip())
    return "\n\n".join(pieces)[:budget].rstrip()


def _person_hero_relations(value: str) -> list[tuple[tuple[str, ...], int]]:
    """Return known person/hero pairs asserted by one candidate event."""
    text = str(value or "")
    hero_keys = _dota2_hero_identity_keys(text)
    if not hero_keys:
        return []
    relations: list[tuple[tuple[str, ...], int]] = []
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        names = tuple(dict.fromkeys((canonical_name, *aliases)))
        spans = [
            span
            for name in names
            for span in _text_name_match_spans(text, name)
        ]
        if not spans:
            continue
        # “YYF观战南枫的末日” binds Doom to Nanfeng, not to YYF.
        if any(re.match(
            r"^(?:观战|观赛|旁观|OB|看比赛|看决赛|解说|点评|直播间(?:热议|讨论|关注))",
            text[end:].lstrip(" ：:，,｜|"),
            re.IGNORECASE,
        ) for _start, end in spans):
            continue
        relations.extend((names, int(hero_key)) for hero_key in hero_keys)
    return relations


def _relation_supported_by_comments(
    person_names: tuple[str, ...],
    hero_key: int,
    comments: list[Any],
) -> bool:
    """Require repeated, nearby and at least once explicit person/hero evidence."""
    hero_name, hero_aliases = _DOTA2_HERO_ALIAS_GROUPS[hero_key]
    hero_terms = (hero_name.split("（", 1)[0], *hero_aliases)
    person_mentions = 0
    hero_mentions = 0
    explicit_mentions = 0
    for comment in comments:
        text = str(getattr(comment, "text", "") or "")
        has_person = any(_text_mentions_name(text, name) for name in person_names)
        has_hero = any(
            _text_mentions_dota2_hero_term(text, term)
            for term in hero_terms
            if term
        )
        person_mentions += int(has_person)
        hero_mentions += int(has_hero)
        explicit_mentions += int(has_person and has_hero)
    return person_mentions >= 2 and hero_mentions >= 2 and explicit_mentions >= 1


def title_person_hero_relations_supported(title: str, verified_description: str) -> bool:
    """Require every title person/hero binding to already exist in verified copy."""
    title_relations = {
        (names[0], hero_key)
        for names, hero_key in _person_hero_relations(title)
    }
    if not title_relations:
        return True
    description_relations = {
        (names[0], hero_key)
        for names, hero_key in _person_hero_relations(verified_description)
    }
    return title_relations <= description_relations


def title_person_hero_relations_supported_with_gsi(
    title: str,
    verified_description: str,
    streamer: str,
    game_segments: Any,
) -> bool:
    """Allow owner/hero bindings independently verified by segmented GSI."""
    if title_person_hero_relations_supported(title, verified_description):
        return True
    if not isinstance(game_segments, list):
        return False
    public_streamer = normalize_dota2_streamer_name(streamer)
    heroes = list(dict.fromkeys(
        str(segment.get("hero") or "").strip()
        for segment in game_segments
        if isinstance(segment, dict)
        and streamer_gameplay_is_verified(segment)
        and str(segment.get("hero") or "").strip()
    ))
    if not public_streamer or not heroes:
        return False
    gsi_grounding = "\n".join(
        f"{public_streamer}使用{hero}。" for hero in heroes
    )
    return title_person_hero_relations_supported(
        title,
        "\n".join(filter(None, (verified_description, gsi_grounding))),
    )


_COMPETITIVE_RESULT_TERMS: dict[str, tuple[str, ...]] = {
    "win": (
        "赢了", "赢下", "获胜", "取胜", "胜利", "战胜", "击败", "晋级",
        "夺冠", "拿下冠军", "首夺", "翻盘成功", "完成翻盘", "逆转取胜",
    ),
    "loss": (
        "输了", "输掉", "输给", "失利", "落败", "告负", "败北", "惨败",
        "淘汰", "被淘汰", "被翻盘", "惨遭翻盘", "痛失好局",
    ),
}


def _competitive_result_polarities(value: str) -> set[str]:
    text = str(value or "").casefold()
    polarities = {
        polarity
        for polarity, terms in _COMPETITIVE_RESULT_TERMS.items()
        if any(term.casefold() in text for term in terms)
    }
    crown = r"(?:冠军|[一二三四五六七八九十\d]+冠王?)"
    if re.search(
        rf"(?:夺|拿到|拿下|获得|斩获|成为).{{0,4}}{crown}"
        rf"|{crown}.{{0,3}}(?:诞生|到手|了)"
        rf"|(?:恭喜|恭迎).{{0,10}}{crown}",
        text,
    ):
        polarities.add("win")
    if (
        re.search(r"(?:提前预祝|预祝|预测|看好|感觉要|冠军相|有望)", text)
        and not re.search(r"(?:最终|赛后|真.{0,2}冠|已经|成功|诞生|到手|确认)", text)
    ):
        polarities.discard("win")
    return polarities


def _person_result_relations(value: str) -> list[tuple[tuple[str, ...], str]]:
    """Return known people explicitly assigned a competitive result."""
    text = str(value or "")
    if not _competitive_result_polarities(text):
        return []
    relations: list[tuple[tuple[str, ...], str]] = []
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        names = tuple(dict.fromkeys((canonical_name, *aliases)))
        spans = [span for name in names for span in _text_name_match_spans(text, name)]
        if not spans:
            continue
        person_polarities: set[str] = set()
        for start, end in spans:
            before = text[max(0, start - 12):start].rstrip(" ：:，,｜|")
            after = text[end:end + 16].lstrip(" ：:，,｜|")
            if re.match(
                r"^(?:观战|观赛|旁观|OB|看比赛|看决赛|解说|点评)",
                after,
                re.IGNORECASE,
            ):
                continue
            if re.match(
                r"^(?:赢了|赢下|获胜|取胜|战胜|击败|淘汰|晋级|夺冠|"
                r"拿下冠军|首夺|翻盘成功|完成翻盘|逆转取胜)",
                after,
            ) or re.search(r"(?:恭喜|恭迎|预祝)$", before):
                person_polarities.add("win")
            if re.match(
                r"^(?:输了|输掉|输给|失利|落败|告负|败北|惨败|"
                r"被淘汰|被击败|被战胜|被翻盘|惨遭翻盘|痛失好局)",
                after,
            ):
                person_polarities.add("loss")
            if re.search(r"(?:淘汰|击败|战胜)$", before):
                person_polarities.add("loss")
            if re.search(r"输给$", before):
                person_polarities.add("win")
        relations.extend((names, polarity) for polarity in person_polarities)
    return relations


def _competitive_result_supported(event: str, comments: list[Any]) -> bool:
    """Require result direction and named winner/loser bindings in raw evidence."""
    polarities = _competitive_result_polarities(event)
    if not polarities:
        return True
    texts = [str(getattr(comment, "text", "") or "") for comment in comments]
    if any(
        not any(polarity in _competitive_result_polarities(text) for text in texts)
        for polarity in polarities
    ):
        return False
    for person_names, polarity in _person_result_relations(event):
        if not any(
            any(_text_mentions_name(text, name) for name in person_names)
            and polarity in _competitive_result_polarities(text)
            for text in texts
        ):
            return False
    return True


def title_competitive_results_supported(title: str, verified_description: str) -> bool:
    """Allow title result claims only when the verified timeline has the same binding."""
    if not _competitive_result_polarities(title) <= _competitive_result_polarities(
        verified_description
    ):
        return False
    title_relations = {
        (names[0], polarity)
        for names, polarity in _person_result_relations(title)
    }
    description_relations = {
        (names[0], polarity)
        for names, polarity in _person_result_relations(verified_description)
    }
    return title_relations <= description_relations


def render_grounded_danmaku_timeline(
    timeline: Any,
    selected_comments: list[Any],
    all_comments: list[Any],
    *,
    delay_seconds: int = 8,
    duration_seconds: float | None = None,
    maximum_points: int | None = None,
    anchor_diagnostics: dict[str, Any] | None = None,
) -> str:
    """Render timeline anchors only from exact, coherent XML evidence clusters."""
    if not isinstance(timeline, list):
        if anchor_diagnostics is not None:
            anchor_diagnostics["timeline_anchor_details"] = []
            anchor_diagnostics["timeline_rejection_reasons"] = {
                "invalid_timeline": 1,
            }
        return ""
    sampled_texts = {
        str(comment.text).strip()
        for comment in selected_comments
        if str(getattr(comment, "text", "")).strip()
    }
    delay = max(0, min(60, int(delay_seconds or 0)))
    maximum = None if duration_seconds is None else max(0.0, float(duration_seconds))
    verified: list[dict[str, Any]] = []
    rejection_reasons: dict[str, int] = {}
    relaxed_screen_spam_count = 0

    def reject(reason: str) -> None:
        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    for item in timeline:
        if not isinstance(item, dict):
            reject("invalid_candidate")
            continue
        raw_evidence_texts = item.get("evidence_texts")
        evidence_texts = list(dict.fromkeys(
            [str(text or "").strip() for text in raw_evidence_texts[:3]]
            if isinstance(raw_evidence_texts, list)
            else [str(item.get("evidence_text") or "").strip()]
        ))
        exact_evidence_texts = [
            text for text in evidence_texts
            if text and text in sampled_texts
        ]
        raw_keywords = item.get("evidence_keywords")
        if not isinstance(raw_keywords, list):
            reject("missing_keywords")
            continue
        keywords = list(dict.fromkeys(
            re.sub(r"\s+", " ", str(keyword or "")).strip()
            for keyword in raw_keywords[:4]
            if len(re.sub(r"\s+", "", str(keyword or ""))) >= 2
        ))
        if not keywords:
            reject("missing_keywords")
            continue
        evidence_corpus = "\n".join(exact_evidence_texts).casefold()
        direct_keywords_supported = bool(exact_evidence_texts) and all(
            keyword.casefold() in evidence_corpus for keyword in keywords
        )
        evidence_matches = sorted(
            (
                comment for comment in all_comments
                if str(getattr(comment, "text", "")).strip() in exact_evidence_texts
            ),
            key=lambda comment: float(comment.time),
        )
        matching_comments: list[Any] = []
        if direct_keywords_supported and len(exact_evidence_texts) == len(evidence_texts):
            for first_index, first in enumerate(evidence_matches):
                cluster = [
                    comment for comment in evidence_matches[first_index:]
                    if float(first.time) <= float(comment.time) <= float(first.time) + 30.0
                ]
                cluster_texts = {
                    str(getattr(comment, "text", "")).strip()
                    for comment in cluster
                }
                if all(text in cluster_texts for text in exact_evidence_texts):
                    matching_comments = cluster
                    break

        # Relaxed fallback: exact AI quoting is helpful but not mandatory when
        # the full XML shows a concentrated burst of messages about the same
        # keywords. The timestamp still comes exclusively from real XML.
        if not matching_comments:
            keyword_matches = sorted(
                (
                    comment for comment in all_comments
                    if any(
                        keyword.casefold()
                        in str(getattr(comment, "text", "")).casefold()
                        for keyword in keywords
                    )
                ),
                key=lambda comment: float(comment.time),
            )
            seed_times = [float(comment.time) for comment in evidence_matches]
            required_keyword_count = max(1, (len(keywords) + 1) // 2)
            spam_clusters: list[list[Any]] = []
            for first_index, first in enumerate(keyword_matches):
                cluster = [
                    comment for comment in keyword_matches[first_index:]
                    if float(first.time) <= float(comment.time)
                    <= float(first.time) + _TIMELINE_SPAM_WINDOW_SECONDS
                ]
                if len(cluster) < _TIMELINE_MIN_SPAM_MESSAGES:
                    continue
                if seed_times and min(
                    abs(float(comment.time) - seed)
                    for comment in cluster
                    for seed in seed_times
                ) > _TIMELINE_SPAM_WINDOW_SECONDS:
                    continue
                cluster_corpus = "\n".join(
                    str(getattr(comment, "text", "")) for comment in cluster
                ).casefold()
                supported_keyword_count = sum(
                    keyword.casefold() in cluster_corpus for keyword in keywords
                )
                if supported_keyword_count >= required_keyword_count:
                    spam_clusters.append(cluster)
            if spam_clusters:
                matching_comments = max(
                    spam_clusters,
                    key=lambda cluster: (len(cluster), -float(cluster[0].time)),
                )
                relaxed_screen_spam_count += 1
        if not matching_comments:
            if not exact_evidence_texts:
                reject("evidence_not_exact_sample")
            elif not direct_keywords_supported:
                reject("keywords_not_supported")
            else:
                reject("same_time_screen_spam_not_found")
            continue
        event = re.sub(r"\s+", " ", str(item.get("event") or "")).strip()
        event = re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+", "", event)
        if not event:
            reject("missing_event")
            continue
        unsupported_relation = any(
            not _relation_supported_by_comments(names, hero_key, matching_comments)
            for names, hero_key in _person_hero_relations(event)
        )
        if unsupported_relation:
            reject("person_hero_relation_not_supported")
            continue
        if not _competitive_result_supported(event, matching_comments):
            reject("competitive_result_not_supported")
            continue
        earliest = min(matching_comments, key=lambda comment: float(comment.time))
        xml_anchor = max(0, int(float(earliest.time)))
        corrected = max(0, xml_anchor - delay)
        if maximum is not None and corrected > maximum + 1:
            reject("outside_recording_duration")
            continue
        event_text = event[:120]
        event_key = _compact_alias(event_text)
        evidence_key = frozenset(evidence_texts)
        duplicate = any(
            abs(corrected - int(existing["corrected_seconds"])) <= 60
            and (
                event_key == existing["event_key"]
                or bool(evidence_key.intersection(existing["evidence_key"]))
            )
            for existing in verified
        )
        if duplicate:
            reject("duplicate_event")
            continue
        verified.append({
            "corrected_seconds": corrected,
            "xml_anchor_seconds": xml_anchor,
            "event": event_text,
            "event_key": event_key,
            "evidence_key": evidence_key,
            "evidence_count": len(evidence_texts),
        })

    if not verified:
        if anchor_diagnostics is not None:
            anchor_diagnostics["timeline_anchor_details"] = []
            anchor_diagnostics["timeline_rejection_reasons"] = rejection_reasons
        return ""

    def format_timestamp(total: int) -> str:
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    ordered = sorted(verified, key=lambda item: int(item["corrected_seconds"]))
    if maximum_points is not None and len(ordered) > int(maximum_points):
        point_limit = max(1, int(maximum_points))
        if point_limit == 1:
            ordered = [ordered[0]]
        else:
            indexes = {
                round(index * (len(ordered) - 1) / (point_limit - 1))
                for index in range(point_limit)
            }
            ordered = [ordered[index] for index in sorted(indexes)]
    lines = [
        f"{format_timestamp(int(item['corrected_seconds']))} {item['event']}"
        for item in ordered
    ]
    if anchor_diagnostics is not None:
        anchor_diagnostics["timeline_anchor_details"] = [
            {
                "event": item["event"],
                "xml_anchor": format_timestamp(int(item["xml_anchor_seconds"])),
                "final_timestamp": format_timestamp(int(item["corrected_seconds"])),
                "reaction_delay_seconds": delay,
                "evidence_count": item["evidence_count"],
            }
            for item in ordered
        ]
        anchor_diagnostics["timeline_rejection_reasons"] = rejection_reasons
        anchor_diagnostics["timeline_relaxed_screen_spam_count"] = relaxed_screen_spam_count
    if not lines:
        return ""
    return "重要时间点\n" + "\n".join(lines)


def _multipart_summary_body(description: str) -> str:
    return strip_recording_intro(description)


def render_multipart_description(parts: list[dict[str, Any]], intro: str = "") -> str:
    """Build one Bilibili archive description containing each part's own summary."""
    normalized = [
        item for item in parts
        if isinstance(item, dict) and int(item.get("part_number") or 0) > 0
    ]
    normalized.sort(key=lambda item: int(item.get("part_number") or 0))
    if not normalized:
        return strip_recording_intro(intro)[:1900]

    headings = []
    for item in normalized:
        fields = [f"P{int(item.get('part_number') or 1)}"]
        topic = re.sub(r"[\r\n｜|]+", " ", str(item.get("title_topic") or "")).strip()
        recorded_at = str(item.get("recorded_at") or "").strip()
        if topic:
            fields.append(topic[:40])
        if recorded_at:
            fields.append(recorded_at)
        headings.append(f"【{'｜'.join(fields)}】")

    clean_intro = strip_recording_intro(intro)
    overhead = len(clean_intro) + sum(len(item) + 2 for item in headings)
    body_budget = max(80, (1850 - overhead) // max(1, len(normalized)))
    sections = []
    for heading, item in zip(headings, normalized):
        body = _multipart_summary_body(str(item.get("description") or ""))
        sections.append(
            f"{heading}\n{fit_description_preserving_timeline(body, body_budget)}"
        )
    return "\n\n".join(([clean_intro] if clean_intro else []) + sections)[:1900].rstrip()


def strip_live_stats_from_description(description: str, stats_text: str) -> str:
    """Return the editorial body without pipeline-owned live statistics."""
    stats = str(stats_text or "").strip()
    body = strip_recording_intro(description)
    if not stats:
        return body

    # Old tasks contain one combined block. New tasks put game/equipment data
    # before the editorial body and audience/revenue data after it.
    game_stats, trailing_stats = split_live_stats_sections(stats)
    for block in (stats, game_stats, trailing_stats):
        if not block:
            continue
        while body == block or body.startswith(f"{block}\n"):
            body = body[len(block):].lstrip()
        while body == block or body.endswith(f"\n{block}"):
            body = body[:-len(block)].rstrip()
    return strip_recording_intro(body)


def split_live_stats_sections(stats_text: str) -> tuple[str, str]:
    """Return game/equipment lines for the front and audience lines for the end."""
    original = str(stats_text or "").strip()
    lines = [line.strip() for line in original.splitlines() if line.strip()]
    game_lines = [line for line in lines if line.startswith("🎮 ")]
    if not game_lines:
        return "", original
    trailing_lines = [
        line for line in lines
        if not line.startswith("🎮 ") and "直播数据" not in line and "对局数据" not in line
    ]
    game_stats = "\n".join(("——— 对局数据 ———", *game_lines)) if game_lines else ""
    trailing_stats = (
        "\n".join(("——— 直播数据 ———", *trailing_lines))
        if trailing_lines else ""
    )
    return game_stats, trailing_stats


_GIFT_STATS_ENTRY_RE = re.compile(
    r"\S+×\d+\(单价[^()]*?元/总价[^()]*?元\)"
)


def _fit_gift_stats_line(line: str, limit: int) -> str:
    """Shorten a generated gift line only between complete gift entries."""
    budget = max(0, int(limit or 0))
    text = str(line or "").strip()
    if len(text) <= budget:
        return text
    if budget <= 0:
        return ""

    detail, separator, total = text.partition(" | ")
    entries = _GIFT_STATS_ENTRY_RE.findall(detail)
    suffix = f" | {total}" if separator else ""
    if not entries:
        compact = f"🎁 礼物明细过长{suffix}"
        return compact if len(compact) <= budget else ""

    for kept_count in range(len(entries), 0, -1):
        omitted = len(entries) - kept_count
        marker = f" …（另{omitted}种）" if omitted else ""
        candidate = f"🎁 {' '.join(entries[:kept_count])}{marker}{suffix}"
        if len(candidate) <= budget:
            return candidate
    compact = f"🎁 共{len(entries)}种已核价礼物{suffix}"
    return compact if len(compact) <= budget else ""


def _fit_live_stats(stats_text: str, limit: int) -> str:
    """Fit statistics by complete lines and complete generated gift entries."""
    text = str(stats_text or "").strip()
    budget = max(0, int(limit or 0))
    if len(text) <= budget:
        return text
    if budget <= 0:
        return ""

    lines = text.splitlines()
    gift_index = next(
        (index for index, line in enumerate(lines) if line.startswith("🎁 ")),
        None,
    )
    if gift_index is not None:
        other_lines = lines[:gift_index] + lines[gift_index + 1:]
        other_text = "\n".join(other_lines)
        gift_budget = budget - len(other_text) - (1 if other_text else 0)
        fitted_gift = _fit_gift_stats_line(lines[gift_index], gift_budget)
        if fitted_gift:
            fitted_lines = list(lines)
            fitted_lines[gift_index] = fitted_gift
            candidate = "\n".join(fitted_lines)
            if len(candidate) <= budget:
                return candidate

    kept: list[str] = []
    for line in lines:
        candidate = "\n".join((*kept, line))
        if len(candidate) > budget:
            continue
        kept.append(line)
    return "\n".join(kept).rstrip()


def append_live_stats_to_description(
    description: str,
    stats_text: str,
    limit: int = 1900,
) -> str:
    """Put game data first and audience/revenue statistics last."""
    stats = str(stats_text or "").strip()
    body = strip_live_stats_from_description(description, stats)
    if not stats:
        return fit_description_preserving_timeline(body, limit)

    game_stats, trailing_stats = split_live_stats_sections(stats)
    points = timeline_lines(body)
    if points:
        timeline_headings = max(1, sum(
            1 for line in body.splitlines() if line.strip() == _TIMELINE_HEADING
        ))
        multipart_headings = [
            line.strip() for line in body.splitlines()
            if _MULTIPART_HEADING_RE.fullmatch(line.strip())
        ]
        priority_length = (
            sum(len(point) + 1 for point in points)
            + timeline_headings * (len(_TIMELINE_HEADING) + 2)
            + sum(len(heading) + 2 for heading in multipart_headings)
        )
        body_reserve = min(max(0, limit - 200), priority_length)
    else:
        body_reserve = 0

    separators = sum(bool(value) for value in (game_stats, trailing_stats) if body)
    stats_budget = max(0, limit - body_reserve - separators)
    combined_stats = "\n".join(value for value in (game_stats, trailing_stats) if value)
    combined_stats = _fit_live_stats(combined_stats, stats_budget)
    game_stats, trailing_stats = split_live_stats_sections(combined_stats)

    fixed_length = len(game_stats) + len(trailing_stats)
    fixed_separators = sum(
        1 for left, right in ((game_stats, body), (body, trailing_stats)) if left and right
    )
    body_budget = max(0, limit - fixed_length - fixed_separators)
    has_multipart_sections = any(
        _MULTIPART_HEADING_RE.fullmatch(line.strip())
        for line in body.splitlines()
    )
    fitted_body = (
        fit_multipart_description_preserving_sections(body, body_budget)
        if has_multipart_sections
        else fit_description_preserving_timeline(body, body_budget)
    )
    return "\n".join(
        value for value in (game_stats, fitted_body, trailing_stats) if value
    ).rstrip()


def prepend_live_stats_to_description(
    description: str,
    stats_text: str,
    limit: int = 1900,
) -> str:
    """Compatibility wrapper; new descriptions always place statistics last."""
    return append_live_stats_to_description(description, stats_text, limit)


def live_stats_stage_details(stats_text: str) -> dict[str, Any]:
    """Persist the human-readable statistics instead of only its length."""
    summary = str(stats_text or "").strip()
    return {
        "stats_collected": bool(summary),
        "stats_summary": summary,
        "stats_length": len(summary),
        "outcome": "matched" if summary else "no_data",
    }


def danmaku_stage_details(
    video: Path,
    danmaku_xml: Path,
    comments: list[Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Describe XML coverage and flag implausibly sparse long recordings."""
    details = inspect_danmaku_xml(danmaku_xml, comments)
    duration = recording_effective_duration_seconds(
        video,
        str(cfg.get("ffprobe", "ffprobe")),
    )
    duration_minutes = max(0.0, float(duration or 0.0) / 60.0)
    rate = len(comments) / duration_minutes if duration_minutes > 0 else 0.0
    minimum_duration = max(
        0.0,
        float(cfg.get("danmaku_sparse_warning_min_duration_seconds", 1800) or 1800),
    )
    minimum_rate = max(
        0.0,
        float(cfg.get("danmaku_sparse_warning_min_per_minute", 2.0) or 2.0),
    )
    suspected = bool(
        duration is not None
        and duration >= minimum_duration
        and rate < minimum_rate
    )
    details.update({
        "video_duration_seconds": round(float(duration), 3) if duration is not None else None,
        "danmaku_rate_per_minute": round(rate, 3),
        "danmaku_integrity": "suspected_incomplete" if suspected else "ok",
    })
    if suspected:
        details["danmaku_integrity_reason"] = (
            f"{duration_minutes:.1f} 分钟录播仅保存 {len(comments)} 条有效弹幕，"
            f"低于完整性预警阈值 {minimum_rate:g} 条/分钟；已保留源 XML 供核查"
        )
    return details


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o750)
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS uploads (
                    fingerprint TEXT PRIMARY KEY,
                    video_path TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS upload_stages (
                    fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details_json TEXT,
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (fingerprint, stage),
                    FOREIGN KEY (fingerprint) REFERENCES uploads(fingerprint)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS multipart_sessions (
                    session_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS recording_review_overrides (
                    fingerprint TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (fingerprint) REFERENCES uploads(fingerprint)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS recording_exclusions (
                    video_path TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS uploads_status_updated_idx "
                "ON uploads(status, updated_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS upload_stages_stage_status_updated_idx "
                "ON upload_stages(stage, status, updated_at)"
            )
        try:
            self.path.chmod(0o640)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        """Open a transaction and always release its Windows file handle."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cleanup_expired_retained_xml(self) -> list[str]:
        """Delete only XML files explicitly retained by completed upload tasks."""
        now = datetime.now(timezone.utc)
        deleted: list[str] = []
        with self.connect() as db:
            rows = db.execute(
                """SELECT fingerprint, details_json FROM upload_stages
                   WHERE stage='cleanup' AND status='completed'
                     AND details_json LIKE '%retained_xml_until%'"""
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["details_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                retained_until = str(details.get("retained_xml_until") or "")
                retained_path = str(details.get("retained_xml_path") or "")
                if (
                    not retained_until
                    or not retained_path
                    or details.get("retained_xml_deleted_at")
                ):
                    continue
                try:
                    expires = datetime.fromisoformat(retained_until.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if expires > now:
                    continue
                path = Path(retained_path)
                try:
                    existed = path.exists() or path.is_symlink()
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    details["retained_xml_cleanup_error"] = str(exc)
                else:
                    if existed:
                        deleted.append(str(path))
                    details["retained_xml_deleted_at"] = now.isoformat()
                    details.pop("retained_xml_cleanup_error", None)
                    details["retained"] = [
                        item for item in details.get("retained", [])
                        if str(item) != str(path)
                    ]
                db.execute(
                    """UPDATE upload_stages SET details_json=?, updated_at=?
                       WHERE fingerprint=? AND stage='cleanup'""",
                    (
                        json.dumps(details, ensure_ascii=False, default=str),
                        utc_now(),
                        row["fingerprint"],
                    ),
                )
        return deleted

    def upload_exists(self, key: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM uploads WHERE fingerprint = ? LIMIT 1",
                    (key,),
                ).fetchone()
                is not None
            )

    def exclude_recording(
        self,
        path: Path,
        room_id: str,
        reason: str = "record_only",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO recording_exclusions
                   (video_path, room_id, reason, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(video_path) DO UPDATE SET
                     room_id=excluded.room_id,
                     reason=excluded.reason""",
                (str(path.expanduser().resolve()), room_id, reason, utc_now()),
            )

    def claim(self, key: str, path: Path, platform: str, retry: bool = False) -> bool:
        now = utc_now()
        with self.connect() as db:
            # Serialize the read/claim pair. Multiple recorder workers may finish
            # segments at nearly the same instant.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM uploads WHERE fingerprint = ?", (key,)).fetchone()
            if row and row["status"] == "completed":
                return False
            if row and row["status"] == "processing" and not retry:
                return False
            db.execute(
                """INSERT INTO uploads
                   (fingerprint, video_path, platform, status, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'processing', 1, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     video_path=excluded.video_path, platform=excluded.platform,
                     status='processing', attempts=uploads.attempts + 1,
                     error=NULL, updated_at=excluded.updated_at""",
                (key, str(path), platform, now, now),
            )
            for stage, status in (
                ("detect", "completed"), ("record", "completed"),
                ("ass", "pending"), ("burn", "pending"), ("live_stats", "pending"),
                ("xml_identity", "pending"), ("ai", "pending"),
                ("cover_16x9", "pending"), ("cover_4x3", "pending"),
                ("upload", "pending"), ("collection", "pending"),
                ("comment", "pending"),
                ("cleanup", "pending"),
            ):
                db.execute(
                    """INSERT INTO upload_stages
                       (fingerprint, stage, status, updated_at, started_at, finished_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(fingerprint, stage) DO UPDATE SET
                         status=CASE WHEN excluded.stage IN ('detect', 'record') THEN 'completed'
                                     WHEN upload_stages.status='completed' THEN upload_stages.status
                                     ELSE excluded.status END,
                         error=NULL, updated_at=excluded.updated_at""",
                    (key, stage, status, now, now if status == "completed" else None,
                     now if status == "completed" else None),
                )
            db.execute(
                """UPDATE upload_stages SET details_json=?
                   WHERE fingerprint=? AND stage='record'""",
                (json.dumps({"video_path": str(path), "size_bytes": path.stat().st_size}, ensure_ascii=False), key),
            )
        return True

    def claim_record_only(
        self,
        key: str,
        path: Path,
        room_id: str,
        danmaku_xml: Path | None,
    ) -> bool:
        """Create an inspectable task for local record-only post-processing."""
        now = utc_now()
        result = {
            "room_id": room_id,
            "record_only": True,
            "worker_pid": os.getpid(),
        }
        stages = ("record", "ass", "burn", "cover", "remux", "verify", "cleanup")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM uploads WHERE fingerprint = ?",
                (key,),
            ).fetchone()
            if row and row["status"] in {"completed", "processing"}:
                return False
            db.execute(
                """INSERT INTO uploads
                   (fingerprint, video_path, platform, status, attempts, result_json,
                    created_at, updated_at)
                   VALUES (?, ?, 'record_only', 'processing', 1, ?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     video_path=excluded.video_path,
                     platform='record_only',
                     status='processing',
                     attempts=uploads.attempts + 1,
                     result_json=excluded.result_json,
                     error=NULL,
                     updated_at=excluded.updated_at""",
                (
                    key,
                    str(path),
                    json.dumps(result, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            for stage in stages:
                completed = stage == "record" and danmaku_xml is not None
                details = None
                if completed:
                    details = json.dumps(
                        {
                            "video_path": str(path),
                            "size_bytes": path.stat().st_size,
                            "danmaku_xml": str(danmaku_xml),
                            "safe_finalized": True,
                        },
                        ensure_ascii=False,
                    )
                db.execute(
                    """INSERT INTO upload_stages
                       (fingerprint, stage, status, details_json, error,
                        started_at, finished_at, updated_at)
                       VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                       ON CONFLICT(fingerprint, stage) DO UPDATE SET
                         status=CASE
                           WHEN upload_stages.status='completed' THEN upload_stages.status
                           ELSE excluded.status
                         END,
                         details_json=CASE
                           WHEN upload_stages.status='completed' THEN upload_stages.details_json
                           ELSE excluded.details_json
                         END,
                         error=NULL,
                         started_at=CASE
                           WHEN upload_stages.status='completed' THEN upload_stages.started_at
                           ELSE excluded.started_at
                         END,
                         finished_at=CASE
                           WHEN upload_stages.status='completed' THEN upload_stages.finished_at
                           ELSE excluded.finished_at
                         END,
                         updated_at=excluded.updated_at""",
                    (
                        key,
                        stage,
                        "completed" if completed else "pending",
                        details,
                        now if completed else None,
                        now if completed else None,
                        now,
                    ),
                )
        return True

    def stage(self, key: str, stage: str, status: str, details: Any = None,
              error: str | None = None) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "failed", "skipped", "warning"} else None
        with self.connect() as db:
            db.execute(
                """INSERT INTO upload_stages
                   (fingerprint, stage, status, details_json, error, started_at, finished_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(fingerprint, stage) DO UPDATE SET
                     status=excluded.status,
                     details_json=COALESCE(excluded.details_json, upload_stages.details_json),
                     error=excluded.error,
                     started_at=CASE WHEN excluded.status='running' THEN excluded.started_at
                                     ELSE upload_stages.started_at END,
                     finished_at=excluded.finished_at,
                     updated_at=excluded.updated_at""",
                (key, stage, status,
                 json.dumps(details, ensure_ascii=False, default=str) if details is not None else None,
                 error, started_at, finished_at, now),
            )

    def stage_state(self, key: str, stage: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT status, details_json, error, updated_at
                   FROM upload_stages WHERE fingerprint=? AND stage=?""",
                (key, stage),
            ).fetchone()
        if not row:
            return {}
        try:
            details = json.loads(row["details_json"]) if row["details_json"] else {}
        except (TypeError, json.JSONDecodeError):
            details = {}
        return {
            "status": str(row["status"] or ""),
            "details": details if isinstance(details, dict) else {},
            "error": str(row["error"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def finish(self, key: str, status: str, result: Any = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET status=?, result_json=COALESCE(?, result_json), error=?, updated_at=? WHERE fingerprint=?",
                (status, json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                 error, utc_now(), key),
            )

    def results(self, key: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT result_json FROM uploads WHERE fingerprint=?", (key,)).fetchone()
        if not row or not row["result_json"]:
            return {}
        try:
            value = json.loads(row["result_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def review_override(self, key: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata_json FROM recording_review_overrides WHERE fingerprint=?",
                (key,),
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["metadata_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def save_review_override(self, key: str, metadata: dict[str, Any]) -> None:
        """Persist worker-side review flags without losing editor fields."""
        now = str(metadata.get("updated_at") or utc_now())
        with self.connect() as db:
            db.execute(
                """INSERT INTO recording_review_overrides
                   (fingerprint, metadata_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                     metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at""",
                (key, json.dumps(metadata, ensure_ascii=False, default=str), now),
            )

    def multipart_session(self, session_key: str, *, include_closed: bool = False) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT status, result_json FROM multipart_sessions WHERE session_key=?",
                (session_key,),
            ).fetchone()
        if not row or (row["status"] != "open" and not include_closed):
            return {}
        try:
            value = json.loads(row["result_json"])
            if isinstance(value, dict):
                value["_session_status"] = row["status"]
                return value
            return {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def save_multipart_session(
        self,
        session_key: str,
        result: dict[str, Any],
        *,
        status: str = "open",
    ) -> None:
        now = utc_now()
        stored_result = {key: value for key, value in result.items() if key != "_session_status"}
        payload = json.dumps(stored_result, ensure_ascii=False, default=str)
        with self.connect() as db:
            db.execute(
                """INSERT INTO multipart_sessions
                   (session_key, status, result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_key) DO UPDATE SET
                     status=excluded.status, result_json=excluded.result_json,
                     updated_at=excluded.updated_at""",
                (session_key, status, payload, now, now),
            )

    def upload_session_key(self, key: str) -> str:
        result = self.results(key)
        return str(result.get("multipart_session") or "")

    def close_multipart_session(self, session_key: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE multipart_sessions SET status='closed', updated_at=? "
                "WHERE session_key=? AND status='open'",
                (utc_now(), session_key),
            )
        return cursor.rowcount > 0

    def delete_multipart_session(self, session_key: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM multipart_sessions WHERE session_key=?",
                (session_key,),
            )
        return cursor.rowcount > 0

    def failed_paths(self) -> list[Path]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT video_path FROM uploads WHERE status='failed' ORDER BY updated_at"
            ).fetchall()
        return [Path(row["video_path"]) for row in rows]

    def recent(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM uploads ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()


def find_cover(video: Path, cfg: dict[str, Any], work_dir: Path) -> Path:
    configured = str(cfg.get("cover_path", "")).strip()
    if configured:
        cover = resolve_path(configured, cfg)
        if not cover.is_file():
            raise FileNotFoundError(f"封面不存在: {cover}")
        return cover

    candidates: list[Path] = []
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidates.extend((video.with_suffix(ext), video.parent / "cover" / f"{video.stem}{ext}"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    work_dir.mkdir(parents=True, exist_ok=True)
    cover = work_dir / "cover.jpg"
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    configured_seek = max(0, int(cfg.get("cover_seek_seconds", 10)))
    seek_candidates = list(dict.fromkeys((configured_seek, 3, 1, 0)))
    errors: list[str] = []
    for seek_seconds in seek_candidates:
        cover.unlink(missing_ok=True)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", str(seek_seconds), "-i", str(video),
            "-frames:v", "1", "-q:v", "2", str(cover),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            **_hidden_subprocess_kwargs(),
        )
        if completed.returncode == 0 and cover.is_file() and cover.stat().st_size > 0:
            return cover
        message = completed.stderr.strip()[-500:]
        errors.append(f"{seek_seconds}秒: {message or '未生成图片'}")
    raise RuntimeError(f"FFmpeg 自动截取封面失败（已尝试多个时间点）: {' | '.join(errors)[-1600:]}")


def strip_danmaku_edition_marker(value: str) -> str:
    """Remove the submission-only danmaku marker from cover-facing text."""
    cleaned = re.sub(r"\s*弹幕版\s*", "", str(value or ""))
    cleaned = re.sub(r"[｜|]\s*[｜|]", "｜", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" -_｜|·")


def recording_danmaku_edition_title(title: str) -> str:
    """Place the burned-danmaku edition marker immediately before title time."""
    clean_title = strip_danmaku_edition_marker(title)
    time_match = re.search(
        r"[｜|]\s*(?P<time>\d{1,2}-\d{1,2}\s+\d{1,2}[:：]\d{2})\s*$",
        clean_title,
    )
    if time_match:
        prefix = clean_title[:time_match.start()].rstrip(" ｜|")
        return f"{prefix}｜弹幕版 {time_match.group('time')}"
    return f"{clean_title}｜弹幕版" if clean_title else "弹幕版"


def recording_cover_headline(
    title: str,
    ai_topic: str = "",
    streamer: str = "",
) -> str:
    """Extract a cover-safe headline without dates, clocks or template chrome."""
    title = strip_danmaku_edition_marker(title)
    ai_topic = strip_danmaku_edition_marker(ai_topic)
    generic_topics = {"直播精彩内容", "精彩内容", "直播回放", "精彩直播", "直播录像"}
    candidate = str(ai_topic or "").strip()
    if candidate in generic_topics:
        candidate = ""
    if not candidate:
        parts = [part.strip() for part in re.split(r"[｜|]", str(title or "")) if part.strip()]
        cleaned_parts = []
        for part in parts:
            cleaned = re.sub(r"【[^】]*(?:直播|回放)[^】]*】", "", part)
            cleaned = re.sub(r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b", "", cleaned)
            cleaned = re.sub(r"\b\d{1,2}[-/.月]\d{1,2}(?:日)?\b", "", cleaned)
            cleaned = re.sub(r"\b\d{1,2}[:：]\d{2}(?::\d{2})?\b", "", cleaned)
            cleaned = cleaned.strip(" -_｜|·")
            if cleaned:
                cleaned_parts.append(cleaned)
        if len(cleaned_parts) >= 2:
            candidate = cleaned_parts[1]
        else:
            candidate = cleaned_parts[0] if cleaned_parts else "直播精彩内容"
    candidate = re.sub(r"【[^】]*(?:直播|回放)[^】]*】", "", candidate)
    candidate = re.sub(r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b", "", candidate)
    candidate = re.sub(r"\b\d{1,2}月\d{1,2}日\b", "", candidate)
    candidate = re.sub(r"\b\d{1,2}[:：]\d{2}(?::\d{2})?\b", "", candidate)
    candidate = re.sub(r"\b(?:上午|下午|凌晨|早上|晚上|深夜)?\d{1,2}\s*[点时]\b", "", candidate)
    candidate = re.sub(r"(?:今天|今日|今晚|昨天|明天|凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|深夜)", "", candidate)
    candidate = re.sub(r"[\r\n｜|]+", " ", candidate)
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" -_｜|·")
    candidate = candidate.split("；", 1)[0].strip()
    candidate = candidate or "直播精彩内容"
    subject_name = recording_cover_subject_name(streamer, title, ai_topic)
    headline = candidate
    if (
        ai_topic
        and subject_name
        and topic_mentions_streamer(title, streamer)
        and not topic_mentions_streamer(headline, streamer)
    ):
        headline = f"{subject_name}{candidate}"
    return headline


def recording_cover_display_text(
    headline: str,
    proposed_text: str = "",
    streamer: str = "",
) -> str:
    """Choose compact, grounded cover copy without slicing a sentence."""
    source = normalize_recording_title_filler(
        strip_danmaku_edition_marker(headline).split("；", 1)[0]
    )
    source = source.strip(" -_｜|：:，,。.!！；; ")
    if not source or recording_text_contains_negative_rumor(source):
        return ""

    def normalize_candidate(value: str) -> str:
        candidate = normalize_recording_title_filler(
            strip_danmaku_edition_marker(value)
        )
        candidate = candidate.split("；", 1)[0]
        return candidate.strip(" -_｜|：:，,。.!！；; ")

    def is_grounded(candidate: str) -> bool:
        if not candidate or len(candidate) > RECORDING_COVER_TEXT_HARD_LIMIT:
            return False
        if recording_text_contains_negative_rumor(candidate):
            return False
        if re.search(r"\b20\d{2}\b|\d{1,2}[:：]\d{2}", candidate):
            return False
        if (
            streamer
            and not topic_mentions_streamer(source, streamer)
            and topic_mentions_streamer(candidate, streamer)
        ):
            return False
        for prefix in ("观众讨论", "直播间调侃"):
            if source.startswith(prefix) and not candidate.startswith(prefix):
                return False
        source_key = _compact_alias(source)
        candidate_key = _compact_alias(candidate)
        if not source_key or not candidate_key:
            return False
        if candidate_key in source_key:
            return True
        source_pairs = {
            source_key[index:index + 2]
            for index in range(max(0, len(source_key) - 1))
        }
        candidate_pairs = {
            candidate_key[index:index + 2]
            for index in range(max(0, len(candidate_key) - 1))
        }
        required_overlap = min(3, max(1, len(candidate_pairs) // 3))
        return len(source_pairs & candidate_pairs) >= required_overlap

    proposed = normalize_candidate(proposed_text)
    if is_grounded(proposed):
        return proposed
    if len(source) <= RECORDING_COVER_TEXT_PREFERRED_LIMIT:
        return source

    clauses = [
        normalize_candidate(clause)
        for clause in re.split(r"[，,。.!！?？]", source)
        if normalize_candidate(clause)
    ]
    candidates: list[tuple[int, int, str]] = []
    for index, clause in enumerate(clauses):
        if re.match(r"^(?:但|却|仍|还|又|随后|最终|结果)", clause):
            continue
        if is_grounded(clause):
            candidates.append((recording_title_event_strength(clause), -index, clause))
        if index + 1 < len(clauses):
            combined = f"{clause}，{clauses[index + 1]}"
            if is_grounded(combined):
                candidates.append((recording_title_event_strength(combined), -index, combined))
    if candidates:
        compact_candidates = [
            candidate
            for candidate in candidates
            if len(candidate[2]) <= RECORDING_COVER_TEXT_PREFERRED_LIMIT
        ]
        return max(compact_candidates or candidates)[2]
    if len(source) <= RECORDING_COVER_TEXT_HARD_LIMIT:
        return source
    # No complete, grounded short form was available. A text-free cover is
    # preferable to cutting the title or inventing a slogan.
    return ""


def recording_cover_text_layout_instruction(
    cover_text: str,
    target_size: tuple[int, int],
) -> str:
    """Return aspect-aware copy layout guidance for the image model."""
    text = str(cover_text or "").strip()
    width, height = target_size
    aspect = width / height if height else 1
    if not text:
        return (
            "本次没有可安全压缩的封面短文案：画面不得生成任何标题、字幕、字母或数字，"
            "只用人物、动作和环境表达核心事件。"
        )
    if len(text) <= 10:
        line_rule = "使用单行大字；只有遇到自然语义停顿时才可分成两行"
    elif len(text) <= RECORDING_COVER_TEXT_PREFERRED_LIMIT:
        line_rule = "使用两行大字，每行保持一个完整短语，不得拆词"
    else:
        line_rule = "使用两至三行，按语义短语换行并适当减小字号，不得省略任何字"
    placement = (
        "文字区放在画面左侧或右侧约三分之一处，人物与事件主体占其余主要空间"
        if aspect >= 1.6
        else "文字区放在上方或下方约三分之一处，人物与事件主体保持在视觉中心"
    )
    return (
        f"封面短文案共{len(text)}字：{line_rule}；{placement}。"
        "文字区最多占画面约四成，四周保留至少8%的安全边距，不能贴边、被人物遮挡或在后续裁切中缺字。"
    )


def recording_cover_hero_matches_title(hero: str, title: str) -> bool:
    """Reject telemetry only when the reviewed headline names another hero.

    Event-timestamp-matched GSI is stronger than silence in a natural-language
    title. Requiring every title to repeat the hero discarded valid hero and
    equipment references for headlines such as "高地推进后基地爆炸".
    """
    hero_name = str(hero or "").strip().casefold()
    title_text = str(title or "").casefold()
    if not hero_name or not title_text:
        return False
    title_hero_keys = _dota2_hero_identity_keys(title_text)
    if not title_hero_keys:
        return True
    hero_keys = _dota2_hero_identity_keys(hero_name)
    if hero_keys:
        return bool(hero_keys & title_hero_keys)
    for canonical_name, aliases in _DOTA2_HERO_ALIAS_GROUPS:
        canonical_short = re.split(r"[（(]", canonical_name, maxsplit=1)[0].strip()
        names = {
            canonical_name.casefold(),
            canonical_short.casefold(),
            *(alias.casefold() for alias in aliases),
        }
        if hero_name in names:
            return any(name and name in title_text for name in names)
    return hero_name in title_text


def dota2_gsi_equipment_prompt_instruction(
    main_items: list[str],
    neutral_item: str = "",
    upgrade_states: list[str] | None = None,
) -> str:
    """Describe GSI equipment without conflating slots and extra states."""
    main = [str(item).strip() for item in main_items[:6] if str(item).strip()]
    neutral = str(neutral_item or "").strip()
    upgrades = [
        str(item).strip()
        for item in (upgrade_states or [])
        if str(item).strip()
    ]
    if not main and not neutral and not upgrades:
        return ""
    sections: list[str] = []
    if main:
        sections.append(
            "主播本局最终主装备栏快照（最多六格）："
            + ", ".join(main)
            + "。"
        )
    else:
        sections.append("本段没有可靠的最终主装备栏快照。")
    if neutral:
        sections.append(f"主播本局中立物品：{neutral}；中立物品不占主装备六格。")
    if upgrades:
        sections.append(
            "主播本局额外升级状态："
            + ", ".join(upgrades)
            + "；这些状态不得重复算作第七件主装备。"
        )
    sections.append(
        "只能表现上述已确认的装备与状态，不得增加名单外装备。"
        "装备名称只用于身份识别，禁止按中文或英文名称的字面含义自行设计外形。"
        "只将随附的 Valve 官方装备图标作为物品外形参考，不指定装备在画面中的排列、穿戴或合成方式。"
    )
    return "".join(sections)


def require_dota2_item_reference(
    reference_path: Path | None,
    errors: list[str] | None = None,
) -> Path:
    """Prevent a known-equipment cover from silently losing all item visuals."""
    if reference_path is not None:
        return reference_path
    detail = "；".join(str(error) for error in (errors or []) if str(error).strip())
    raise RuntimeError(
        "Dota 2 官方装备参考不可用，已停止生成以避免发布缺少装备的封面"
        + (f"：{detail}" if detail else "")
    )


def recording_cover_event_context(
    description: str,
    headline: str = "",
) -> tuple[str, str]:
    """Return timestamp-free cover context, preferring verified timeline events."""
    def relevant_events(events: list[str]) -> list[str]:
        events = [event for event in events if event]
        headline_key = _compact_alias(headline)
        if not headline_key or len(headline_key) < 2:
            return events
        headline_pairs = {
            headline_key[index:index + 2]
            for index in range(len(headline_key) - 1)
        }

        def headline_overlap(event: str) -> int:
            event_key = _compact_alias(event)
            event_pairs = {
                event_key[index:index + 2]
                for index in range(max(0, len(event_key) - 1))
            }
            return len(headline_pairs & event_pairs)

        ranked = sorted(
            ((headline_overlap(event), index, event) for index, event in enumerate(events)),
            key=lambda item: (-item[0], item[1]),
        )
        matched = [event for score, _, event in ranked if score > 0][:1]
        return matched or events[:1]

    points = timeline_lines(description)
    if points:
        events = [
            re.sub(r"^\d{1,2}:\d{2}(?::\d{2})?\s+", "", point).strip()
            for point in points
        ]
        events = relevant_events(events)
        return "；".join(events)[:700], "verified_timeline"
    clean_lines = [
        line.strip()
        for line in str(description or "").splitlines()
        if line.strip()
        and not line.strip().startswith(("———", "🎮 ", "🎁 ", "💎 ", "💬 ", "👥 "))
    ]
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？!?；;])", " ".join(clean_lines))
        if sentence.strip()
    ]
    return " ".join(relevant_events(sentences))[:700], "description"


def recording_cover_event_timestamp_seconds(
    description: str,
    event_context: str,
) -> float | None:
    """Return the verified timeline offset corresponding to cover context."""
    context_key = _compact_alias(event_context)
    if not context_key:
        return None
    best: tuple[int, float] | None = None
    for line in timeline_lines(description):
        match = re.match(
            r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s+(.+)$",
            line,
        )
        if not match:
            continue
        hours_text, minutes_text, seconds_text, event = match.groups()
        event_key = _compact_alias(event)
        if not event_key:
            continue
        context_pairs = {
            context_key[index:index + 2]
            for index in range(max(0, len(context_key) - 1))
        }
        event_pairs = {
            event_key[index:index + 2]
            for index in range(max(0, len(event_key) - 1))
        }
        score = len(context_pairs & event_pairs)
        if score <= 0:
            continue
        offset = (
            int(hours_text or 0) * 3600
            + int(minutes_text) * 60
            + int(seconds_text)
        )
        if best is None or score > best[0]:
            best = (score, float(offset))
    return best[1] if best is not None else None


def recording_cover_reference(streamer: str) -> tuple[str, Path] | None:
    """Return a curated identity reference for a known streamer."""
    normalized = normalize_dota2_streamer_name(streamer)
    if normalized == "果小果":
        if GUOXIAOGUO_COVER_REFERENCE.is_file():
            return "果小果", GUOXIAOGUO_COVER_REFERENCE
    if normalized == "DD":
        if XIEBIN_DD_COVER_REFERENCE.is_file():
            return "谢彬DD", XIEBIN_DD_COVER_REFERENCE
    if str(streamer or "").strip() in GUOMIN_DAJIUGE_STREAMER_ALIASES:
        if GUOMIN_DAJIUGE_COVER_REFERENCE.is_file():
            return "国民大舅哥", GUOMIN_DAJIUGE_COVER_REFERENCE
    return None


def recording_cover_reference_instruction(reference_name: str) -> str:
    if reference_name == "果小果":
        return (
            "上传的参考图是主播果小果的固定角色形象。必须以图中角色为唯一原型，"
            "保留深棕色长发、红棕色星光大眼、脸颊红晕、两侧红色蝴蝶结和头顶荷包蛋发饰；"
            "头顶标志必须是荷包蛋发饰：不规则白色蛋白包住圆润的金黄色蛋黄，荷包蛋下方是"
            "醒目的红色大蝴蝶结；绝对不能画成蛋壳、破壳小鸡、普通帽子、花朵或只剩黄色圆点。"
            "保持底稿原有的二次元 Q 版画风，禁止重绘成另一种动漫脸或改成真人，也不要生成成其他角色。"
            "可以根据直播主题受控调整表情、背景、服装和姿势，但脸型比例、五官关系和标志发饰不能明显偏离。"
        )
    if reference_name == "谢彬DD":
        return (
            "上传的参考照片是主播谢彬 DD 本人经过裁切的固定人物底稿。必须以图中同一人为唯一人物原型，"
            "保留短黑发、脸型、眉眼、鼻唇和整体身份辨识度；照片中的黑色夹克与胸前握拳手势只作为"
            "体态参考，可以根据直播主题受控调整表情、服装、动作、光影和背景。人物脸部必须保留底稿的真人原貌与原始画风，"
            "不得动漫化、Q版化、换脸或重新生成另一张相似但不同的脸；英雄服装和游戏背景可以插画化。"
        )
    return (
        f"上传的参考照片是主播 {reference_name} 本人。必须以照片中的人物为唯一人物原型，"
        "直接保留其原有脸部、五官、发型和画风；可以根据直播主题受控调整适度表情、背景、服装和姿势，"
        "但不得动漫化、Q版化、换脸或重新生成成另一个相似人物。"
    )


def download_recording_avatar_reference(url: str, cfg: dict[str, Any]) -> Path:
    """Download and persist a room avatar for reuse by later recording parts."""
    avatar_url = str(url or "").strip()
    if not re.match(r"^https?://", avatar_url, re.IGNORECASE):
        raise ValueError("直播间头像地址无效")
    configured_cache = str(cfg.get("avatar_cache_dir") or "").strip()
    if configured_cache:
        cache_root = resolve_path(configured_cache, cfg)
    else:
        # The bridge state directory is writable and persistent in native and
        # Docker deployments. This avoids the old /data/.avatar-cache owner.
        state_path = resolve_path(
            str(cfg.get("state_db") or ".bridge/state.sqlite3"),
            cfg,
        )
        cache_root = state_path.parent / "avatar-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_root / f"{hashlib.sha256(avatar_url.encode()).hexdigest()[:24]}.jpg"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    request = urllib.request.Request(
        avatar_url,
        headers={"User-Agent": "Mozilla/5.0 PotatoFlow/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as remote:
        raw = remote.read(8 * 1024 * 1024 + 1)
    if not raw:
        raise ValueError("直播间头像为空")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("直播间头像超过 8 MB")
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(raw)
    temporary.replace(destination)
    return destination


def recording_avatar_reference_instruction(
    streamer: str,
) -> str:
    return (
        f"上传的参考图是主播 {streamer or '主播'} 的直播间头像。请优先以头像中的人物、"
        "角色、吉祥物或标志性形象作为封面主体底稿，保持发型、五官、配色、服装特征和"
        "角色辨识度；可以根据直播主题扩展横向背景与动作，但不要替换成无关人物或角色。"
    )


def recording_cover_subject_copy_instruction(
    streamer: str,
    headline: str,
    cover_subject_name: str,
) -> str:
    """Keep owner copy only when the verified headline already names the owner."""
    subject = cover_subject_name or streamer or "主播"
    if topic_mentions_streamer(headline, streamer):
        return (
            f"核心文案已经包含当前主播称呼“{subject}”，必须清晰保留；"
            "称呼可以放在开头或自然融入句子，但不得排成“主角｜主题”的固定栏目格式。"
        )
    return (
        "核心文案没有当前主播称呼，不得为了房间归属强塞主播名、外号或实名；"
        "只能逐字使用已经核验的核心文案。"
    )


_DOTA2_HERO_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("电炎绝手（Snapfire）", ("老奶奶", "电炎绝手", "snapfire")),
    ("风暴之灵（Storm Spirit）", ("蓝猫", "storm spirit")),
    ("灰烬之灵（Ember Spirit）", ("火猫", "ember spirit")),
    ("大地之灵（Earth Spirit）", ("土猫", "earth spirit")),
    ("虚无之灵（Void Spirit）", ("紫猫", "void spirit")),
    ("变体精灵（Morphling）", ("水人", "morphling")),
    ("天穹守望者（Arc Warden）", ("电狗", "arc warden")),
    ("狙击手（Sniper）", ("火枪", "矮子", "sniper")),
    ("裂魂人（Spirit Breaker）", ("白牛", "spirit breaker")),
    ("撼地者（Earthshaker）", ("小牛", "神牛", "earthshaker")),
    ("上古巨神（Elder Titan）", ("大牛", "elder titan")),
    ("熊战士（Ursa）", ("拍拍", "拍拍熊", "ursa")),
    ("影魔（Shadow Fiend）", ("影魔", "sf", "shadow fiend")),
    ("敌法师（Anti-Mage）", ("敌法", "am", "anti-mage")),
    ("幻影长矛手（Phantom Lancer）", ("猴子", "pl", "phantom lancer")),
    ("幻影刺客（Phantom Assassin）", ("幻刺", "pa", "phantom assassin")),
    ("圣堂刺客（Templar Assassin）", ("圣堂", "ta", "templar assassin")),
    ("矮人直升机（Gyrocopter）", ("飞机", "gyrocopter")),
    ("编织者（Weaver）", ("蚂蚁", "weaver")),
    ("斯拉克（Slark）", ("小鱼", "小鱼人", "slark")),
    ("斯拉达（Slardar）", ("大鱼", "大鱼人", "slardar")),
    ("卓尔游侠（Drow Ranger）", ("小黑", "drow ranger")),
    ("美杜莎（Medusa）", ("大娜迦", "美杜莎", "medusa")),
    ("娜迦海妖（Naga Siren）", ("小娜迦", "娜迦", "naga siren")),
    ("克林克兹（Clinkz）", ("骨弓", "小骷髅", "clinkz")),
    ("帕格纳（Pugna）", ("骨法", "pugna")),
    ("水晶室女（Crystal Maiden）", ("冰女", "cm", "crystal maiden")),
    ("莉娜（Lina）", ("火女", "lina")),
    ("痛苦女王（Queen of Pain）", ("女王", "qop", "queen of pain")),
    ("殁境神蚀者（Outworld Destroyer）", ("黑鸟", "od", "outworld destroyer")),
    ("祈求者（Invoker）", ("卡尔", "invoker")),
    ("修补匠（Tinker）", ("tk", "修补匠", "tinker")),
    ("死亡先知（Death Prophet）", ("死亡先知", "dp", "death prophet")),
    ("帕克（Puck）", ("仙女龙", "puck")),
    ("莱席拉克（Leshrac）", ("拉席克", "老鹿", "leshrac")),
    ("食人魔魔法师（Ogre Magi）", ("蓝胖", "ogre magi")),
    ("光之守卫（Keeper of the Light）", ("光法", "kotl", "keeper of the light")),
    ("瘟疫法师（Necrophos）", ("瘟疫法师", "死灵法", "死灵法师", "nec", "necrophos")),
    ("自然先知（Nature's Prophet）", ("先知", "furion", "nature's prophet")),
    ("暗影萨满（Shadow Shaman）", ("小y", "小歪", "shadow shaman")),
    ("干扰者（Disruptor）", ("萨尔", "disruptor")),
    ("戴泽（Dazzle）", ("暗牧", "戴泽", "dazzle")),
    ("工程师（Techies）", ("炸弹人", "炸弹", "techies")),
    ("赏金猎人（Bounty Hunter）", ("赏金", "bh", "bounty hunter")),
    ("力丸（Riki）", ("隐刺", "力丸", "riki")),
    ("噬魂鬼（Lifestealer）", ("小狗", "噬魂鬼", "lifestealer")),
    ("齐天大圣（Monkey King）", ("大圣", "mk", "monkey king")),
    ("主宰（Juggernaut）", ("剑圣", "jugg", "juggernaut")),
    ("冥魂大帝（Wraith King）", ("骷髅王", "wk", "wraith king")),
    ("混沌骑士（Chaos Knight）", ("混沌", "ck", "chaos knight")),
    ("露娜（Luna）", ("月骑", "露娜", "luna")),
    ("恐怖利刃（Terrorblade）", ("tb", "恐怖利刃", "terrorblade")),
    ("虚空假面（Faceless Void）", ("虚空", "faceless void")),
    ("巨魔战将（Troll Warlord）", ("巨魔", "troll", "troll warlord")),
    ("龙骑士（Dragon Knight）", ("龙骑", "dk", "dragon knight")),
    ("钢背兽（Bristleback）", ("钢背", "刚背", "刚被", "bristleback")),
    ("半人马战行者（Centaur Warrunner）", ("人马", "centaur", "centaur warrunner")),
    ("马格纳斯（Magnus）", ("猛犸", "马格纳斯", "magnus")),
    ("潮汐猎人（Tidehunter）", ("潮汐", "tide", "tidehunter")),
    ("军团指挥官（Legion Commander）", ("军团", "lc", "legion commander")),
    ("末日使者（Doom）", ("末日", "doom")),
    ("昆卡（Kunkka）", ("船长", "kunkka")),
    ("孽主（Underlord）", ("大屁股", "孽主", "underlord")),
    ("石鳞剑士（Pangolier）", ("滚滚", "pangolier")),
    ("伐木机（Timbersaw）", ("伐木机", "花母鸡", "timbersaw")),
    ("发条技师（Clockwerk）", ("发条", "clockwerk")),
    ("炼金术士（Alchemist）", ("炼金", "alchemist")),
    ("沙王（Sand King）", ("沙王", "sk", "sand king")),
    ("剃刀（Razor）", ("雷泽", "电魂", "电棍", "razor")),
    ("哈斯卡（Huskar）", ("神灵", "huskar")),
    ("蝙蝠骑士（Batrider）", ("蝙蝠", "batrider")),
    ("兽王（Beastmaster）", ("兽王", "beastmaster")),
    ("斧王（Axe）", ("斧王", "axe")),
    ("帕吉（Pudge）", ("屠夫", "胖子", "pudge")),
    ("巫医（Witch Doctor）", ("巫医", "witch doctor", "wd")),
    # Valve's complete hero roster snapshot. Common community aliases stay in
    # the groups above; these entries keep less frequently discussed heroes
    # from disappearing when GSI is unavailable.
    ("祸乱之源（Bane）", ("祸乱之源", "bane")),
    ("血魔（Bloodseeker）", ("血魔", "bloodseeker")),
    ("米拉娜（Mirana）", ("米拉娜", "白虎", "pom", "mirana")),
    ("斯温（Sven）", ("斯温", "流浪", "流浪剑客", "sven")),
    ("小小（Tiny）", ("小小", "tiny")),
    ("复仇之魂（Vengeful Spirit）", ("复仇之魂", "复仇", "vs", "vengeful spirit", "vengefulspirit")),
    ("风行者（Windranger）", ("风行者", "风行", "windranger", "windrunner")),
    ("宙斯（Zeus）", ("宙斯", "zeus", "zuus")),
    ("巫妖（Lich）", ("巫妖", "lich")),
    ("莱恩（Lion）", ("莱恩", "lion")),
    ("谜团（Enigma）", ("谜团", "enigma")),
    ("术士（Warlock）", ("术士", "warlock")),
    ("剧毒术士（Venomancer）", ("剧毒术士", "剧毒", "venomancer")),
    ("冥界亚龙（Viper）", ("冥界亚龙", "毒龙", "viper")),
    ("黑暗贤者（Dark Seer）", ("黑暗贤者", "黑贤", "dark seer", "dark_seer")),
    ("全能骑士（Omniknight）", ("全能骑士", "全能", "omniknight")),
    ("魅惑魔女（Enchantress）", ("魅惑魔女", "小鹿", "enchantress")),
    ("暗夜魔王（Night Stalker）", ("暗夜魔王", "夜魔", "night stalker", "night_stalker")),
    ("育母蜘蛛（Broodmother）", ("育母蜘蛛", "蜘蛛", "broodmother")),
    ("杰奇洛（Jakiro）", ("杰奇洛", "双头龙", "jakiro")),
    ("陈（Chen）", ("chen",)),
    ("幽鬼（Spectre）", ("幽鬼", "spectre")),
    ("远古冰魄（Ancient Apparition）", ("远古冰魄", "冰魂", "aa", "ancient apparition", "ancient_apparition")),
    ("沉默术士（Silencer）", ("沉默术士", "沉默", "silencer")),
    ("狼人（Lycan）", ("狼人", "lycan")),
    ("酒仙（Brewmaster）", ("酒仙", "熊猫", "brewmaster")),
    ("暗影恶魔（Shadow Demon）", ("暗影恶魔", "毒狗", "sd", "shadow demon", "shadow_demon")),
    ("独行德鲁伊（Lone Druid）", ("独行德鲁伊", "德鲁伊", "熊德", "ld", "lone druid", "lone_druid")),
    ("米波（Meepo）", ("米波", "地卜师", "meepo")),
    ("树精卫士（Treant Protector）", ("树精卫士", "大树", "treant protector", "treant")),
    ("不朽尸王（Undying）", ("不朽尸王", "尸王", "undying")),
    ("拉比克（Rubick）", ("拉比克", "rubick")),
    ("司夜刺客（Nyx Assassin）", ("司夜刺客", "小强", "na", "nyx assassin", "nyx_assassin")),
    ("艾欧（Io）", ("艾欧", "小精灵", "io", "wisp")),
    ("维萨吉（Visage）", ("维萨吉", "死灵龙", "visage")),
    ("巨牙海民（Tusk）", ("巨牙海民", "海民", "tusk")),
    ("天怒法师（Skywrath Mage）", ("天怒法师", "天怒", "skywrath mage", "skywrath_mage")),
    ("亚巴顿（Abaddon）", ("亚巴顿", "死骑", "abaddon")),
    ("凤凰（Phoenix）", ("凤凰", "phoenix")),
    ("神谕者（Oracle）", ("神谕者", "oracle")),
    ("寒冬飞龙（Winter Wyvern）", ("寒冬飞龙", "冰龙", "winter wyvern", "winter_wyvern")),
    ("邪影芳灵（Dark Willow）", ("邪影芳灵", "小仙女", "花仙子", "dark willow", "dark_willow")),
    ("天涯墨客（Grimstroke）", ("天涯墨客", "墨客", "grimstroke")),
    ("玛尔斯（Mars）", ("玛尔斯", "mars")),
    ("森海飞霞（Hoodwink）", ("森海飞霞", "小松鼠", "hoodwink")),
    ("破晓辰星（Dawnbreaker）", ("破晓辰星", "大锤", "锤妹", "dawnbreaker")),
    ("玛西（Marci）", ("玛西", "marci")),
    ("獸（Primal Beast）", ("原始兽", "primal beast", "primal_beast")),
    ("琼英碧灵（Muerta）", ("琼英碧灵", "奶绿", "muerta")),
    ("百戏大王（Ringmaster）", ("百戏大王", "马戏团", "ringmaster")),
    ("凯（Kez）", ("凯", "kez")),
    ("朗戈（Largo）", ("朗戈", "largo")),
)


def _tag_identity_key(value: object) -> str:
    """Return a conservative semantic key for short recording tags."""
    key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())
    if len(key) >= 4 and len(key) % 2 == 0:
        half = len(key) // 2
        if key[:half] == key[half:]:
            key = key[:half]
    return key


def dedupe_recording_tags(tags: Iterable[object], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = str(raw_tag or "").strip()
        key = _tag_identity_key(tag)
        if not tag or not key or key in seen:
            continue
        result.append(tag)
        seen.add(key)
        if limit is not None and len(result) >= limit:
            break
    return result


def _text_mentions_dota2_hero_term(text: object, term: object) -> bool:
    """Match one hero term while rejecting dangerously ambiguous one-char names."""
    folded = str(text or "").casefold()
    candidate = str(term or "").strip().casefold()
    if not folded or not candidate:
        return False
    if re.fullmatch(r"[a-z][a-z0-9' -]*", candidate):
        # Dota has many established two-letter hero aliases (DP, SF, PA,
        # AM, TB, and others). ASCII token boundaries already keep these from
        # matching inside ordinary words such as "dps" or "template", so only
        # one-letter candidates need to remain disabled.
        return len(candidate) >= 2 and bool(re.search(
            rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])",
            folded,
        ))
    compact = _compact_alias(candidate)
    if len(compact) <= 1:
        # Chen/Primal Beast/Kez currently have one-character localized names.
        # Accept those only as an exact field (for example, a structured tag),
        # never as a substring of ordinary Chinese prose such as “陈述”.
        return _compact_alias(folded) == compact
    if candidate == "人马":
        return candidate in folded.replace("原班人马", "")
    return compact in _compact_alias(folded)


def _dota2_hero_identity_keys(value: object) -> set[str]:
    folded = str(value or "").casefold()
    matched: set[str] = set()
    for hero_index, (canonical_name, aliases) in enumerate(_DOTA2_HERO_ALIAS_GROUPS):
        terms = (canonical_name.split("（", 1)[0], *aliases)
        for term in terms:
            if _text_mentions_dota2_hero_term(folded, term):
                matched.add(str(hero_index))
                break
    return matched


def recording_text_mentions_specific_dota2_hero(value: object, hero: object) -> bool:
    """Match a structured hero name even when it is absent from alias groups."""
    text = str(value or "").casefold()
    hero_name = str(hero or "").strip().casefold()
    if not text or not hero_name:
        return False
    normalized_text = _compact_alias(text)
    normalized_hero = _compact_alias(hero_name)
    if len(normalized_hero) > 1 and normalized_hero in normalized_text:
        return True
    for canonical_name, aliases in _DOTA2_HERO_ALIAS_GROUPS:
        terms = (canonical_name.split("（", 1)[0], *aliases)
        normalized_terms = {
            _compact_alias(term)
            for term in terms
            if _compact_alias(term)
        }
        if normalized_hero not in normalized_terms:
            continue
        for term in terms:
            if _text_mentions_dota2_hero_term(text, term):
                return True
    return False


def _contains_unverified_dota2_hero(value: object) -> bool:
    return bool(_dota2_hero_identity_keys(value))


def _timeline_claims_streamer_hero(line: str, streamer: str, hero_key: str) -> bool:
    """Require the room owner to be the nearby subject before a hero mention."""
    normalized_streamer = normalize_dota2_streamer_name(streamer)
    owner_aliases = {str(streamer or ""), normalized_streamer}
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        if canonical_name == normalized_streamer:
            owner_aliases.update(aliases)
            break
    hero_name, hero_aliases = _DOTA2_HERO_ALIAS_GROUPS[int(hero_key)]
    hero_terms = {hero_name.split("（", 1)[0], *hero_aliases}
    opposing_relation = re.compile(
        r"(?:被|击杀|杀(?:了|掉)?|追杀|对阵|对面|面对|克制|输给|战胜|"
        r"围攻|抓(?:死|到)?|切(?:死|入)?|秒(?:了|掉)?|躲(?:开|过)?|逃(?:走|跑)?)",
        re.IGNORECASE,
    )
    reverse_binding = re.compile(
        r"(?:是|才是|就是|给|由|归|属于|的|玩|使用|操刀|选择|选的|拿的|这局|本局)",
        re.IGNORECASE,
    )
    all_hero_spans: list[tuple[int, int, str]] | None = None

    def hero_spans_in_line() -> list[tuple[int, int, str]]:
        nonlocal all_hero_spans
        if all_hero_spans is not None:
            return all_hero_spans
        all_hero_spans = []
        for other_key, (other_name, other_aliases) in enumerate(_DOTA2_HERO_ALIAS_GROUPS):
            for other_term in {other_name.split("（", 1)[0], *other_aliases}:
                if len(_compact_alias(other_term)) <= 1:
                    continue
                all_hero_spans.extend(
                    (start, end, str(other_key))
                    for start, end in _text_name_match_spans(line, other_term)
                )
        return all_hero_spans
    for owner_alias in owner_aliases:
        owner_spans = _text_name_match_spans(line, owner_alias)
        for hero_term in hero_terms:
            if len(_compact_alias(hero_term)) <= 1:
                continue
            hero_spans = _text_name_match_spans(line, hero_term)
            for owner_start, owner_end in owner_spans:
                for hero_start, hero_end in hero_spans:
                    if owner_end <= hero_start:
                        gap = line[owner_end:hero_start]
                        intervening_hero = any(
                            other_key != str(hero_key)
                            and owner_end <= other_start
                            and other_end <= hero_start
                            for other_start, other_end, other_key in hero_spans_in_line()
                        )
                        if (
                            len(gap) <= 16
                            and not intervening_hero
                            and not opposing_relation.search(gap)
                        ):
                            return True
                    elif hero_end <= owner_start:
                        gap = line[hero_end:owner_start]
                        owner_tail = line[owner_end:owner_end + 6]
                        relation = f"{gap}{owner_tail}"
                        intervening_hero = any(
                            other_key != str(hero_key)
                            and hero_end <= other_start
                            and other_end <= owner_start
                            for other_start, other_end, other_key in hero_spans_in_line()
                        )
                        if (
                            len(gap) <= 16
                            and not intervening_hero
                            and not opposing_relation.search(gap)
                            and reverse_binding.search(relation)
                        ):
                            return True
    return False


def _danmaku_owner_hero_evidence(
    streamer: str,
    hero_keys: set[str],
    comments: Iterable[Any],
) -> dict[str, list[str]]:
    """Return heroes repeatedly and directly bound to the owner in raw XML."""
    evidence: dict[str, list[str]] = {}
    for hero_key in hero_keys:
        distinct_mentions: dict[str, tuple[float, str]] = {}
        for comment in comments:
            text = str(getattr(comment, "text", "") or "").strip()
            if not text or not _timeline_claims_streamer_hero(
                text,
                streamer,
                hero_key,
            ):
                continue
            normalized = _compact_alias(text)
            if normalized:
                try:
                    timestamp = float(getattr(comment, "time", 0.0) or 0.0)
                except (TypeError, ValueError):
                    timestamp = 0.0
                distinct_mentions.setdefault(normalized, (timestamp, text))
        # One isolated claim is not enough to turn chat into a player identity.
        # Two independently worded direct bindings within one gameplay-sized
        # window provide a conservative fallback when GSI was unavailable.
        mentions = sorted(distinct_mentions.values())
        for index, (timestamp, _text) in enumerate(mentions):
            nearby = [
                text
                for other_time, text in mentions[index:]
                if other_time - timestamp <= 900
            ]
            if len(nearby) >= 2:
                evidence[hero_key] = nearby[:5]
                break
    return evidence


def _danmaku_hero_presence_evidence(
    hero_keys: set[str],
    comments: Iterable[Any],
) -> dict[str, list[str]]:
    """Return heroes independently repeated in raw XML, regardless of player."""
    evidence: dict[str, list[str]] = {}
    comment_list = list(comments)
    for hero_key in hero_keys:
        hero_name, hero_aliases = _DOTA2_HERO_ALIAS_GROUPS[int(hero_key)]
        hero_terms = {hero_name.split("（", 1)[0], *hero_aliases}
        distinct_mentions: dict[str, tuple[float, str]] = {}
        for comment in comment_list:
            text = str(getattr(comment, "text", "") or "").strip()
            if not text or not any(
                _text_mentions_dota2_hero_term(text, term)
                for term in hero_terms
            ):
                continue
            normalized = _compact_alias(text)
            if normalized:
                try:
                    timestamp = float(getattr(comment, "time", 0.0) or 0.0)
                except (TypeError, ValueError):
                    timestamp = 0.0
                distinct_mentions.setdefault(normalized, (timestamp, text))
        mentions = sorted(distinct_mentions.values())
        for index, (timestamp, _text) in enumerate(mentions):
            nearby = [
                text
                for other_time, text in mentions[index:]
                if other_time - timestamp <= 900
            ]
            if len(nearby) >= 2:
                evidence[hero_key] = nearby[:5]
                break
    return evidence


def filter_unverified_dota2_metadata(
    title_topic: str,
    description: str,
    tags: Iterable[object],
    *,
    streamer: str = "",
    verified_timeline: str = "",
    raw_comments: Iterable[Any] = (),
) -> tuple[str, str, list[str], dict[str, Any]]:
    """Remove unsupported owner-hero claims without deleting ordinary discussion."""
    original_tags = [
        str(tag or "").strip()
        for tag in tags
        if str(tag or "").strip()
    ]
    title_hero_keys = _dota2_hero_identity_keys(title_topic)
    description_hero_keys = _dota2_hero_identity_keys(description)
    tag_hero_keys = set().union(*(
        _dota2_hero_identity_keys(tag)
        for tag in original_tags
    )) if original_tags else set()
    metadata_hero_keys = title_hero_keys | description_hero_keys | tag_hero_keys
    # The model-generated timeline cannot prove itself. Raw XML may provide an
    # independent fallback, but only when multiple distinct comments directly
    # bind a known owner alias to the same hero.
    raw_comment_list = list(raw_comments)
    danmaku_evidence = _danmaku_owner_hero_evidence(
        streamer,
        metadata_hero_keys,
        raw_comment_list,
    )
    hero_presence_evidence = _danmaku_hero_presence_evidence(
        metadata_hero_keys,
        raw_comment_list,
    )
    supported_hero_keys = set(danmaku_evidence)
    present_hero_keys = set(hero_presence_evidence)

    unsupported_title_hero_keys = title_hero_keys - supported_hero_keys
    title_claims_owner_hero = any(
        _timeline_claims_streamer_hero(title_topic, streamer, hero_key)
        for hero_key in unsupported_title_hero_keys
    )
    filtered_topic = (
        title_topic
        if not unsupported_title_hero_keys or not title_claims_owner_hero
        else ""
    )
    filtered_lines: list[str] = []
    for line in str(description or "").splitlines():
        sentences = re.split(r"(?<=[。！？!?])", line)
        kept_sentences: list[str] = []
        for sentence in sentences:
            unsupported_sentence_hero_keys = (
                _dota2_hero_identity_keys(sentence) - supported_hero_keys
            )
            claims_owner_hero = any(
                _timeline_claims_streamer_hero(sentence, streamer, hero_key)
                for hero_key in unsupported_sentence_hero_keys
            )
            if not claims_owner_hero:
                kept_sentences.append(sentence)
        filtered_line = "".join(kept_sentences)
        # A removed timestamp line must not leave a blank placeholder. Preserve
        # only blank lines that were already present in the source description.
        if line.strip() and not filtered_line.strip():
            continue
        filtered_lines.append(filtered_line)
    filtered_description = "\n".join(filtered_lines).strip()
    hero_tags = [
        tag for tag in original_tags
        if (
            (tag_hero_keys := _dota2_hero_identity_keys(tag))
            and not tag_hero_keys <= (supported_hero_keys | present_hero_keys)
        )
    ]
    filtered_tags = dedupe_recording_tags(tag for tag in original_tags if tag not in hero_tags)
    details = {
        "unverified_hero_topic_removed": filtered_topic != title_topic,
        "unverified_hero_description_removed": filtered_description != str(description or "").strip(),
        "unverified_hero_tags_removed": hero_tags,
        "verified_timeline_hero_evidence": [
            _DOTA2_HERO_ALIAS_GROUPS[int(key)][0]
            for key in sorted(supported_hero_keys, key=int)
        ],
        "hero_evidence_source": (
            "danmaku_owner_hero_consensus" if supported_hero_keys else "none"
        ),
        "danmaku_owner_hero_evidence": {
            _DOTA2_HERO_ALIAS_GROUPS[int(key)][0]: values
            for key, values in danmaku_evidence.items()
        },
        "danmaku_hero_presence_evidence": {
            _DOTA2_HERO_ALIAS_GROUPS[int(key)][0]: values
            for key, values in hero_presence_evidence.items()
        },
    }
    return filtered_topic, filtered_description, filtered_tags, details


def recording_cover_danmaku_game_context(
    evidence_details: dict[str, Any],
    *verified_content: str,
) -> dict[str, Any] | None:
    """Build a hero-only cover context from the XML owner/hero consensus."""
    if evidence_details.get("hero_evidence_source") != "danmaku_owner_hero_consensus":
        return None
    heroes = list(dict.fromkeys(
        str(value or "").split("（", 1)[0].strip()
        for value in evidence_details.get("verified_timeline_hero_evidence", [])
        if str(value or "").strip()
    ))
    matching = [
        hero
        for hero in heroes
        if any(
            recording_text_mentions_specific_dota2_hero(content, hero)
            for content in verified_content
        )
    ]
    if len(matching) != 1:
        return None
    return {
        "hero": matching[0],
        "items": [],
        "neutral": "",
        "scepter": False,
        "shard": False,
        "identity_source": "xml_repeated_owner_hero_relation",
    }


_DOTA2_ITEM_CONTEXT_ALIASES = (
    "bkb",
    "mkb",
    "a杖",
    "a魔晶",
    "跳刀",
    "力量跳",
    "敏捷跳",
    "智力跳",
    "羊刀",
    "大根",
    "大灵匣",
    "小灵匣",
    "大吹风",
    "推推",
    "大推推",
    "大炮",
    "小炮",
    "大电锤",
    "小电锤",
    "大隐刀",
    "大晕锤",
    "小晕锤",
    "大散失",
    "大骨灰",
    "大支配",
    "大勋章",
)


def recording_cover_has_dota2_context(streamer: str, *content: str) -> bool:
    """Avoid treating ordinary words as Dota items on unrelated streams."""
    combined = "\n".join(str(value or "") for value in content).casefold()
    if re.search(r"(?<![a-z0-9])dota\s*2?(?![a-z0-9])|刀塔", combined):
        return True
    # A streamer who usually plays Dota 2 can still switch to an unrelated
    # RPG.  Room identity alone therefore cannot authorize item matching:
    # generic words such as "刷新" and "宝石" otherwise become Refresh Orb
    # and Gem of True Sight badges on a non-Dota cover.  Reliable GSI is
    # handled before this fallback; without it, require an actual hero or a
    # deliberately strong Dota item alias in the selected event text.
    if _contains_unverified_dota2_hero(combined):
        return True
    return any(alias.casefold() in combined for alias in _DOTA2_ITEM_CONTEXT_ALIASES)


def recording_cover_dota2_instruction(*content: str) -> str:
    """Resolve common Chinese Dota 2 hero nicknames for the image prompt."""
    combined = "\n".join(str(value or "") for value in content)
    folded = combined.casefold()
    matched: list[str] = []
    for canonical_name, aliases in _DOTA2_HERO_ALIAS_GROUPS:
        for alias in sorted(aliases, key=len, reverse=True):
            alias_folded = alias.casefold()
            if re.fullmatch(r"[a-z][a-z0-9' -]*", alias_folded):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                    folded,
                )
            else:
                found = alias_folded in folded
            if found:
                matched.append(f"{alias}＝{canonical_name}")
                break

    resolved = "；".join(matched) if matched else "本次未检出可确定的英雄俗称"
    literal_cat_rules: list[str] = []
    if any("Storm Spirit" in item for item in matched):
        literal_cat_rules.append(
            "特别注意：蓝猫只能是风暴之灵（Storm Spirit）——蓝色皮肤、宽体型男性元素之灵、"
            "蓝色东方长袍与圆帽、环绕闪电能量；绝对不能画成蓝色猫、猫咪吉祥物或其他作品的猫。"
        )
    if any("Void Spirit" in item for item in matched):
        literal_cat_rules.append(
            "特别注意：紫猫只能是虚无之灵（Void Spirit）——紫色能量、白发白须、紫白护甲与双刃的"
            "男性元素之灵；绝对不能画成紫色猫、猫咪吉祥物或其他作品的猫。"
        )
    return (
        "Dota 2 游戏角色消歧规则：如果标题或摘要涉及 DOTA、Dota 2、刀塔，或出现英雄俗称，"
        "必须把它理解为 Valve《Dota 2》的对应英雄，并按该英雄在 Dota 2 中可辨识的体型、"
        "服装、主色、武器与技能特效来设计；禁止按词语字面画成动物、普通人物，也禁止混入"
        "《英雄联盟》、宝可梦或其他作品的角色。"
        f"本次识别结果：{resolved}。"
        f"{''.join(literal_cat_rules)}"
        "若摘要里还有未列出的 Dota 2 俗称，应先在语义上还原为该英雄的中英文正式名再作画；"
        "无法确定时宁可使用 Dota 2 对局氛围和技能特效，不要凭字面臆造角色。"
    )


def recording_cover_dota2_streamer_instruction(
    streamer: str,
    *content: str,
) -> str:
    """Resolve common Dota 2 streamer nicknames without replacing the cover subject."""
    combined = "\n".join((str(streamer or ""), *(str(value or "") for value in content)))
    folded = combined.casefold()
    normalized_streamer = normalize_dota2_streamer_name(streamer)
    matched: list[str] = []
    seen: set[str] = set()
    for canonical_name, aliases in _dota2_streamer_alias_groups_for_content(
        combined
    ):
        found_alias = ""
        for alias in sorted(aliases, key=len, reverse=True):
            alias_folded = alias.casefold()
            if re.fullmatch(r"[a-z][a-z0-9_ -]*", alias_folded):
                found = re.search(
                    rf"(?<![a-z0-9]){re.escape(alias_folded)}(?![a-z0-9])",
                    folded,
                )
            else:
                found = alias_folded in folded
            if found:
                found_alias = alias
                break
        if (
            canonical_name == normalized_streamer
            or found_alias
        ) and canonical_name not in seen:
            seen.add(canonical_name)
            matched.append(
                f"{found_alias or streamer}＝Dota 2 主播/选手 {canonical_name}"
            )
    if not matched:
        return (
            "斗鱼 Dota 2 主播昵称规则：遇到主播昵称或职业选手外号时，应结合 Dota 2 语境理解，"
            "不要把昵称按字面画成动物、职业或陌生虚构人物；无法确认身份时不要擅自换脸。"
        )
    return (
        "斗鱼 Dota 2 主播昵称消歧："
        + "；".join(matched)
        + "。这些映射只用于理解标题和事件；封面主体仍必须以当前直播间的封面人物底稿为准，"
        "其他被提及选手不能取代主播成为另一张脸。"
    )


def recording_cover_verified_hero_cosplay_instruction(
    hero: str,
    *,
    gameplay_verified: bool,
) -> str:
    """Describe only the verified hero fact without imposing a composition mode."""
    hero_name = str(hero or "").strip()
    if not hero_name or not gameplay_verified:
        return (
            "本段没有结构化数据确认当前主播亲自使用某个英雄；"
            "不得让主播穿成被观战、被讨论或仅由弹幕猜测的英雄。"
        )
    return f"结构化游戏数据已确认当前主播亲自使用 {hero_name}。"


def recording_cover_streamer_role_instruction(
    streamer: str,
    title: str,
) -> tuple[str, str]:
    """Keep the cover's room-owner role consistent with the final title."""
    title_text = str(title or "")
    if topic_mentions_streamer(title_text, streamer) and re.search(
        r"(?:观战|观赛|旁观|OB|看比赛|看决赛|解说|点评)",
        title_text,
        re.IGNORECASE,
    ):
        return (
            "spectating",
            "当前主播在本段是观战、解说或点评者。封面仍以当前主播头像为主视觉入口，"
            "但只能表现观看、关注或反应，禁止把第三方选手的操作、英雄或冠军结果画成"
            "当前主播完成；比赛事件应作为背景或次要叙事层。",
        )
    if topic_mentions_streamer(title_text, streamer) and re.search(
        r"(?:直播间(?:热议|讨论|关注)|热议|讨论)",
        title_text,
        re.IGNORECASE,
    ):
        return (
            "room_discussion",
            "当前主播只是直播间身份与封面主视觉入口，标题没有证明其参赛或观战。"
            "只能表现直播间关注、讨论或自然反应，不得把标题中的第三方动作、英雄、"
            "胜负或荣誉归给当前主播。",
        )
    return (
        "default_owner",
        "当前主播是封面默认主视觉身份；具体动作、英雄、胜负和荣誉仍只能沿用最终标题"
        "与已核验简介明确归属于当前主播的事实。",
    )


@contextmanager
def image_generation_queue(cfg: dict[str, Any]):
    """Serialize image-model requests across web threads and bridge processes."""
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    lock_path = state_path.parent / "image-generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    wait_started = time.monotonic()
    with _IMAGE_GENERATION_THREAD_LOCK, lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not _queue_lock_is_busy(exc):
                        raise
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield time.monotonic() - wait_started
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def ai_metadata_queue(cfg: dict[str, Any]):
    """Finish one recording's AI description/title flow before the next."""
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    concurrency = 1
    lock_dir = state_path.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    wait_started = time.monotonic()
    handle = None
    while handle is None:
        for slot in range(concurrency):
            candidate = (lock_dir / f"ai-metadata-{slot}.lock").open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    candidate.seek(0, os.SEEK_END)
                    if candidate.tell() == 0:
                        candidate.write(b"\0")
                        candidate.flush()
                    candidate.seek(0)
                    msvcrt.locking(candidate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = candidate
                break
            except OSError as exc:
                candidate.close()
                if not _queue_lock_is_busy(exc):
                    raise
        if handle is None:
            time.sleep(0.1)
    try:
        yield time.monotonic() - wait_started
    finally:
        if handle is not None:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


@contextmanager
def ai_metadata_request_slot(cfg: dict[str, Any]):
    """Share a hard request-concurrency cap across all metadata tasks."""
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    concurrency = max(
        1,
        min(8, int(cfg.get("ai_metadata_request_concurrency", 3) or 3)),
    )
    lock_dir = state_path.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    while handle is None:
        for slot in range(concurrency):
            candidate = (lock_dir / f"ai-metadata-request-{slot}.lock").open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    candidate.seek(0, os.SEEK_END)
                    if candidate.tell() == 0:
                        candidate.write(b"\0")
                        candidate.flush()
                    candidate.seek(0)
                    msvcrt.locking(candidate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle = candidate
                break
            except OSError as exc:
                candidate.close()
                if not _queue_lock_is_busy(exc):
                    raise
        if handle is None:
            time.sleep(0.05)
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def multipart_session_queue(cfg: dict[str, Any], session_key: str):
    """Serialize every state read and submission belonging to one multipart session."""
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    digest = hashlib.sha256(str(session_key).encode("utf-8")).hexdigest()[:20]
    lock_path = state_path.parent / f"multipart-session-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _MULTIPART_SESSION_THREAD_LOCKS[
        int(digest[:8], 16) % len(_MULTIPART_SESSION_THREAD_LOCKS)
    ]
    with thread_lock, lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not _queue_lock_is_busy(exc):
                        raise
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def generate_recording_cover_with_ai(
    title: str,
    ai_topic: str,
    description: str,
    streamer: str,
    cfg: dict[str, Any],
    work_dir: Path,
    target_size: tuple[int, int] | None = None,
    output_path: Path | None = None,
    recording_dir: Path | None = None,
    game_context: dict[str, Any] | None = None,
    game_context_locked: bool = False,
    cover_text: str = "",
    shared_reference_cache: dict[str, Any] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Generate one independent AI cover for the requested Bilibili aspect ratio."""
    root = resolve_app_root(cfg)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from modules.ai_enhancer import get_openai_client  # type: ignore
    from modules.config_manager import load_config as load_app_config  # type: ignore

    ai_cfg = load_app_config()
    enabled = bool(ai_cfg.get("AI_GENERATE_RECORDING_COVER", False))
    cover_event_context, cover_context_source = recording_cover_event_context(description)
    cover_subject_name = recording_cover_subject_name(streamer, title)
    headline = recording_cover_headline(title, "", streamer)
    details: dict[str, Any] = {
        "ai_cover_enabled": enabled,
        "ai_cover_headline": headline,
        "ai_cover_subject_name": cover_subject_name,
        "ai_cover_excludes_time": True,
        "ai_cover_context_source": cover_context_source,
        "ai_cover_event_context": cover_event_context,
    }
    if not enabled:
        return None, details
    image_api_key = str(
        ai_cfg.get("OPENAI_IMAGE_API_KEY")
        or ai_cfg.get("OPENAI_API_KEY")
        or ""
    ).strip()
    if not image_api_key:
        raise ValueError("未配置图片或全局 AI API Key，无法生成录播封面")

    image_model = str(ai_cfg.get("OPENAI_IMAGE_MODEL_NAME") or "gpt-image-2").strip()
    image_base_url = str(ai_cfg.get("OPENAI_IMAGE_BASE_URL") or "").strip()
    client_config = dict(ai_cfg)
    client_config["OPENAI_API_KEY"] = image_api_key
    if image_base_url:
        client_config["OPENAI_BASE_URL"] = image_base_url
    custom_reference_value = str(cfg.get("cover_reference_path") or "").strip()
    custom_reference_path = Path(custom_reference_value).expanduser()
    if custom_reference_value and not custom_reference_path.is_absolute():
        custom_reference_path = WORKSPACE_ROOT / custom_reference_path
    if custom_reference_value and not custom_reference_path.is_file():
        raise FileNotFoundError(f"封面人物底稿不存在: {custom_reference_path}")
    custom_reference = (
        (normalize_dota2_streamer_name(streamer) or streamer, custom_reference_path)
        if custom_reference_value and custom_reference_path.is_file()
        else None
    )
    reference = custom_reference or recording_cover_reference(streamer)
    reference_kind = "custom" if custom_reference else ("dedicated" if reference else "")
    if not reference:
        avatar_url = str(cfg.get("streamer_avatar_url") or "").strip()
        if not avatar_url:
            raise ValueError(
                f"主播 {streamer or '未知主播'} 未配置封面人物底稿，"
                "且未获取到该直播间头像，无法生成 AI 封面"
            )
        try:
            avatar_reference = download_recording_avatar_reference(avatar_url, cfg)
        except Exception as exc:
            raise ValueError(
                f"主播 {streamer or '未知主播'} 未配置封面人物底稿，"
                f"且该直播间头像不可用: {exc}"
            ) from exc
        reference = (
            normalize_dota2_streamer_name(streamer) or streamer or "主播",
            avatar_reference,
        )
        reference_kind = "avatar"
    reference_name = reference[0]
    reference_paths: list[Path] = [reference[1]]
    reference_roles: list[str] = [
        (
            f"当前直播间主播 {reference_name or streamer or '主播'} 的人物身份底稿；"
            "这是当前主播脸部、发型、服装、配饰和画风的唯一身份来源"
        )
    ]

    def add_cover_reference(path: Path, role: str) -> int:
        """Append one reference with an explicit, index-aligned visual role."""
        if path in reference_paths:
            index = reference_paths.index(path)
            if role not in reference_roles[index]:
                reference_roles[index] = f"{reference_roles[index]}；{role}"
            return index + 1
        reference_paths.append(path)
        reference_roles.append(role)
        return len(reference_paths)
    if reference_kind == "dedicated":
        reference_instruction = recording_cover_reference_instruction(reference_name)
    elif reference_kind == "custom":
        reference_instruction = (
            f"上传的参考图是用户为主播 {streamer or '主播'} 指定的人物形象底稿，"
            "必须把图中的人物或角色作为封面唯一主角，严格保持脸部、发型、服装、"
            "标志性配饰、主色与画风的辨识度。可以根据本段内容调整表情、动作和背景，"
            "但不得换脸、真人化或替换成其他角色。"
        )
    elif reference_kind == "avatar":
        reference_instruction = recording_avatar_reference_instruction(streamer)
    else:
        reference_instruction = ""
    # The current streamer's custom character reference or room avatar is the
    # primary identity source. Only people explicitly named in the final
    # submission title may add a guest reference. Timeline descriptions often
    # mention many players incidentally; attaching those avatars lets a clearer
    # guest photo override a stylized current-room reference.
    guest_candidates = recording_cover_guest_candidates(streamer, title)
    guest_references: list[dict[str, str]] = []
    guest_reference_errors: list[dict[str, str]] = []
    for guest_candidate in guest_candidates:
        try:
            resolved_guest = resolve_recording_guest_avatar(guest_candidate, cfg)
            if resolved_guest is None:
                guest_reference_errors.append({
                    "name": guest_candidate["name"],
                    "error": "斗鱼搜索未形成唯一精确匹配",
                })
                continue
            guest_reference = download_recording_avatar_reference(
                resolved_guest["avatar_url"],
                cfg,
            )
            reference_index = add_cover_reference(
                guest_reference,
                (
                    f"最终投稿标题中的次要人物 {resolved_guest['name']} 的身份头像；"
                    "只定义该次要人物，不得改变或混入当前主播身份"
                ),
            )
            guest_references.append({
                **resolved_guest,
                "reference_path": str(guest_reference),
                "reference_index": str(reference_index),
            })
        except Exception as exc:
            guest_reference_errors.append({
                "name": guest_candidate["name"],
                "error": str(exc),
            })
    details["ai_cover_guest_streamers"] = guest_references
    details["ai_cover_guest_reference_errors"] = guest_reference_errors
    details["ai_cover_guest_candidate_source"] = "submission_title"
    if guest_references:
        guest_identity_instruction = (
            "额外主播身份参考："
            + "；".join(
                f"Image {guest['reference_index']} 对应标题中的“{guest['mentioned_as']}”＝主播 {guest['name']}"
                for guest in guest_references
            )
            + "。只有内容确实需要该主播出镜时才能添加，其外观必须依据对应头像；"
            "不得与当前直播间主角的封面人物底稿混合或换脸。"
        )
    elif guest_candidates:
        guest_identity_instruction = (
            "最终投稿标题提到了其他主播，但未能从已保存直播间或斗鱼接口取得唯一可靠的头像；"
            "禁止在画面中生成或猜测该人物的脸。"
        )
    else:
        guest_identity_instruction = (
            "最终投稿标题未发现可可靠出镜的其他主播；不要根据简介、时间线、普通人名、"
            "弹幕用户名或职业选手外号"
            "自行增加陌生人物。"
        )
    dota2_instruction = recording_cover_dota2_instruction(
        title,
        ai_topic,
        description,
    )
    # Prefer Douyu's explicit streamer-view hero and its final in-recording
    # equipment snapshot. XML identity is retained only for legacy snapshots.
    tooltip_hero = ""
    tooltip_items: list[str] = []
    tooltip_kda_instruction = ""
    tooltip_context_enabled = bool(cfg.get("douyu_stats_enabled", True)) and bool(
        cfg.get("douyu_stats_cover_context_enabled", True)
    )
    details["ai_cover_tooltip_context_enabled"] = tooltip_context_enabled
    if tooltip_context_enabled and (game_context_locked or recording_dir is not None):
        try:
            anchor = game_context
            if not game_context_locked and recording_dir is not None:
                from modules.douyu_stats_formatter import get_game_for_cover  # type: ignore
                anchor = get_game_for_cover(recording_dir)
            if anchor and not recording_cover_hero_matches_title(
                str(anchor.get("hero") or ""),
                f"{title}\n{cover_event_context}",
            ):
                details["ai_cover_hero_context_rejected"] = str(
                    anchor.get("hero") or ""
                )
                anchor = None
            if anchor:
                tooltip_hero = str(anchor.get("hero") or "")
                tooltip_items = [
                    str(item) for item in anchor.get("items", [])[:6] if str(item)
                ]
                if anchor.get("neutral"):
                    tooltip_items.append(str(anchor["neutral"]))
                if anchor.get("scepter"):
                    tooltip_items.append("A杖")
                if anchor.get("shard"):
                    tooltip_items.append("魔晶")
                if all(key in anchor for key in ("kills", "deaths", "assists")):
                    tooltip_kda_instruction = (
                        f"主播本局最终 K/D/A 为 {anchor['kills']}/{anchor['deaths']}/"
                        f"{anchor['assists']}，KDA 为 {anchor.get('kda')}。"
                    )
                    details["ai_cover_streamer_kda"] = {
                        "kills": anchor["kills"],
                        "deaths": anchor["deaths"],
                        "assists": anchor["assists"],
                        "kda": anchor.get("kda"),
                    }
                details["ai_cover_identity_source"] = str(
                    anchor.get("identity_source") or ""
                )
        except Exception as exc:
            details["ai_cover_tooltip_error"] = str(exc)

    if tooltip_hero or tooltip_items:
        if tooltip_items:
            dota2_item_instruction = (
                f"主播本局最终六格主装备（最后一次有效阵容快照）："
                f"{', '.join(tooltip_items)}。"
                "只能表现这份列表中的主装备，数量不得超过列表数量；不得额外添加第七件装备。"
                "装备名称只用于身份识别，禁止按中文或英文名称的字面含义自行设计外形。"
                "禁止在封面底部或任何位置生成物品栏、装备卡槽、装备图标排布或游戏 UI；"
                "装备只可作为角色造型与场景语义参考，不得绘制仿冒的装备图标。"
            )
        else:
            dota2_item_instruction = ""
        if tooltip_hero:
            identity_source = str(details.get("ai_cover_identity_source") or "")
            hero_only_from_danmaku = identity_source in {
                "xml_repeated_hero_only",
                "xml_dominant_hero_only",
                "xml_repeated_owner_hero_relation",
            }
            if hero_only_from_danmaku:
                hero_source_instruction = (
                    "（由本段完整 XML 弹幕中独立重复或同一时段集中刷屏、且唯一占优的英雄讨论确认；"
                    "本证据只确认英雄，不确认任何装备）"
                )
            else:
                hero_source_instruction = "（来自斗鱼主播视角数据）"
            dota2_instruction = (
                f"主播本局使用的英雄为 {tooltip_hero}{hero_source_instruction}。"
                + (
                    f"画面中的主播游戏角色只能是 {tooltip_hero}；"
                    "不得根据标题、简介或常识补画其他具体英雄、装备、物品栏或游戏 UI。"
                    if hero_only_from_danmaku
                    else ""
                )
                + tooltip_kda_instruction
                + dota2_instruction
            )
        details["ai_cover_tooltip_hero"] = tooltip_hero
        details["ai_cover_tooltip_items"] = tooltip_items
        details["ai_cover_dota2_source"] = (
            "danmaku_hero"
            if str(details.get("ai_cover_identity_source") or "") in {
                "xml_repeated_hero_only",
                "xml_dominant_hero_only",
                "xml_repeated_owner_hero_relation",
            }
            else "tooltip"
        )
        dota2_item_matches = match_dota2_items(*tooltip_items)
        dota2_item_instruction += dota2_item_prompt_instruction(dota2_item_matches)
    elif game_context_locked:
        dota2_item_matches = []
        dota2_instruction = (
            "本段没有可靠匹配到主播同一场对局。禁止展示、猜测或补画任何具体 "
            "DOTA 2 英雄；如需游戏氛围，只能使用不含角色身份的抽象场景。"
        )
        dota2_item_instruction = (
            "本段没有可靠匹配到主播同一场对局的英雄与装备数据。"
            "禁止展示、猜测或补画任何具体 DOTA 2 英雄和装备图标。"
        )
        details["ai_cover_dota2_source"] = "locked_no_match"
    else:
        dota2_item_matches = (
            match_dota2_items(title, ai_topic, description)
            if recording_cover_has_dota2_context(
                streamer,
                title,
                ai_topic,
                description,
            )
            else []
        )
        dota2_item_instruction = dota2_item_prompt_instruction(dota2_item_matches)
        details["ai_cover_dota2_source"] = "text_match"
    if dota2_item_matches:
        item_reference_path, item_reference_errors = build_dota2_item_reference_sheet(
            dota2_item_matches,
            Path("/data/cache/dota2/items"),
            work_dir / "dota2_item_references.png",
        )
        details["ai_cover_dota2_items"] = [
            {
                "alias": match.alias,
                "chinese_name": match.item.chinese_name,
                "english_name": match.item.english_name,
                "icon_slug": match.item.icon_slug,
            }
            for match in dota2_item_matches
        ]
        details["ai_cover_dota2_item_reference_errors"] = item_reference_errors
        if item_reference_path is not None:
            add_cover_reference(
                item_reference_path,
                "Valve 官方装备图标表；只定义装备外观，不得提供、改变或生成任何人物身份",
            )
            details["ai_cover_dota2_item_reference_used"] = True
            details["ai_cover_dota2_item_reference_path"] = str(item_reference_path)
        else:
            details["ai_cover_dota2_item_reference_used"] = False
            dota2_item_instruction = (
                "本局装备的官方图标参考不可用。为避免画错装备，禁止展示任何具体装备图标。"
            )
    if tooltip_hero:
        hero_reference_path, official_hero, hero_reference_error = (
            build_dota2_hero_reference(
                tooltip_hero,
                Path("/data/cache/dota2/heroes"),
                work_dir / "dota2_hero_reference.png",
            )
        )
        details["ai_cover_dota2_official_hero"] = (
            {
                "chinese_name": official_hero.chinese_name,
                "english_name": official_hero.english_name,
                "icon_slug": official_hero.icon_slug,
            }
            if official_hero
            else None
        )
        details["ai_cover_dota2_hero_reference_error"] = hero_reference_error
        if hero_reference_path is not None:
            add_cover_reference(
                hero_reference_path,
                (
                    f"Valve 官方 Dota 2 英雄 {tooltip_hero} 参考；只定义游戏英雄外观，"
                    "不得作为当前主播或任何真人的脸部参考"
                ),
            )
            details["ai_cover_dota2_hero_reference_used"] = True
            details["ai_cover_dota2_hero_reference_path"] = str(hero_reference_path)
            dota2_instruction = (
                f"随附的 DOTA 2 OFFICIAL HERO REFERENCE 是 {tooltip_hero} 的 Valve 官方英雄参考。"
                "若画面出现该英雄，必须保持官方脸部、体型、护甲、武器、轮廓和主色特征；"
                "不得替换成其他英雄、其他游戏角色或仅凭中文名称臆造。"
                + dota2_instruction
            )
        else:
            details["ai_cover_dota2_hero_reference_used"] = False
            dota2_instruction = (
                "本局英雄的官方参考图不可用。为避免画错英雄，禁止展示任何具体英雄。"
            )
    dota2_ability_matches = (
        match_dota2_abilities(title, ai_topic, description)
        if recording_cover_has_dota2_context(
            streamer,
            title,
            ai_topic,
            description,
        )
        else []
    )
    dota2_ability_instruction = dota2_ability_prompt_instruction(
        dota2_ability_matches
    )
    if dota2_ability_matches:
        ability_reference_path, ability_reference_errors = (
            build_dota2_ability_reference_sheet(
                dota2_ability_matches,
                resolve_path(".dota2-ability-cache", cfg),
                work_dir / "dota2_ability_references.png",
            )
        )
        details["ai_cover_dota2_abilities"] = [
            {
                "alias": match.alias,
                "hero_chinese_name": match.ability.hero_chinese_name,
                "hero_english_name": match.ability.hero_english_name,
                "chinese_name": match.ability.chinese_name,
                "english_name": match.ability.english_name,
                "icon_slug": match.ability.icon_slug,
            }
            for match in dota2_ability_matches
        ]
        details["ai_cover_dota2_ability_reference_errors"] = (
            ability_reference_errors
        )
        if ability_reference_path is not None:
            add_cover_reference(
                ability_reference_path,
                "Valve 官方 Dota 2 技能图标表；只定义技能视觉元素，不得提供或改变人物身份",
            )
            details["ai_cover_dota2_ability_reference_used"] = True
            details["ai_cover_dota2_ability_reference_path"] = str(
                ability_reference_path
            )
        else:
            details["ai_cover_dota2_ability_reference_used"] = False
    dota2_streamer_instruction = recording_cover_dota2_streamer_instruction(
        streamer,
        title,
    )
    reference_map_instruction = "\n".join(
        f"Image {index}: {role}。"
        for index, role in enumerate(reference_roles, start=1)
    )
    target_width, target_height = target_size or (1146, 717)
    orientation = "横向" if target_width >= target_height else "竖向"
    aspect_label = (
        "16:10"
        if target_size is None
        else f"{target_width}:{target_height}"
    )
    if abs((target_width / target_height) - (16 / 9)) < 0.02:
        composition_instruction = (
            "这是个人空间横向封面。主体和唯一标题必须完整留在 16:9 横向安全区域，"
            "左右保留呼吸空间，适合个人空间大图展示。"
        )
        cover_variant = "16x9"
    elif abs((target_width / target_height) - (4 / 3)) < 0.02:
        composition_instruction = (
            "这是首页推荐 4:3 卡片封面。重新采用更集中、更紧凑的独立构图，"
            "主体和唯一标题靠近视觉中心，不能沿用或模拟 16:9 封面的裁切结果。"
        )
        cover_variant = "4x3"
    else:
        composition_instruction = "请针对目标画幅独立构图，主体和标题均保持完整。"
        cover_variant = aspect_label.replace(":", "x")
    cover_subject_copy_instruction = recording_cover_subject_copy_instruction(
        streamer,
        headline,
        cover_subject_name,
    )
    prompt = f"""
为直播录播生成一张{orientation} {aspect_label} 视频封面，画面精致、主体明确、对比强烈，在缩略图尺寸下仍清晰。
主播：{streamer or "主播"}
封面主角称呼：{cover_subject_name or streamer or "主播"}
与投稿标题共用的核心事件：{headline}

只围绕核心标题设计画面，将“{headline}”作为唯一标题文字；不要出现完整投稿标题。
{cover_subject_copy_instruction}
{composition_instruction}
{dota2_instruction}
{dota2_item_instruction}
{dota2_ability_instruction}
{dota2_streamer_instruction}
{guest_identity_instruction}
{reference_instruction}
输入图片职责（不得跨图片混用身份）：
{reference_map_instruction}
绝对禁止出现日期、年份、月份、星期、钟表、具体时间、时间戳、倒计时、房间号、视频时长、平台界面、二维码和水印。
不要添加“直播回放”、主播开播时间或任何数字日期信息。避免大段文字，中文必须清楚易读。
本直播间的封面创作要求：{str(cfg.get("ai_cover_prompt") or DEFAULT_RECORDING_COVER_AI_PROMPT).strip()}
""".strip()
    image_client = get_openai_client(client_config).images
    requested_ratio = (target_width / target_height) if target_height else 0
    if abs(requested_ratio - (16 / 9)) < 0.02:
        image_size_key = "OPENAI_IMAGE_SIZE_16X9"
    elif abs(requested_ratio - (4 / 3)) < 0.02:
        image_size_key = "OPENAI_IMAGE_SIZE_4X3"
    else:
        image_size_key = "OPENAI_IMAGE_SIZE"
    image_size = str(
        ai_cfg.get(image_size_key)
        or ai_cfg.get("OPENAI_IMAGE_SIZE")
        or "1536x1024"
    )
    with image_generation_queue(cfg) as queue_wait_seconds:
        details["ai_cover_queue_wait_seconds"] = round(queue_wait_seconds, 3)
        if reference_paths:
            with ExitStack() as stack:
                reference_handles = [
                    stack.enter_context(path.open("rb"))
                    for path in reference_paths
                ]
                response = image_client.edit(
                    model=image_model,
                    image=(
                        reference_handles
                        if len(reference_handles) > 1
                        else reference_handles[0]
                    ),
                    prompt=prompt,
                    size=image_size,
                )
            details.update({
                "ai_cover_reference_used": True,
                "ai_cover_reference_name": reference_name,
                "ai_cover_reference_path": str(reference_paths[0]),
                "ai_cover_reference_paths": [str(path) for path in reference_paths],
                "ai_cover_reference_roles": list(reference_roles),
                "ai_cover_reference_count": len(reference_paths),
                "ai_cover_reference_kind": reference_kind,
            })
        else:
            response = image_client.generate(
                model=image_model,
                prompt=prompt,
                size=image_size,
            )
    item = response.data[0] if getattr(response, "data", None) else None
    if item is None:
        raise RuntimeError("图片模型没有返回封面")
    encoded = getattr(item, "b64_json", None)
    image_url = str(getattr(item, "url", "") or "").strip()
    if encoded:
        raw = base64.b64decode(encoded)
    elif image_url:
        request = urllib.request.Request(image_url, headers={"User-Agent": "PotatoFlow/1.0"})
        with urllib.request.urlopen(request, timeout=180) as remote:
            raw = remote.read()
    else:
        raise RuntimeError("图片模型返回结果中没有图片数据")
    if not raw:
        raise RuntimeError("图片模型返回了空图片")

    work_dir.mkdir(parents=True, exist_ok=True)
    source = work_dir / f"ai_cover_{cover_variant}_source.png"
    cover = output_path or (work_dir / "ai_cover.jpg")
    cover.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(raw)
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-vf",
        (
            f"scale={target_width}:{target_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={target_width}:{target_height}"
        ),
        "-frames:v", "1", "-q:v", "2", str(cover),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        **_hidden_subprocess_kwargs(),
    )
    if output_path is not None:
        source.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass
    if completed.returncode != 0 or not cover.is_file():
        message = completed.stderr.strip()[-1000:]
        raise RuntimeError(f"AI 封面尺寸处理失败: {message}")
    details.update({
        "ai_cover_generated": True,
        "ai_cover_model": image_model,
        "ai_cover_path": str(cover),
        "ai_cover_prompt": prompt,
        "ai_cover_width": target_width,
        "ai_cover_height": target_height,
        "ai_cover_variant": cover_variant,
        "ai_cover_requested_size": image_size,
    })
    return cover, details


def cleanup_uploaded_recording(
    video: Path,
    danmaku_xml: Path | None,
    upload_video: Path,
    artifact_dir: Path | None = None,
    retained_paths: Iterable[Path | None] = (),
    xml_retention_hours: float = 0.0,
) -> dict[str, Any]:
    """Remove recording inputs and generated artifacts after upload is durable."""
    retained_paths = tuple(retained_paths)
    retained_resolved = {
        candidate.resolve()
        for candidate in retained_paths
        if candidate is not None
    }
    candidates = [
        ("video", video),
        ("danmaku_xml", danmaku_xml),
    ]
    if upload_video.resolve() != video.resolve():
        candidates.append(("upload_video", upload_video))
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[Path] = set()
    for kind, candidate in candidates:
        if candidate is None:
            continue
        path = candidate.resolve()
        if path in seen:
            continue
        seen.add(path)
        if path in retained_resolved:
            continue
        try:
            existed = path.exists() or path.is_symlink()
            path.unlink(missing_ok=True)
            if existed and not path.exists() and not path.is_symlink():
                deleted.append(str(path))
        except OSError as exc:
            failed.append({"kind": kind, "path": str(path), "error": str(exc)})
    if artifact_dir is not None:
        artifact_path = artifact_dir.resolve()
        try:
            if artifact_path.is_dir():
                artifact_files = [
                    str(item.resolve())
                    for item in artifact_path.rglob("*")
                    if item.is_file() or item.is_symlink()
                ]
                shutil.rmtree(artifact_path)
                deleted.extend(
                    item for item in artifact_files
                    if item not in deleted and not Path(item).exists()
                )
                if not artifact_path.exists():
                    deleted.append(str(artifact_path))
        except OSError as exc:
            failed.append({
                "kind": "artifacts",
                "path": str(artifact_path),
                "error": str(exc),
            })
    retained = []
    for candidate in retained_paths:
        if candidate is None:
            continue
        path = candidate.resolve()
        if path.is_file() and str(path) not in retained:
            retained.append(str(path))
    result: dict[str, Any] = {
        "deleted": deleted,
        "retained": retained,
        "failed": failed,
    }
    if (
        danmaku_xml is not None
        and danmaku_xml.resolve() in retained_resolved
        and danmaku_xml.is_file()
        and xml_retention_hours > 0
    ):
        result["retained_xml_path"] = str(danmaku_xml.resolve())
        result["retained_xml_until"] = (
            datetime.now(timezone.utc) + timedelta(hours=float(xml_retention_hours))
        ).isoformat()
    return result


def persist_pipeline_cover(
    store: StateStore,
    key: str,
    cover: Path,
    variant: str = "16x9",
    video: Path | None = None,
) -> Path:
    """Persist the cover next to the recording (or in the artifact dir as fallback)."""
    source = cover.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"投稿封面不存在: {source}")
    suffix = source.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    safe_variant = "4x3" if variant == "4x3" else "16x9"
    if video is not None and video.parent.is_dir():
        stem = video.stem
        name = f"{stem}_4x3{suffix}" if safe_variant == "4x3" else f"{stem}{suffix}"
        target = (video.parent / name).resolve()
    else:
        target_dir = store.path.parent / "artifacts" / "task-covers"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_dir.chmod(0o750)
        target = (target_dir / f"{key}-{safe_variant}{suffix}").resolve()
    if source != target:
        shutil.copy2(source, target)
    target.chmod(0o640)
    return target


def recording_metadata_values(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> dict[str, str]:
    stem = video.stem
    datetime_match = re.search(r"(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)", stem)
    time_match = re.search(r"20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+)$", stem)
    current_filename_match = re.match(
        r"(.+?)_[0-9a-f]{6}_(.+)_(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)$",
        stem,
        re.IGNORECASE,
    )
    recorder_filename_match = re.match(
        r"(.+?)_(.+)_(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)$",
        stem,
    )
    marker_match = re.match(r"(.+?)_[0-9a-f]{6}(?=20\d{2}-\d{2}-\d{2})", stem, re.IGNORECASE)
    streamer = str(cfg.get("streamer_name") or "").strip()
    if not streamer:
        if current_filename_match:
            streamer = current_filename_match.group(1).strip("_- ")
        elif marker_match:
            streamer = marker_match.group(1).strip("_- ")
    streamer = normalize_dota2_streamer_name(streamer)
    if current_filename_match:
        live_title = current_filename_match.group(2).strip("_- ")
    elif recorder_filename_match:
        live_title = recorder_filename_match.group(2).strip("_- ")
    else:
        live_title = time_match.group(1).strip("_- ") if time_match else ""
    topic = re.sub(r"[\r\n｜|]+", " ", str(ai_topic or live_title or "直播精彩内容")).strip()
    if datetime_match:
        recorded_text = datetime_match.group(1)
        recorded_format = (
            "%Y-%m-%d_%H-%M-%S"
            if re.search(r"_\d{2}-\d{2}-\d{2}$", recorded_text)
            else "%Y-%m-%d_%H-%M"
        )
        recorded_at = datetime.strptime(recorded_text, recorded_format)
    elif video.exists():
        recorded_at = datetime.fromtimestamp(video.stat().st_mtime)
    else:
        recorded_at = datetime.now()
    return {
        "stem": stem,
        "name": video.name,
        "suffix": video.suffix.lstrip("."),
        "streamer": streamer or "主播",
        "ai_topic": topic,
        "date": recorded_at.strftime("%m-%d %H:%M"),
        "live_title": live_title,
        "recording_intro": (
            f"直播录播：{streamer or '主播'}《{live_title}》。"
            if live_title
            else f"直播录播：{streamer or '主播'}。"
        ),
    }


def topic_mentions_streamer(topic: str, streamer: str) -> bool:
    """Return whether a topic already names the streamer or a known alias."""
    topic_key = _compact_alias(topic)
    streamer_key = _compact_alias(streamer)
    if not topic_key or not streamer_key or streamer_key == _compact_alias("主播"):
        return False
    candidates = {str(streamer or "").strip(), normalize_dota2_streamer_name(streamer)}
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        keys = {_compact_alias(canonical_name), *(_compact_alias(alias) for alias in aliases)}
        if streamer_key in keys:
            candidates.add(canonical_name)
            candidates.update(aliases)
    return any(
        (candidate_key := _compact_alias(candidate))
        and candidate_key in topic_key
        for candidate in candidates
    )


def infer_streamer_participation_mode(
    description: str,
    streamer: str,
    *,
    gameplay_verified: bool = False,
) -> str:
    """Return playing, spectating, or unknown from independent evidence."""
    if gameplay_verified:
        return "playing"
    public_name = normalize_dota2_streamer_name(streamer)
    if any(
        normalize_dota2_streamer_name(names[0]) == public_name
        for names, _hero_key in _person_hero_relations(description)
    ):
        return "playing"
    observer_pattern = re.compile(
        r"(?:观战|观赛|旁观|OB|看比赛|看决赛|陪伴吃瓜|解说比赛)",
        re.IGNORECASE,
    )
    for line in timeline_lines(description):
        if observer_pattern.search(line) and topic_mentions_streamer(line, streamer):
            return "spectating"
    gameplay_pattern = re.compile(
        r"(?:游玩|试玩|操作|操刀|选择(?:职业|角色|套装|[^，。]{0,8}装备)|"
        r"出装|购买[^，。]{0,8}装备|面对BOSS|"
        r"战力(?:达到|冲上|升到)|打BOSS|推图|卡关)",
        re.IGNORECASE,
    )
    for line in timeline_lines(description):
        if gameplay_pattern.search(line) and topic_mentions_streamer(line, streamer):
            return "playing"
    return "unknown"


def dota2_gsi_hero_is_usable(hero: Any) -> bool:
    """Reject telemetry placeholders and internal identifiers as hero names."""
    name = str(hero or "").strip()
    if not name:
        return False
    folded = name.casefold()
    compact = re.sub(r"\s+", "", folded)
    if re.fullmatch(r"(?:未知|unknown)(?:[\(（\[].*[\)）\]])?", compact):
        return False
    if re.fullmatch(r"(?:hero|role)[_ -]?\d+", compact):
        return False
    if re.fullmatch(r"\d+", compact) or compact.startswith("npc_dota_hero_"):
        return False
    return True


def streamer_gameplay_is_verified(game: Any) -> bool:
    """Only owner-bound telemetry may prove that the room owner is playing."""
    if not isinstance(game, dict) or not dota2_gsi_hero_is_usable(game.get("hero")):
        return False
    return str(game.get("identity_source") or "").strip() not in {
        "xml_repeated_hero_only",
        "xml_dominant_hero_only",
    }


def contextualize_streamer_title_topic(
    topic: str,
    streamer: str,
    participation_mode: str,
) -> str:
    """Keep verified participation natural and remove room-owner label filler."""
    clean_topic = normalize_recording_title_filler(topic)
    clean_topic = re.sub(r"^[\s｜|:：·-]+|[\s｜|]+$", "", clean_topic).strip()
    clean_streamer = str(streamer or "").strip()
    if not clean_topic or not clean_streamer or clean_streamer == "主播":
        return clean_topic
    streamer_key = _compact_alias(clean_streamer)
    candidates = {clean_streamer, normalize_dota2_streamer_name(clean_streamer)}
    for canonical_name, aliases in _all_dota2_streamer_alias_groups():
        group = {canonical_name, *aliases}
        if streamer_key in {_compact_alias(name) for name in group}:
            candidates.update(group)
            break
    leading_name = next(
        (
            name for name in sorted(candidates, key=len, reverse=True)
            if name and clean_topic.casefold().startswith(name.casefold())
        ),
        "",
    )
    if topic_mentions_streamer(clean_topic, clean_streamer) and not leading_name:
        return clean_topic
    if leading_name:
        separator_label = bool(re.match(
            r"^\s",
            clean_topic[len(leading_name):],
        ))
        raw_remainder = re.sub(
            r"^[\s｜|:：·-]+",
            "",
            clean_topic[len(leading_name):],
        ).strip()
        remainder = normalize_recording_title_filler(raw_remainder)
        mechanical_prefix = separator_label or remainder != raw_remainder or bool(re.match(
            r"^(?:直播中|直播间(?:热议|讨论|关注)|热议|讨论|关注)[：:，,]?",
            raw_remainder,
            re.I,
        ))
        safe_relation = (
            re.match(r"^(?:观战|观赛|旁观|OB|看比赛|看决赛|解说|点评)", remainder, re.I)
            if participation_mode == "spectating"
            else None
        )
        if safe_relation:
            return clean_topic
        if participation_mode == "playing" and not mechanical_prefix:
            return clean_topic
        clean_topic = remainder or clean_topic
    if participation_mode == "spectating":
        prefix = f"{clean_streamer}观战"
    else:
        return clean_topic
    return f"{prefix}{clean_topic}".strip()


def render_metadata(
    video: Path,
    cfg: dict[str, Any],
    ai_topic: str = "",
) -> tuple[str, str, list[str]]:
    values = recording_metadata_values(video, cfg, ai_topic)
    title_template = str(cfg.get("title_template") or DEFAULT_TITLE_TEMPLATE)
    if ai_topic.strip():
        # A verified AI topic is already a complete natural event sentence.
        # Do not let a legacy/custom template put the room owner or a fixed
        # "直播回放" suffix back around the event-led title.
        title = f"{values['ai_topic']}｜{values['date']}".strip()
    else:
        title = title_template.format_map(values).strip()
    if topic_mentions_streamer(values["ai_topic"], values["streamer"]):
        redundant_prefix = f"{values['streamer']}｜"
        if title.startswith(redundant_prefix):
            title = title[len(redundant_prefix):].lstrip()
    description = str(
        cfg.get("description_template") or DEFAULT_DESCRIPTION_TEMPLATE
    ).format_map(values).strip()
    tags = dedupe_recording_tags(cfg.get("tags", []))
    if not title:
        raise ValueError("渲染后的标题为空")
    return title, description, tags


def import_app(cfg: dict[str, Any]):
    root = resolve_app_root(cfg)
    if not (root / "modules").is_dir():
        raise FileNotFoundError(f"PotatoFlow 应用目录无效: {root}")
    sys.path.insert(0, str(root))
    from modules.bilibili_uploader import BilibiliUploader  # type: ignore
    from modules.config_manager import load_config as load_app_config  # type: ignore
    return BilibiliUploader, load_app_config


def enhance_recording_metadata(
    title: str,
    description: str,
    existing_tags: list[str],
    cover: Path,
    fallback_partition_id: str,
    cfg: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any]]:
    """Apply PotatoFlow tag and Bilibili partition automation to a recording."""
    root = resolve_app_root(cfg)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from modules.ai_enhancer import (  # type: ignore
        generate_video_tags,
        recommend_bilibili_partition,
    )
    from modules.bilibili_zones import get_zone_list_sub  # type: ignore
    from modules.config_manager import load_config as load_app_config  # type: ignore

    ai_cfg = load_app_config()
    generate_tags_enabled = bool(ai_cfg.get("GENERATE_TAGS", False))
    recommend_partition_enabled = bool(ai_cfg.get("RECOMMEND_PARTITION", False))
    include_cover = bool(ai_cfg.get("RECOMMEND_PARTITION_WITH_COVER", False))
    openai_config = {
        "OPENAI_API_KEY": ai_cfg.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": ai_cfg.get("OPENAI_BASE_URL", ""),
        "OPENAI_MODEL_NAME": ai_cfg.get("OPENAI_MODEL_NAME", "gpt-3.5-turbo"),
        "OPENAI_THINKING_ENABLED": ai_cfg.get("OPENAI_THINKING_ENABLED", False),
        "OPENAI_TIMEOUT_SECONDS": ai_cfg.get("OPENAI_TIMEOUT_SECONDS", 600),
        "FIXED_PARTITION_ID_BILIBILI": ai_cfg.get("FIXED_PARTITION_ID_BILIBILI", ""),
        "RECOMMEND_PARTITION_WITH_COVER": include_cover,
    }

    generated_tags: list[str] = []
    final_tags = dedupe_recording_tags(existing_tags)
    if generate_tags_enabled:
        generated_tags = [
            str(tag).strip()
            for tag in (
                generate_video_tags(
                    title,
                    description,
                    openai_config=openai_config,
                    task_id=None,
                )
                or []
            )
            if str(tag).strip()
        ][:6]
        final_tags = dedupe_recording_tags([*final_tags, *generated_tags])

    partition_id = str(fallback_partition_id or "").strip()
    selection: dict[str, Any] = {}
    if recommend_partition_enabled:
        fixed_partition_id = str(
            openai_config.get("FIXED_PARTITION_ID_BILIBILI") or ""
        ).strip()
        dota2_default = bool(
            not fixed_partition_id
            and recording_cover_has_dota2_context(
                "",
                title,
                description,
                *final_tags,
            )
        )
        if dota2_default:
            selection = {
                "id": "171",
                "source": "dota2_default",
                "confidence": 1.0,
                "reason_summary": "Dota 2 内容默认使用电子竞技分区",
                "alternatives": [],
            }
            partition_id = "171"
        else:
            zone_data = get_zone_list_sub()
        if not dota2_default and zone_data:
            selection = recommend_bilibili_partition(
                title,
                description,
                zone_data,
                tags=final_tags,
                openai_config=openai_config,
                task_id=None,
                cover_path=str(cover),
                include_cover_for_ai=include_cover,
            ) or {}
            recommended = str(selection.get("id") or "").strip()
            if recommended:
                partition_id = recommended

    details = {
        "tag_generation_enabled": generate_tags_enabled,
        "generated_tags": generated_tags,
        "final_tags": final_tags,
        "partition_recommendation_enabled": recommend_partition_enabled,
        "recommended_partition_id": str(selection.get("id") or "").strip() or None,
        "selected_partition_id": partition_id or None,
        "partition_source": selection.get("source"),
        "partition_confidence": selection.get("confidence"),
        "partition_reason": selection.get("reason_summary") or "",
        "partition_alternatives": selection.get("alternatives") or [],
        "cover_for_partition_ai": bool(
            recommend_partition_enabled and include_cover and cover.is_file()
        ),
        "partition_cover_path": (
            str(cover)
            if recommend_partition_enabled and include_cover and cover.is_file()
            else None
        ),
    }
    return final_tags, partition_id, details


def _generate_danmaku_metadata_with_ai(
    comments,
    base_description: str,
    cfg: dict[str, Any],
    grounding_context: dict[str, Any] | None = None,
    timeline_duration_seconds: float | None = None,
    timeline_diagnostics: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Generate a grounded description and concise title topic from danmaku."""
    if not comments or not bool(cfg.get("ai_danmaku_summary_enabled", True)):
        return base_description, ""
    try:
        root = resolve_app_root(cfg)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from modules.ai_enhancer import get_openai_client, _request_json_object  # type: ignore
        from modules.config_manager import load_config as load_app_config  # type: ignore

        ai_cfg = load_app_config()
        if not ai_cfg.get("OPENAI_API_KEY"):
            print("WARN 未配置 PotatoFlow OPENAI_API_KEY，跳过弹幕 AI 简介", file=sys.stderr)
            return base_description, ""
        full_batch_enabled = bool(cfg.get("ai_danmaku_full_batch_enabled", True))
        if full_batch_enabled:
            discovery_batches = batch_summary_comments(
                comments,
                int(cfg.get("ai_danmaku_batch_comments", 600)),
            )
            selected = [comment for batch in discovery_batches for comment in batch]
        else:
            selected = select_summary_comments(
                comments,
                int(cfg.get("ai_danmaku_max_comments", 800)),
            )
            discovery_batches = [selected]
        timeline_minimum, timeline_maximum = timeline_target_range(
            timeline_duration_seconds
        )
        reaction_delay = max(
            0,
            min(60, int(cfg.get("ai_danmaku_reaction_delay_seconds", 8) or 0)),
        )
        diagnostics = timeline_diagnostics if timeline_diagnostics is not None else {}
        diagnostics.update({
            "timeline_target_min": timeline_minimum,
            "timeline_target_max": timeline_maximum,
            "timeline_reaction_delay_seconds": reaction_delay,
            "timeline_anchor_policy": "same_time_screen_spam_or_exact_xml",
            "timeline_cluster_window_seconds": int(_TIMELINE_SPAM_WINDOW_SECONDS),
            "timeline_min_screen_spam_messages": _TIMELINE_MIN_SPAM_MESSAGES,
            "timeline_retry_attempted": False,
            "full_batch_enabled": full_batch_enabled,
            "source_comment_count": len(comments),
            "effective_comment_count": len(selected),
            "discovery_batch_count": len(discovery_batches),
        })
        verified_game = (
            (grounding_context or {}).get("game")
            if isinstance(grounding_context, dict)
            else None
        )
        verified_game_segments = (
            (grounding_context or {}).get("game_segments")
            if isinstance(grounding_context, dict)
            else None
        )
        verified_game_segments = [
            dict(segment)
            for segment in (
                verified_game_segments
                if isinstance(verified_game_segments, list)
                else []
            )
            if isinstance(segment, dict)
            and str(segment.get("hero") or "").strip()
            and float(segment.get("end_seconds") or 0)
            > float(segment.get("start_seconds") or 0)
        ]
        streamer_gameplay_verified = bool(
            streamer_gameplay_is_verified(verified_game)
            or verified_game_segments
        )
        participation_mode = "playing" if streamer_gameplay_verified else "unknown"
        verified_live_context = dict(grounding_context or {})
        if not streamer_gameplay_verified:
            verified_live_context.pop("game", None)
            verified_live_context.pop("game_segments", None)
        payload = {
            "base_description": base_description,
            "streamer_identity": {
                "configured_name": str(cfg.get("streamer_name") or "").strip(),
                "public_name": normalize_dota2_streamer_name(
                    str(cfg.get("streamer_name") or "").strip()
                ),
                "preferred_description_name": preferred_recording_title_name(
                    str(cfg.get("streamer_name") or "").strip()
                ),
                "preferred_title_name": preferred_recording_title_name(
                    str(cfg.get("streamer_name") or "").strip()
                ),
                "editorial_names": list(dict.fromkeys(
                    alias
                    for canonical_name, aliases in _all_dota2_streamer_alias_groups()
                    if normalize_dota2_streamer_name(
                        str(cfg.get("streamer_name") or "").strip()
                    ) == canonical_name
                    for alias in (canonical_name, *aliases)
                )),
            },
            "comment_count": len(comments),
            "sampled_comments": "",
            "sampled_comment_evidence": [],
            "verified_live_context": verified_live_context,
            "streamer_participation": {
                "gameplay_verified": streamer_gameplay_verified,
                "mode": participation_mode,
                "relationship_policy": (
                    "structured_game_identity_only"
                    if streamer_gameplay_verified
                    else "repeated_explicit_xml_relation_only"
                ),
            },
            "timeline_target_count": {
                "minimum": timeline_minimum,
                "maximum": timeline_maximum,
            },
            "timestamp_reaction_delay_seconds": reaction_delay,
        }
        legacy_prompt = str(cfg.get("ai_danmaku_prompt") or "").strip()
        title_prompt = str(
            cfg.get("ai_title_prompt") or DEFAULT_RECORDING_TITLE_AI_PROMPT
        ).strip()
        description_prompt = str(
            cfg.get("ai_description_prompt") or DEFAULT_RECORDING_DESCRIPTION_AI_PROMPT
        ).strip()
        legacy_instruction = (
            f"本直播间旧版自定义要求：{legacy_prompt}"
            if legacy_prompt
            else ""
        )
        system_prompt = f"""
你是直播录播编辑。根据按时间采样的观众弹幕，为哔哩哔哩录播生成内容充实的中文简介。
只能总结弹幕能支持的主题、高潮时刻和观众反应，不得虚构主播说过的话或未出现的事件。
verified_live_context 是在 AI 之前完成的直播统计与主播同场对局识别结果；主播最终持有的装备、KDA
只能使用其中已经确认的数据，禁止从弹幕、标题或常识猜测，且不得把其他对局的数据混入本段。
英雄身份优先使用 verified_live_context；结构化身份缺失时，允许按下述严格规则由多条原始 XML
直接人物—英雄绑定形成保守共识。弹幕明确讨论购买、未购买、替换或使用某件装备时可以忠实写入事件，
但不能把讨论内容改写成主播最终物品栏，也不能把归属不明的装备强行绑定给当前主播。
verified_live_context.game 表示整段录播只有一场可确认的主播对局；
verified_live_context.game_segments 表示一小时录播内按 GSI 时间切开的多场主播对局，每段的
start_seconds/end_seconds 都是相对录播开头的秒数。生成某个 timeline 事件时只能使用覆盖该事件时间的
那一段英雄、装备和 KDA；事件落在段外或边界无法唯一匹配时不要绑定英雄装备。不得因为录播中出现多个英雄
就删除全部对局数据，也不得把前一局的英雄与后一局的装备拼在一起。
game_segments.ended_confirmed 只确认该英雄段已结束并用于切开前后对局，不确认胜负；不得仅凭它生成
“本局结束”“转入下一局”等低信息事件。只有同一事件窗口的 XML 原文明确给出获胜、落败、翻盘、基地告破或
决定性收尾，才可写具体结果，并应优先保留进 timeline 供标题选择。
streamer_participation 是强制事实边界。当 mode=unknown 时，不得默认声称当前主播正在参赛。非 DOTA2 的游戏、
小游戏、户外或互动节目中，只有同一事件窗口内多条连续弹幕以当前主播可靠别名或无歧义的第二人称直接指向
具体操作、选择或结果，且没有观战、转播或第三方动作冲突时，才可据实写成当前主播参与；普通催促、单条“你”
或只出现房间名均不足以证明。只有弹幕明确出现当前主播观战、观赛、OB、解说或点评的证据时，才可据实写成观战。其他人物、比赛或节目事件已有
明确证据时直接描述具体人物、动作和结果，不得为了回避当前主播身份而套用“直播间讨论/热议/关注”。
当 mode=spectating 时，当前主播
只能被描述为观战、观赛、解说或点评。上述两种模式均禁止写成主播操刀、使用、选择、出装、击杀、
阵亡或操作任何英雄。verified_live_context.game 或与事件时间唯一匹配的 game_segments 结构化身份记录
可直接绑定当前主播；结构化记录
缺失时，DOTA2 只有多条连续弹幕明确以“你/主播别名 + 英雄”反复确认、至少一条原文同时出现可靠人名与英雄、
且没有观战或冲突证据时，才允许绑定当前主播。观战中的其他选手与英雄也遵循同一严格规则，例如
持续明确“南枫使用末日使者”时可写“南枫的末日使者”。人物名与英雄名
仅仅同时出现、孤立单条弹幕或模型旧稿不能证明关系；证据不足时只能写中性比赛事实。
verified_live_context.live_stats 只作为事实参考。description 严禁复制或输出“直播数据”区块、礼物、
在线人数、英雄装备统计表；这些内容由投稿流程在最后一步独立渲染，并且只渲染一次。
不要引用用户名、UID、广告或重复刷屏。base_description 是已清理好的主播和直播标题前缀。
streamer_identity 是当前直播间主播的可靠身份；description 提到当前主播时优先使用
preferred_description_name（例如谢彬/谢彬DD统一写“奶哥”，YYF默认写“枫哥”），不要在同一份简介中无意义地
交替使用房间名、实名和多个外号。editorial_names 是同一主播可用的可靠名称，只能用于身份消歧，
不得将这些别名当成多个人。昵称只改变公开文案，不改变人物身份和动作归属；不能因为昵称带有贬义或玩梗含义，
就自行补充失误、情绪或结果。
弹幕中确实提到的其他主播、选手或嘉宾可以写入，但必须有能明确指向该人物的原文证据；
不得把弹幕用户名、模糊外号、英雄名或同名对象当成真实人物。涉及人物的句子必须写清“谁做了什么”。
对每个关键事件按5W1H检查：When 由程序回到 XML 自动定位；event 尽量交代 Who（谁）、What（做了什么）、
Where（哪一局、地图、游戏阶段或现场场景）、Why（有证据的原因）、How（过程、转折）以及结果。地点不是必填的
现实地名，原因也不得从常识、结果或情绪反推；任何一项缺少原文证据就省略，不能为凑齐六项编造。
人物之间的一起玩、同队配合、互为对手、接力或观战关系也必须分别由连续明确原文支持。证据能够确认时，
event 应写清“谁和谁以什么关系做了什么”，并在参与者或关系变化时生成新的时间点；只因多个人名在相邻弹幕
出现，不得猜成共同游玩，也不得把观战者写成队友或对手。
人物身份、人物与英雄的归属、最终胜负是三类独立事实：后两者证据不足时，不得连已有明确
原文支持的选手姓名一起删掉。如果弹幕明确表明这是“谢彬对阵眼子”，就应保留该对阵关系；
英雄归属无法确认时，可后续写“谢彬一方/眼子一方”或单独描述英雄对阵，不得擅自把英雄绑给选手。
同时不要为了规避风险而回避明显结论：同一时间窗口内有多条连续、指向一致且无冲突的原文时，视为证据比较明显，
应当明确写出参赛双方、对阵关系、已发生的关键局势或结果，不得总是降级成“弹幕讨论”“一方”“似乎”。
英雄归属仍需要人名与英雄的重复明确绑定；最终胜负仍需要赛后确认性证据，不能用赛中预测代替。
description 只作为本批次的简短候选摘要，不要重复 base_description，也不要输出文件名、内部编号、
录制时间或“重要时间点”标题；最终投稿简介只会使用通过 XML 核验的 timeline 行。
按 sampled_comment_evidence 从前到后分析，不能为迎合标题把后半段事件提前，也不能把不同时段的
独立话题写成同一条因果链。description 提到的每个关键事实都必须有对应 timeline；没有可核验的
evidence_texts/evidence_keywords 就删除该事实，禁止生成脱离时间点的长篇总结。
timeline 只选择 sampled_comment_evidence 有直接证据或同一时间出现集中刷屏的事件。每项返回 event、evidence_texts
和 evidence_keywords；evidence_texts 必须一字不改地复制输入中 1 至 3 条 text，
evidence_keywords 是这些弹幕中足以支持整个 event 的 1 至 4 个原文关键词。
	签约、加入或转入某组织、官宣、解约、开除、婚恋、疾病、违法、收入等现实身份与状态属于高风险事实。
	本流程输入只有观众弹幕，无法独立核验画面、官方页面或当事人原话；即使大量弹幕声称“已经显示”“转了”或
	“宣布了”，event 也必须明确保留来源限定；较负面、未经证实、可能损害人物名誉的现实传言必须整条删除，
	不得通过添加“弹幕称”继续保留；中性现实消息使用“观众讨论”，明显玩笑使用“直播间调侃”，不得改写成“页面显示”或
	无来源限定的事实结论。只有 verified_live_context 中明确提供的结构化事实才可直接陈述。
如果证据充足，timeline 应覆盖录播开头、中段和结尾，并返回 {timeline_minimum} 至
{timeline_maximum} 个彼此不同的关键看点。这个区间是覆盖目标而不是凑数命令：看点密集、事件彼此独立且证据充分时，
尽量接近上限；内容平淡、重复或证据不足时允许低于下限，绝不能用普通问候、无变化的持续过程、重复弹幕、
“继续游戏/继续聊天”等无信息条目补齐数量。适用于所有直播类型：优先选择内容推进、关键决定、
意外变化、精彩表现、重要互动、节目效果、争议讨论、情绪高潮和阶段切换。只有输入明确属于
游戏内容时，才额外考虑阵容选择、关键交锋、操作失误、局势转折和翻盘；不得把聊天、访谈、
户外、才艺或其他直播强行描述成游戏对局。不要把同一事件拆成多条，也不要为了达到数量编造内容。
时间分布只用于防止漏掉整段内容，不能机械地每隔固定分钟生成一条；连续十几分钟没有新事件可以留空，
短时间内出现多个彼此独立且有完整证据的看点则可以分别保留。相邻候选如果主语、动作、对象和结果基本相同，
应合并成一条；只有出现新阶段、新决定、明确转折或最终结果，才可拆成新的时间点。
对游戏对局，时间点的信息价值优先级为“人物+英雄+具体事件” > “人物+具体事件” > “英雄+具体事件” >
只有弹幕反应。同一窗口中大量弹幕稳定地反复出现某人和某英雄，且至少一条原文将两者明确连接、
不存在其他人物或英雄的冲突绑定时，应当完成人物—英雄归因，并写清该人物的英雄发生了什么；
不得只因为证据来自弹幕就退化成“某英雄引发讨论”。
上述“人物—英雄—事件—结果”结构只适用于能确认为比赛或游戏对局的内容；聊天、听歌、户外、才艺、访谈、
查看旧节目等非比赛内容只按“人物—话题/事件—反应/结果”叙述，禁止强行补英雄、对阵或胜负。
竞技比赛类内容还必须保持上下文可读性：当本批证据首次明确出现参赛双方、关键选手或队伍时，
优先生成一条交代“谁对阵谁”的候选事件。后续事件中，原文能确认人物或所属一方时，优先使用
人名或“某人一方”承接，不要退化成“多人”“一方”“场上一方”。只有证据本身确实无法识别人物时才可使用中性主语。
重要事件涉及人物时，event 必须写明当前主播或被提及的其他人物“谁做了什么”；
无法从证据确定人物时宁可写“主播”或省略该事件，不得猜测姓名。
每条 event 必须是 evidence_texts 的最小忠实改写：主语、对象、动作、数字、原因、结果和“首波”
“翻盘”“阵亡”等阶段性判断都必须由原文直接说明或由同一 30 秒窗口的多条证据共同支持；
不得仅因附近讨论了相关英雄、装备或选人，就向 event 补入原文没有的动作与结果。
胜负与荣誉属于最高风险事实：“谁赢、谁输、谁淘汰谁、谁晋级、谁夺冠、谁翻盘或被翻盘”必须有
同一证据窗口内人物与结果方向的明确原文绑定。只看到“牛”“可惜”“结束了”“冠军”身份梗，或只知道
一方领先/落后，都不能推断最终胜负；证据冲突时删除胜负结论，宁可写中性过程也绝不能写反。
“提前预祝”“感觉要赢”“冠军相”等可以作为当时观众对局势的预测保留，但 event 必须明确写成
“弹幕提前预祝/预测/看好”，不得省略预测语气或改写成已经获胜；后续只有出现赛后确认性证据，
才能另写一条最终胜负或夺冠结果。
当赛后同一时间窗口有多条一致的“获胜/落败/淘汰/晋级/夺冠”表述，且人物和结果方向清楚、没有相反证据时，
应当直接下结论，不得因为过度保守改写成“弹幕开始讨论冠军”。
一条像总结稿的超长弹幕不能独自支撑包含多个先后环节的复合 event；应拆分并寻找相邻佐证，
找不到就只保留该原文能直接支持的最小事实。
不要返回时间戳；程序会使用 evidence_keywords 回到完整 XML 查找第一条匹配弹幕。
程序优先使用逐字一致的 evidence_texts 回到完整 XML 定位；如果模型引用不完整或多条证据分散，
同一 60 秒窗口内至少 3 条弹幕围绕 evidence_keywords 集中刷屏也可以形成时间点。
时间仍由程序从完整 XML 中确定，不得自行编造或手写时间戳。
弹幕时间晚于画面事件：应选最早一批明确相关弹幕作为证据锚点，不要选择刷屏高峰；程序会按
timestamp_reaction_delay_seconds 将最终时间统一前移，请勿在 AI 内再次手动减秒。
只有最早证据的绝对 XML 时间确实位于整段录制最初一分钟，且内容明显承接此前进程时，event 才可写成
“开场已处于……”或“开场承接……”。batch_context 的批次起点不是录播起点；后续批次的第一条即使承接
上一批，也严禁使用“开场”措辞，不得把30分钟、40分钟等中后段事件误标为开场。
最终时间戳已经表示事件阶段，event 默认不写“前段/中段/后段”。确有叙事必要时，只能用事件的绝对 XML 时间
相对于整段 video_duration_seconds 的位置判断：前三分之一、中间三分之一、后三分之一；不得按当前批次内的
先后位置判断，不能把04分钟写成中段、10分钟写成后段。
当 payload 含 batch_context 时，本次只分析该批弹幕，并以 payload.timeline_target_count 为准，
每批返回 1 至 3 个最重要的候选事件；程序会在全部批次完成后统一合并和筛选。
{DOTA2_METADATA_DISAMBIGUATION}
本直播间的简介要求：{description_prompt}
{legacy_instruction}
	本直播间自定义要求只能补充题材重点、语气和风格，不能推翻事实证据、人物归属、时间顺序、5W1H缺证省略和风险过滤规则；
	较负面的未经证实现实传言无论自定义要求如何指定来源词都必须删除；
	具体比赛、游戏表现和现场事件不得使用“直播间热议/讨论/关注”作为兜底主语。
返回 JSON 对象：{{"description":"...","timeline":[{{"event":"...","evidence_texts":["..."],"evidence_keywords":["..."]}}]}}，description 不超过 1600 个中文字符。
""".strip()
        ai_client = get_openai_client(ai_cfg)
        model_name = str(ai_cfg.get("OPENAI_MODEL_NAME", "gpt-4o-mini"))
        thinking_enabled = bool(ai_cfg.get("OPENAI_THINKING_ENABLED", False))
        metadata_request_cap = max(
            1,
            min(8, int(cfg.get("ai_metadata_request_concurrency", 3) or 3)),
        )
        batch_concurrency = max(
            1,
            min(
                6,
                int(cfg.get("ai_danmaku_batch_concurrency", 3)),
                len(discovery_batches),
            ),
        )
        batch_started_at = time.perf_counter()

        def analyze_batch(
            batch_index: int,
            batch: list[Any],
        ) -> tuple[int, dict[str, Any], float]:
            request_started_at = time.perf_counter()
            batch_payload = dict(payload)
            batch_payload["sampled_comment_evidence"] = [
                {
                    "timestamp_seconds": max(0, int(float(comment.time))),
                    "text": str(comment.text),
                }
                for comment in batch
            ]
            batch_payload["batch_context"] = {
                "index": batch_index,
                "count": len(discovery_batches),
                "comment_count": len(batch),
            }
            batch_payload["timeline_target_count"] = {
                "minimum": 1,
                "maximum": 3,
            }
            with ai_metadata_request_slot(cfg):
                batch_result = _request_json_object(
                    client=ai_client,
                    model_name=model_name,
                    system_prompt=system_prompt,
                    payload=batch_payload,
                    max_tokens=1200,
                    temperature=0.2,
                    thinking_enabled=thinking_enabled,
                    logger_obj=None,
                    scene_name=(
                        "recording_danmaku_summary"
                        if len(discovery_batches) == 1
                        else "recording_danmaku_summary_batch"
                    ),
                )
            return (
                batch_index,
                batch_result if isinstance(batch_result, dict) else {},
                time.perf_counter() - request_started_at,
            )

        batch_results_by_index: dict[int, dict[str, Any]] = {}
        batch_timings: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=batch_concurrency) as executor:
            futures = {
                executor.submit(analyze_batch, batch_index, batch): batch_index
                for batch_index, batch in enumerate(discovery_batches, start=1)
            }
            for future in as_completed(futures):
                batch_index = futures[future]
                try:
                    result_index, batch_result, elapsed = future.result()
                    batch_results_by_index[result_index] = batch_result
                    batch_timings.append({
                        "index": result_index,
                        "elapsed_seconds": round(elapsed, 3),
                    })
                except Exception as exc:
                    diagnostics.setdefault("discovery_batch_errors", []).append({
                        "index": batch_index,
                        "error": str(exc)[:240],
                    })
        batch_results = [
            batch_results_by_index[index]
            for index in sorted(batch_results_by_index)
        ]
        diagnostics.update({
            "discovery_batch_concurrency": batch_concurrency,
            "metadata_task_concurrency": max(
                1, min(1, int(cfg.get("ai_metadata_concurrency", 1) or 1))
            ),
            "metadata_request_concurrency_cap": metadata_request_cap,
            "discovery_batch_elapsed_seconds": round(
                time.perf_counter() - batch_started_at,
                3,
            ),
            "discovery_batch_timings": sorted(
                batch_timings,
                key=lambda item: int(item["index"]),
            ),
        })
        if not batch_results:
            batch_error = ai_batch_error_summary(
                diagnostics.get("discovery_batch_errors")
            )
            raise RuntimeError(
                "全部弹幕分析批次均失败"
                + (f"：{batch_error}" if batch_error else "")
            )
        generated_description = "\n".join(
            str(result.get("description") or "").strip()
            for result in batch_results
            if str(result.get("description") or "").strip()
        )
        generated_description = strip_live_stats_from_description(
            generated_description,
            str((grounding_context or {}).get("live_stats") or ""),
        )
        generated_description = re.sub(
            r"^直播录播[：:].*?[。.!！]\s*",
            "",
            generated_description,
            count=1,
        ).strip()
        generated_description = remove_negative_rumor_text(
            strip_ai_timeline_lines(generated_description)
        )
        raw_timeline = [
            item
            for result in batch_results
            if isinstance(result.get("timeline"), list)
            for item in result.get("timeline", [])
            if not recording_text_contains_negative_rumor(
                str(item.get("event") or "") if isinstance(item, dict) else str(item)
            )
        ]
        timeline_text = render_grounded_danmaku_timeline(
            raw_timeline,
            selected,
            comments,
            delay_seconds=reaction_delay,
            duration_seconds=timeline_duration_seconds,
            maximum_points=None,
            anchor_diagnostics=diagnostics,
        )
        verified_count = len(timeline_lines(timeline_text))
        if verified_count < timeline_minimum and len(selected) >= timeline_minimum:
            diagnostics["timeline_retry_attempted"] = True
            retry_payload = dict(payload)
            retry_selected = select_summary_comments(selected, 800)
            retry_payload["sampled_comment_evidence"] = [
                {
                    "timestamp_seconds": max(0, int(float(comment.time))),
                    "text": str(comment.text),
                }
                for comment in retry_selected
            ]
            retry_payload.update({
                "verified_timeline": timeline_lines(timeline_text),
                "timeline_shortfall": timeline_minimum - verified_count,
            })
            diagnostics["description_regeneration_attempted"] = True
            retry_prompt = f"""
你正在重新生成一份时间点不完整的直播录播简介。首稿目标至少 {timeline_minimum} 条重要时间点，
程序只核验通过 {verified_count} 条。必须重新生成完整 description 和完整 timeline，不是只补几条时间点；
description 中的每个关键事件都要在 timeline 中有对应证据，timeline 尽量覆盖开头、中段和结尾，
不得复述同一事件凑数。目标数量只是帮助检查遗漏：若输入确有更多独立且可靠的看点，应尽量详细收录；
若证据不足则允许少于目标，禁止使用问候、持续过程、普通闲聊或“继续游戏/继续聊天”补齐。
	每条必须写清可确认的主语、具体动作或讨论对象，以及变化、结果或观众反应；无法确认人物时使用中性主语。
	英雄优先使用 verified_live_context；缺失时，只有多条原始 XML 直接、重复且无冲突地绑定人物与同一英雄才可写入。
	装备讨论可按原文忠实保留，但最终持有装备与 KDA 只能来自 verified_live_context；不得猜测游戏名或胜负。
	仅由弹幕支持的中性现实消息必须保留来源限定；
	较负面、未经证实、可能损害人物名誉的现实传言必须整条删除，不得用“弹幕称”包装后保留。
相邻候选如果是同一人物、同一动作和同一结果的延续，应合并；只有新阶段、新决定、转折或结果才能拆开。
每项优先从 sampled_comment_evidence 逐字复制 1 至 3 条 evidence_texts，
并提供足以支持 event 的 evidence_keywords；同一 60 秒内至少 3 条相关刷屏也可以作为证据。
证据不足就删掉正文中的对应事件，绝不能编造。不要返回时间戳。
返回 JSON 对象：{{"description":"...","timeline":[{{"event":"...","evidence_texts":["..."],"evidence_keywords":["..."]}}]}}。
""".strip()
            try:
                with ai_metadata_request_slot(cfg):
                    retry_result = _request_json_object(
                        client=ai_client,
                        model_name=model_name,
                        system_prompt=retry_prompt,
                        payload=retry_payload,
                        max_tokens=1400,
                        temperature=0.1,
                        thinking_enabled=thinking_enabled,
                        logger_obj=None,
                        scene_name="recording_danmaku_description_regenerate",
                    )
                regenerated_description = str(
                    (retry_result or {}).get("description", "")
                ).strip()
                regenerated_description = strip_live_stats_from_description(
                    regenerated_description,
                    str((grounding_context or {}).get("live_stats") or ""),
                )
                regenerated_description = re.sub(
                    r"^直播录播[：:].*?[。.!！]\s*",
                    "",
                    regenerated_description,
                    count=1,
                ).strip()
                regenerated_description = remove_negative_rumor_text(
                    strip_ai_timeline_lines(regenerated_description)
                )
                regenerated_timeline = (
                    [
                        item
                        for item in list((retry_result or {}).get("timeline") or [])
                        if not recording_text_contains_negative_rumor(
                            str(item.get("event") or "")
                            if isinstance(item, dict)
                            else str(item)
                        )
                    ]
                    if isinstance((retry_result or {}).get("timeline"), list)
                    else []
                )
                regenerated_timeline_text = render_grounded_danmaku_timeline(
                    regenerated_timeline,
                    selected,
                    comments,
                    delay_seconds=reaction_delay,
                    duration_seconds=timeline_duration_seconds,
                    maximum_points=None,
                    anchor_diagnostics=diagnostics,
                )
                regenerated_count = len(timeline_lines(regenerated_timeline_text))
                if regenerated_count >= verified_count:
                    generated_description = regenerated_description
                    raw_timeline = regenerated_timeline
                    timeline_text = regenerated_timeline_text
                    verified_count = regenerated_count
                    diagnostics["description_regeneration_used"] = True
                else:
                    diagnostics["description_regeneration_used"] = False
            except Exception as exc:
                diagnostics["timeline_retry_error"] = str(exc)[:240]
                print(f"WARN 弹幕简介重新生成失败，保留首轮结果: {exc}", file=sys.stderr)
        verified_candidates = timeline_lines(timeline_text)
        if len(verified_candidates) > timeline_maximum:
            selection_payload = {
                "verified_candidates": [
                    {"index": index, "timeline": line}
                    for index, line in enumerate(verified_candidates)
                ],
                "target_count": {
                    "minimum": timeline_minimum,
                    "maximum": timeline_maximum,
                },
            }
            selection_prompt = """
你是直播录播简介的全局编辑。输入只包含已经通过完整 XML 校验的候选时间点。
从中选择最值得进入最终简介的事件，优先保留具体动作、结果、转折、重要互动和节目效果，
删除重复、空泛或信息量低的候选，并覆盖录播开头、中段和结尾。
最终简介以“尽可能详细但不重复”为目标：只要候选具有独立的信息增量、证据可靠且不是同一事件的换句话说，
就应尽量保留，不能为了让简介短而只选少数几个；候选超过上限时才淘汰相对较弱的项目。
游戏对局中，优先级为“人物+英雄+事件” > “人物+事件” > “英雄+事件” > “只有弹幕反应”。
通过密集且无冲突弹幕已完成人物—英雄归因的候选，信息量高于只写“沉默没开大”等无主体句子，应优先保留。
如果是竞技比赛，最终简介必须让读者知道“谁和谁在比赛”：候选中存在有证据的参赛双方或
关键选手信息时，必须至少保留一条交代对阵关系的事件，并优先保留用已确认人名而非“多人”“一方”
等泛称说清关键事件的候选。英雄归属和最终胜负仍必须各自有独立明确证据，不得为了补齐人名而猜测。
若候选中已有多条一致证据支持的明确结论，应优先保留该结论，不得为了“安全”只选更空泛的讨论句。
不得改写、合并或补充任何候选事实，只返回 selected_indexes JSON 数组。
""".strip()
            selected_indexes: list[int] = []
            try:
                with ai_metadata_request_slot(cfg):
                    selection_result = _request_json_object(
                        client=ai_client,
                        model_name=model_name,
                        system_prompt=selection_prompt,
                        payload=selection_payload,
                        max_tokens=300,
                        temperature=0.1,
                        thinking_enabled=thinking_enabled,
                        logger_obj=None,
                        scene_name="recording_danmaku_timeline_select",
                    )
                raw_indexes = (
                    selection_result.get("selected_indexes", [])
                    if isinstance(selection_result, dict)
                    else []
                )
                if isinstance(raw_indexes, list):
                    selected_indexes = sorted({
                        int(index)
                        for index in raw_indexes
                        if str(index).strip().lstrip("-").isdigit()
                        and 0 <= int(index) < len(verified_candidates)
                    })
                if not timeline_minimum <= len(selected_indexes) <= timeline_maximum:
                    selected_indexes = []
            except Exception as exc:
                diagnostics["timeline_global_selection_error"] = str(exc)[:240]
            if selected_indexes:
                diagnostics["timeline_global_selection_source"] = "ai_verified_indexes"
            else:
                selected_indexes = sorted({
                    round(index * (len(verified_candidates) - 1) / (timeline_maximum - 1))
                    for index in range(timeline_maximum)
                })
                diagnostics["timeline_global_selection_source"] = "even_fallback"
            verified_candidates = [
                verified_candidates[index] for index in selected_indexes
            ]
            timeline_text = "重要时间点\n" + "\n".join(verified_candidates)
        diagnostics["timeline_preselection_verified_count"] = verified_count
        verified_count = len(verified_candidates)
        diagnostics.update({
            "timeline_candidate_count": len(raw_timeline),
            "timeline_verified_count": verified_count,
            "timeline_rejected_count": max(0, len(raw_timeline) - verified_count),
            "timeline_target_met": verified_count >= timeline_minimum,
            "timeline_shortfall": max(0, timeline_minimum - verified_count),
            "timeline_evidence_status": (
                "sufficient" if verified_count >= timeline_minimum else "insufficient"
            ),
        })
        if timeline_text:
            # Verified timestamp lines are the editorial body. The earlier
            # prose draft is intentionally discarded so a long narrative can
            # never drift away from a sparse, clickable timeline.
            description = "\n".join(timeline_lines(timeline_text))
        else:
            description = (
                f"{base_description}{generated_description}"
                if generated_description
                else base_description
            )
        final_description = (
            fit_description_preserving_timeline(description, 1800)
            if description else base_description
        )
        participation_mode = infer_streamer_participation_mode(
            final_description,
            str(payload["streamer_identity"].get("public_name") or ""),
            gameplay_verified=streamer_gameplay_verified,
        )
        payload["streamer_participation"]["mode"] = participation_mode
        payload["streamer_identity"]["preferred_title_name"] = (
            preferred_recording_title_name(
                str(payload["streamer_identity"].get("public_name") or ""),
                final_description,
            )
        )
        title_payload = {
            "streamer_identity": payload["streamer_identity"],
            "streamer_participation": payload["streamer_participation"],
            "verified_live_context": payload["verified_live_context"],
            "video_duration_seconds": timeline_duration_seconds,
            "final_description": final_description,
            "verified_timeline": timeline_lines(final_description),
        }
        title_system_prompt = f"""
你是哔哩哔哩直播录播标题编辑。简介已经生成并通过时间点校验，现在只能根据 final_description
和 verified_timeline 拟定标题，不得使用首稿、直播间默认标题或输入外的信息。
必须遵守 streamer_participation：mode=spectating 时，只有标题核心事件确实来自主播的观看、解说或
点评视角，才把当前主播写成观战、观赛、解说或点评者；mode=unknown 时不得凭房间归属声称主播参赛或观战，
但 final_description/verified_timeline 已通过完整 XML 核验的人物—英雄直接关系可以原样用于标题。
除此之外应直接描述已核验的具体事件、其他明确人物、英雄或比赛，不得补“主播直播间热议/讨论/调侃”等模板前缀。
spectating 禁止写成当前主播正在操刀任何英雄；unknown 也不得新增已核验时间线之外的主播英雄关系。其他选手与英雄仅可沿用 final_description 中由多条连续、明确且
无冲突上下文确认的关系；若只知道两个英雄出现在对局中，只能写“英雄甲对阵英雄乙”，不得猜成
“主播操刀英雄甲”或“某人使用英雄乙”。
当 mode=playing 且标题选中的时间点能唯一落入 verified_live_context.game_segments 时，该段英雄是当前主播
已确认使用的英雄。若时间点描述的是主播当局操作、出装、团战、推进或结果，标题必须用该英雄作自然主语，
不得省略成“高地推进”“连续失误”等无主体表达；同时必须自然使用 streamer_identity.preferred_title_name，
至少一次交代清楚“谁用什么英雄”，不能只写英雄而遗漏人物，也不能写成“主播名｜事件”的栏目标签。
标题默认选择简介中最有看点的一个具体事件；若有两个同等重要、彼此独立且都值得展示的事件，允许同时入题，
但必须把更重要的事件放在前面，并用中文分号“；”分隔。最多两个事件，不能加入第三个；每个事件的核心动作、
人物和结果都必须分别能在简介或已核验时间点中找到，不能合并成不存在的因果关系。
拟题前先判断整段结构：同一挑战、对局或养成过程贯穿多个时间点时，使用 main_arc，把起点、关键转折和结果
串成一条主线；只有观众反应确实推动事件或构成核心笑点时才纳入主线。节目、聊天或小游戏频繁切换时，
使用 two_highlights，选择两个跨阶段强看点；
只有少量有效时间点时使用 sparse，不为凑长度虚构整小时内容。不得先挑一句再反推整段主题。
结合 video_duration_seconds 判断标题覆盖范围：45分钟以上且 verified_timeline 至少6条时，标题应概括贯穿全段的
主线；若没有单一主线，就选择两个覆盖不同阶段的最强独立看点并用分号分隔。此类长录播必须由至少两个跨阶段
时间点支撑，不能把一小时录播写成十几秒片段的关键词摘要。
选择时依次比较：明确结果或强反差、关键操作或决定、阶段性转折、可复述的节目效果、信息明确的重要讨论。
若 verified_live_context 的 game/game_segments 标记 ended_confirmed，它只用于确定对局边界，不能单独作为标题；
只有 verified_timeline 同时明确写出胜负、翻盘、基地告破或决定性收尾时，具体结束结果才提升为最高优先级。
禁止生成“本局结束”“转入下一局”“开始下一把”等没有具体结果的信息。
即使标题还有其他看点，也不得用“结束后转入下一局、进入某英雄对局”等过场语串联；直接写两局各自的具体事件。
弹幕很多但没有具体动作、对象或结果，不等于标题价值高。“进入后段/进入环节/继续进行/第几圈左右”只是过程状态，
不能单独作为标题；应优先选择后续已经出现的完成、反超、胜负、分数、关键操作或强反差。多个候选强度接近时，
优先选择证据最集中、人物关系最清楚、
脱离直播上下文仍能读懂的事件；只有两个事件都足够重要且在48字内仍能分别完整表达时才同时保留，不能压缩成关键词拼盘。
标题中的胜负、晋级、淘汰、夺冠、翻盘或被翻盘关系必须与 final_description 的人物和方向完全一致；
不得把观战者写成获胜者，也不得因领先、欢呼、嘲讽或“五冠王”等身份梗推断本场结果。
签约、加入或转入某组织、官宣、解约、开除、婚恋、疾病、违法、收入等现实身份与状态，如果仅由弹幕支持，
标题必须保留来源限定。较负面、未经证实、可能损害人物名誉的现实传言不得入题，也不得通过添加“弹幕称”保留；
中性现实消息使用“观众讨论”，明显玩笑使用“直播间调侃”，绝不能写成已经核实的事实。
当前主播确实是标题事件的参与者、观战者、评价者或话题对象时，优先使用
streamer_identity.preferred_title_name，并自然融入“谁做了什么”的事件句；主播只是房间归属或背景时不要出现，
不得写“主播名直播间热议/讨论”或“主播名直播中其他人做了什么”。
简介能用连续明确证据确认当前主播与其他人物一起玩、组队或对战时，标题优先写清“谁和谁做了什么”，尤其保留
共同挑战、配合、互相追赶、反超或胜负关系；只因两个人名同时出现，或其中一人只是观战、串门、被提及，
不得猜测为一起玩。
本录播以弹幕观看体验为重点，但标题不需要每条都出现“弹幕”或“观众”。先把人物、动作、转折和结果写完整；
删除观众反应后仍不影响事件主线时，优先不写。只有观众反应本身推动剧情、形成明显反差或成为核心笑点时，
标题才可采用“事件主线 + 具体弹幕反应”，写清弹幕在提醒什么、催什么、刷什么梗或调侃成什么；同一标题最多
保留一处观众反应。不得只写没有内容的“弹幕热议/讨论/关注”，也不得为了出现“弹幕”而牺牲事件主线。
结构化 GSI 已确认主播当局操作时，直接写主播、英雄和事件，不得以“观众讨论、弹幕认为、直播间质疑”开头。
结构示例只用于理解写法，绝不能复制未出现在输入中的事实：连续挑战可写“某人一人挑战十人接力，三圈套圈后
后程悬念拉满”；多环节可写“连战多款小游戏，从断崖垫底到手速局终于第一”；若刷梗确实是核心笑点，才可写
“合成EX后战力冲万亿，小怪依旧刮痧，0%输出笑翻弹幕”。
游戏内容要让读者自然理解游戏语境：DOTA2 默认通过英雄、装备、模式、比赛或选手体现，不机械写游戏名；
其他游戏只有可靠识别且有助理解时才自然写入，无法确认游戏名时不得猜测。
标题要像自然中文事件句，而不是搜索关键词堆叠、摘要栏目名或营销结论；避免连续堆放人名、英雄名和情绪词。
不得出现未知、Role 编号、内部 ID、文件名、任务号、模型诊断词或直播间默认标题中的无证据信息。
禁止“主播名｜事件”“主播名：事件”格式，不含日期、时间和“直播回放”。完整信息优先，不追求越短越好；
普通录播通常用22至44字；一小时长录播在证据充足时通常用30至46字保留主线、转折和结果，只有有效内容确实稀疏时
才可更短，绝不能为凑字补猜。标题最多48个字符；
双事件标题也必须在48字内
分别表达完整，超长时优先删除较弱事件或重新改写，禁止截断半句话。
{title_prompt}
本直播间自定义要求只能补充题材重点、语气和风格，不能推翻标题长度、证据边界、人物关系、主播名使用、
观众反应数量、长录播覆盖范围和风险过滤规则；较负面的未经证实现实传言无论自定义要求如何指定都不得入题；
具体比赛、游戏表现和现场事件不得使用“直播间热议/讨论/关注”作为兜底主语。
verified_timeline 按0开始编号。返回标题时必须同时返回 selected_timeline_indexes，列出直接支撑标题的时间点；
长录播的主线标题也必须列出至少两个跨阶段证据，不能只引用单个瞬间。
同时返回 coverage_mode，值只能是 main_arc、two_highlights 或 sparse。
同时返回 cover_text：只压缩标题中排序第一的核心事件，证据充足时优先16至24字、最多28字，可比投稿标题短；
保留人物或英雄、关键装备或阶段、核心动作、转折或结果中至少两类有区分度的信息，不要只剩一个泛化动作；
有效内容确实稀疏时可用8至15字。不得新增事实、不得强塞主播名、不得删除中性消息的来源限定。
无法安全压缩时返回空字符串。
返回 JSON 对象：{{"title_topic":"...","cover_text":"...","coverage_mode":"main_arc","selected_timeline_indexes":[0,4]}}。
""".strip()
        title_generation_validated = False
        proposed_cover_text = ""
        try:
            title_topic = ""
            for title_attempt in range(3):
                with ai_metadata_request_slot(cfg):
                    title_result = _request_json_object(
                        client=ai_client,
                        model_name=model_name,
                        system_prompt=title_system_prompt,
                        payload=title_payload,
                        max_tokens=300,
                        temperature=0.25,
                        thinking_enabled=thinking_enabled,
                        logger_obj=None,
                        scene_name="recording_danmaku_title_from_description",
                    )
                candidate_topic = normalize_recording_title_filler(
                    str((title_result or {}).get("title_topic", "")).strip()
                )
                rejection_reason = ""
                if len(candidate_topic) > RECORDING_TITLE_TOPIC_LIMIT:
                    diagnostics.update({
                        "title_topic_over_limit": True,
                        "title_topic_over_limit_original": candidate_topic,
                    })
                    rejection_reason = f"标题超过{RECORDING_TITLE_TOPIC_LIMIT}字"
                elif recording_title_topic_is_vague(candidate_topic):
                    rejection_reason = "标题只描述过程或使用空泛套话"
                elif recording_title_uses_opaque_attribution(candidate_topic):
                    rejection_reason = "标题使用了被指、被曝或据称等模糊来源词"
                elif recording_title_topic_is_underfilled(
                    candidate_topic,
                    timeline_duration_seconds,
                    len(title_payload["verified_timeline"]),
                ):
                    diagnostics["title_topic_underfilled_for_long_video"] = True
                    rejection_reason = "长录播标题只覆盖一个过短的微小节点"
                elif not recording_title_timeline_coverage_is_sufficient(
                    (title_result or {}).get("selected_timeline_indexes"),
                    timeline_duration_seconds,
                    len(title_payload["verified_timeline"]),
                    title_payload["verified_timeline"],
                ):
                    diagnostics["title_topic_long_video_coverage_rejected"] = True
                    rejection_reason = "长录播标题没有跨阶段时间点支撑"
                else:
                    selected_timeline_indexes = (
                        (title_result or {}).get("selected_timeline_indexes")
                    )
                    game_segments = title_payload["verified_live_context"].get(
                        "game_segments"
                    )
                    if not isinstance(game_segments, list) or not game_segments:
                        single_game = title_payload["verified_live_context"].get("game")
                        if streamer_gameplay_is_verified(single_game):
                            game_segments = [{
                                **single_game,
                                "start_seconds": 0,
                                "end_seconds": max(
                                    float(timeline_duration_seconds or 0),
                                    24 * 60 * 60,
                                ),
                            }]
                    missing_gsi_heroes = recording_title_missing_selected_gsi_heroes(
                        candidate_topic,
                        selected_timeline_indexes,
                        title_payload["verified_timeline"],
                        game_segments,
                    )
                    if missing_gsi_heroes:
                        diagnostics["title_topic_missing_selected_gsi_heroes"] = (
                            missing_gsi_heroes
                        )
                        rejection_reason = (
                            "已选游戏事件遗漏对应的主播 GSI 英雄："
                            + "、".join(missing_gsi_heroes)
                        )
                    elif recording_title_missing_selected_gsi_streamer(
                        candidate_topic,
                        selected_timeline_indexes,
                        title_payload["verified_timeline"],
                        game_segments,
                        str(
                            title_payload["streamer_identity"].get(
                                "preferred_title_name"
                            )
                            or title_payload["streamer_identity"].get("public_name")
                            or ""
                        ),
                    ):
                        diagnostics["title_topic_missing_selected_gsi_streamer"] = True
                        rejection_reason = (
                            "已选主播游戏事件遗漏已确认的人物主语："
                            + str(
                                title_payload["streamer_identity"].get(
                                    "preferred_title_name"
                                )
                                or title_payload["streamer_identity"].get("public_name")
                                or "主播"
                            )
                        )
                    elif recording_title_audience_prefix_obscures_selected_gsi_gameplay(
                        candidate_topic,
                        selected_timeline_indexes,
                        title_payload["verified_timeline"],
                        game_segments,
                    ):
                        diagnostics[
                            "title_topic_audience_prefix_for_verified_gameplay"
                        ] = True
                        rejection_reason = (
                            "结构化 GSI 已确认主播操作，标题不得用观众或弹幕作开头"
                        )
                if not rejection_reason:
                    title_topic = candidate_topic
                    proposed_cover_text = str(
                        (title_result or {}).get("cover_text") or ""
                    ).strip()
                    diagnostics["title_coverage_mode"] = str(
                        (title_result or {}).get("coverage_mode") or ""
                    ).strip()
                    diagnostics["title_selected_timeline_indexes"] = list(
                        (title_result or {}).get("selected_timeline_indexes") or []
                    )
                    title_generation_validated = True
                    break
                title_payload["rejected_title_topic"] = candidate_topic
                title_payload["rejected_title_reason"] = rejection_reason
                diagnostics["title_topic_retry_reason"] = rejection_reason
            diagnostics["title_topic_source"] = "final_description"
        except Exception as exc:
            diagnostics["title_generation_error"] = str(exc)[:240]
            title_topic = ""
        original_title_topic = title_topic
        if recording_title_topic_is_vague(original_title_topic):
            diagnostics.update({
                "title_topic_manual_review_required": True,
                "title_topic_original": original_title_topic,
                "title_topic_review_reason": "AI 返回空泛或默认标题",
            })
        title_topic = recording_title_topic_from_timeline(
            title_topic,
            final_description,
            diagnostics=diagnostics,
        )
        if (
            not title_generation_validated
            and not recording_title_timeline_coverage_is_sufficient(
                [],
                timeline_duration_seconds,
                len(title_payload["verified_timeline"]),
                title_payload["verified_timeline"],
            )
        ):
            diagnostics.update({
                "title_topic_manual_review_required": True,
                "title_topic_long_video_fallback_rejected": True,
                "title_topic_review_reason": (
                    "长录播标题连续重写后仍未获得跨阶段证据"
                ),
            })
            title_topic = ""
        if title_topic != original_title_topic:
            diagnostics.update({
                "title_topic_vague_replaced": True,
                "title_topic_original": original_title_topic,
                "title_topic_replacement_source": "verified_timeline",
            })
        attributed_title_topic = contextualize_streamer_title_topic(
            title_topic,
            str(
                payload["streamer_identity"].get("preferred_title_name")
                or payload["streamer_identity"].get("public_name")
                or ""
            ),
            participation_mode,
        )
        if len(attributed_title_topic) > RECORDING_TITLE_TOPIC_LIMIT:
            diagnostics.update({
                "title_topic_context_over_limit": True,
                "title_topic_context_over_limit_original": attributed_title_topic,
            })
            attributed_title_topic = title_topic
        if attributed_title_topic != title_topic:
            diagnostics.update({
                "title_topic_streamer_context_added": True,
                "title_topic_before_streamer_context": title_topic,
                "title_topic_participation_mode": participation_mode,
            })
            title_topic = attributed_title_topic
        title_gsi_segments = payload["verified_live_context"].get("game_segments")
        if not isinstance(title_gsi_segments, list) or not title_gsi_segments:
            title_single_game = payload["verified_live_context"].get("game")
            title_gsi_segments = (
                [title_single_game]
                if streamer_gameplay_is_verified(title_single_game)
                else []
            )
        title_streamer = str(
            payload["streamer_identity"].get("preferred_title_name")
            or payload["streamer_identity"].get("public_name")
            or ""
        )
        if not title_person_hero_relations_supported_with_gsi(
            title_topic,
            final_description,
            title_streamer,
            title_gsi_segments,
        ):
            diagnostics.update({
                "title_topic_person_hero_relation_rejected": True,
                "title_topic_before_relation_filter": title_topic,
            })
            fallback_topic = recording_title_topic_from_timeline(
                "",
                final_description,
                diagnostics=diagnostics,
            )
            title_topic = contextualize_streamer_title_topic(
                fallback_topic,
                str(payload["streamer_identity"].get("public_name") or ""),
                participation_mode,
            )
            if not title_person_hero_relations_supported_with_gsi(
                title_topic,
                final_description,
                title_streamer,
                title_gsi_segments,
            ):
                title_topic = ""
        if not title_competitive_results_supported(title_topic, final_description):
            diagnostics.update({
                "title_topic_competitive_result_rejected": True,
                "title_topic_before_result_filter": title_topic,
            })
            title_topic = ""
        negative_title_rumor = recording_text_contains_negative_rumor(title_topic)
        qualified_title_topic = qualify_danmaku_only_real_world_claim(title_topic)
        if qualified_title_topic != title_topic:
            diagnostics.update({
                "title_topic_danmaku_claim_qualified": True,
                "title_topic_before_danmaku_claim_filter": title_topic,
            })
            if title_topic and not qualified_title_topic:
                if negative_title_rumor:
                    diagnostics.update({
                        "title_topic_negative_rumor_rejected": True,
                        "title_topic_manual_review_required": True,
                        "title_topic_review_reason": "负面未经证实现实传言不得进入标题",
                    })
                else:
                    diagnostics.update({
                        "title_topic_qualification_over_limit": True,
                        "title_topic_manual_review_required": True,
                        "title_topic_review_reason": (
                            f"添加来源限定后超过{RECORDING_TITLE_TOPIC_LIMIT}字，已拒绝不完整标题"
                        ),
                    })
            title_topic = qualified_title_topic
        cover_text = recording_cover_display_text(
            title_topic,
            proposed_cover_text,
            str(payload["streamer_identity"].get("public_name") or ""),
        )
        diagnostics.update({
            "cover_text": cover_text,
            "cover_text_length": len(cover_text),
            "cover_text_source": (
                "ai_grounded"
                if proposed_cover_text and cover_text == normalize_recording_title_filler(
                    proposed_cover_text
                ).split("；", 1)[0].strip(" -_｜|：:，,。.!！；; ")
                else ("deterministic_fallback" if cover_text else "text_free")
            ),
        })
        final_verified_count = len(timeline_lines(final_description))
        diagnostics.update({
            "timeline_verified_count": final_verified_count,
            "timeline_rejected_count": max(0, len(raw_timeline) - final_verified_count),
            "timeline_target_met": final_verified_count >= timeline_minimum,
            "timeline_shortfall": max(0, timeline_minimum - final_verified_count),
            "timeline_evidence_status": (
                "sufficient"
                if final_verified_count >= timeline_minimum
                else "insufficient"
            ),
        })
        return final_description, title_topic
    except Exception as exc:
        error_detail = safe_task_error_detail(exc)
        diagnostics = timeline_diagnostics if timeline_diagnostics is not None else {}
        diagnostics["ai_metadata_error"] = error_detail
        diagnostics["ai_metadata_error_type"] = exc.__class__.__name__
        print(
            f"WARN 弹幕 AI 简介生成失败，使用原简介: {error_detail}",
            file=sys.stderr,
        )
        return base_description, ""


def generate_danmaku_metadata_with_ai(
    comments,
    base_description: str,
    cfg: dict[str, Any],
    grounding_context: dict[str, Any] | None = None,
    timeline_duration_seconds: float | None = None,
    timeline_diagnostics: dict[str, Any] | None = None,
    queue_entered_callback: Callable[[float], None] | None = None,
) -> tuple[str, str]:
    """Queue one task, then generate its AI description and title together."""
    if not comments or not bool(cfg.get("ai_danmaku_summary_enabled", True)):
        return base_description, ""
    with ai_metadata_queue(cfg) as queue_wait_seconds:
        if timeline_diagnostics is not None:
            timeline_diagnostics["ai_metadata_queue_wait_seconds"] = round(
                queue_wait_seconds,
                3,
            )
        if queue_entered_callback is not None:
            queue_entered_callback(queue_wait_seconds)
        return _generate_danmaku_metadata_with_ai(
            comments,
            base_description,
            cfg,
            grounding_context,
            timeline_duration_seconds,
            timeline_diagnostics,
        )


def upload_one(video: Path, base_cfg: dict[str, Any], store: StateStore,
               dry_run: bool = False, retry: bool = False,
               danmaku_xml: Path | None = None,
               session_key: str = "") -> bool:
    if session_key:
        cfg = effective_config(base_cfg, video)
        with multipart_session_queue(cfg, session_key):
            return _upload_one_unlocked(
                video,
                base_cfg,
                store,
                dry_run=dry_run,
                retry=retry,
                danmaku_xml=danmaku_xml,
                session_key=session_key,
            )
    return _upload_one_unlocked(
        video,
        base_cfg,
        store,
        dry_run=dry_run,
        retry=retry,
        danmaku_xml=danmaku_xml,
        session_key=session_key,
    )


def _upload_one_unlocked(video: Path, base_cfg: dict[str, Any], store: StateStore,
                         dry_run: bool = False, retry: bool = False,
                         danmaku_xml: Path | None = None,
                         session_key: str = "") -> bool:
    cfg = effective_config(base_cfg, video)
    platform = "bilibili"
    wait_until_stable(video, int(cfg.get("stable_checks", 2)), float(cfg.get("stable_interval_seconds", 2)))
    danmaku_xml = danmaku_xml or find_danmaku_xml(video)
    key = fingerprint(video, danmaku_xml)
    prior_result = store.results(key)
    if retry and not session_key:
        previous_session_key = store.upload_session_key(key)
        previous_session = (
            store.multipart_session(previous_session_key, include_closed=True)
            if previous_session_key
            else {}
        )
        previous_pending_video = str(
            previous_session.get("pending_first_video") or ""
        ).strip()
        same_pending_first_part = bool(
            previous_pending_video
            and Path(previous_pending_video).expanduser().resolve() == video.resolve()
        )
        # Repair the original multipart session when this job is its pending
        # first part. Detaching it would leave every later segment blocked by
        # a session that can never acquire a BVID.
        if (
            isinstance(previous_session.get("bilibili"), dict)
            or same_pending_first_part
        ):
            session_key = previous_session_key
    review_override = store.review_override(key)
    prior_burn_stage = store.stage_state(key, "burn") if retry else {}
    prior_ai_stage = store.stage_state(key, "ai") if retry else {}
    prior_cover16_stage = store.stage_state(key, "cover_16x9") if retry else {}
    prior_cover43_stage = store.stage_state(key, "cover_4x3") if retry else {}
    if retry and not prior_cover16_stage:
        prior_cover16_stage = store.stage_state(key, "cover")
    if retry and not prior_cover43_stage:
        prior_cover43_stage = store.stage_state(key, "cover")
    is_new_task = not store.upload_exists(key)
    if not store.claim(key, video, platform, retry=retry):
        print(f"SKIP 已处理或正在处理: {video}")
        return True
    if is_new_task and not dry_run:
        emit_recording_task_added_notification(
            cfg,
            fingerprint_value=key,
            video=video,
            task_kind="recording_upload",
        )

    multipart = (
        store.multipart_session(session_key, include_closed=retry)
        if session_key
        else {}
    )
    session_status = str(multipart.pop("_session_status", "open")) if multipart else "open"
    if session_key and not multipart:
        multipart = {
            "pending_first_video": str(video.resolve()),
            "title": "",
            "description": "",
            "tags": [],
            "source_url": str(cfg.get("source_url", "")).strip(),
        }
        if not dry_run:
            store.save_multipart_session(session_key, multipart)
    pending_first_video = str(multipart.get("pending_first_video") or "")
    blocked_by_pending_part = bool(
        session_key
        and pending_first_video
        and Path(pending_first_video).resolve() != video.resolve()
        and not multipart.get("bilibili")
    )
    existing_submission = multipart.get("bilibili") if multipart else None
    explicit_review_hold = bool(
        review_override.get("hold_before_cover")
        and review_override.get("pre_upload_review_requested_at")
    )
    review_confirmed = bool(
        review_override.get("pre_upload_review_confirmed_at")
    )
    raw_manual_review_fields = review_override.get("manual_review_fields")
    manual_review_fields = (
        {
            str(field).strip()
            for field in raw_manual_review_fields
            if str(field).strip()
        }
        if isinstance(raw_manual_review_fields, list)
        else None
    )

    def review_field_applies(field: str) -> bool:
        """Apply only explicit/confirmed edits, never an implicit stale snapshot."""
        if field not in review_override:
            return False
        if isinstance(existing_submission, dict) or explicit_review_hold or review_confirmed:
            return True
        if manual_review_fields is not None:
            return field in manual_review_fields
        # Compatibility for reviews saved before field provenance existed. The
        # old save path accidentally set hold_before_cover without recording an
        # explicit review request; that exact shape must not override a retry.
        return not bool(review_override.get("hold_before_cover"))

    prior_bilibili = prior_result.get("bilibili")
    resuming_uploaded_part = bool(
        retry
        and isinstance(prior_bilibili, dict)
        and prior_bilibili.get("bvid")
    )
    if resuming_uploaded_part:
        part_number = max(1, int(prior_result.get("part_number") or 1))
    else:
        part_number = (
            int(existing_submission.get("part_count") or 0) + 1
            if isinstance(existing_submission, dict)
            else 1
        )
    recording_duration_seconds = recording_effective_duration_seconds(
        video,
        str(cfg.get("ffprobe", "ffprobe")),
    )
    store.finish(key, "processing", {
        **prior_result,
        "worker_pid": os.getpid(),
        "multipart_session": session_key or None,
        "part_number": part_number,
        "video_duration_seconds": recording_duration_seconds,
    })
    work_dir = store.path.parent / "artifacts" / key[:16]
    current_stage = "ass"
    try:
        if blocked_by_pending_part:
            current_stage = "upload"
            raise RuntimeError("前一分P尚未上传成功，请先重试前一分P")

        title, description, tags = render_metadata(video, cfg)
        manual_cover_path = str(review_override.get("cover_path") or "").strip()
        manual_cover43_path = str(review_override.get("cover43_path") or "").strip()
        if manual_cover_path and Path(manual_cover_path).is_file():
            original_cover = Path(manual_cover_path)
        else:
            current_stage = "cover_16x9"
            original_cover = find_cover(video, cfg, work_dir)
        cover = original_cover
        cover43: Path | None = (
            Path(manual_cover43_path)
            if manual_cover43_path and Path(manual_cover43_path).is_file()
            else None
        )
        source_url = str(cfg.get("source_url", "")).strip()

        current_stage = "ass"
        upload_video = video
        danmaku_burned_for_upload = False
        ass_path = None
        comments = []
        store.stage(key, "ass", "running", {"danmaku_xml": str(danmaku_xml) if danmaku_xml else None})
        if danmaku_xml and bool(cfg.get("danmaku_enabled", True)):
            comments = parse_danmaku_xml(danmaku_xml)
            if comments:
                width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
                ass_path = build_ass(
                    comments,
                    work_dir / f"{video.stem}.ass",
                    width=width,
                    height=height,
                    font_name=str(cfg.get("danmaku_font_name", "Noto Sans CJK SC")),
                    font_size=int(cfg.get("danmaku_font_size", 42)),
                    duration=float(cfg.get("danmaku_duration_seconds", 10)),
                    opacity=float(cfg.get("danmaku_opacity", 0.92)),
                )
                if bool(cfg.get("danmaku_burn_in", False)) and not dry_run:
                    current_stage = "burn"
                    burned_output = video.with_name(f"{video.stem}.danmaku.mp4")
                    burn_stage_details = {
                        "source_video_path": str(video),
                        "ass_path": str(ass_path),
                        "burned_video_path": str(burned_output),
                        "burned_video_location": "recording_directory",
                    }
                    reusable_burn = None
                    if retry:
                        reusable_burn, reuse_details = reusable_burned_video_for_retry(
                            video,
                            prior_burn_stage,
                            str(cfg.get("ffprobe", "ffprobe")),
                        )
                        if reusable_burn is not None:
                            upload_video = reusable_burn
                            danmaku_burned_for_upload = True
                            burn_stage_details.update(reuse_details)
                            store.stage(key, "burn", "completed", burn_stage_details)

                    if reusable_burn is None:
                        store.stage(key, "burn", "queued", burn_stage_details)

                        def update_burn_queue(status: str) -> None:
                            store.stage(
                                key,
                                "burn",
                                "running" if status == "burning" else "queued",
                                burn_stage_details,
                            )

                        def update_burn_progress(progress: dict[str, Any]) -> None:
                            burn_stage_details.update(progress)
                            store.stage(key, "burn", "running", burn_stage_details)
                        upload_video = burn_ass(
                            video,
                            ass_path,
                            burned_output,
                            ffmpeg=str(cfg.get("ffmpeg", "ffmpeg")),
                            fonts_dir=resolve_path(
                                str(cfg.get("danmaku_fonts_dir", "potatoflow-app/fonts")), cfg
                            ),
                            preset=str(cfg.get("danmaku_encode_preset", "medium")),
                            crf=int(cfg.get("danmaku_encode_crf", 20)),
                            encoder=str(cfg.get("danmaku_encoder", "cpu")),
                            queue_status_callback=update_burn_queue,
                            progress_callback=update_burn_progress,
                        )
                        danmaku_burned_for_upload = True
                        store.stage(key, "burn", "completed", {
                            **burn_stage_details,
                            "burned_video_path": str(upload_video),
                            "burn_in": True,
                        })
                    store.exclude_recording(
                        upload_video,
                        "",
                        reason="generated_burn",
                    )
                else:
                    store.stage(key, "burn", "skipped", {
                        "reason": (
                            "试运行不转码"
                            if dry_run and bool(cfg.get("danmaku_burn_in", False))
                            else "直播间未开启 ASS 弹幕烧录"
                        ),
                        "burn_in": bool(cfg.get("danmaku_burn_in", False)),
                    })
                current_stage = "ass"
                ass_details = danmaku_stage_details(video, danmaku_xml, comments, cfg)
                ass_details.update({
                    "danmaku_xml": str(danmaku_xml),
                    "ass_path": str(ass_path),
                    "burn_in": bool(cfg.get("danmaku_burn_in", False)),
                })
                store.stage(
                    key,
                    "ass",
                    "warning"
                    if ass_details["danmaku_integrity"] == "suspected_incomplete"
                    else "completed",
                    ass_details,
                )
            else:
                print(f"WARN 弹幕 XML 中没有可用弹幕: {danmaku_xml}", file=sys.stderr)
                ass_details = danmaku_stage_details(video, danmaku_xml, comments, cfg)
                ass_details.update({
                    "danmaku_xml": str(danmaku_xml),
                    "reason": "XML 中没有可用弹幕",
                })
                store.stage(
                    key,
                    "ass",
                    "warning"
                    if ass_details["danmaku_integrity"] == "suspected_incomplete"
                    else "skipped",
                    ass_details,
                )
                store.stage(key, "burn", "skipped", {"reason": "XML 中没有可用弹幕"})
        else:
            store.stage(
                key,
                "ass",
                "skipped",
                {
                    "reason": "未找到弹幕 XML 或弹幕处理未启用",
                    "video_duration_seconds": recording_duration_seconds,
                },
            )
            store.stage(key, "burn", "skipped", {"reason": "未生成 ASS 字幕"})

        # Collect stable live context before AI so metadata and both cover
        # variants are grounded in the same recording and the same game.
        app_root = resolve_app_root(cfg)
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        stats_enabled = bool(cfg.get("douyu_stats_enabled", True))
        append_stats_enabled = bool(cfg.get("douyu_stats_append_description", True))
        cover_context_enabled = bool(cfg.get("douyu_stats_cover_context_enabled", True))
        stats_text = ""
        live_stats_prepared = True
        current_stage = "live_stats"
        if not stats_enabled:
            store.stage(key, "live_stats", "skipped", {"reason": "斗鱼直播数据统计已关闭", "outcome": "disabled"})
        else:
            store.stage(key, "live_stats", "running", {"description_before_length": len(description)})
            try:
                from modules.douyu_stats_formatter import get_stats_for_description  # type: ignore
                stats_text = str(get_stats_for_description(str(video.parent)) or "")[:1900]
                if stats_text:
                    store.stage(
                        key,
                        "live_stats",
                        "completed",
                        live_stats_stage_details(stats_text),
                    )
                else:
                    store.stage(key, "live_stats", "skipped", {"reason": "本次录播时间内没有匹配的直播数据", "outcome": "no_data"})
            except Exception as exc:
                store.stage(key, "live_stats", "warning", {"reason": "直播数据整理失败，但不阻断投稿", "outcome": "failed_non_blocking"}, error=str(exc))

        locked_game_context: dict[str, Any] | None = None
        locked_game_segments: list[dict[str, Any]] = []
        identity_prepared = True
        current_stage = "xml_identity"
        if not stats_enabled:
            store.stage(key, "xml_identity", "skipped", {"reason": "斗鱼直播数据统计已关闭", "outcome": "disabled"})
        elif not cover_context_enabled:
            store.stage(key, "xml_identity", "skipped", {"reason": "XML 主播英雄与装备识别已关闭", "outcome": "disabled"})
        else:
            store.stage(key, "xml_identity", "running", {"danmaku_xml": str(danmaku_xml or ""), "comment_count": len(comments)})
            try:
                from modules.douyu_stats_formatter import get_game_for_cover, get_game_segments, get_identity_diagnostics  # type: ignore
                locked_game_context = get_game_for_cover(str(video.parent))
                locked_game_segments = get_game_segments(str(video.parent))
                identity_diagnostics = get_identity_diagnostics(str(video.parent))
                if locked_game_context:
                    anchor = locked_game_context
                    store.stage(key, "xml_identity", "completed", {
                        "danmaku_xml": str(danmaku_xml or ""), "comment_count": len(comments),
                        **identity_diagnostics, "streamer_hero": str(anchor.get("hero") or ""),
                        "streamer_items": [str(item) for item in anchor.get("items", [])[:6] if str(item)],
                        "streamer_neutral": str(anchor.get("neutral") or ""),
                        "streamer_scepter": bool(anchor.get("scepter")),
                        "streamer_shard": bool(anchor.get("shard")),
                        "equipment_snapshot_unix_ts": float(anchor.get("equipment_snapshot_unix_ts") or 0),
                        "gsi_observed_seconds": float(anchor.get("gsi_observed_seconds") or 0),
                        "xml_mention_score": int(anchor.get("xml_mention_score") or 0),
                        "xml_mention_burst_score": int(anchor.get("xml_mention_burst_score") or 0),
                        "xml_runner_up_score": int(anchor.get("xml_runner_up_score") or 0),
                        "xml_mention_share": float(anchor.get("xml_mention_share") or 0),
                        "identity_source": str(anchor.get("identity_source") or ""),
                        "kills": anchor.get("kills"), "deaths": anchor.get("deaths"),
                        "assists": anchor.get("assists"), "kda": anchor.get("kda"),
                        "game_segment_count": len(locked_game_segments),
                        "game_segments": locked_game_segments,
                        "outcome": "matched",
                    })
                elif locked_game_segments:
                    store.stage(key, "xml_identity", "completed", {
                        "danmaku_xml": str(danmaku_xml or ""),
                        "comment_count": len(comments),
                        **identity_diagnostics,
                        "game_segment_count": len(locked_game_segments),
                        "game_segments": locked_game_segments,
                        "identity_source": "gsi_game_segments",
                        "reason": "录播包含多场可靠 GSI 对局，将按事件时间匹配英雄与装备",
                        "outcome": "segmented",
                    })
                else:
                    store.stage(key, "xml_identity", "skipped", {**identity_diagnostics, "reason": "未形成唯一可靠的主播同场对局证据", "outcome": "no_data"})
            except Exception as exc:
                store.stage(key, "xml_identity", "warning", {"reason": "主播英雄识别失败，但不阻断投稿", "outcome": "failed_non_blocking"}, error=str(exc))

        locked_game_segments = [
            segment
            for segment in locked_game_segments
            if streamer_gameplay_is_verified(segment)
        ]
        locked_gameplay_verified = bool(
            streamer_gameplay_is_verified(locked_game_context)
            or locked_game_segments
        )
        verified_live_context: dict[str, Any] = {"live_stats": stats_text}
        if streamer_gameplay_is_verified(locked_game_context):
            verified_live_context["game"] = {
                key_name: locked_game_context.get(key_name)
                for key_name in ("hero", "items", "neutral", "scepter", "shard", "kills", "deaths", "assists", "kda", "identity_source")
                if locked_game_context.get(key_name) not in (None, "", [])
            }
        if locked_game_segments:
            verified_live_context["game_segments"] = locked_game_segments

        current_stage = "ai"
        ai_topic = ""
        ai_details: dict[str, Any] = {}
        prior_ai_details = (
            prior_ai_stage.get("details")
            if isinstance(prior_ai_stage.get("details"), dict)
            else {}
        )
        manual_review_metadata_ready = bool(
            retry
            and review_field_applies("title")
            and str(review_override.get("title") or "").strip()
            and review_field_applies("description")
            and review_field_applies("partition_id")
            and str(review_override.get("partition_id") or "").strip().isdigit()
            and not explicit_review_hold
        )
        reuse_ai = bool(
            retry
            and (
                manual_review_metadata_ready
                or (
                    prior_ai_stage.get("status") in {"completed", "skipped"}
                    and prior_ai_details.get("title")
                    and (
                        prior_ai_details.get("description_body")
                        or prior_ai_details.get("description")
                    )
                )
            )
        )
        partition = str(cfg.get("bilibili_partition_id", "")).strip()
        metadata_automation: dict[str, Any] = {}
        if reuse_ai:
            ai_details = dict(prior_ai_details)
            title = str(
                review_override.get("title")
                if manual_review_metadata_ready
                else ai_details.get("title")
                or title
            ).strip()
            # New tasks persist the editorial body separately. For old tasks,
            # migrate the previously composed description back to a clean body
            # before the one and only submission composition step below.
            description = strip_live_stats_from_description(
                str(
                    review_override.get("description")
                    if manual_review_metadata_ready
                    else ai_details.get("description_body")
                    or ai_details.get("description")
                    or description
                ),
                stats_text,
            )
            ai_details["description_body"] = description
            ai_topic = str(ai_details.get("title_topic") or "")
            previous_tags = (
                review_override.get("tags")
                if manual_review_metadata_ready
                else ai_details.get("final_tags")
            )
            if isinstance(previous_tags, list):
                tags = [
                    str(tag).strip()
                    for tag in previous_tags
                    if str(tag).strip()
                ]
            partition = str(
                review_override.get("partition_id")
                if manual_review_metadata_ready
                else ai_details.get("selected_partition_id")
                or prior_result.get("partition_id")
                or partition
            ).strip()
            previous_automation = prior_result.get("metadata_automation")
            if isinstance(previous_automation, dict):
                metadata_automation = dict(previous_automation)
            ai_details["reused_on_retry"] = True
            if manual_review_metadata_ready:
                ai_details.update({
                    "manual_review_override": True,
                    "manual_review_bypassed_failed_ai": (
                        bool(prior_ai_details.get("manual_review_bypassed_failed_ai"))
                        or prior_ai_stage.get("status") == "failed"
                    ),
                    "title": title,
                    "description": description,
                    "description_body": description,
                    "final_tags": tags,
                    "selected_partition_id": partition,
                })
            store.stage(
                key,
                "ai",
                (
                    "completed"
                    if manual_review_metadata_ready
                    else str(prior_ai_stage.get("status") or "completed")
                ),
                ai_details,
            )
        else:
            timeline_details: dict[str, Any] = {}
            if comments and not dry_run and bool(cfg.get("ai_danmaku_summary_enabled", True)):
                queued_ai_details = {"comment_count": len(comments)}
                store.stage(key, "ai", "queued", queued_ai_details)

                def mark_ai_metadata_running(queue_wait_seconds: float) -> None:
                    store.stage(key, "ai", "running", {
                        **queued_ai_details,
                        "ai_metadata_queue_wait_seconds": round(
                            queue_wait_seconds,
                            3,
                        ),
                    })

                description, ai_topic = generate_danmaku_metadata_with_ai(
                    comments,
                    description,
                    cfg,
                    verified_live_context,
                    recording_duration_seconds,
                    timeline_details,
                    mark_ai_metadata_running,
                )
                title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
                ai_details.update({
                    "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    "title": title,
                    "description": description,
                    "description_body": description,
                    "comment_count": len(comments),
                })
                ai_details.update(timeline_details)
            else:
                reason = "试运行" if dry_run else ("未配置可分析弹幕" if not comments else "AI 简介未启用")
                ai_details.update({
                    "reason": reason,
                    "title": title,
                    "description": description,
                    "description_body": description,
                })

            fallback_title_topic = str(
                recording_metadata_values(video, cfg)["ai_topic"] or ""
            ).strip()
            title_topic_is_fallback = bool(
                not str(ai_topic or "").strip()
                or (
                    fallback_title_topic
                    and _compact_alias(ai_topic) == _compact_alias(fallback_title_topic)
                )
            )

            # Partition selection is independent from title approval. Run it
            # before the manual-review gate so a failed/vague AI title does
            # not force the reviewer to choose an otherwise recommendable
            # partition by hand.
            if not dry_run and not existing_submission:
                store.stage(key, "ai", "running", ai_details)
                try:
                    tags, partition, metadata_automation = enhance_recording_metadata(
                        title,
                        description,
                        tags,
                        original_cover,
                        partition,
                        cfg,
                    )
                    ai_details.update(metadata_automation)
                except Exception as exc:
                    metadata_automation = {"metadata_automation_error": str(exc)}
                    ai_details.update(metadata_automation)
                    print(f"WARN 录播 AI 标签或分区推荐失败，使用原配置: {exc}", file=sys.stderr)

            if (
                not dry_run
                and not existing_submission
                and not isinstance(prior_result.get("bilibili"), dict)
                and not (
                    review_field_applies("title")
                    and str(review_override.get("title") or "").strip()
                )
                and not explicit_review_hold
                and (
                    title_topic_is_fallback
                    or recording_title_topic_is_vague(ai_topic)
                )
            ):
                metadata_error = (
                    safe_task_error_detail(timeline_details.get("ai_metadata_error"))
                    if timeline_details.get("ai_metadata_error")
                    else ""
                )
                review_error = (
                    f"AI 简介/标题生成失败：{metadata_error}；任务已终止，需要人工审核"
                    if metadata_error
                    else "AI 标题为空、空泛或回退到直播间默认标题，任务已终止，需要人工审核"
                )
                ai_details.update({
                    "manual_review_required": True,
                    "manual_review_reason": review_error,
                    "title_topic": str(ai_topic or ""),
                    "fallback_title_topic": fallback_title_topic,
                    "title_topic_is_fallback": title_topic_is_fallback,
                })
                store.stage(key, "ai", "failed", ai_details, error=review_error)
                raise RuntimeError(review_error)

        metadata_values_for_evidence = recording_metadata_values(video, cfg)
        if not locked_gameplay_verified and recording_cover_has_dota2_context(
            metadata_values_for_evidence["streamer"],
            title,
            description,
            *tags,
        ):
            original_ai_topic = ai_topic
            filtered_topic, filtered_description, filtered_tags, evidence_filter = (
                filter_unverified_dota2_metadata(
                    ai_topic,
                    description,
                    tags,
                    streamer=metadata_values_for_evidence["streamer"],
                    verified_timeline=description,
                    raw_comments=comments,
                )
            )
            ai_topic = filtered_topic
            description = filtered_description
            tags = filtered_tags
            if ai_topic != original_ai_topic:
                title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
            ai_details.update(evidence_filter)
            ai_details["title_topic"] = ai_topic or metadata_values_for_evidence["ai_topic"]
            ai_details["title"] = title
            ai_details["description"] = description
            ai_details["final_tags"] = tags
            metadata_automation["final_tags"] = tags

        if not str(ai_topic or "").strip() and description:
            recovered_title_topic = recording_title_topic_from_timeline(
                "",
                description,
                diagnostics=ai_details,
            )
            if recovered_title_topic and not recording_title_topic_is_vague(
                recovered_title_topic
            ):
                ai_topic = recovered_title_topic
                title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
                ai_details.update({
                    "title_topic": ai_topic,
                    "title": title,
                    "title_topic_recovered_from_description": True,
                    "title_topic_recovery_source": "verified_description_timeline",
                })

        post_filter_fallback_topic = str(
            recording_metadata_values(video, cfg)["ai_topic"] or ""
        ).strip()
        post_filter_title_is_fallback = bool(
            not str(ai_topic or "").strip()
            or (
                post_filter_fallback_topic
                and _compact_alias(ai_topic) == _compact_alias(post_filter_fallback_topic)
            )
        )
        if (
            not dry_run
            and not existing_submission
            and not isinstance(prior_result.get("bilibili"), dict)
            and not (
                review_field_applies("title")
                and str(review_override.get("title") or "").strip()
            )
            and not explicit_review_hold
            and (
                post_filter_title_is_fallback
                or recording_title_topic_is_vague(ai_topic)
            )
        ):
            review_error = "标题经证据过滤后为空、空泛或回退到直播间默认标题，任务已终止，需要人工审核"
            ai_details.update({
                "manual_review_required": True,
                "manual_review_reason": review_error,
                "title_topic": str(ai_topic or ""),
                "fallback_title_topic": post_filter_fallback_topic,
                "title_topic_is_fallback": post_filter_title_is_fallback,
                "title_topic_rejected_after_evidence_filter": True,
            })
            store.stage(key, "ai", "failed", ai_details, error=review_error)
            raise RuntimeError(review_error)

        part_values = recording_metadata_values(video, cfg, ai_topic=ai_topic)
        part_topic = str(ai_topic or part_values["ai_topic"]).strip()
        part_description = description
        part_generated_title = title
        if multipart:
            title = str(multipart.get("title") or title)
            tags = list(multipart.get("tags") or tags)
            source_url = str(multipart.get("source_url") or source_url)
            partition = str(multipart.get("partition_id") or partition)
            if isinstance(multipart.get("metadata_automation"), dict):
                metadata_automation = dict(multipart["metadata_automation"])
                ai_details.update(metadata_automation)

        applied_review_fields: list[str] = []
        if review_field_applies("title"):
            title = str(review_override.get("title") or title).strip()
            applied_review_fields.append("title")
        if review_field_applies("description"):
            description = strip_live_stats_from_description(
                str(review_override.get("description") or description),
                stats_text,
            )
            part_description = description
            applied_review_fields.append("description")
        override_tags = review_override.get("tags")
        if review_field_applies("tags") and isinstance(override_tags, list):
            tags = dedupe_recording_tags(override_tags, limit=6)
            applied_review_fields.append("tags")
        if review_field_applies("partition_id"):
            partition = str(review_override.get("partition_id") or partition).strip()
            applied_review_fields.append("partition_id")

        if danmaku_burned_for_upload:
            title = recording_danmaku_edition_title(title)

        tags = dedupe_recording_tags(tags, limit=12)

        page_title = recording_part_title(video, part_number, part_topic)
        multipart_parts: list[dict[str, Any]] = []
        recording_intro = part_values["recording_intro"]
        if multipart:
            multipart_parts = [
                dict(item)
                for item in (multipart.get("parts") or [])
                if isinstance(item, dict)
            ]
            # Upgrade an active session created before per-part metadata existed.
            if existing_submission and not multipart_parts and multipart.get("description"):
                legacy_title = str(multipart.get("title") or "")
                legacy_fields = [field.strip() for field in legacy_title.split("｜")]
                legacy_topic = legacy_fields[1] if len(legacy_fields) > 1 else "直播精彩内容"
                multipart_parts.append({
                    "part_number": 1,
                    "title_topic": legacy_topic,
                    "page_title": f"P1｜{legacy_topic}",
                    "description": str(multipart.get("description") or ""),
                    "recorded_at": "",
                })
            multipart_parts = [
                item for item in multipart_parts
                if int(item.get("part_number") or 0) != part_number
            ]
            multipart_parts.append({
                "part_number": part_number,
                "title_topic": part_topic,
                "page_title": page_title,
                "title": part_generated_title,
                "description": part_description,
                "recorded_at": part_values["date"],
            })
            recording_intro = str(
                multipart.get("recording_intro") or recording_intro
            ).strip()
            description = render_multipart_description(
                multipart_parts,
                recording_intro,
            )

        description_body = strip_live_stats_from_description(description, stats_text)
        description = description_body
        ai_details.update({
            "title_topic": part_topic,
            "part_title": part_generated_title,
            "part_description": part_description,
            "page_title": page_title,
            "title": title,
            "description": description,
            "description_body": description_body,
            "final_tags": tags,
            "selected_partition_id": partition or None,
        })

        if applied_review_fields:
            ai_details.update({
                "manual_review_applied": True,
                "manual_review_applied_fields": applied_review_fields,
                "manual_review_updated_at": review_override.get("updated_at"),
            })
        ai_was_used = bool(
            comments and bool(cfg.get("ai_danmaku_summary_enabled", True))
        ) or bool(
            metadata_automation.get("tag_generation_enabled")
            or metadata_automation.get("partition_recommendation_enabled")
            or metadata_automation.get("metadata_automation_error")
        )
        ai_stage_status = "completed" if ai_was_used else "skipped"
        store.stage(key, "ai", ai_stage_status, ai_details)

        app_root = resolve_app_root(cfg)
        if str(app_root) not in sys.path:
            sys.path.insert(0, str(app_root))
        stats_enabled = bool(cfg.get("douyu_stats_enabled", True))
        cover_context_enabled = bool(cfg.get("douyu_stats_cover_context_enabled", True))

        current_stage = "xml_identity"
        if identity_prepared:
            pass
        elif not stats_enabled:
            store.stage(key, "xml_identity", "skipped", {
                "reason": "斗鱼直播数据统计已关闭",
                "outcome": "disabled",
            })
        elif not cover_context_enabled:
            store.stage(key, "xml_identity", "skipped", {
                "reason": "XML 主播英雄与装备识别已关闭",
                "outcome": "disabled",
            })
        else:
            store.stage(key, "xml_identity", "running", {
                "danmaku_xml": str(danmaku_xml or ""),
                "comment_count": len(comments),
            })
            try:
                from modules.douyu_stats_formatter import (  # type: ignore
                    get_game_for_cover,
                    get_identity_diagnostics,
                )

                anchor = get_game_for_cover(str(video.parent))
                identity_diagnostics = get_identity_diagnostics(str(video.parent))
                if anchor:
                    store.stage(key, "xml_identity", "completed", {
                        "danmaku_xml": str(danmaku_xml or ""),
                        "comment_count": len(comments),
                        **identity_diagnostics,
                        "streamer_hero": str(anchor.get("hero") or ""),
                        "streamer_items": [
                            str(item) for item in anchor.get("items", [])[:6] if str(item)
                        ],
                        "streamer_neutral": str(anchor.get("neutral") or ""),
                        "streamer_scepter": bool(anchor.get("scepter")),
                        "streamer_shard": bool(anchor.get("shard")),
                        "equipment_snapshot_unix_ts": float(
                            anchor.get("equipment_snapshot_unix_ts") or 0
                        ),
                        "xml_mention_score": int(anchor.get("xml_mention_score") or 0),
                        "xml_mention_burst_score": int(
                            anchor.get("xml_mention_burst_score") or 0
                        ),
                        "xml_runner_up_score": int(anchor.get("xml_runner_up_score") or 0),
                        "xml_mention_share": float(anchor.get("xml_mention_share") or 0),
                        "gsi_observed_seconds": float(
                            anchor.get("gsi_observed_seconds") or 0
                        ),
                        "identity_source": str(anchor.get("identity_source") or ""),
                        "kills": anchor.get("kills"),
                        "deaths": anchor.get("deaths"),
                        "assists": anchor.get("assists"),
                        "kda": anchor.get("kda"),
                        "kda_available": all(
                            field in anchor for field in ("kills", "deaths", "assists")
                        ),
                        "outcome": "matched",
                    })
                else:
                    if not identity_diagnostics["stats_available"]:
                        reason = "未找到斗鱼 Dota2 统计快照"
                    elif (
                        identity_diagnostics["type_tooltips_messages"]
                        + identity_diagnostics["type_tooltips_http_snapshots"]
                    ) == 0:
                        reason = "本次录制未获取 Dota2 阵容数据"
                    elif identity_diagnostics["type_tooltips_valid_snapshots"] == 0:
                        reason = "Dota2 数据未形成完整的 10 人阵容"
                    elif identity_diagnostics["type_tooltips_game_snapshots"] == 0:
                        reason = "Dota2 数据尚未形成稳定对局快照"
                    else:
                        reason = "斗鱼未提供主播视角英雄，XML 也未形成唯一可靠证据"
                    store.stage(key, "xml_identity", "skipped", {
                        "danmaku_xml": str(danmaku_xml or ""),
                        "comment_count": len(comments),
                        **identity_diagnostics,
                        "reason": reason,
                        "outcome": "no_data",
                    })
            except Exception as exc:
                store.stage(key, "xml_identity", "warning", {
                    "reason": "主播英雄识别失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                }, error=str(exc))

        current_stage = "live_stats"
        append_stats_enabled = bool(cfg.get("douyu_stats_append_description", True))
        if live_stats_prepared and stats_text:
            description_body = strip_live_stats_from_description(
                description_body,
                stats_text,
            )
            if append_stats_enabled:
                description = append_live_stats_to_description(description, stats_text)
                ai_details["stats_appended"] = True
                ai_details["stats_prepended"] = False
                ai_details["stats_position"] = "end"
                print("[bridge] 预先整理的直播统计数据已置于简介末尾", file=sys.stderr)
            else:
                description = description_body
                ai_details["stats_appended"] = False
                ai_details["stats_prepended"] = False
                ai_details["stats_position"] = None
            store.stage(key, "live_stats", "completed", {
                "stats_appended": append_stats_enabled,
                "stats_prepended": False,
                "stats_position": "end" if append_stats_enabled else None,
                "description_length": len(description),
                **live_stats_stage_details(stats_text),
            })
        else:
            description = description_body
            ai_details["stats_appended"] = False
            ai_details["stats_prepended"] = False
            ai_details["stats_position"] = None

        # Persist both representations. Retries always reuse description_body;
        # description is the exact value sent to Bilibili and shown in details.
        ai_details["description_body"] = description_body
        ai_details["description"] = description
        store.stage(key, "ai", ai_stage_status, ai_details)

        def pause_for_review_if_requested() -> bool:
            """Honor a durable review request at every external-side-effect boundary."""
            latest_review_override = store.review_override(key)
            latest_explicit_hold = bool(
                latest_review_override.get("hold_before_cover")
                and latest_review_override.get("pre_upload_review_requested_at")
            )
            if dry_run or not latest_explicit_hold:
                return False
            paused_result = store.results(key)
            paused_result.update({
                "video_path": str(video),
                "final_video_path": str(upload_video),
                "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                "ass_path": str(ass_path) if ass_path else None,
                "title": title,
                "description": description,
                "tags": tags,
                "partition_id": partition,
                "metadata_automation": metadata_automation,
                "bilibili_account_id": str(cfg.get("bilibili_account_id") or ""),
                "bilibili_account_name": str(cfg.get("bilibili_account_name") or ""),
                "pre_upload_review": True,
            })
            store.finish(key, "paused", paused_result)
            print("PAUSED AI 投稿信息已生成，等待人工确认后再生成封面")
            return True

        # Re-read the durable stop flag after AI and again before each model or
        # upload boundary. A request arriving during a cover call may terminate
        # that worker, while a request arriving before upload is observed here.
        if pause_for_review_if_requested():
            return True

        cover_game_context = locked_game_context if locked_gameplay_verified else None
        if cover_game_context is None:
            cover_game_context = recording_cover_danmaku_game_context(
                ai_details,
                title,
                description_body,
            )
            if cover_game_context:
                ai_details["cover_danmaku_hero_context"] = dict(cover_game_context)
        if cover_game_context and not recording_cover_hero_matches_title(
            str(cover_game_context.get("hero") or ""),
            f"{title}\n{description_body}",
        ):
            cover_game_context = None

        current_stage = "cover_16x9"
        cover_generation: dict[str, Any] = {}
        cover_reference_cache: dict[str, Any] = {}
        cover16_status = "skipped"
        cover43_status = "skipped"
        session_cover = str(multipart.get("cover_path") or "").strip() if multipart else ""
        session_cover43 = str(multipart.get("cover43_path") or "").strip() if multipart else ""
        prior_cover16_details = (
            prior_cover16_stage.get("details")
            if isinstance(prior_cover16_stage.get("details"), dict)
            else {}
        )
        prior_cover43_details = (
            prior_cover43_stage.get("details")
            if isinstance(prior_cover43_stage.get("details"), dict)
            else {}
        )
        retry_cover_path = ""
        retry_cover43_path = ""
        force_cover_regeneration = bool(
            review_override.get("regenerate_covers_on_resume")
        )
        if retry and not force_cover_regeneration:
            for value in (
                review_override.get("cover_path"),
                prior_result.get("cover_path"),
                prior_cover16_details.get("ai_cover_path"),
                prior_cover16_details.get("cover_used_for_upload"),
            ):
                candidate = str(value or "").strip()
                if candidate and Path(candidate).is_file():
                    retry_cover_path = candidate
                    break
            for value in (
                review_override.get("cover43_path"),
                prior_result.get("cover43_path"),
                prior_cover43_details.get("ai_cover_4x3_path"),
                prior_cover43_details.get("cover43_used_for_upload"),
            ):
                candidate = str(value or "").strip()
                if candidate and Path(candidate).is_file():
                    retry_cover43_path = candidate
                    break
        if manual_cover_path and Path(manual_cover_path).is_file():
            cover = Path(manual_cover_path)
            cover_generation = {
                "manual_review_cover": True,
                "ai_cover_path": str(cover),
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            }
            cover16_status = "completed"
        elif session_cover and Path(session_cover).is_file():
            cover = Path(session_cover)
            cover_generation = dict(multipart.get("cover_generation") or {})
            cover_generation.update({
                "ai_cover_reused": True,
                "ai_cover_path": str(cover),
                "original_cover_path": str(original_cover),
            })
            cover16_status = "completed"
        elif retry_cover_path:
            cover = Path(retry_cover_path)
            cover_generation = dict(prior_cover16_details)
            cover_generation.update({
                "ai_cover_reused": True,
                "reused_on_retry": True,
                "ai_cover_path": str(cover),
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            })
            cover16_status = "completed"
        elif not dry_run and not existing_submission:
            if pause_for_review_if_requested():
                return True
            store.stage(key, "cover_16x9", "running", {
                "title": title,
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                "original_cover_path": str(original_cover),
            })
            try:
                generated_cover, cover_generation = generate_recording_cover_with_ai(
                    title=title,
                    ai_topic=ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    description=description_body,
                    streamer=recording_metadata_values(video, cfg)["streamer"],
                    cfg=cfg,
                    work_dir=work_dir,
                    target_size=(1920, 1080),
                    output_path=work_dir / "ai_cover_16x9.jpg",
                    recording_dir=video.parent,
                    game_context=cover_game_context,
                    game_context_locked=True,
                    cover_text=str(ai_details.get("cover_text") or ""),
                    shared_reference_cache=cover_reference_cache,
                )
                if generated_cover:
                    cover = generated_cover
                cover_generation.update({
                    "cover_used_for_upload": str(cover),
                    "original_cover_path": str(original_cover),
                })
                cover_status = (
                    "completed"
                    if cover_generation.get("ai_cover_generated")
                    else "skipped"
                )
                cover16_status = cover_status
            except Exception as exc:
                cover_generation = {
                    "ai_cover_enabled": True,
                    "ai_cover_generated": False,
                    "ai_cover_error": str(exc),
                    "cover_fallback": "视频截图",
                    "cover_used_for_upload": str(original_cover),
                    "original_cover_path": str(original_cover),
                }
                cover = original_cover
                cover16_status = "warning"
                print(f"WARN AI 录播封面生成失败，回退视频截图: {exc}", file=sys.stderr)
        else:
            reason = "试运行" if dry_run else "后续分P沿用当前稿件封面"
            cover_generation = {
                "reason": reason,
                "cover_used_for_upload": str(cover),
                "original_cover_path": str(original_cover),
            }
            cover16_status = "skipped"

        if not dry_run:
            cover = persist_pipeline_cover(store, key, cover, "16x9", video=video)
            cover_generation["cover_used_for_upload"] = str(cover)
            cover_generation["ai_cover_16x9_path"] = str(cover)
            if cover_generation.get("ai_cover_generated") or cover_generation.get("ai_cover_path"):
                cover_generation["ai_cover_path"] = str(cover)
        store.stage(key, "cover_16x9", cover16_status, cover_generation)

        # The homepage 4:3 cover is a second, independent model request. It is
        # optional for upload and is never synthesized from the 16:9 image.
        current_stage = "cover_4x3"
        cover43_generation: dict[str, Any] = {}
        if pause_for_review_if_requested():
            return True
        if manual_cover43_path and Path(manual_cover43_path).is_file():
            cover43 = Path(manual_cover43_path)
            cover43_status = "completed"
            cover43_generation = {
                "manual_review_cover43": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            }
        elif session_cover43 and Path(session_cover43).is_file():
            cover43 = Path(session_cover43)
            cover43_status = "completed"
            cover43_generation = {
                "ai_cover_4x3_reused": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            }
        elif retry_cover43_path:
            cover43 = Path(retry_cover43_path)
            cover43_status = "completed"
            cover43_generation = dict(prior_cover43_details)
            cover43_generation.update({
                "ai_cover_4x3_reused": True,
                "reused_on_retry": True,
                "ai_cover_4x3_path": str(cover43),
                "cover43_used_for_upload": str(cover43),
            })
        elif not dry_run and not existing_submission:
            store.stage(key, "cover_4x3", "running", {
                "title": title,
                "title_topic": ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
            })
            try:
                generated_cover43, cover43_details = generate_recording_cover_with_ai(
                    title=title,
                    ai_topic=ai_topic or recording_metadata_values(video, cfg)["ai_topic"],
                    description=description_body,
                    streamer=recording_metadata_values(video, cfg)["streamer"],
                    cfg=cfg,
                    work_dir=work_dir,
                    target_size=(1600, 1200),
                    output_path=work_dir / "ai_cover_4x3.jpg",
                    recording_dir=video.parent,
                    game_context=cover_game_context,
                    game_context_locked=True,
                    cover_text=str(ai_details.get("cover_text") or ""),
                    shared_reference_cache=cover_reference_cache,
                )
                if generated_cover43:
                    cover43 = generated_cover43
                cover43_generation.update({
                    f"ai_cover_4x3_{key_name.removeprefix('ai_cover_')}": value
                    for key_name, value in cover43_details.items()
                })
                cover43_status = (
                    "completed"
                    if cover43_generation.get("ai_cover_4x3_generated")
                    else "skipped"
                )
            except Exception as exc:
                cover43_generation.update({
                    "ai_cover_4x3_generated": False,
                    "ai_cover_4x3_error": str(exc),
                    "reason": "4:3 首页推荐封面生成失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                })
                cover43_status = "warning"
                print(f"WARN AI 4:3 首页推荐封面生成失败（不影响 16:9 投稿）: {exc}", file=sys.stderr)
        else:
            cover43_status = "skipped"
            cover43_generation = {
                "reason": "试运行" if dry_run else "后续分P沿用当前稿件封面",
                "outcome": "skipped",
            }

        if not dry_run and cover43 is not None and cover43.is_file():
            try:
                cover43 = persist_pipeline_cover(store, key, cover43, "4x3", video=video)
                cover43_generation["ai_cover_4x3_path"] = str(cover43)
                cover43_generation["cover43_used_for_upload"] = str(cover43)
            except Exception as exc:
                cover43 = None
                cover43_status = "warning"
                cover43_generation.update({
                    "ai_cover_4x3_error": str(exc),
                    "reason": "4:3 首页推荐封面保存失败，但不阻断投稿",
                    "outcome": "failed_non_blocking",
                })
        store.stage(key, "cover_4x3", cover43_status, cover43_generation)
        cover_generation.update(cover43_generation)
        if force_cover_regeneration:
            refreshed_review = store.review_override(key)
            refreshed_review["regenerate_covers_on_resume"] = False
            refreshed_review["covers_regenerated_at"] = utc_now()
            refreshed_review["updated_at"] = refreshed_review["covers_regenerated_at"]
            store.save_review_override(key, refreshed_review)

        summary = {"video": str(video), "upload_video": str(upload_video),
                   "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                   "ass_path": str(ass_path) if ass_path else None,
                   "danmaku_count": len(comments), "cover": str(cover),
                   "cover_path": str(cover),
                   "cover43": str(cover43) if cover43 else None,
                   "cover43_path": str(cover43) if cover43 else None,
                   "original_cover": str(original_cover), "platform": platform,
                   "title": title, "description": description, "tags": tags, "source_url": source_url,
                   "partition_id": partition, "metadata_automation": metadata_automation,
                   "bilibili_account_id": str(cfg.get("bilibili_account_id") or ""),
                   "bilibili_account_name": str(cfg.get("bilibili_account_name") or ""),
                   "cover_generation": cover_generation,
                   "multipart_session": session_key or None, "part_number": part_number,
                   "page_title": page_title, "part_title": part_generated_title,
                   "part_description": part_description}
        if pause_for_review_if_requested():
            return True
        if dry_run:
            store.stage(key, "upload", "skipped", {"reason": "试运行未投稿"})
            store.stage(key, "collection", "skipped", {"reason": "试运行未处理合集"})
            store.stage(key, "cleanup", "skipped", {"reason": "试运行不清理源文件"})
            store.finish(key, "dry_run", summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return True

        current_stage = "upload"
        upload_stage_details = {
            "title": title,
            "cover": str(cover),
            "tags": tags,
            "partition_id": partition,
            "part_number": part_number,
            "page_title": page_title,
            "bilibili_account_id": str(cfg.get("bilibili_account_id") or ""),
            "bilibili_account_name": str(cfg.get("bilibili_account_name") or ""),
            "existing_bvid": (
                existing_submission.get("bvid")
                if isinstance(existing_submission, dict)
                else None
            ),
        }
        upload_stage_details["worker_pid"] = os.getpid()
        store.stage(key, "upload", "queued", upload_stage_details)
        BilibiliUploader, _ = import_app(cfg)
        cookie = resolve_path(str(cfg.get("bilibili_cookies", "")), cfg)
        if not cookie.is_file():
            raise ValueError(f"bilibili Cookie 文件不存在：{cookie}")
        if not partition:
            raise ValueError("bilibili 未配置有效的投稿分区 ID")
        previous = store.results(key)
        previous.update({
            "tags": tags,
            "partition_id": partition,
            "metadata_automation": metadata_automation,
            "cover_generation": cover_generation,
            "cover_path": str(cover),
            "cover43_path": str(cover43) if cover43 else None,
        })
        result = previous.get("bilibili")
        uploader = None
        uploaded_now = False
        peak_upload_speed = 0.0
        final_upload_progress: dict[str, Any] | None = None
        if not isinstance(result, dict) or not result.get("bvid"):
            uploader = BilibiliUploader(cookie_file=str(cookie))

            def _on_upload_progress(progress: dict) -> None:
                nonlocal peak_upload_speed, final_upload_progress
                current_speed = float(
                    progress.get("speed_bytes_per_second")
                    or progress.get("speed_bytes_per_sec")
                    or 0
                )
                peak_upload_speed = max(peak_upload_speed, current_speed)
                final_upload_progress = {
                    **progress,
                    "peak_speed_bytes_per_second": peak_upload_speed,
                }
                store.stage(
                    key,
                    "upload",
                    "running",
                    {**upload_stage_details, "upload_progress": final_upload_progress},
                )

            def _on_upload_queue_status(status: str) -> None:
                store.stage(
                    key,
                    "upload",
                    "running" if status == "uploading" else "queued",
                    upload_stage_details,
                )

            ok, result = uploader.upload_video(
                video_file_path=str(upload_video), cover_file_path=str(cover), title=title,
                cover43_file_path=str(cover43) if cover43 else "",
                description=description, tags=tags, partition_id=partition,
                youtube_url=source_url, task_id=key[:12],
                page_titles=[page_title],
                existing_submission=existing_submission,
                is_original=True,
                # Collection membership is a separate durable stage below so
                # it can retry without uploading the video again.
                collection_id="",
                progress_detail_callback=_on_upload_progress,
                queue_status_callback=_on_upload_queue_status,
            )
            if not ok:
                raise RuntimeError(f"bilibili 上传失败: {result}")
            previous.update({"bilibili": result, "ass_path": str(ass_path) if ass_path else None})
            uploaded_now = True
            # Persist the BVID immediately so a process restart cannot create a
            # duplicate video submission.
            store.finish(key, "video_uploaded", previous)

        if session_key:
            session_state = {
                "bilibili": previous.get("bilibili"),
                "title": title,
                "description": description,
                "tags": tags,
                "source_url": source_url,
                "partition_id": partition,
                "metadata_automation": metadata_automation,
                "cover_generation": cover_generation,
                "cover_path": str(cover),
                "cover43_path": str(cover43) if cover43 else None,
                "last_video": str(video),
                "recording_intro": recording_intro,
                "parts": multipart_parts,
            }
            store.save_multipart_session(
                session_key,
                session_state,
                status=session_status if retry else "open",
            )

        completed_upload_progress = (
            {
                **final_upload_progress,
                "speed_bytes_per_second": 0,
                "eta_seconds": 0,
            }
            if final_upload_progress
            else None
        )
        store.stage(key, "upload", "completed", {
            "title": title, "description": description, "cover": str(cover),
            "cover43": str(cover43) if cover43 else None,
            "tags": tags, "partition_id": partition,
            "bilibili": previous.get("bilibili"),
            "description_comment": previous.get("description_comment"),
            "part_number": part_number,
            "page_title": page_title,
            "part_title": part_generated_title,
            "part_description": part_description,
            "upload_progress": completed_upload_progress,
            "peak_speed_bytes_per_second": peak_upload_speed or None,
        })
        # The upload result is durable, but the task has not reached its
        # terminal state until the configured source cleanup has finished.
        # Keeping the top-level status at video_uploaded also prevents file
        # management endpoints from treating the source as deletable.
        store.finish(key, "video_uploaded", previous)

        collection_id = str(cfg.get("bilibili_collection_id") or "").strip()
        bilibili_result = previous.get("bilibili")
        bilibili_result = (
            dict(bilibili_result) if isinstance(bilibili_result, dict) else {}
        )
        existing_collection = bilibili_result.get("collection")
        existing_collection = (
            existing_collection if isinstance(existing_collection, dict) else {}
        )
        if not collection_id:
            store.stage(
                key,
                "collection",
                "skipped",
                {"reason": "当前直播间未配置 B站合集"},
            )
        elif part_number > 1 and not resuming_uploaded_part:
            store.stage(
                key,
                "collection",
                "skipped",
                {
                    "reason": "后续分P沿用首P已加入的合集",
                    "season_id": int(collection_id),
                },
            )
        elif (
            existing_collection.get("added")
            and str(existing_collection.get("season_id") or "") == collection_id
        ):
            store.stage(
                key,
                "collection",
                "completed",
                {**existing_collection, "reused_on_retry": True},
            )
        else:
            current_stage = "collection"
            collection_details = {
                "enabled": True,
                "added": False,
                "season_id": int(collection_id),
                "worker_pid": os.getpid(),
            }
            store.stage(key, "collection", "running", collection_details)
            if uploader is None:
                uploader = BilibiliUploader(cookie_file=str(cookie))
            collection_result = uploader.add_to_collection(
                bilibili_result,
                collection_id,
                title=title,
            )
            collection_result = (
                collection_result if isinstance(collection_result, dict) else {
                    **collection_details,
                    "error": "上传器未返回有效的合集处理结果",
                }
            )
            bilibili_result["collection"] = collection_result
            previous["bilibili"] = bilibili_result
            store.finish(key, "video_uploaded", previous)
            if not collection_result.get("added"):
                collection_error = str(
                    collection_result.get("error") or "加入合集失败"
                )
                store.stage(
                    key,
                    "collection",
                    "failed",
                    collection_result,
                    error=collection_error,
                )
                raise RuntimeError(f"B站合集处理失败：{collection_error}")
            store.stage(key, "collection", "completed", collection_result)
            if session_key:
                session_state["bilibili"] = bilibili_result
                store.save_multipart_session(
                    session_key,
                    session_state,
                    status=session_status if retry else "open",
                )

        current_stage = "comment"
        description_comment = previous.get("description_comment")
        comment_enabled = bool(cfg.get("post_description_comment", True))
        pin_comment_enabled = bool(cfg.get("pin_description_comment", True))
        comment_skipped_for_multipart = bool(
            isinstance(existing_submission, dict)
            and existing_submission.get("bvid")
        )
        comment_retry_pending = bool(
            isinstance(description_comment, dict)
            and description_comment.get("enabled", True)
            and (
                not description_comment.get("posted")
                or (
                    pin_comment_enabled
                    and not description_comment.get("pinned")
                )
            )
        )
        if not comment_enabled:
            store.stage(key, "comment", "skipped", {"reason": "配置为不发布简介评论"})
        elif comment_skipped_for_multipart:
            store.stage(key, "comment", "skipped", {"reason": "后续分P沿用当前稿件评论"})
        elif not uploaded_now and not comment_retry_pending:
            store.stage(key, "comment", "skipped", {"reason": "没有待补偿的简介评论"})
        else:
            if not (
                isinstance(description_comment, dict)
                and description_comment.get("posted")
            ):
                store.stage(key, "comment", "running", {
                    "pin_requested": pin_comment_enabled,
                })
                if uploader is None:
                    uploader = BilibiliUploader(cookie_file=str(cookie))
                if not hasattr(uploader, "publish_description_comment"):
                    store.stage(
                        key,
                        "comment",
                        "skipped",
                        {"reason": "当前上传器不支持简介评论"},
                    )
                    description_comment = None
                else:
                    description_comment = uploader.publish_description_comment(
                        result=previous.get("bilibili") or {},
                        description=description,
                        pin=pin_comment_enabled,
                    )
                    if isinstance(description_comment, dict):
                        description_comment.setdefault("enabled", True)
                previous["description_comment"] = description_comment
                store.finish(key, "video_uploaded", previous)
            elif pin_comment_enabled and not description_comment.get("pinned"):
                store.stage(key, "comment", "running", {
                    **description_comment,
                    "pin_requested": True,
                    "retry_pin_only": True,
                })
                if uploader is None:
                    uploader = BilibiliUploader(cookie_file=str(cookie))
                if not hasattr(uploader, "retry_description_comment_pin"):
                    description_comment = {
                        **description_comment,
                        "pin_error": "当前上传器不支持仅重试评论置顶",
                    }
                else:
                    description_comment = uploader.retry_description_comment_pin(
                        result=previous.get("bilibili") or {},
                        comment=description_comment,
                    )
                previous["description_comment"] = description_comment
                store.finish(key, "video_uploaded", previous)
            if description_comment is None:
                pass
            elif not (
                isinstance(description_comment, dict)
                and description_comment.get("posted")
            ):
                comment_error = str(
                    (description_comment or {}).get("error")
                    if isinstance(description_comment, dict)
                    else ""
                ) or "简介评论发布失败"
                store.stage(
                    key,
                    "comment",
                    "failed",
                    description_comment if isinstance(description_comment, dict) else {},
                    error=comment_error,
                )
                raise RuntimeError(f"B站简介评论处理失败：{comment_error}")
            elif pin_comment_enabled and not description_comment.get("pinned"):
                pin_error = str(
                    description_comment.get("pin_error")
                    or "简介评论已发布，但置顶失败"
                )
                store.stage(
                    key,
                    "comment",
                    "failed",
                    description_comment,
                    error=pin_error,
                )
                raise RuntimeError(f"B站简介评论处理失败：{pin_error}")
            else:
                store.stage(key, "comment", "completed", description_comment)

        if bool(cfg.get("delete_recording_after_upload", True)):
            current_stage = "cleanup"
            store.stage(key, "cleanup", "running", {
                "video_path": str(video),
                "danmaku_xml": str(danmaku_xml) if danmaku_xml else None,
                "upload_video_path": str(upload_video),
            })
            xml_retention_hours = max(
                0.0,
                float(
                    24
                    if cfg.get("danmaku_xml_retention_hours") is None
                    else cfg["danmaku_xml_retention_hours"]
                ),
            )
            previous["source_cleanup"] = cleanup_uploaded_recording(
                video,
                danmaku_xml,
                upload_video,
                artifact_dir=work_dir,
                retained_paths=(
                    cover,
                    cover43,
                    danmaku_xml if xml_retention_hours > 0 else None,
                ),
                xml_retention_hours=xml_retention_hours,
            )
            cleanup_failures = previous["source_cleanup"].get("failed") or []
            if cleanup_failures:
                cleanup_error = f"有 {len(cleanup_failures)} 个录播源文件或临时产物清理失败"
                store.stage(
                    key,
                    "cleanup",
                    "failed",
                    previous["source_cleanup"],
                    error=cleanup_error,
                )
                store.finish(key, "failed", previous, error=cleanup_error)
                raise RuntimeError(cleanup_error)
            store.stage(
                key,
                "cleanup",
                "completed",
                previous["source_cleanup"],
            )
        else:
            store.stage(
                key,
                "cleanup",
                "skipped",
                {"reason": "配置为上传后保留录播源文件"},
            )
        store.finish(key, "completed", previous)
        emit_recording_task_result_notification(
            cfg,
            fingerprint_value=key,
            video=video,
            task_kind="recording_upload",
            status="completed",
            result=previous,
            title=title,
        )
        print(f"OK 上传完成: {video}")
        return True
    except Exception as exc:
        store.stage(key, current_stage, "failed", error=str(exc))
        store.finish(key, "failed", error=str(exc))
        if not dry_run:
            emit_recording_task_result_notification(
                cfg,
                fingerprint_value=key,
                video=video,
                task_kind="recording_upload",
                status="failed",
                error=str(exc),
                stage=current_stage,
            )
        print(f"ERROR {video}: {exc}", file=sys.stderr)
        return False


def generate_record_only_ass(
    video: Path,
    base_cfg: dict[str, Any],
    received_paths: list[Path] | None = None,
) -> Path | None:
    """Generate a retained ASS file without triggering player auto-loading."""
    cfg = effective_config(base_cfg, video)
    danmaku_xml = wait_for_danmaku_xml(
        video,
        received_paths,
        timeout=float(cfg.get("record_only_xml_wait_seconds", 8)),
    )
    if danmaku_xml is None:
        print(f"WARN 仅录制文件未找到同名 XML，无法生成 ASS: {video}", file=sys.stderr)
        return None
    comments = parse_danmaku_xml(danmaku_xml)
    if not comments:
        print(
            f"ERROR 弹幕 XML 中没有可用弹幕，未生成空 ASS: {danmaku_xml}",
            file=sys.stderr,
        )
        return None
    width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
    # Keep the editable subtitle inside the same recording session, but not
    # beside the burned MP4. Many players automatically load a same-stem ASS;
    # doing that for an already burned video renders every comment twice.
    ass_dir = video.parent / "ass"
    ass_dir.mkdir(parents=True, exist_ok=True)
    ass_path = ass_dir / f"{video.stem}.zh-CN.ass"
    generated = build_ass(
        comments,
        ass_path,
        width=width,
        height=height,
        font_name=str(cfg.get("danmaku_font_name", "Noto Sans CJK SC")),
        font_size=int(cfg.get("danmaku_font_size", 42)),
        duration=float(cfg.get("danmaku_duration_seconds", 10)),
        opacity=float(cfg.get("danmaku_opacity", 0.92)),
    )
    for legacy_path in (
        video.with_suffix(".ass"),
        video.with_name(f"{video.stem}.zh-CN.ass"),
    ):
        if legacy_path != generated:
            legacy_path.unlink(missing_ok=True)
    return generated


def generate_record_only_cover(video: Path, base_cfg: dict[str, Any]) -> Path:
    """Generate an AI cover beside the video using its native resolution."""
    cfg = effective_config(base_cfg, video)
    title, description, _ = render_metadata(video, cfg)
    ai_topic = recording_metadata_values(video, cfg)["ai_topic"]
    danmaku_xml = find_danmaku_xml(video)
    if danmaku_xml and bool(cfg.get("ai_danmaku_summary_enabled", True)):
        comments = parse_danmaku_xml(danmaku_xml)
        if comments:
            description, ai_topic = generate_danmaku_metadata_with_ai(
                comments,
                description,
                cfg,
                timeline_duration_seconds=video_duration_seconds(
                    video,
                    str(cfg.get("ffprobe", "ffprobe")),
                ),
            )
            title, _, _ = render_metadata(video, cfg, ai_topic=ai_topic)
    width, height = probe_video_size(video, str(cfg.get("ffprobe", "ffprobe")))
    cover = video.with_suffix(".jpg")
    generated, details = generate_recording_cover_with_ai(
        title=title,
        ai_topic=ai_topic,
        description=description,
        streamer=recording_metadata_values(video, cfg)["streamer"],
        cfg=cfg,
        work_dir=video.parent / ".potato-cover-artifacts",
        target_size=(width, height),
        output_path=cover,
        recording_dir=video.parent,
    )
    if not generated or not details.get("ai_cover_generated"):
        raise RuntimeError("录播 AI 封面未启用或图片模型没有生成封面")
    return generated


def remux_record_only_flv_with_cover(
    video: Path,
    cover: Path,
    base_cfg: dict[str, Any],
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    *,
    output_path: Path | None = None,
    original_flv: Path | None = None,
) -> Path:
    """Remux a recording to MP4 and attach its cover without another video encode."""
    if not cover.is_file():
        raise RuntimeError(f"内嵌封面不存在: {cover}")

    cfg = effective_config(base_cfg, video)
    output = output_path or video.with_suffix(".mp4")
    original = original_flv or video
    temporary = output.with_name(f".{output.stem}.potato-remux.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        str(cfg.get("ffmpeg", "ffmpeg")),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(cover),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "1:v:0",
        "-c",
        "copy",
        "-disposition:v:1",
        "attached_pic",
        "-metadata:s:v:1",
        "title=PotatoFlow cover",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=float(cfg.get("record_only_remux_timeout_seconds", 3600)),
            **_hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = (completed.stderr or completed.stdout or "FFmpeg 未生成 MP4").strip()
            raise RuntimeError(f"录播封装 MP4 失败: {detail}")
        if progress_callback:
            progress_callback(
                "remux_completed",
                {
                    "output_path": str(output),
                    "temporary_path": str(temporary),
                    "copy_mode": "-c copy",
                    "size_bytes": temporary.stat().st_size,
                },
            )
            progress_callback("verify_running", {"output_path": str(output)})

        probe = subprocess.run(
            [
                str(cfg.get("ffprobe", "ffprobe")),
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream_disposition=attached_pic",
                "-of",
                "json",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            **_hidden_subprocess_kwargs(),
        )
        try:
            streams = json.loads(probe.stdout or "{}").get("streams", [])
        except json.JSONDecodeError:
            streams = []
        if probe.returncode != 0 or not any(
            int(stream.get("disposition", {}).get("attached_pic", 0)) == 1
            for stream in streams
            if isinstance(stream, dict)
        ):
            raise RuntimeError("MP4 已生成，但未检测到内嵌封面")
        if progress_callback:
            progress_callback(
                "verify_completed",
                {"output_path": str(output), "attached_pic": 1},
            )
            progress_callback(
                "cleanup_running",
                {"original_flv": str(original), "output_path": str(output)},
            )

        temporary.replace(output)
        if video != output:
            video.unlink(missing_ok=True)
        if original != video and original != output:
            original.unlink(missing_ok=True)
        if progress_callback:
            progress_callback(
                "cleanup_completed",
                {
                    "original_flv": str(original),
                    "final_video_path": str(output),
                    "original_flv_deleted": True,
                },
            )
        return output
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将录制产物交给 PotatoFlow 上传")
    parser.add_argument("--config", default="bridge.config.json", help="JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="处理参数或 stdin 中的视频路径")
    ingest.add_argument("paths", nargs="*")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--retry", action="store_true", help="允许重试指定的失败任务")
    ingest.add_argument("--session-key", default="", help="将分段追加到同一场直播稿件")
    record_only = sub.add_parser("record-only", help="登记仅录制文件，永久跳过自动投稿")
    record_only.add_argument("paths", nargs="*")
    record_only.add_argument("--room-id", required=True)
    sub.add_parser("retry", help="重试失败记录")
    finalize_session = sub.add_parser(
        "finalize-session",
        help="导入手动停止时的最终录制文件，然后结束分P追加会话",
    )
    finalize_session.add_argument("paths", nargs="*")
    finalize_session.add_argument("--session-key", required=True)
    close_session = sub.add_parser("close-session", help="结束直播的分P追加会话")
    close_session.add_argument("--session-key", required=True)
    status = sub.add_parser("status", help="显示最近记录")
    status.add_argument("--limit", type=int, default=30)
    return parser


def video_duration_seconds(path: Path, ffprobe: str = "ffprobe") -> float | None:
    """Return media duration, or None when the recorder file cannot be probed."""
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            return None
        duration = float(completed.stdout.strip())
        return duration if duration >= 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def recording_wall_clock_upper_bound_seconds(path: Path) -> float | None:
    """Estimate a safe upper bound from the recorder timestamp and final mtime."""
    match = re.search(
        r"(20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}(?:-\d{2})?)",
        path.stem,
    )
    if not match:
        return None
    value = match.group(1)
    date_format = "%Y-%m-%d_%H-%M-%S" if len(value) == 19 else "%Y-%m-%d_%H-%M"
    try:
        started_at = datetime.strptime(value, date_format)
        elapsed = datetime.fromtimestamp(path.stat().st_mtime) - started_at
    except (OSError, ValueError):
        return None
    seconds = elapsed.total_seconds()
    if seconds < 0 or seconds > 7 * 24 * 3600:
        return None
    return seconds


def recording_effective_duration_seconds(
    path: Path,
    ffprobe: str = "ffprobe",
) -> float | None:
    """Return recorder duration capped by its observed wall-clock lifetime."""
    duration = video_duration_seconds(path, ffprobe)
    wall_clock_duration = recording_wall_clock_upper_bound_seconds(path)
    if wall_clock_duration is None:
        return duration
    return (
        min(duration, wall_clock_duration)
        if duration is not None
        else wall_clock_duration
    )


def main(argv: list[str] | None = None) -> int:
    ensure_pipeline_process_group()
    configure_linux_ca_environment()
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.config))
    state_path = resolve_path(str(cfg.get("state_db", ".bridge/state.sqlite3")), cfg)
    store = StateStore(state_path)
    if args.command not in {"status", "close-session"}:
        store.cleanup_expired_retained_xml()

    if args.command == "close-session":
        closed = store.close_multipart_session(str(args.session_key))
        print(f"OK 分P会话已结束: {args.session_key}" if closed else f"SKIP 没有活动分P会话: {args.session_key}")
        return 0

    if args.command == "status":
        for row in store.recent(max(1, args.limit)):
            error = f" error={row['error']}" if row["error"] else ""
            print(f"{row['updated_at']} {row['status']:10} attempts={row['attempts']} "
                  f"{row['platform']:9} {row['video_path']}{error}")
        return 0

    if args.command == "record-only":
        received_paths = input_paths(args.paths)
        paths = [path for path in received_paths if path.suffix.lower() in VIDEO_EXTENSIONS]
        if not paths:
            print("没有收到可登记的录播文件", file=sys.stderr)
            return 2
        ok = True
        for path in paths:
            if not path.is_file():
                print(f"ERROR 文件不存在: {path}", file=sys.stderr)
                ok = False
                continue
            store.exclude_recording(path, str(args.room_id))
            record_cfg = effective_config(cfg, path)
            minimum_duration = max(
                300.0,
                float(
                    record_cfg.get(
                        "MIN_RECORDING_UPLOAD_DURATION_SECONDS",
                        300,
                    )
                    or 300
                ),
            )
            duration = recording_effective_duration_seconds(
                path,
                str(record_cfg.get("ffprobe", "ffprobe")),
            )
            if duration is not None and duration < minimum_duration:
                print(
                    f"SKIP 视频时长 {duration:.1f} 秒，小于 {minimum_duration:.0f} 秒："
                    f"不创建任何任务: {path}",
                    file=sys.stderr,
                )
                continue
            danmaku_xml = wait_for_danmaku_xml(
                path,
                received_paths,
                timeout=float(record_cfg.get("record_only_xml_wait_seconds", 8)),
            )
            key = fingerprint(path)
            is_new_task = not store.upload_exists(key)
            if not store.claim_record_only(
                key,
                path,
                str(args.room_id),
                danmaku_xml,
            ):
                print(f"SKIP 仅录制任务已存在或正在处理: {path}")
                continue
            if is_new_task:
                emit_recording_task_added_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                )
            if danmaku_xml is None:
                error = "录制已结束，但未找到稳定的 XML 弹幕文件"
                store.stage(key, "record", "failed", {
                    "video_path": str(path),
                    "size_bytes": path.stat().st_size,
                    "safe_finalized": False,
                }, error=error)
                store.finish(key, "failed", error=error)
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="failed",
                    error=error,
                    stage="record",
                )
                print(f"ERROR 仅录制文件未找到 XML，已保留原 FLV: {path}", file=sys.stderr)
                ok = False
                continue
            current_stage = "ass"
            burned_output = path.with_suffix(".mp4")
            if burned_output == path:
                burned_output = path.with_name(f"{path.stem}.danmaku.mp4")
            try:
                ass_state = store.stage_state(key, "ass")
                ass_candidate = Path(str(ass_state.get("details", {}).get("ass_path") or ""))
                if (
                    ass_state.get("status") == "completed"
                    and ass_candidate.is_file()
                    and ass_candidate.stat().st_size > 0
                ):
                    ass_path = ass_candidate
                    store.stage(key, "ass", "completed", {
                        **ass_state.get("details", {}),
                        "reused_on_retry": True,
                    })
                else:
                    store.stage(key, "ass", "running", {"danmaku_xml": str(danmaku_xml)})
                    ass_path = generate_record_only_ass(path, cfg, received_paths)
                    if ass_path is None:
                        raise RuntimeError("弹幕 XML 为空或无有效弹幕，未生成 ASS 字幕")
                    store.stage(
                        key,
                        "ass",
                        "completed",
                        {"danmaku_xml": str(danmaku_xml), "ass_path": str(ass_path)},
                    )

                burn_enabled = bool(record_cfg.get("danmaku_burn_in", False))
                remux_source = path
                if burn_enabled:
                    current_stage = "burn"
                    burn_state = store.stage_state(key, "burn")
                    burn_candidate = Path(str(
                        burn_state.get("details", {}).get("burned_video_path") or ""
                    ))
                    if (
                        burn_state.get("status") == "completed"
                        and burn_candidate.is_file()
                        and burn_candidate.stat().st_size > 0
                    ):
                        remux_source = burn_candidate
                        store.stage(key, "burn", "completed", {
                            **burn_state.get("details", {}),
                            "reused_on_retry": True,
                        })
                    else:
                        burn_stage_details = {
                            "source_video_path": str(path),
                            "ass_path": str(ass_path),
                        }
                        store.stage(key, "burn", "queued", burn_stage_details)

                        def update_burn_queue(status: str) -> None:
                            store.stage(
                                key,
                                "burn",
                                "running" if status == "burning" else "queued",
                                burn_stage_details,
                            )

                        def update_burn_progress(progress: dict[str, Any]) -> None:
                            burn_stage_details.update(progress)
                            store.stage(key, "burn", "running", burn_stage_details)
                        remux_source = burn_ass(
                            path,
                            ass_path,
                            burned_output,
                            ffmpeg=str(record_cfg.get("ffmpeg", "ffmpeg")),
                            fonts_dir=resolve_path(
                                str(record_cfg.get("danmaku_fonts_dir", "potatoflow-app/fonts")),
                                record_cfg,
                            ),
                            preset=str(record_cfg.get("danmaku_encode_preset", "medium")),
                            crf=int(record_cfg.get("danmaku_encode_crf", 20)),
                            encoder=str(record_cfg.get("danmaku_encoder", "cpu")),
                            queue_status_callback=update_burn_queue,
                            progress_callback=update_burn_progress,
                        )
                        store.stage(key, "burn", "completed", {
                            **burn_stage_details,
                            "burned_video_path": str(remux_source),
                            "burn_in": True,
                            "visible_output": True,
                        })
                    if remux_source != path:
                        store.exclude_recording(remux_source, str(args.room_id))
                else:
                    store.stage(key, "burn", "skipped", {
                        "reason": "直播间未开启 ASS 弹幕烧录",
                        "burn_in": False,
                    })

                current_stage = "cover"
                cover_state = store.stage_state(key, "cover")
                cover_candidate = Path(str(
                    cover_state.get("details", {}).get("ai_cover_path") or ""
                ))
                if (
                    cover_state.get("status") == "completed"
                    and cover_candidate.is_file()
                    and cover_candidate.stat().st_size > 0
                ):
                    cover_path = cover_candidate
                    store.stage(key, "cover", "completed", {
                        **cover_state.get("details", {}),
                        "reused_on_retry": True,
                    })
                else:
                    store.stage(key, "cover", "running")
                    try:
                        cover_path = generate_record_only_cover(path, cfg)
                    except Exception as exc:
                        cover_path = None
                        store.stage(key, "cover", "skipped", {
                            "reason": str(exc),
                            "continue_without_cover": True,
                        })
                    else:
                        store.stage(
                            key,
                            "cover",
                            "completed",
                            {"ai_cover_path": str(cover_path)},
                        )

                def update_remux_progress(
                    event: str,
                    details: dict[str, Any],
                ) -> None:
                    nonlocal current_stage
                    if event == "remux_completed":
                        store.stage(key, "remux", "completed", details)
                    elif event == "verify_running":
                        current_stage = "verify"
                        store.stage(key, "verify", "running", details)
                    elif event == "verify_completed":
                        store.stage(key, "verify", "completed", details)
                    elif event == "cleanup_running":
                        current_stage = "cleanup"
                        store.stage(key, "cleanup", "running", details)
                    elif event == "cleanup_completed":
                        store.stage(key, "cleanup", "completed", details)

                if cover_path is None:
                    store.stage(key, "remux", "skipped", {
                        "reason": "未生成封面，无需封装 MP4",
                        "source_video_path": str(remux_source),
                    })
                    store.stage(key, "verify", "skipped", {
                        "reason": "未执行封面封装",
                    })
                    store.stage(key, "cleanup", "skipped", {
                        "reason": "保留原始录制文件",
                    })
                    final_video = remux_source
                else:
                    current_stage = "remux"
                    store.stage(
                        key,
                        "remux",
                        "running",
                        {"source_flv": str(path), "cover_path": str(cover_path)},
                    )
                    final_video = remux_record_only_flv_with_cover(
                        remux_source,
                        cover_path,
                        record_cfg,
                        progress_callback=update_remux_progress,
                        output_path=path.with_suffix(".mp4"),
                        original_flv=path,
                    )
                if final_video != path:
                    store.exclude_recording(final_video, str(args.room_id))
                result = {
                    "room_id": str(args.room_id),
                    "record_only": True,
                    "video_path": str(path),
                    "final_video_path": str(final_video),
                    "danmaku_xml": str(danmaku_xml),
                    "ass_path": str(ass_path),
                    "cover_path": str(cover_path) if cover_path else None,
                    "attached_pic": int(cover_path is not None),
                    "danmaku_burn_in": burn_enabled,
                    "burned_video_path": str(final_video) if burn_enabled else None,
                    "original_flv_deleted": cover_path is not None and final_video != path,
                    "video_duration_seconds": video_duration_seconds(
                        final_video,
                        str(record_cfg.get("ffprobe", "ffprobe")),
                    ),
                }
                store.finish(key, "completed", result)
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="completed",
                    result=result,
                )
            except Exception as exc:
                if current_stage == "burn":
                    burned_output.unlink(missing_ok=True)
                store.stage(key, current_stage, "failed", error=str(exc))
                store.finish(key, "failed", error=str(exc))
                emit_recording_task_result_notification(
                    record_cfg,
                    fingerprint_value=key,
                    video=path,
                    task_kind="record_only",
                    status="failed",
                    error=str(exc),
                    stage=current_stage,
                )
                print(
                    f"ERROR 仅录制本地处理失败，已保留原 FLV: {path}: {exc}",
                    file=sys.stderr,
                )
                ok = False
                continue
            print(
                f"OK 仅录制文件已保留并跳过自动投稿: "
                f"{final_video}，ASS: {ass_path}，封面: {cover_path or '已跳过'}"
            )
        return 0 if ok else 1

    retry = args.command == "retry" or bool(getattr(args, "retry", False))
    received_paths = store.failed_paths() if args.command == "retry" else input_paths(args.paths)
    paths = [path for path in received_paths if path.suffix.lower() in VIDEO_EXTENSIONS]
    if args.command == "finalize-session" and not paths:
        closed = store.close_multipart_session(str(args.session_key))
        print(
            f"OK 分P会话已结束: {args.session_key}"
            if closed
            else f"SKIP 没有活动分P会话: {args.session_key}"
        )
        return 0
    if not paths:
        print("没有收到可处理的视频路径", file=sys.stderr)
        return 2
    ok = True
    for path in paths:
        if not path.is_file():
            print(f"ERROR 文件不存在: {path}", file=sys.stderr)
            ok = False
            continue
        minimum_duration = max(
            300.0,
            float(cfg.get("MIN_RECORDING_UPLOAD_DURATION_SECONDS", 300) or 300),
        )
        duration = recording_effective_duration_seconds(
            path,
            str(cfg.get("ffprobe", "ffprobe")),
        )
        if duration is not None and duration < minimum_duration:
            print(
                f"SKIP 视频时长 {duration:.1f} 秒，小于 {minimum_duration:.0f} 秒："
                f"不创建任务、不进行 AI 处理或投稿: {path}",
                file=sys.stderr,
            )
            continue
        danmaku_xml = find_danmaku_xml(path, received_paths)
        ok = upload_one(
            path, cfg, store,
            dry_run=bool(getattr(args, "dry_run", False)),
            retry=retry,
            danmaku_xml=danmaku_xml,
            session_key=str(getattr(args, "session_key", "") or ""),
        ) and ok
    if args.command == "finalize-session":
        if ok:
            closed = store.close_multipart_session(str(args.session_key))
            print(
                f"OK 最终分段已导入，分P会话已结束: {args.session_key}"
                if closed
                else f"OK 最终分段已导入，无需关闭空会话: {args.session_key}"
            )
        else:
            store.delete_multipart_session(str(args.session_key))
            print(
                f"WARN 最终分段导入失败，失败任务已保留且旧分P关系已解除: {args.session_key}",
                file=sys.stderr,
            )
        # The failed task is persisted and retryable in WebUI. Recording has
        # already stopped, so do not turn safe finalization into a recorder
        # process failure.
        return 0
    if not ok and str(getattr(args, "session_key", "") or "") and not retry:
        # A failed segment is already visible and retryable in the WebUI.  Do
        # not abort the recorder live event stream here: later segments still need
        # to be recorded, and the end-of-stream hook must close this session so
        # the next broadcast cannot append to the old submission.
        print("WARN 分P处理失败已记录，录制与后续分段将继续", file=sys.stderr)
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
