"""Read-only summary for the current Processing Engine v2 benchmark."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import processing_v2


def _pct(part: int, whole: int) -> float:
    return (part / whole * 100.0) if whole else 0.0


def main() -> int:
    connection = connect(readonly=True)
    try:
        pipeline = processing_v2.PIPELINE_VERSION

        prefilter = connection.execute(
            """SELECT decision,count(*) AS n
                 FROM processing_v2_prefilter_results
                WHERE pipeline_version=?
                GROUP BY decision""",
            (pipeline,),
        ).fetchall()
        pf = {row["decision"]: int(row["n"]) for row in prefilter}
        raw = sum(pf.values())
        passed = pf.get("pass", 0)

        hydration = connection.execute(
            """SELECT status,count(*) AS n,
                      round(avg(length(full_text))) AS avg_chars
                 FROM processing_v2_hydration_results
                WHERE pipeline_version=?
                GROUP BY status""",
            (pipeline,),
        ).fetchall()

        analysis = connection.execute(
            """SELECT count(*) AS docs,
                      count(*) FILTER (WHERE briefing_relevant) AS briefing,
                      count(*) FILTER (WHERE influence_relevant) AS influence,
                      count(*) FILTER (WHERE event_relevant) AS event
                 FROM processing_v2_document_analysis
                WHERE pipeline_version=?""",
            (pipeline,),
        ).fetchone()
        analyzed = int(analysis["docs"] or 0)

        by_kind = connection.execute(
            """SELECT d.source_kind,count(*) AS docs,
                      count(*) FILTER (WHERE a.briefing_relevant) AS briefing,
                      count(*) FILTER (WHERE a.influence_relevant) AS influence,
                      count(*) FILTER (WHERE a.event_relevant) AS event,
                      round(avg(a.input_chars)) AS avg_input_chars
                 FROM processing_v2_document_analysis a
                 JOIN source_documents d ON d.id=a.source_document_id
                WHERE a.pipeline_version=?
                GROUP BY d.source_kind
                ORDER BY d.source_kind""",
            (pipeline,),
        ).fetchall()

        calls = connection.execute(
            """SELECT count(*) AS calls,
                      count(*) FILTER (WHERE success) AS successful_calls,
                      count(*) FILTER (WHERE NOT success) AS failed_calls,
                      coalesce(sum(document_count) FILTER (WHERE success),0) AS successful_docs,
                      coalesce(sum(input_tokens) FILTER (WHERE success),0) AS input_tokens,
                      coalesce(sum(cached_input_tokens) FILTER (WHERE success),0) AS cached_input_tokens,
                      coalesce(sum(output_tokens) FILTER (WHERE success),0) AS output_tokens,
                      coalesce(sum(duration_ms) FILTER (WHERE success),0) AS duration_ms
                 FROM processing_v2_ai_calls
                WHERE pipeline_version=?""",
            (pipeline,),
        ).fetchone()

        prompt_versions = connection.execute(
            """SELECT prompt_version,count(*) AS docs
                 FROM processing_v2_document_analysis
                WHERE pipeline_version=?
                GROUP BY prompt_version
                ORDER BY prompt_version""",
            (pipeline,),
        ).fetchall()

        queue = connection.execute(
            """SELECT stage,status,count(*) AS n
                 FROM processing_v2_jobs
                WHERE pipeline_version=?
                GROUP BY stage,status
                ORDER BY stage,status""",
            (pipeline,),
        ).fetchall()

        duration_ms = int(calls["duration_ms"] or 0)
        successful_docs = int(calls["successful_docs"] or 0)
        docs_per_minute = (
            successful_docs / (duration_ms / 60000.0)
            if duration_ms > 0 else 0.0
        )

        print(f"Processing Engine v2 benchmark — {pipeline}")
        print()
        print("PIPELINE")
        print(f"  raw sample:              {raw}")
        print(f"  prefilter pass:          {passed}")
        print(f"  prefilter saved from AI: {raw - passed} ({_pct(raw-passed, raw):.1f}%)")
        for key in sorted(pf):
            print(f"    {key:20} {pf[key]}")
        print()
        print("HYDRATION")
        for row in hydration:
            print(
                f"  {row['status']:12} {int(row['n']):4} "
                f"avg_chars={int(row['avg_chars'] or 0)}"
            )
        print()
        print("AI")
        print(f"  analyzed docs:           {analyzed}")
        print(f"  successful calls:        {int(calls['successful_calls'] or 0)}")
        print(f"  failed calls:            {int(calls['failed_calls'] or 0)}")
        print(f"  measured input tokens:   {int(calls['input_tokens'] or 0)}")
        print(f"  cached input tokens:     {int(calls['cached_input_tokens'] or 0)}")
        print(f"  measured output tokens:  {int(calls['output_tokens'] or 0)}")
        print(f"  successful model time:   {duration_ms / 1000.0:.1f}s")
        print(f"  throughput:              {docs_per_minute:.1f} docs/min")
        print()
        print("ROUTING")
        print(
            f"  briefing relevant:       {int(analysis['briefing'] or 0)} "
            f"({_pct(int(analysis['briefing'] or 0), analyzed):.1f}%)"
        )
        print(
            f"  influence relevant:      {int(analysis['influence'] or 0)} "
            f"({_pct(int(analysis['influence'] or 0), analyzed):.1f}%)"
        )
        print(
            f"  event relevant:          {int(analysis['event'] or 0)} "
            f"({_pct(int(analysis['event'] or 0), analyzed):.1f}%)"
        )
        print()
        print("BY SOURCE KIND")
        for row in by_kind:
            print(
                f"  {row['source_kind']:10} docs={int(row['docs'])} "
                f"BR={int(row['briefing'])} INF={int(row['influence'])} "
                f"EVENT={int(row['event'])} "
                f"avg_input_chars={int(row['avg_input_chars'] or 0)}"
            )
        print()
        print("PROMPT VERSIONS")
        for row in prompt_versions:
            print(f"  {row['prompt_version']}: {int(row['docs'])}")
        print()
        print("QUEUE")
        for row in queue:
            print(f"  {row['stage']:10} {row['status']:12} {int(row['n'])}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
