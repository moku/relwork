"""Structured data shared across pipeline stages."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Reference(BaseModel):
    label: str = Field(description="Citation label as printed, e.g. '14'")
    title: str
    authors: str = Field(description="First author et al., as printed")
    year: int | None
    venue: str = ""
    arxiv_id: str = Field(default="", description="arXiv id if printed, else empty")
    doi: str = Field(default="", description="DOI if printed, else empty")


class ReferenceList(BaseModel):
    references: list[Reference]


class CitedWork(BaseModel):
    label: str = Field(description="Citation label, e.g. '14'")
    role: str = Field(description="Why the seed cites it, one short Korean sentence")


class RelatedWorkGroup(BaseModel):
    name: str = Field(description="Subsection or theme name used by the seed authors")
    summary: str = Field(description="Korean summary of how the seed positions this group")
    cited: list[CitedWork]


class Evaluation(BaseModel):
    benchmarks: list[str]
    models: list[str]
    metrics: list[str]


class SeedProfile(BaseModel):
    problem: str = Field(description="Problem the paper addresses (Korean)")
    contributions: list[str] = Field(description="Core contributions (Korean)")
    method_components: list[str] = Field(description="Key method building blocks (Korean)")
    evaluation: Evaluation
    baselines: list[str] = Field(description="Methods it compares against")
    limitations: list[str] = Field(description="Limitations the authors admit (Korean)")
    related_work_groups: list[RelatedWorkGroup]


class Facet(BaseModel):
    id: str = Field(description="Short id: F1, F2, ...")
    origin: str = Field(description="'seed' (axis of the seed's own contribution) or 'context' (axis where the user's research departs from the seed)")
    priority: str = Field(description="'high' | 'medium' | 'low' relevance to the user's research context")
    name: str = Field(description="Korean facet name")
    description: str = Field(description="What counts as related work on this axis (Korean)")
    include: list[str] = Field(description="Concrete inclusion criteria (Korean)")
    exclude: list[str] = Field(description="Near-misses that should NOT count (Korean)")
    search_queries: list[str] = Field(description="3-6 English search queries")
    anchor_labels: list[str] = Field(description="Seed reference labels that exemplify this facet")
    anchor_known: list[str] = Field(
        description="Papers named in the user's context that exemplify this facet (name + arXiv id)"
    )
    relation_types: list[str] = Field(
        description="Relation types expected here, e.g. same-problem-different-method"
    )


class FacetSet(BaseModel):
    facets: list[Facet]
    rationale: str = Field(description="Why these axes, in Korean")


class TriageItem(BaseModel):
    idx: int
    facet: str = Field(description="Best-matching facet id, or 'none'")


class TriageResult(BaseModel):
    items: list[TriageItem]


class JudgeItem(BaseModel):
    idx: int
    facet: str = Field(description="Primary facet id, or 'none'")
    secondary_facets: list[str]
    relation: str = Field(
        description="direct_competitor | same_problem_different_method | "
        "same_mechanism_different_problem | baseline | benchmark | component | survey | unrelated"
    )
    grade: int = Field(description="0 unrelated, 1 peripheral, 2 related, 3 direct competitor")
    reason: str = Field(description="One Korean sentence: why this facet/grade")
    contrast: str = Field(
        description="One Korean sentence: how it differs from the researcher's idea (empty if grade<2)"
    )


class JudgeResult(BaseModel):
    items: list[JudgeItem]
