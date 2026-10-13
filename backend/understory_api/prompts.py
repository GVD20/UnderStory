from __future__ import annotations

import json


PROMPT_VERSION = "chapter-packet-v1"
PROMPT_VERSIONS = [PROMPT_VERSION, "chapter-packet-v2-relations", "chapter-packet-v3-faithful"]

SYSTEM_PROMPT = """你是 LightRocket 的 Chapter Reader。你的任务是把单章小说编译为可追溯的叙事包，而不是撰写最终 Wiki。

要求：
1. 只依据输入原文，不补充外部知识，不猜测后续剧情。
2. 所有实体、事件、关系、设定与轨迹候选都尽量绑定输入中存在的 lr:// locator。
3. chapter_intro 是读者进入本章前看到的无破坏性导读：描述氛围、起点和阅读关注点，不揭示结局、真相或关键转折。
4. summary_short 可以概括完整章节；summary_detailed 应覆盖起因、发展、转折与结果。
5. 局部人物和对象可以保留候选状态；不要决定最终 Wiki 分类，不要合并不确定实体。
6. 输出必须是一个 JSON object，不要输出 Markdown、代码围栏或解释文字。
7. setting_fragments 每项必须使用 name/category/description/evidence；candidate_track_items 每项必须使用 title/category/note/locator/importance，不要把字段改名为 value、name、type 或 evidence。
"""

RELATION_PROMPT_EXTENSION = """
关系提取补充规则：
- relations 是本章局部观察，不是最终知识图谱；只记录原文明示或由连续行为直接支持的关系。
- 教导、请求、欺骗、信任、冲突、合作、亲属等对情节有解释力的互动可以记录。
- predicate 使用简短稳定的中文短语；subject/object 使用 entities 中的名称。
- 每条关系至少绑定一个直接支持该互动的 locator。没有证据时宁可不写。
"""

FAITHFUL_PROMPT_EXTENSION = """
忠实记录模式（默认 Reader Worker）：
- 你只能看到这一章，不具备全书上下文。不要判断角色的全书定位，不要做最终实体合并，也不要为 Wiki 决定取舍。
- 目标是高召回、事无巨细地复述和整理“本章实际写了什么”。不要为了简短而省略次要场景、插曲、动作、人物态度、状态变化、规则说明、回忆、转述与章节末尾结果。
- summary_detailed 应是充分详细的全章记录；narrative_outline 按原文顺序列出完整情节步骤。
- scenes 应覆盖全章而非只挑主线。实体 description 只描述本章可见信息；actions 和 state_changes 分开列出。
- events 同时记录 causes 与 consequences。小事件可以标为 minor，但不要因为“看起来不重要”而删除。
- key_details 保存可能供上层核查的具体细节；dialogue_highlights 只保存对人物、设定或事件有解释力的原文位置与简短说明。
- narrative_observations 可记录叙述视角、回忆、故事中故事、信息是否来自传闻等形式特征，但不要做无证据的文学评论。
- 允许较长输出，宁可完整，不要追求漂亮或精炼。最终 Wiki 文风不在本层处理。
"""


def get_system_prompt(version: str) -> str:
    if version == "chapter-packet-v2-relations":
        return SYSTEM_PROMPT + RELATION_PROMPT_EXTENSION
    if version == "chapter-packet-v3-faithful":
        return SYSTEM_PROMPT + RELATION_PROMPT_EXTENSION + FAITHFUL_PROMPT_EXTENSION
    return SYSTEM_PROMPT

OUTPUT_EXAMPLE = {
    "chapter_id": "series-v01-c01",
    "chapter_intro": "旅人来到一座以魔法为生活秩序的国家，并在意外相遇中开始重新观察这里。",
    "summary_short": "完整章节摘要。",
    "summary_detailed": "覆盖完整发展与结果的详细摘要。",
    "narrative_outline": ["按原文顺序记录的步骤一", "步骤二"],
    "scenes": [
        {
            "title": "场景标题",
            "summary": "场景发生了什么",
            "start_locator": "lr://series/example/v01/c01/p0001",
            "end_locator": "lr://series/example/v01/c01/p0010",
        }
    ],
    "entities": [
        {
            "name": "人物名",
            "entity_type": "character",
            "role": "本章作用",
            "description": "只依据本章可见内容的详细描述",
            "aliases": [],
            "actions": ["本章中的具体行动"],
            "state_changes": ["本章内发生的状态变化"],
            "evidence": [{"locator": "lr://series/example/v01/c01/p0002", "note": "依据"}],
        }
    ],
    "events": [
        {
            "title": "事件名",
            "summary": "事件概述",
            "participants": ["人物名"],
            "locations": ["地点名"],
            "importance": "major",
            "causes": ["直接原因"],
            "consequences": ["直接结果"],
            "evidence": [{"locator": "lr://series/example/v01/c01/p0003", "note": "依据"}],
        }
    ],
    "relations": [],
    "setting_fragments": [
        {
            "name": "设定名", "category": "魔法制度", "description": "设定说明",
            "evidence": [{"locator": "lr://series/example/v01/c01/p0004", "note": "依据"}],
        }
    ],
    "open_questions": [],
    "key_details": [{"locator": "lr://series/example/v01/c01/p0005", "note": "具体细节"}],
    "dialogue_highlights": [{"locator": "lr://series/example/v01/c01/p0006", "note": "对白作用"}],
    "narrative_observations": ["第一人称叙事"],
    "candidate_track_items": [
        {
            "title": "首次出现", "category": "first_appearance", "note": "为何值得追踪",
            "locator": "lr://series/example/v01/c01/p0002", "importance": 0.7,
        }
    ],
}


def build_chapter_prompt(chapter_id: str, title: str, paragraphs: list[tuple[str, str]]) -> str:
    source = "\n".join(f"[{locator}] {text}" for locator, text in paragraphs)
    return (
        f"请把以下章节编译为 JSON 叙事包。\n"
        f"chapter_id: {chapter_id}\n"
        f"chapter_title: {title}\n\n"
        f"JSON 字段与格式示例：\n{json.dumps(OUTPUT_EXAMPLE, ensure_ascii=False)}\n\n"
        f"章节原文：\n{source}"
    )
