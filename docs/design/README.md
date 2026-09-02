# Design documents

Working notes written while building the pipeline, kept because they record why
things are the way they are — the BFS overlap analysis behind the persistent
`fetched_tags` table, the reasoning behind the time-local ECDF skill feature, and
the draft-agent brainstorm that motivated the whole dataset.

**These are historical.** File paths in them refer to the layout before the code was
packaged under `src/bsetl/`, and some describe work that has since been completed
differently. They are a record of the reasoning, not current documentation. For
current structure see the [README](../../README.md), and for the schema see the
[data dictionary](../DATA_DICTIONARY.md).

| Document | What it works through |
| --- | --- |
| [`etl_pipeline_optimization_plan.md`](etl_pipeline_optimization_plan.md) | Why crawl efficiency decays as the database grows, and what to do about it |
| [`etl_optimization_agent_checklist.md`](etl_optimization_agent_checklist.md) | The implementation breakdown for those fixes |
| [`time_local_ecdf_skill_feature_checklist.md`](time_local_ecdf_skill_feature_checklist.md) | Design of the `skill_ns` feature |
| [`clean_db_update.md`](clean_db_update.md) | Moving to a single-pass write into the clean schema |
| [`draft_agent_brainstorm.md`](draft_agent_brainstorm.md) | The downstream modeling problem this dataset feeds |
