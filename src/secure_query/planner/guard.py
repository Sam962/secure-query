"""Deterministic guards that compare the question against the catalog.

Everything here runs without the model. That is the point: prompting reduced
the planner's substitution rate but never to zero, and a control that depends
on the model cannot bound the model's failures. These checks always fire.

Two failure modes are covered, both observed in the accuracy suite:

    restricted request  the question names a high-PII column. Refuse up front
                        and say so, rather than silently returning a narrower
                        answer that omits the field the user asked for.

    dropped concept     the plan never touches something the question named,
                        so the system answered a different, easier question.

Both guards fail toward refusal. Over-refusing costs coverage, which is
measurable and recoverable; under-refusing ships a wrong number, which is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import ColumnRef, LiteralValue, LogicalPlan

_MIN_PART_LEN = 4

# Words that are catalog column names *and* ordinary English quantifiers.
# "How many invoices are there in total?" names Invoice.Total without asking
# for it, so treating these as requested concepts causes false refusals.
# "name" is deliberately absent: someone who says "name" in a data question
# almost always wants a name column, and returning opaque ids instead is the
# error we want caught.
_GENERIC_TERMS = frozenset(
    {
        "amount",
        "average",
        "code",
        "count",
        "data",
        "date",
        "first",
        "last",
        "many",
        "number",
        "sum",
        "time",
        "total",
        "type",
        "value",
    }
)

# Question glue words — not evidence of an out-of-scope domain concept.
_STOP_WORDS = frozenset(
    {
        "what",
        "which",
        "how",
        "show",
        "list",
        "top",
        "just",
        "the",
        "one",
        "all",
        "every",
        "each",
        "across",
        "there",
        "our",
        "their",
        "they",
        "them",
        "have",
        "has",
        "had",
        "does",
        "did",
        "most",
        "least",
        "highest",
        "lowest",
        "single",
        "much",
        "more",
        "than",
        "from",
        "with",
        "that",
        "this",
        "were",
        "was",
        "are",
        "been",
        "being",
        "into",
        "over",
        "under",
        "between",
        "either",
        "only",
        "also",
        "group",
        "grouped",
        "grouping",
        "breakdown",
        "anything",
        "something",
        "based",
        "issued",
        "billed",
        "sold",
        "earn",
        "earns",
        "earned",
        "earning",
        "located",
        "appear",
        "appearances",
        "year",
        "years",
        "quarter",
        "month",
        "march",
        "limit",
        "limited",
        "rows",
        "row",
        "every",
        "work",
        "works",
        "defined",
        "exist",
        "exists",
        "catalog",
        "database",
        "higher",
        "lower",
        "greater",
        "appear",
        "distinct",
        "either",
        "both",
        "per",
        "each",
        "largest",
        "smallest",
        "biggest",
        "big",
        "little",
        "identify",
        "link",
        "links",
        "item",
        "items",
        "people",
        "roughly",
        "about",
        "approximately",
        "appear",
        "appears",
        "appeared",
    }
)


@dataclass(frozen=True)
class Concept:
    """A catalog entity a question might be referring to."""

    table: str
    column: str | None = None

    def __str__(self) -> str:
        return self.table if self.column is None else f"{self.table}.{self.column}"


def restricted_request(question: str, catalog: Catalog) -> str | None:
    """Reason to refuse before planning, or None to proceed.

    Only refuses on terms whose every possible meaning is a high-PII column.
    A term shared with an ordinary column is not evidence of a restricted
    request: "billing" appears in both `BillingAddress` (restricted) and
    `BillingCountry` (not), so asking about billing country must still work.
    """
    asked = question_terms(question)
    matched: dict[str, set[Concept]] = {}
    sensitive: set[str] = set()

    for table in catalog.tables:
        for col in table.columns:
            concept = Concept(table=table.name, column=col.name)
            for term in concept_terms(col.name) & asked:
                matched.setdefault(term, set()).add(concept)
                if col.pii_risk == "high":
                    sensitive.add(str(concept))

    restricted = {
        term: concepts
        for term, concepts in matched.items()
        if concepts and all(str(c) in sensitive for c in concepts)
    }
    if not restricted:
        return None
    return (
        f"That question asks for restricted data: {_describe(restricted)}. "
        "Those fields are not available through this interface."
    )


def dropped_concepts(question: str, plan: LogicalPlan, catalog: Catalog) -> str | None:
    """Reason to refuse after planning, or None if the plan covers the question.

    A term is satisfied if *any* catalog entity it could refer to appears in the
    plan, because "customer" may mean the Customer table or Invoice.CustomerId
    and either reading answers the user.
    """
    used = plan_concepts(plan, catalog)
    touched = {c.table for c in used}
    display = {
        Concept(table=t.name, column=t.display_column.split(".", 1)[1])
        for t in catalog.tables
        if t.display_column
    }
    vocabulary = catalog_vocabulary(catalog)
    literals = _filter_literal_terms(question)
    missing: dict[str, set[Concept]] = {}

    for term in question_terms(question):
        if term in _GENERIC_TERMS or term in _STOP_WORDS or term in literals:
            continue
        candidates = set().union(*(vocabulary.get(v, set()) for v in _expand_term(term)))
        if not candidates:
            continue
        if candidates & used:
            continue
        # An entity word ("tracks", "genre") is covered when the plan touches a
        # table it can mean; an attribute word ("revenue") needs its column.
        # A table's display column names the entity itself. Joins are
        # catalog-resolved, so touching a table means reading it.
        if any(c.column is None or c in display for c in candidates) and {
            c.table for c in candidates
        } & touched:
            continue
        missing[term] = candidates

    if not missing:
        return None
    return (
        f"The question asks about {_describe(missing)}, which this plan never returns. "
        "Refusing rather than answering a narrower question."
    )


def _describe(missing: dict[str, set[Concept]], *, examples: int = 2) -> str:
    """Name the words the user said, not every column they could have meant.

    A common word like "name" matches nine columns across the catalog; listing
    all of them buries the one thing the reader needs, which is which part of
    their question went unanswered.
    """
    # "address" and its singular-stripped form "addres" hit the same columns;
    # report the word the user actually typed, once.
    by_candidates: dict[frozenset[Concept], str] = {}
    for term, candidates in missing.items():
        key = frozenset(candidates)
        best = by_candidates.get(key)
        if best is None or (len(term), term) > (len(best), best):
            by_candidates[key] = term

    parts: list[str] = []
    for key, term in sorted(by_candidates.items(), key=lambda kv: kv[1]):
        candidates = sorted(str(c) for c in key)
        shown = ", ".join(candidates[:examples])
        extra = len(candidates) - examples
        if extra > 0:
            shown = f"{shown}, and {extra} more"
        parts.append(f'"{term}" ({shown})')
    return "; ".join(parts)


# Operations the IR cannot express: arithmetic between aggregates, windows,
# and comparisons against an aggregate. These words are about the query
# language, not any dataset. Approved ratio metrics return before this check.
_INEXPRESSIBLE_RE = re.compile(
    r"\b(percent|percentage|share|ratio|proportion|growth|grow|grew|change|changed|"
    r"increase|decrease|difference|month over month|year over year|"
    r"than (?:the )?(?:average|mean)|above average|below average)\b",
    re.IGNORECASE,
)


def inexpressible_request(question: str) -> str | None:
    """Reason to refuse when the question needs math the LogicalPlan cannot do."""
    match = _INEXPRESSIBLE_RE.search(question)
    if match is None:
        return None
    return (
        f'The question asks for a "{match.group(0)}", which needs arithmetic between '
        "aggregates or across periods; no plan here can compute it exactly. "
        "Refusing rather than answering a nearby question."
    )


_AVERAGE_RE = re.compile(r"\b(average|averages|avg|mean)\b", re.IGNORECASE)


def unrelated_metric(question: str, metric_id: str) -> str | None:
    """Reason to refuse when an approved metric shares no word with the question.

    Metrics bypass plan-shape guards, so a model that picks `line_item_revenue`
    for "how many tracks" would otherwise return a confident wrong number.
    """
    asked = question_terms(question)
    words = {w for w in re.split(r"[^a-z0-9]+", metric_id.lower()) if len(w) >= _MIN_PART_LEN}
    if any(_expand_term(w) & asked for w in words):
        return None
    return (
        f"The approved metric {metric_id!r} does not match what the question asks for. "
        "Refusing rather than answering a different question."
    )


_COUNT_RE = re.compile(r"\b(how many|number of|count of)\b", re.IGNORECASE)


def dropped_count(question: str, plan: LogicalPlan) -> str | None:
    """Reason to refuse when the question asks for a count and the plan lists rows."""
    if plan.aggregations or not _COUNT_RE.search(question):
        return None
    return (
        "The question asks how many, but this plan dropped the count and lists rows. "
        "Refusing rather than answering a different question."
    )


def dropped_average(question: str, plan: LogicalPlan) -> str | None:
    """Reason to refuse when the question asks for an average and the plan has none.

    "average" is in _GENERIC_TERMS because it names no catalog column, so
    dropped_concepts cannot see it. Without this check "average revenue per
    customer" can come back as a SUM and a COUNT side by side: real numbers,
    but not the one asked for. Approved ratio metrics never reach this guard.
    """
    if not _AVERAGE_RE.search(question):
        return None
    if any(agg.fn == "avg" for agg in plan.aggregations):
        return None
    return (
        "The question asks for an average, but this plan dropped it (no AVG is "
        "computed). Refusing rather than answering a different question."
    )


_AVG_PER_RE = re.compile(
    r"\b(?:average|averages|avg|mean)\b.*?\bper\s+([a-z]+)(?:\s+([a-z]+))?", re.IGNORECASE
)


def averaged_per_other_entity(
    question: str, plan: LogicalPlan, catalog: Catalog
) -> str | None:
    """Reason to refuse when "average X per Y" averages rows of a table other than Y.

    AVG(Total) over invoices is the average per *invoice*. "Average spend per
    customer" is a total divided by a count of customers, and grouping by
    customer does not fix it: that is each customer's average invoice. Only an
    AVG over Y's own rows ("average total per invoice") answers the question.
    "Per" is ambiguous ("average length per genre" usually means for each
    genre), so the refusal says how to ask for the grouped reading.
    """
    match = _AVG_PER_RE.search(question)
    averaged = {a.column.table_id for a in plan.aggregations if a.fn == "avg" and a.column}
    if match is None or not averaged:
        return None
    word, next_word = match.group(1).lower(), (match.group(2) or "").lower()
    terms = _expand_term(word) | ({word + next_word} if next_word else set())
    vocabulary = catalog_vocabulary(catalog)
    display = {t.name: t.display_column for t in catalog.tables}
    entity_tables = {
        c.table
        for term in terms
        for c in vocabulary.get(term, set())
        if c.column is None or display.get(c.table) == f"{c.table}.{c.column}"
    }
    if not entity_tables or averaged <= entity_tables:
        return None
    return (
        f'The question asks for an average per {word}, but this plan averages '
        f"{', '.join(sorted(averaged))} rows. A per-{word} figure is a ratio (a total "
        f"divided by a count of {word}s), which no plan here can compute. "
        f'To get one average for each {word}, ask "average … for each {word}".'
    )


_VALUE_WORD_RE = re.compile(r"\b(?:[A-Z][\w'&]*|(?:19|20)\d\d)\b")


def dropped_literals(question: str, plan: LogicalPlan, catalog: Catalog) -> str | None:
    """Reason to refuse when the question names a value no filter uses.

    "Which AC/DC album has the most tracks?" answered without an AC/DC filter
    is a confident answer to a different question. Proper nouns and years are
    filter values (the prompt says so); each must appear in some filter literal.
    The question's first word, catalog terms and calendar words are skipped.
    """
    literals: list[str] = []
    for filt in plan.filters:
        for name in ("value", "low", "high", "pattern"):
            lit = getattr(filt, name, None)
            if isinstance(lit, LiteralValue):
                literals.append(str(lit.value).lower())
        for lit in getattr(filt, "values", []):
            literals.append(str(lit.value).lower())
    haystack = " ".join(literals)
    vocabulary = catalog_vocabulary(catalog)

    missing: list[str] = []
    for match in _VALUE_WORD_RE.finditer(question):
        word = match.group(0)
        before = question[: match.start()].rstrip()
        if not before or before[-1] in ".?!:" or len(word) < 2:
            continue  # sentence-initial capital, not a proper noun
        lower = word.lower()
        if lower in _CALENDAR_TERMS or _term_in_vocabulary(lower, vocabulary):
            continue
        if lower not in haystack:
            missing.append(word)
    if missing:
        shown = ", ".join(f'"{w}"' for w in dict.fromkeys(missing))
        return (
            f"The question names {shown}, but no filter in this plan uses it. "
            "Refusing rather than answering without that condition."
        )

    # The reverse: a value the plan filters on that the question never says.
    asked = question.lower()
    numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", question.replace(",", ""))}
    invented = [
        lit
        for filt in plan.filters
        for lit in _string_literals(filt)
        if lit.strip("%").lower() not in asked
    ]
    numeric = [lit for filt in plan.filters for lit in _numeric_literals(filt)]
    numeric += [h.value for h in plan.having]
    invented += [str(lit.value) for lit in numeric if float(lit.value) not in numbers]
    if invented:
        shown = ", ".join(f'"{v}"' for v in dict.fromkeys(invented))
        return (
            f"This plan filters on {shown}, which the question does not mention; "
            "no filter in this plan may add conditions the user did not ask for."
        )
    return None


def _numeric_literals(filt: object) -> list[LiteralValue]:
    lits = [getattr(filt, n, None) for n in ("value", "low", "high")] + list(getattr(filt, "values", []))
    return [lit for lit in lits if isinstance(lit, LiteralValue) and lit.type in ("integer", "float")]


def _string_literals(filt: object) -> list[str]:
    lits = [getattr(filt, n, None) for n in ("value", "pattern")] + list(getattr(filt, "values", []))
    return [
        lit.value for lit in lits if isinstance(lit, LiteralValue) and isinstance(lit.value, str)
    ]


def _expand_term(term: str) -> set[str]:
    """Singular/plural variants so 'countries' matches catalog term 'country'."""
    variants = {term}
    if term.endswith("ies") and len(term) > 4:
        variants.add(term[:-3] + "y")
    elif term.endswith("s") and len(term) > 3 and not term.endswith("ss"):
        variants.add(term[:-1])
    return variants


def _filter_literal_terms(question: str) -> set[str]:
    """Words used as filter values (geo names, years), not domain concepts."""
    raw = [w for w in re.split(r"[^a-zA-Z0-9]+", question) if w]
    lower = [w.lower() for w in raw]
    literals: set[str] = set()
    triggers = {
        "in",
        "from",
        "to",
        "at",
        "between",
        "either",
        "or",
        "than",
        "over",
        "under",
        "above",
        "below",
        "named",
        "called",
        "like",
    }
    for i, w in enumerate(lower):
        if w.isdigit():
            literals |= _expand_term(w)
        if i > 0 and lower[i - 1] in triggers:
            literals |= _expand_term(w)
        # Proper nouns (Brazil, London, USA) are filter literals, not schema terms.
        if raw[i][:1].isupper() and w not in triggers and len(w) >= 2:
            literals |= _expand_term(w)
    return literals


def _term_in_vocabulary(term: str, vocabulary: dict[str, set[Concept]]) -> bool:
    for variant in _expand_term(term):
        if variant in vocabulary:
            return True
    return False


def question_terms(question: str) -> set[str]:
    """Normalised words from the question, plus adjacent pairs joined together.

    The pairs let "billing country" match a BillingCountry column.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", question.lower()) if w]
    terms: set[str] = set()
    for w in words:
        terms |= _expand_term(w)
    terms |= {a + b for a, b in zip(words, words[1:], strict=False)}
    return terms


def concept_terms(name: str) -> set[str]:
    """Ways a user might name a catalog entity: whole name and its word parts."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower()
    parts = [p for p in re.split(r"[^a-z0-9]+", spaced) if p]
    terms = {name.lower()}
    terms |= {p for p in parts if len(p) >= _MIN_PART_LEN}
    terms |= {t[:-1] for t in set(terms) if t.endswith("s") and len(t) > 3}
    return terms


def catalog_vocabulary(catalog: Catalog) -> dict[str, set[Concept]]:
    """Map every term a user might say to the catalog entities it could mean."""
    vocabulary: dict[str, set[Concept]] = {}

    def add(term: str, concept: Concept) -> None:
        vocabulary.setdefault(term, set()).add(concept)

    for table in catalog.tables:
        for term in concept_terms(table.name):
            add(term, Concept(table=table.name))
        for col in table.columns:
            for term in concept_terms(col.name):
                add(term, Concept(table=table.name, column=col.name))
    for syn in catalog.synonyms:
        # Whole phrase only: "music genre" must not make "music" alone mean Genre.Name.
        # Spaces are dropped to match question_terms' adjacent-word pairs.
        phrase = re.sub(r"[^a-z0-9]+", "", syn.term.lower())
        add(phrase, Concept(table=syn.table_id, column=syn.column_id))
    return vocabulary


def opaque_grouping_keys(
    question: str, plan: LogicalPlan, catalog: Catalog
) -> str | None:
    """Refuse when grouping by an FK id but the question asks for a readable label."""
    if plan.group_by is None:
        return None
    # Explicitly pinned to an id (e.g. "identify the rep by their id").
    if re.search(r"\bby\s+(their\s+)?id\b", question.lower()):
        return None

    used = plan_concepts(plan, catalog)
    for col in plan.group_by.columns:
        spec = catalog.get_column(col.table_id, col.column_id)
        if spec is None or not spec.label_for:
            continue
        label_table, label_col = spec.label_for.split(".", 1)
        label = Concept(table=label_table, column=label_col)
        if label in used:
            continue
        return (
            f"The plan groups by {col.table_id}.{col.column_id} but the question "
            f"asks for a human-readable label; group by {spec.label_for} instead."
        )
    return None


# Calendar words describe a time filter, not a business concept. The planner
# turns them into date literals; the guard must not refuse them as unknown.
_CALENDAR_TERMS = frozenset(
    {
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december",
        "half", "halves", "quarter", "quarters", "quarterly", "month", "months",
        "monthly", "week", "weeks", "weekly", "daily", "annual", "annually",
        "yearly", "today", "yesterday", "since", "before", "after", "during",
        "until", "through", "second", "third", "fourth", "date", "dates",
    }
)


def out_of_scope_request(question: str, catalog: Catalog) -> str | None:
    """Refuse when the question names concepts absent from catalog + synonyms + metrics."""
    unknown = unknown_terms(question, catalog)
    if not unknown:
        return None
    shown = ", ".join(f'"{t}"' for t in unknown[:5])
    extra = len(unknown) - 5
    if extra > 0:
        shown = f"{shown}, and {extra} more"
    return (
        f"The question mentions terms not in the approved catalog or synonyms: {shown}. "
        "Refusing rather than guessing."
    )


def unknown_terms(question: str, catalog: Catalog) -> list[str]:
    """Question words that match nothing in the catalog, synonyms or metrics.

    A word list cannot tell "suppliers" (missing data) from "bought" (ordinary
    English), so the planner gets these as a hint rather than as a refusal.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", question.lower()) if w]
    asked: set[str] = set()
    for w in words:
        asked |= _expand_term(w)
    vocabulary = catalog_vocabulary(catalog)
    literals = _filter_literal_terms(question)
    unknown: list[str] = []

    for term in sorted(asked):
        if term in _GENERIC_TERMS or term in _STOP_WORDS or len(term) < _MIN_PART_LEN:
            continue
        if term in _CALENDAR_TERMS:
            continue
        if term in literals:
            continue
        if _term_in_vocabulary(term, vocabulary):
            continue
        if any(term in concept_terms(mid) for mid in catalog.metric_ids):
            continue
        unknown.append(term)
    return unknown


def plan_concepts(plan: LogicalPlan, catalog: Catalog) -> set[Concept]:
    """Every table and column the plan reads, including its output projection."""
    from secure_query.kernel.validate import is_list_intent, safe_projection

    concepts: set[Concept] = {Concept(table=plan.source)}

    def add_column(ref: ColumnRef | None) -> None:
        if ref is not None:
            concepts.add(Concept(table=ref.table_id, column=ref.column_id))

    for join in plan.joins:
        concepts.add(Concept(table=join.right_table))
        for condition in join.conditions:
            add_column(condition.left)
            add_column(condition.right)

    for filt in plan.filters:
        add_column(filt.column)
        value = getattr(filt, "value", None)
        if isinstance(value, ColumnRef):
            add_column(value)

    if plan.group_by is not None:
        for col in plan.group_by.columns:
            add_column(col)
        for bucket in plan.group_by.time_buckets:
            add_column(bucket.column)

    for agg in plan.aggregations:
        add_column(agg.column)

    for order in plan.order_by:
        add_column(order.column)

    # A list intent returns every approved column of the tables it reads, so
    # those columns are genuinely part of the answer even if unnamed elsewhere.
    if is_list_intent(plan):
        for col in safe_projection(plan, catalog):
            add_column(col)

    return concepts
