import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.grok_local_v3 import AUDIENCES, VERSION
from jsonschema import Draft202012Validator

DECISIONS = {"KEEP", "CONTEXT", "REVIEW", "DROP"}
PRIORITIES = {"low", "medium", "high"}
SPEAKER_ROLES = {"speaker", "quoted", "mentioned", "addressee", "future_speaker", "editorial", "none"}
SOURCE_ROLES = {"originator", "official", "relay", "aggregator", "commentary", "unclear"}
STORY_RELATIONS = {"new_candidate", "duplicate_candidate", "development_candidate", "unclear", "none"}
REQUIRED = {
    "source_document_id", "decision", "keep_for_analysis", "priority",
    "information_trigger", "speaker", "speaker_role", "mentioned_actors",
    "main_claim", "framing", "target_audiences", "country_spaces",
    "influence_methods", "expected_effect", "source_role", "story_relation",
    "matched_story_key", "duplicate_of_document_id", "added_value",
    "insufficient_text", "confidence", "reason",
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def validate_schema(batch: dict, result: dict) -> str:
    raw = str(batch.get("schema_path") or "").strip()
    schema_path = Path(raw) if raw else ROOT / "grok" / "local_v3" / "OUTPUT_SCHEMA_V3.json"
    if not schema_path.exists():
        fail(f"schema file not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        where = ".".join(str(x) for x in err.absolute_path) or "<root>"
        fail(f"JSON schema validation failed at {where}: {err.message}")
    return str(schema_path)


def _norm_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def validate_result(row: dict, expected_ids: set[int], text_by_id: dict[int, str]) -> None:
    if not isinstance(row, dict):
        fail("each result must be an object")
    missing = sorted(REQUIRED - set(row))
    if missing:
        fail(f"result {row.get('source_document_id')} missing fields: {missing}")
    doc_id = int(row["source_document_id"])
    if doc_id not in expected_ids:
        fail(f"unexpected source_document_id {doc_id}")
    if row["decision"] not in DECISIONS:
        fail(f"invalid decision for {doc_id}: {row['decision']}")
    if row["priority"] not in PRIORITIES:
        fail(f"invalid priority for {doc_id}: {row['priority']}")
    if row["speaker_role"] not in SPEAKER_ROLES:
        fail(f"invalid speaker_role for {doc_id}: {row['speaker_role']}")
    if row["source_role"] not in SOURCE_ROLES:
        fail(f"invalid source_role for {doc_id}: {row['source_role']}")
    if row["story_relation"] not in STORY_RELATIONS:
        fail(f"invalid story_relation for {doc_id}: {row['story_relation']}")
    if not isinstance(row["keep_for_analysis"], bool):
        fail(f"keep_for_analysis must be boolean for {doc_id}")
    if not isinstance(row["insufficient_text"], bool):
        fail(f"insufficient_text must be boolean for {doc_id}")
    confidence = int(row["confidence"])
    if not 0 <= confidence <= 100:
        fail(f"confidence out of range for {doc_id}: {confidence}")
    reason = str(row["reason"] or "").strip()
    if not reason:
        fail(f"reason is empty for {doc_id}")
    if row["decision"] != "DROP":
        if not reason.startswith("EVIDENCE: ") or " | " not in reason:
            fail(f"non-DROP reason must start with EVIDENCE: <verbatim fragment> | for {doc_id}")
        evidence = reason[len("EVIDENCE: "):].split(" | ", 1)[0].strip().strip('"??')
        if len(evidence) < 12:
            fail(f"evidence fragment too short for {doc_id}")
        source_text = _norm_ws(text_by_id.get(doc_id, ""))
        if _norm_ws(evidence) not in source_text:
            fail(f"evidence fragment not found in source text for {doc_id}")

    audiences = row["target_audiences"]
    if not isinstance(audiences, list) or any(a not in AUDIENCES for a in audiences):
        fail(f"invalid target_audiences for {doc_id}: {audiences}")
    for field in ("mentioned_actors", "country_spaces", "influence_methods"):
        if not isinstance(row[field], list):
            fail(f"{field} must be an array for {doc_id}")
    if row["decision"] == "DROP":
        if row["keep_for_analysis"]:
            fail(f"DROP cannot keep_for_analysis for {doc_id}")
        if row["story_relation"] != "none":
            fail(f"DROP must have story_relation=none for {doc_id}")
    if row["decision"] in {"KEEP", "CONTEXT"} and not row["keep_for_analysis"]:
        fail(f"{row['decision']} must keep_for_analysis for {doc_id}")
    if row["story_relation"] == "development_candidate":
        if not str(row["matched_story_key"] or "").strip():
            fail(f"development requires matched_story_key for {doc_id}")
        if not str(row["added_value"] or "").strip():
            fail(f"development requires added_value for {doc_id}")
    if row["story_relation"] == "duplicate_candidate":
        other = row["duplicate_of_document_id"]
        if row["decision"] == "KEEP":
            fail(f"duplicate_candidate cannot be KEEP for {doc_id}")
        if other is not None and int(other) == doc_id:
            fail(f"document cannot duplicate itself: {doc_id}")
        if not str(row["matched_story_key"] or "").strip():
            fail(f"duplicate_candidate requires matched_story_key for {doc_id}")
        if other is None:
            fail(f"duplicate_candidate requires duplicate_of_document_id for {doc_id}")
    if row["decision"] == "KEEP" and int(row["confidence"]) <= 60 and row["story_relation"] == "unclear":
        fail(f"low-confidence unclear KEEP must be REVIEW or CONTEXT for {doc_id}")
    if row["insufficient_text"] and row["decision"] != "REVIEW":
        fail(f"insufficient_text=true requires REVIEW for {doc_id}")
    if row["decision"] == "REVIEW" and row["story_relation"] == "development_candidate":
        fail(f"REVIEW cannot assert development for {doc_id}")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: grok_local_v3_validate.py <batch.json> <result.json>")
    batch_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    schema_path = validate_schema(batch, result)
    if result.get("batch_id") != batch.get("batch_id"):
        fail("batch_id mismatch")
    if result.get("analyzer_version") != VERSION:
        fail(f"analyzer_version must be {VERSION}")
    expected = [int(item["source_document_id"]) for item in batch.get("items", [])]
    rows = result.get("results")
    if not isinstance(rows, list):
        fail("results must be an array")
    ids = [int(row.get("source_document_id", 0)) for row in rows if isinstance(row, dict)]
    if len(ids) != len(set(ids)):
        fail("duplicate source_document_id in results")
    if sorted(ids) != sorted(expected):
        missing = sorted(set(expected) - set(ids))
        extra = sorted(set(ids) - set(expected))
        fail(f"coverage mismatch: missing={missing} extra={extra}")
    expected_ids = set(expected)
    text_by_id = {int(item["source_document_id"]): str(item.get("text") or "") for item in batch.get("items", [])}
    for row in rows:
        validate_result(row, expected_ids, text_by_id)

    counts = Counter(row["decision"] for row in rows)
    relation_counts = Counter(row["story_relation"] for row in rows)
    summary = {
        "ok": True,
        "batch_id": batch["batch_id"],
        "processed": len(rows),
        "decisions": dict(counts),
        "story_relations": dict(relation_counts),
        "average_confidence": round(sum(int(row["confidence"]) for row in rows) / len(rows), 1),
        "schema_validation": {"ok": True, "schema_path": schema_path},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
