# Superset AI Agent extension

This directory follows Apache Superset's native extension layout for packaging
and backend API registration. Superset 6.1 does not expose the newer chat
contribution API, so the frontend adds an "AI Agent" item to Superset's navbar,
between "SQL" and the Semantic Layer menus, for every signed-in user, and
renders its own page below Superset's header at `/ai-agent/`.

## The AI Agent page

The page works like a chatbot: the user's conversations on the left, grouped by
when they were last active, and the open one on the right. A conversation
(`text_to_bi_agent_chat_session`) is about one dashboard, chosen when it starts
and fixed afterwards; a question cannot be asked without one. Opened from a
dashboard, the navbar link is `/ai-agent/?dashboard=<id or slug>` and that
dashboard is preselected; `/ai-agent/?session=<id>` reopens a conversation.
Every turn is a row of the question log with its `session_id` and the
`tool_calls` it ran, so a reopened conversation shows each answer and step
again. Deleting a conversation keeps its questions in the log with no session.

## Asking the user back

What a qualitative concept or an undefined term means is often a personal sense
("gió mạnh", "đông khách"), so the agent asks the user instead of an
administrator. When a term or rule is missing, `propose_missing_items` drafts
one question per concept with two to four options (the likeliest first; a rule
option is a measurable threshold on the found metric). The page shows them as a
card, one question at a time: the user picks an option (keys 1–N, Enter) or
writes their own under "Khác", then sends every answer at once, or skips. The
answers are saved per user and dashboard (`text_to_bi_agent_user_definition`)
and the question is answered again with them. While saving, one model call per
confirmed rule (`modules/definitions/services/conditions.py`, in parallel) turns its text
into checked filters on one dataset (`conditions`: column, label, operator,
value), so every later question uses that exact threshold, as with an approved
rule; a definition no filter can express keeps `conditions` NULL and is followed
from its text. Editing a definition's text structures it again. Every later
conversation of that user on that dashboard uses them; the "Cách hiểu của bạn"
panel lists, edits and deletes them. An approved term or rule always takes precedence over a
user's definition of the same concept.

Administrators never edit users' definitions. In the Semantic Layer's "Xử lý câu
hỏi" module, the "Cách hiểu của người dùng" tab groups them by concept and
counts the users behind each, as a signal of where a shared definition would
help. "Tạo thuật ngữ chung" / "Tạo expert rule chung" only prefills the name
(and an example question); the shared definition and threshold are the
business experts', not a copy of the users'. Saving it marks the definitions
`promoted`; "Giữ là cách hiểu cá nhân" marks them `ignored`; a user editing a
definition puts it back to `pending`. A missing metric still goes to
administrators, in the "Thiếu metric" tab.

## What the agent does

For each question the agent works out which governed business content it is
about, runs the approved metrics through the Semantic Layer, answers with the
numbers, and logs the question so administrators can fill the gaps:

1. The browser sends the question and its conversation; the server reads the
   conversation's last three answered turns from the log, and what the last
   of them queried (`last_queries`: its `query_result`, or each answered
   part of `part_results`, kept as dataset, metrics, dimensions, filters
   without a rule's, compared period, scope and first five rows). The
   classifier sees that turn's scope; a follow-up (`follow_up` other than
   `new`) answered as one question gives the whole query to
   `understand_question`, which keeps its filters and values unless the
   question changes them, and a plan on the same dataset with no time filter
   gets that turn's time filters, noted in the result.
2. `classify_question` is one model call that sees only the question, the
   conversation and the dashboard's title, never the catalog, and labels the
   question on the four axes of `docs/specs/taxonomy-cau-hoi.md`
   (`modules/chat/services/triage.py`): whether data answers it, the intent of
   each part, how it splits into single-intent `sub_questions` (at most four,
   `parallel` or `sequential` through `depends_on`), and how it relates to the
   previous turn. `checked_triage` makes the labels consistent instead of
   trusting them. Only an `answerable` question goes on. A `blocked` one
   (`injection`, `write`, `pii`, `access`, `harmful`) gets a fixed reply and
   reaches no other model call; `chitchat`, `out_of_domain`, `unsupported` and
   `unclear` get a one to three sentence reply (`DECLINED_REPLY`) without the
   catalog; `metadata` (how a metric is computed, what data there is, up to
   when) is answered from the catalog (`DATA_DESCRIPTION`) without a query.
   Parts set aside from an answerable question (`set_aside`) are mentioned at
   the end of its answer.
3. `load_semantic_catalog` reads the Semantic Layer's consumer endpoints
   (`/semantic-catalog`, `/glossary/discovery`, `/expert-rules/discover`, then
   `/dataset/<id>/dimension-members` for each dataset with approved metrics) in
   parallel over HTTP inside the container, forwarding the user's session
   cookie, so the agent only sees approved content on datasets that user may
   access. The catalog is cached per user for 60 seconds.
4. `understand_question` is one model call that does what used to be four
   steps. It reads the classifier's sub-question, so greetings and parts set
   aside are gone. A question split into several (`parallel` or `sequential`)
   runs steps 4 to 6 once per sub-question, each understood without the
   conversation since it already stands alone: the independent ones in threads
   of their own, side by side, and a sequential one once the sub-questions it
   `depends_on` have answered, with `{{q<id>}}` replaced by the first row's
   first dimension (or first figure) of that answer. Its tool calls carry
   `part` and an ID of their own (`q<id>_call_<n>`), and it ends with one
   `part_result` event (`id`, `question`, `result`, `query_error`, `skipped`).
   A failing sub-question keeps its error and the others go on; one whose
   source found nothing is `skipped`; only when every one fails does the
   question fail. Before the call, `chat/services/retrieval.py` ranks the
   approved metrics against the question without a model (whole names, codes
   and linked glossary terms first, then rare words of names and definitions;
   a catalog over 40 metrics lists only its 15 best plus what the turn before
   queried) and finds up to three questions answered before with an approved
   metric that share at least half their words with it (the FAQ, from the
   question log); both go into the prompt and into the step's `retrieved`
   argument (docs/specs/truy-hoi-metric-va-faq.md). The sub-question's intent
   goes into the prompt too, and `chat/services/intents.py` checks each plan's
   form against it before it runs: a trend not split by time gets the metric's
   time column, a ranking with no order is ordered by its first metric, and a
   ranking, list or breakdown with no grouping, or a comparison with nothing to
   compare, goes back to the model as an invalid plan (`shaped` argument of
   `query_metrics`; docs/specs/taxonomy-cau-hoi.md §5.1).
   The call rewrites a follow-up into a standalone question;
   picks the glossary terms, metrics, expert rules and the user's own
   definitions the question is
   about (IDs it invents are dropped); lists `undefined_concepts` (business
   concepts nothing defines; time, place and aggregation words do not count);
   classifies the question as `qualitative` (worded by perception with a vague
   threshold, so it needs an expert rule) or `quantitative`; and plans the
   metric query. The plan names the dataset, metrics, dimensions and filters
   tagged by source: `time` (dates computed from today in Vietnam's time),
   `place` (key values found through the dataset's labelled values, so "Hà Nội"
   becomes `thanh_pho_key = 1`), `user_definition` (only for a user's definition
   without stored conditions) and `question` (a threshold the asker gave). It
   may also rank (`order_by` a metric and `limit` 1–100 groups) and compare
   (`comparison`: the same query over another period, such as last week). The
   catalog's `time_ranges` (first and last date of each dataset date column, from
   `dimension-members`) let it pick dates the data covers. The model runs with
   `reasoning_effort="low"`.
5. When only a metric is missing, the agent climbs from approved content to the
   data model (`run_agent(..., builder=MetricBuilder(...))`). A missing term or
   rule is always asked back first.
   - `load_data_model` reads the Semantic Layer's `/agent-sources`: aggregate
     datasets (`dataset_type='aggregate'`, the fact/dim tables) with their
     columns, data dictionary text and saved formulas, and base datasets
     (`'base'`, the dm/tb tables) with foreign keys. Short text code columns
     carry their stored `values` (such as `THANH_CONG|THAT_BAI`), so a formula
     filters on a real value rather than a guess.
   - `draft_metric` writes one metric on an aggregate column, following the
     formulas already there (for example `loai_quan_sat = 'DU_BAO_NGAY'` for daily
     figures), plus the query. `create_metric` posts it to `/agent-metrics`,
     which checks the formula, reuses a metric with the same formula, or saves a
     new one as an `ai_agent` candidate. The question is answered at once with a
     `provisional` result ("Tạm tính · chờ duyệt"), and still logged as
     `needs_review` until an administrator approves the metric. The result
     carries `formula`: the expression and, from `modules/chat/services/formula.py`, a
     sentence of text and column parts ("Đếm số Mã lần nạp, chỉ tính các dòng
     có Trạng thái là “THAT_BAI”") that the page shows with data dictionary
     labels and switches to column names on request; a formula beyond the
     contract grammar shows as its expression. When the figure is empty or all
     zero, the agent runs the metric once more over all the data with no
     filter; if that also gives nothing, `doubt` (`empty` or `zero`) warns the
     asker that the formula may be wrong instead of stating the figure.
   - If no aggregate column fits, `draft_dataset` writes one read-only SELECT
     over base datasets plus the metric to put on it, and `propose_dataset`
     posts it to `/agent-dataset-proposals`. Nothing runs: an administrator
     reviews it in "Xử lý câu hỏi" → "Đề xuất dataset", where creating it makes
     a Superset virtual dataset, classes it as aggregate, and adds the metric as
     an AI Agent candidate, so the next such question takes the previous step.
     Creating it also fills the new dataset's data dictionary keys: a column
     read unchanged from a key column (PK, FK or unique) of a base dataset is an
     FK referencing that column, and stays PK or unique only while the query
     keeps the base rows one to one (read from the FROM table, no GROUP BY or
     DISTINCT, every join on a unique column). Data lineage is inferred from
     those references; a source no column references gets a manual `flow` edge
     into the new dataset.
   - Otherwise, or when the Semantic Layer refuses a draft,
     `propose_missing_items` drafts the questions back to the user
     (`clarifications`) and a metric proposal for administrators
     (`suggestions`), and no query runs. A failure here is reported but does not
     stop the answer.
6. Otherwise `query_metrics` checks the plan against the catalog
   (`modules/chat/services/query.py`): only listed columns, filterable or groupable as
   used, and the supported operators. An approved expert rule's filters come
   from the rule's own conditions, never from the model's copy of them, and a
   rule with a condition no filter can express stops the query. A user's
   definition with stored `conditions` is applied the same way and replaces the
   model's filters on its columns. Ranking, the row limit and the comparison run
   on the agent side, because the execute endpoint takes only dimensions,
   filters and a time grain: a comparison needs time filters and no date dimension, and runs every
   metric again over the other period, adding `<metric>__compare` columns. The
   requested dates are checked against `time_ranges` (`coverage`: `full`,
   `partial` or `none`), so a period outside the data is told apart from one
   where nothing matched. Each metric then
   runs through the Semantic Layer's governed
   `.../metrics/<id>/semantic-query/execute` endpoint as the user (cookie and
   CSRF token forwarded), in parallel, and the answers merge into one table: a
   row per dimension value, a column per metric, key values shown by their
   labels. A `result` event carries it to the browser before the answer
   streams. A plan that fails or finds nothing is mended and run again, at
   most three runs in all (`modules/chat/services/repair.py`,
   `docs/specs/xu-ly-loi-truy-van.md`). No row with a text filter
   (`value_mismatch`): `explore_values` groups the same metric by that column,
   as the user and with no filter, to read the values the data holds, and a
   value matching exactly one of them without accents or case ("Huế" is
   "Hue") replaces the asked one with no model call, noted in the result's
   `notes`. A plan the checks reject (`invalid_plan`), a query Superset
   refuses or fails (`rejected_query`, 400 or 500), or a spelling no stored
   value matches goes to `repair_query`, a model call given the reason and the
   stored values; a Semantic Layer that did not answer (`transient`) is asked
   the same again. No access (`not_allowed`), a period outside the data and no
   matching row are not tried again. What is left after the last run becomes
   `query_error`, and `attempts` lets the answer say why.
   Each catalog metric carries its approved `query_defaults` (grain, row
   kind, time column, default time window and default filters), which the
   Semantic Layer applies itself unless a filter names the same column. The
   model sees them, so it adds no time filter when the question names no
   period, and sets `time_grain` only when the question splits time at
   another unit; `plan_query` sends it only when every metric is split by
   its own time column, and raises a grain finer than the data to the
   data's grain with a note. The `applied_defaults` of each answer become
   the result's `defaults`, and a default time window adds a note naming
   the period, so the answer says which period it used.
7. The final answer streams as tokens, written for a business user from the
   numbers in the result: the direct answer first, then the criterion and
   whether it is the business's shared definition or the user's own, "theo dự
   báo" for dates after today.

The stream opens with a `session` event naming the conversation (new or
existing). Each step then reaches the browser as a tool call (`tool_start`,
`tool_end`), then `result` (or `part_result` per sub-question) when a query ran,
`token` events, and a closing
`done` event. A question is `resolved` when it found at least one metric, has no
`undefined_concepts`, and has an approved or user's expert rule if it is
qualitative; otherwise it is `needs_review` with `missing` listing what was not
found. A question answered without a query lacks nothing: `metadata` and
`chitchat` are `resolved`, `out_of_domain`, `unsupported` and `unclear` are
`declined`, and `blocked` is `blocked`. A question split into sub-questions
lacks what any of them lacks, and its glossary terms, metrics, rules,
clarifications and suggestions gather theirs. `done` also carries
`clarifications`, the `user_definitions` used, `result`, `query_error` and, for
a split question, `parts` in the classifier's order. The log stores `triage`
(the classifier's labels, NULL on older questions), `requires_expert_rule`
(NULL on older questions), `suggestions`, `clarifications` with
`clarification_state` (`pending`, `confirmed`, `dismissed`), `retry_of` on the
answer given again after a confirmation, `query_result`, and `part_results` for
a split question. The page shows each sub-question's table or chart under its
number and text, and tags each step with the sub-question it belongs to. A metric proposal opens on the Metrics page,
because metrics are created in the Superset dataset editor.

The page shows a result as one number per metric, a line or columns over time
(a line per group, with a legend, for up to eight groups), ranked horizontal
bars across groups (in the asked order when ranked), or a table; the chart and
table views switch in place. A comparison shows each number with the other
period's value and the change, and the table gains "kỳ so sánh" and "Thay đổi"
columns. The heading names the ranking, the comparison and any gap in the data's
date range. The conditions used appear as chips naming their source, and "Xem
truy vấn" opens the request sent for each metric and period with the SQL the
Semantic Layer ran, which it returns to administrators only. The "Cách hiểu của
bạn" panel shows a definition's stored conditions as chips.

## Extension structure

```text
extensions/ai-agents/
├── extension.json
├── backend/
│   ├── src/text_to_bi/superset_ai_agent/
│   │   ├── entrypoint.py                 # registers the APIs and the /ai-agent/ page
│   │   ├── migrations.py                 # ensure_agent_tables: creates and upgrades every module's tables
│   │   ├── common/                       # BaseModel, BaseRepository, AgentError and its abstract roots, formatter
│   │   ├── core/                         # Settings, OpenRouter chat model, admin and CSRF checks, handle_errors
│   │   └── modules/
│   │       ├── chat/                     # pipeline, prompts, query planner, formula text, /chat/stream
│   │       ├── sessions/                 # conversations: /sessions API and the /ai-agent/ page view
│   │       ├── questions/                # the question log, /questions/review, /questions/<id>/handling
│   │       ├── definitions/              # users' definitions: their own, their conditions, the admin review
│   │       ├── schema_link/              # join paths and SQL over base tables, the analysis lab
│   │       ├── semantic_layer/           # the Semantic Layer read and called over HTTP as the user, no tables
│   │       └── superset/                 # Superset's dashboards and users: read only, no tables
│   └── tests/
└── frontend/
    ├── src/index.tsx
    ├── src/integration/installAIAgent.tsx         # navbar item, page mount
    ├── src/features/chat/AgentPage.tsx            # conversations and the open conversation
    ├── src/features/chat/components/ChatTurn.tsx  # one turn: tool calls, answer, outcome, question card
    ├── src/features/chat/components/DefinitionsPanel.tsx  # "Cách hiểu của bạn"
    ├── src/features/chat/components/QueryResultView.tsx   # number, chart or table, conditions, queries
    ├── src/shared/api/chat.ts
    └── webpack.config.js
```

The backend follows the layers and module layout of `docs/handbook/kien-truc.md`
and the error, transaction and CSRF rules of `docs/handbook/hanh-vi.md`, as the
Semantic Layer does. A module that owns tables has `models/`, `repositories/`,
`dependencies.py`, `exceptions.py`, `services/` and `routes/`; `semantic_layer`
and `superset` only have services. The tests cover the services that import
neither Flask nor Superset, so keep pure logic (parsing, planning, the pipeline)
apart from the services that use a repository.

## API

- `POST /extensions/text-to-bi/superset-ai-agent/chat/stream` takes
  `{question, session_id}` to continue a conversation, or
  `{question, dashboard_id}` to start one about a dashboard the user may open,
  and answers with Server-Sent Events. It needs `can read on
  text_to_bi_chat_api` and a CSRF token.
- `GET .../sessions/` lists the user's conversations; `GET .../sessions/<id>`
  returns one with its turns; `PUT .../sessions/<id>` takes `{title}`;
  `DELETE .../sessions/<id>` deletes it (CSRF for both). A user only sees their
  own conversations. `GET .../sessions/<id>` also returns the user's
  definitions on its dashboard.
- `PUT .../sessions/<id>/questions/<question_id>/clarification` takes
  `{state: "confirmed", definitions: [{kind, concept, name, definition}]}` or
  `{state: "dismissed"}` (CSRF); confirming a concept again replaces the user's
  earlier definition of it on that dashboard.
- `GET .../definitions/?dashboard_id=` lists the user's definitions;
  `PUT .../definitions/<id>` takes `{name, definition}` and structures the
  conditions again when the text changed; `DELETE
  .../definitions/<id>` forgets one (CSRF, own definitions only).
- `GET .../definitions/review` lists every user's definitions; `PUT
  .../definitions/review` takes `{ids, status}` with `pending`, `promoted` or
  `ignored` (admin only, CSRF).
- `GET /ai-agent/` serves the page to users with `can read on
  text_to_bi_chat_api`.
- `GET /extensions/text-to-bi/superset-ai-agent/questions/review?handling=&missing=`
  lists `needs_review` questions with their handling (admin only).
- `PUT /extensions/text-to-bi/superset-ai-agent/questions/<id>/handling` takes
  `{status, note}`, where status is `pending`, `to_create`, `created` or
  `ignored` (admin only, CSRF).

The Semantic Layer's "Xử lý câu hỏi" module uses the question and definition review endpoints.

## Question log and the overview dashboard

`text_to_bi_agent_question`, `text_to_bi_agent_question_handling`,
`text_to_bi_agent_chat_session` and `text_to_bi_agent_user_definition` live in
Superset's metadata database and are created on first use.
`simulation/superset/bootstrap_governance.py` (run by `superset-init`) publishes read-only
views of them in the `text_to_bi_reporting` schema, a login role that may read
only that schema, the "Text to BI Governance" connection, and the
"Tổng quan AI Agent" dashboard that the Semantic Layer's "Tổng quan" module
embeds. That connection is flagged `text_to_bi_internal`, so the Semantic Layer
leaves it out of its database and dataset pickers. Rerun the script after
changing the views or charts:

```sh
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps superset-init python /app/pythonpath/bootstrap_governance.py
```

## Build and install

There is no build script for this extension yet. Bundle it the same way
`scripts/build.py` bundles the Semantic Layer (run `superset-extensions bundle`
in this directory, resolving `npm` to `npm.cmd` on Windows), write the bundle to
`extensions/superset-ai-agent-0.1.0.supx`, then copy it into the
`superset_extensions` volume and restart Superset as the root `AGENTS.md`
describes.

`OPENROUTER_API_KEY` comes from the repository root's `.env`, which Compose loads
into the Superset containers. Keep that file out of commits and bundles.

## Schema link (in progress)

`modules/schema_link/` answers over base tables when no metric can
(`docs/specs/schema-link.md`). Its `services/graph.py` holds the Semantic Layer's
`/schema-graph`, read as the user through the `semantic_layer` module and cached
60 seconds; `goal.py` checks a goal
(measures, group by, filters, kinds of rows) against it, turning an indicator
code such as `nhiet_do_c` into the values table's column, the catalog's code
filter and its aggregation; `paths.py` finds the join tree, Dijkstra from the
root along many-to-one edges first and a directed Steiner tree otherwise;
`sql.py` builds the SQL as a sqlglot tree; `analysis.py` tells the run as the
four stages the page draws (goal, nodes, graph, chosen path).

A question reaches a goal in two steps (`analysis.link_question`). `linking.py`
ranks, with no model, what the question names: every column (by its label,
synonyms, name and description), stored code value, indicator (by name and by
code without its unit, "tong luong mua" for `tong_luong_mua_mm`) and table is
matched against the question with accents and case set aside; a phrase named
whole weighs by its length, so "nhiệt độ cao nhất" beats "nhiệt độ", a shared
word by its rarity, and one word alone counts for nothing. A catalog of
indicators is not matched on its values (how its indicators aggregate). The
top 6 tables, 12 columns and 5 indicators are kept, plus the tables a measured
table reaches by keys within two joins, where time, place and grouping live.
`drafting.py` then makes one model call (`LINK_GOAL`) that picks among those
candidates only: measures (an indicator or a measure column, or a row count),
group by (time with a grain, or a label or dimension), filters (values as
stored, dates from today) and the kind of row; `resolve_goal` checks it
against the graph. Administrators try it at `/ai-agent/?lab=schema-link`
("Thử phân tích dữ liệu"): a question shows its candidates with scores and
why, the drafted goal (left in the editor to adjust) and the four stages; a
goal can still be written by hand. Nothing runs the SQL yet, and chat
questions do not reach it yet.
`tests/fixtures/schema_graph_weather.json` is the sample warehouse's graph.
