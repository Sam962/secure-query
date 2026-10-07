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
    """Reason to refuse when the plan never returns a concept the question asks about."""
    return missing_concepts(question, plan_concepts(plan, catalog), catalog)


def missing_concepts(question: str, used: set[Concept], catalog: Catalog) -> str | None:
    """Reason to refuse after planning, or None if the plan covers the question.

    A term is satisfied if *any* catalog entity it could refer to appears in the
    plan, because "customer" may mean the Customer table or Invoice.CustomerId
    and either reading answers the user.
    """
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
    return lists_instead_of_count(question, aggregated=bool(plan.aggregations))


def lists_instead_of_count(question: str, *, aggregated: bool) -> str | None:
    if aggregated or not _COUNT_RE.search(question):
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
    return missing_average(question, averaged=any(a.fn == "avg" for a in plan.aggregations))


def missing_average(question: str, *, averaged: bool) -> str | None:
    if averaged or not _AVERAGE_RE.search(question):
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


_COUNT_NOUN_RE = re.compile(
    r"\b(?:how many|number of|count of)\s+(?:distinct\s+|different\s+|unique\s+)?([a-z]+)",
    re.IGNORECASE,
)


def counted_other_entity(question: str, plan: LogicalPlan, catalog: Catalog) -> str | None:
    """Reason to refuse when "how many X" counts rows of a table that is not X.

    COUNT(*) counts the plan's source rows. "How many products do we sell?"
    answered by counting order lines is a real number for a different noun. The
    source is accepted when X names it, or when another word in the question
    names it through the catalog ("how many tracks were *sold*" may count sale
    lines when the catalog maps "sold" to that table).
    """
    match = _COUNT_NOUN_RE.search(question)
    if match is None:
        return None
    counted = {plan.source for a in plan.aggregations if a.fn == "count" and a.column is None}
    counted |= {a.column.table_id for a in plan.aggregations if a.fn == "count" and a.column}
    if not counted:
        return None
    noun = match.group(1).lower()
    vocabulary = catalog_vocabulary(catalog)
    display = {t.name: t.display_column for t in catalog.tables}
    noun_terms = _expand_term(noun)
    entity_tables = {
        c.table
        for term in noun_terms
        for c in vocabulary.get(term, set())
        if c.column is None or display.get(c.table) == f"{c.table}.{c.column}"
    }
    if not entity_tables or counted <= entity_tables:
        return None
    named = {
        c.table
        for term in question_terms(question) - noun_terms
        for c in vocabulary.get(term, set())
    }
    if counted <= entity_tables | named:
        return None
    return (
        f'The question asks how many {noun}, but this plan counts '
        f"{', '.join(sorted(counted))} rows. Refusing rather than counting something else."
    )


_VALUE_WORD_RE = re.compile(r"\b(?:[A-Z][\w'&]*|(?:19|20)\d\d)\b")


def dropped_literals(question: str, plan: LogicalPlan, catalog: Catalog) -> str | None:
    """Reason to refuse when the question names a value no filter uses, or vice versa.

    "Which AC/DC album has the most tracks?" answered without an AC/DC filter
    is a confident answer to a different question.
    """
    values: list[str] = []
    for filt in plan.filters:
        for name in ("value", "low", "high", "pattern"):
            lit = getattr(filt, name, None)
            if isinstance(lit, LiteralValue):
                values.append(str(lit.value))
        values += [str(lit.value) for lit in getattr(filt, "values", [])]
    strings = [lit for filt in plan.filters for lit in _string_literals(filt)]
    numbers = [lit.value for filt in plan.filters for lit in _numeric_literals(filt)]
    numbers += [h.value.value for h in plan.having]
    return unmatched_values(question, values, strings, numbers, catalog)


def unmatched_values(
    question: str,
    values: list[str],
    strings: list[str],
    numbers: list[float | int],
    catalog: Catalog,
) -> str | None:
    """Named values must be filtered on, and filters may not invent values.

    `values`: every filter literal (proper nouns and years in the question
    must appear among them; sentence-initial capitals, catalog terms and
    calendar words are skipped). `strings` / `numbers`: filter literals that
    must themselves appear in the question.
    """
    haystack = " ".join(v.lower() for v in values)
    vocabulary = catalog_vocabulary(catalog)
    identifiers = _identifier_parts(catalog)

    missing: list[str] = []
    for match in _VALUE_WORD_RE.finditer(question):
        word = re.sub(r"['’]s$", "", match.group(0))  # Kyle's -> Kyle
        before = question[: match.start()].rstrip()
        if not before or before[-1] in ".?!:" or len(word) < 2:
            continue  # sentence-initial capital, not a proper noun
        lower = word.lower()
        if lower in _CALENDAR_TERMS or _term_in_vocabulary(lower, vocabulary):
            continue
        if _expand_term(lower) & identifiers:
            continue  # short catalog words the vocabulary skips: "TV", "IDs"
        if lower in haystack or _same_stem(lower, haystack):
            continue
        missing.append(word)
    if missing:
        shown = ", ".join(f'"{w}"' for w in dict.fromkeys(missing))
        return (
            f"The question names {shown}, but no filter in this plan uses it. "
            "Refusing rather than answering without that condition."
        )

    asked = question.lower()
    said = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", question.replace(",", ""))}
    said |= {float(_NUMBER_WORDS[w]) for w in re.findall(r"[a-z]+", asked) if w in _NUMBER_WORDS}
    invented = [v for v in strings if v.strip("%").lower() not in asked]
    invented += [str(n) for n in numbers if float(n) not in said]
    if invented:
        shown = ", ".join(f'"{v}"' for v in dict.fromkeys(invented))
        return (
            f"This plan filters on {shown}, which the question does not mention; "
            "no filter in this plan may add conditions the user did not ask for."
        )
    return None


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "dozen": 12, "twenty": 20, "hundred": 100, "thousand": 1000,
}


def _identifier_parts(catalog: Catalog) -> set[str]:
    """Every word part of every table and column name, however short."""
    parts: set[str] = set()
    for table in catalog.tables:
        for name in (table.name, *(c.name for c in table.columns)):
            spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower()
            parts.update(p for p in re.split(r"[^a-z0-9]+", spaced) if p)
    return parts


def _same_stem(word: str, haystack: str) -> bool:
    """"asian" / "asia", "european" / "europe": a value and its adjective form."""
    if len(word) < 5:
        return False
    return any(
        len(token) >= 4 and (word.startswith(token) or token.startswith(word[:-2]))
        for token in re.findall(r"[a-z]+", haystack)
    )


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
