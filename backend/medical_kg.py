"""Medical knowledge lookup adapted from the local RAGQnASystem project.

The service prefers Neo4j when configured, and falls back to the DiseaseKG
JSON file so the Agent still works during lightweight demos.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MEDICAL_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "medical_new_2.json"


FIELD_LABELS = {
    "desc": "疾病简介",
    "cause": "疾病病因",
    "prevent": "预防措施",
    "cure_lasttime": "治疗周期",
    "cured_prob": "治愈概率",
    "easy_get": "易感人群",
    "symptom": "相关症状",
    "check": "所需检查",
    "cure_department": "就诊科室",
    "cure_way": "治疗方式",
    "common_drug": "常用药品",
    "recommand_drug": "推荐药品",
    "do_eat": "宜吃食物",
    "not_eat": "忌吃食物",
    "recommand_eat": "推荐食谱",
    "acompany": "并发疾病",
    "drug_detail": "药品/厂商信息",
}


INTENT_FIELDS = [
    (("简介", "是什么", "介绍", "科普"), ["desc"]),
    (("病因", "原因", "为什么"), ["cause"]),
    (("预防", "避免"), ["prevent"]),
    (("治疗周期", "多久能好", "多长时间"), ["cure_lasttime"]),
    (("治愈", "治好", "概率"), ["cured_prob"]),
    (("易感", "容易得", "人群"), ["easy_get"]),
    (("症状", "表现"), ["symptom"]),
    (("检查", "查什么", "检测"), ["check"]),
    (("科室", "挂什么科", "挂哪个科", "看什么科"), ["cure_department"]),
    (("治疗", "怎么办", "怎么治"), ["cure_way", "common_drug", "recommand_drug"]),
    (("药", "用药", "吃什么药"), ["common_drug", "recommand_drug"]),
    (("宜吃", "能吃", "适合吃"), ["do_eat", "recommand_eat"]),
    (("忌吃", "不能吃", "少吃", "禁忌"), ["not_eat"]),
    (("食谱", "饮食"), ["do_eat", "not_eat", "recommand_eat"]),
    (("并发", "引发"), ["acompany"]),
    (("生产商", "厂家", "谁生产"), ["drug_detail"]),
]


def _data_path() -> Path:
    return Path(os.getenv("MEDICAL_KG_DATA_PATH") or DEFAULT_MEDICAL_DATA_PATH)


def _safe_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _parse_json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    if line.endswith(","):
        line = line[:-1]
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


@lru_cache(maxsize=1)
def _load_medical_records() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[str]]]:
    path = _data_path()
    if not path.exists():
        return [], {}, {}

    records: list[dict[str, Any]] = []
    disease_index: dict[str, dict[str, Any]] = {}
    symptom_index: dict[str, list[str]] = {}

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            item = _parse_json_line(line)
            if not item:
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            records.append(item)
            disease_index[name] = item
            for symptom in _safe_list(item.get("symptom")):
                symptom_index.setdefault(symptom, []).append(name)

    return records, disease_index, symptom_index


def _choose_fields(query: str) -> list[str]:
    selected: list[str] = []
    for keywords, fields in INTENT_FIELDS:
        if any(keyword in query for keyword in keywords):
            selected.extend(fields)
    if not selected:
        selected = ["desc", "symptom", "check", "cure_department"]
    return list(dict.fromkeys(selected))


def _find_disease(query: str) -> tuple[str | None, dict[str, Any] | None, list[str]]:
    records, disease_index, symptom_index = _load_medical_records()
    for name, item in disease_index.items():
        if name and name in query:
            return name, item, []

    symptom_matches: list[str] = []
    for symptom, diseases in symptom_index.items():
        if symptom and symptom in query:
            symptom_matches.extend(diseases[:5])

    if symptom_matches:
        candidates = list(dict.fromkeys(symptom_matches))[:8]
        candidate_name = candidates[0]
        return candidate_name, disease_index.get(candidate_name), candidates

    for item in records:
        name = str(item.get("name") or "")
        if name and any(token in name for token in query.split()):
            return name, item, []

    return None, None, []


def _format_value(value: Any, max_items: int = 12) -> str:
    values = _safe_list(value)
    if not values:
        return "暂无明确记录"
    if len(values) > max_items:
        return "、".join(values[:max_items]) + f" 等 {len(values)} 项"
    return "、".join(values)


def _lookup_from_json(query: str) -> dict[str, Any]:
    disease_name, item, candidates = _find_disease(query)
    if not item:
        return {
            "source": "local_diseasekg_json",
            "answer": "本地医疗知识库中没有匹配到明确疾病或症状实体。",
            "matched_entity": None,
            "facts": [],
        }

    fields = _choose_fields(query)
    facts = []
    for field in fields:
        if field not in FIELD_LABELS:
            continue
        facts.append({"label": FIELD_LABELS[field], "value": _format_value(item.get(field))})

    if candidates:
        facts.insert(
            0,
            {
                "label": "症状可能相关疾病",
                "value": "、".join(candidates),
            },
        )

    lines = [f"匹配疾病：{disease_name}"]
    for fact in facts:
        lines.append(f"- {fact['label']}：{fact['value']}")

    return {
        "source": "local_diseasekg_json",
        "answer": "\n".join(lines),
        "matched_entity": disease_name,
        "facts": facts,
    }


def _lookup_from_neo4j(query: str) -> dict[str, Any] | None:
    bolt_url = os.getenv("NEO4J_BOLT_URL")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not bolt_url or not user or not password:
        return None

    try:
        from py2neo import Graph
    except ImportError:
        return None

    disease_name, _, _ = _find_disease(query)
    if not disease_name:
        return None

    fields = _choose_fields(query)
    try:
        graph = Graph(bolt_url, user=user, password=password, name=database)
        facts = []
        for field in fields:
            label = FIELD_LABELS.get(field, field)
            if field in ("desc", "cause", "prevent", "cure_lasttime", "cured_prob", "easy_get"):
                prop_map = {
                    "desc": "疾病简介",
                    "cause": "疾病病因",
                    "prevent": "预防措施",
                    "cure_lasttime": "治疗周期",
                    "cured_prob": "治愈概率",
                    "easy_get": "疾病易感人群",
                }
                rows = graph.run(
                    "MATCH (d:疾病 {名称:$name}) RETURN d[$prop] AS value",
                    name=disease_name,
                    prop=prop_map[field],
                ).data()
                value = rows[0].get("value") if rows else ""
            else:
                relation_map = {
                    "symptom": ("疾病的症状", "疾病症状"),
                    "check": ("疾病所需检查", "检查项目"),
                    "cure_department": ("疾病所属科目", "科目"),
                    "cure_way": ("治疗的方法", "治疗方法"),
                    "common_drug": ("疾病使用药品", "药品"),
                    "recommand_drug": ("疾病使用药品", "药品"),
                    "do_eat": ("疾病宜吃食物", "食物"),
                    "not_eat": ("疾病忌吃食物", "食物"),
                    "acompany": ("疾病并发疾病", "疾病"),
                }
                rel = relation_map.get(field)
                if not rel:
                    value = ""
                else:
                    relation, target = rel
                    rows = graph.run(
                        f"MATCH (d:疾病 {{名称:$name}})-[:`{relation}`]->(x:`{target}`) RETURN x.名称 AS value LIMIT 20",
                        name=disease_name,
                    ).data()
                    value = [row["value"] for row in rows if row.get("value")]
            facts.append({"label": label, "value": _format_value(value)})

        lines = [f"匹配疾病：{disease_name}"]
        for fact in facts:
            lines.append(f"- {fact['label']}：{fact['value']}")
        return {
            "source": "neo4j_medical_kg",
            "answer": "\n".join(lines),
            "matched_entity": disease_name,
            "facts": facts,
        }
    except Exception as exc:
        return {
            "source": "neo4j_medical_kg_error",
            "answer": f"Neo4j 查询失败，建议检查连接配置。错误：{exc}",
            "matched_entity": disease_name,
            "facts": [],
        }


def search_medical_kg_context(query: str) -> dict[str, Any]:
    """Return structured medical KG facts for an Agent tool call."""
    result = _lookup_from_neo4j(query) or _lookup_from_json(query)
    result["safety_note"] = (
        "以上内容仅用于医疗科普和就诊准备，不能替代医生诊断或处方；"
        "若症状严重、持续加重或涉及儿童/孕产妇/老人等人群，请及时就医。"
    )
    return result
