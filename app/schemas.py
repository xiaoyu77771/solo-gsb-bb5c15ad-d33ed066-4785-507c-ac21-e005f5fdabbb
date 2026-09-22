"""Pydantic 请求 / 响应模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.mutation import (
    DEFAULT_MAX_MUTATIONS_PER_RULE,
    MAX_MUTATION_DOCUMENTS,
    MAX_MUTATIONS_PER_RULE_LIMIT,
    MUTATION_RULES,
)
from app.profile import MAX_PROFILE_DOCUMENTS
from app.replay import MAX_REPLAY_DOCUMENTS


class AnalysisCreate(BaseModel):
    """POST /api/analyses 请求体。"""

    name: str | None = Field(default=None, max_length=255, description="可选的分析名称")
    old_schema: dict[str, Any] = Field(..., description="旧版 JSON Schema")
    new_schema: dict[str, Any] = Field(..., description="新版 JSON Schema")
    samples: list[Any] | None = Field(default=None, description="可选的样例文档列表")


class Issue(BaseModel):
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str
    counterexample: Any | None = None
    counterexample_path: str | None = None


class DirectionSummary(BaseModel):
    errors: int
    warnings: int
    total: int


class ReportSummary(BaseModel):
    backward: DirectionSummary
    forward: DirectionSummary
    total: int
    errors: int
    warnings: int


class SampleResult(BaseModel):
    index: int
    valid_against_old: bool
    valid_against_new: bool
    direction: str | None = None


class Report(BaseModel):
    backward_compatible: bool
    forward_compatible: bool
    directions: dict[str, str]
    summary: ReportSummary
    issues: list[Issue]
    sample_results: list[SampleResult]


class AnalysisResponse(BaseModel):
    id: int
    name: str | None
    created_at: str
    old_schema: dict[str, Any]
    new_schema: dict[str, Any]
    samples: list[Any]
    report: Report


class AnalysisListItem(BaseModel):
    id: int
    name: str | None
    created_at: str
    backward_compatible: bool
    forward_compatible: bool
    errors: int
    warnings: int


class AnalysisListResponse(BaseModel):
    total: int
    items: list[AnalysisListItem]


class CompareRequest(BaseModel):
    """POST /api/analyses/compare 请求体。"""

    baseline_id: int = Field(..., ge=1, description="基线分析记录 ID")
    current_id: int = Field(..., ge=1, description="当前分析记录 ID")


class ComparedIssue(BaseModel):
    """对比结果中的一条 issue（含稳定指纹，不含反例等大字段）。"""

    fingerprint: str
    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    message: str
    reason: str


class SeverityChangedIssue(ComparedIssue):
    baseline_severity: Literal["error", "warning"]
    current_severity: Literal["error", "warning"]


class IssueGroups(BaseModel):
    added: list[ComparedIssue]
    fixed: list[ComparedIssue]
    persisting: list[ComparedIssue]
    severity_changed: list[SeverityChangedIssue]


class IssueGroupCounts(BaseModel):
    added: int
    fixed: int
    persisting: int
    severity_changed: int


class CompatibilityChange(BaseModel):
    baseline: bool
    current: bool
    changed: bool


class CompatibilityDiff(BaseModel):
    backward: CompatibilityChange
    forward: CompatibilityChange


class MetricDiff(BaseModel):
    baseline: int
    current: int
    delta: int


class DirectionSummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff


class SummaryDiff(BaseModel):
    errors: MetricDiff
    warnings: MetricDiff
    total: MetricDiff
    backward: DirectionSummaryDiff
    forward: DirectionSummaryDiff


class CompareResponse(BaseModel):
    baseline_id: int
    current_id: int
    compatibility: CompatibilityDiff
    summary_diff: SummaryDiff
    issues: IssueGroups
    counts: IssueGroupCounts


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate 发布门禁
# ---------------------------------------------------------------------------


class ExemptionRule(BaseModel):
    """issue 豁免规则：direction / code 字面相等，path 支持单段 ``*`` 通配。"""

    direction: Literal["backward", "forward"] = Field(..., description="兼容方向")
    code: str = Field(..., min_length=1, description="issue 代码（精确匹配）")
    path: str = Field(
        ...,
        description=(
            "JSON Pointer 风格 schema 路径（如 #/properties/a）；"
            "单段 * 为通配（如 #/properties/*），根路径为 #"
        ),
    )

    @model_validator(mode="after")
    def _validate_path(self):
        path = self.path
        if path != "#" and not path.startswith("#/"):
            raise ValueError('path 必须为 "#" 或以 "#/" 开头的 JSON Pointer')
        if path != "#":
            for segment in path[2:].split("/"):
                if segment == "":
                    raise ValueError("path 含空路径段")
        return self


class GateRequest(BaseModel):
    """POST /api/analyses/{id}/gate 请求体。"""

    directions: list[Literal["backward", "forward"]] = Field(
        ..., min_length=1, description="需要检查的兼容方向（至少一个）"
    )
    max_warnings: dict[Literal["backward", "forward"], int] | None = Field(
        default=None,
        description="各方向允许的未豁免 warning 上限（未声明方向视为 0）",
    )
    exemptions: list[ExemptionRule] = Field(
        default_factory=list, description="issue 豁免规则，按声明顺序确定优先级"
    )

    @model_validator(mode="after")
    def _dedupe_directions(self):
        seen: set[str] = set()
        unique: list[str] = []
        for name in self.directions:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        self.directions = unique
        if self.max_warnings:
            for name, value in self.max_warnings.items():
                if value < 0:
                    raise ValueError(f"max_warnings.{name} 不能为负数")
        return self


class GateDirectionCounts(BaseModel):
    errors: int
    warnings: int
    exempted: int
    total: int
    max_warnings: int


class GateBlockingIssue(Issue):
    blocked_reason: Literal["error", "warning_limit_exceeded"]


class GateExemptedIssue(Issue):
    rule_index: int = Field(..., ge=0, description="命中的豁免规则下标（exemptions 从 0 开始）")


class GateResponse(BaseModel):
    # "pass" 是 Python 关键字，模型字段使用 passed，对外序列化为 "pass"
    passed: bool = Field(serialization_alias="pass", description="门禁是否通过")
    counts: dict[Literal["backward", "forward"], GateDirectionCounts]
    blocking: list[GateBlockingIssue]
    exempted: list[GateExemptedIssue]

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 合规画像
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    """POST /api/analyses/{id}/profile 请求体。"""

    documents: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        max_length=MAX_PROFILE_DOCUMENTS,
        description=(
            f"待检 JSON 文档列表（必须全部为 JSON 对象），"
            f"数量 1..{MAX_PROFILE_DOCUMENTS}"
        ),
    )


class ProfileFailure(BaseModel):
    """画像中按位置 / 规则归并后的一组失败。"""

    fingerprint: str = Field(..., description="稳定规则指纹（SHA-256 前 16 位）")
    pointer: str = Field(..., description="失败位置（JSON Pointer，最深公共失败路径）")
    validator: str = Field(..., description="jsonschema 校验规则名（如 required / type）")
    schema_pointer: str = Field(..., description="schema 中触发规则的位置（JSON Pointer）")
    message: str = Field(..., description="确定性的失败说明（不随输入顺序变化）")
    count: int = Field(..., ge=0, description="命中该问题的文档数（每文档每指纹仅计一次）")
    example_indices: list[int] = Field(
        ..., description="示例文档下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class ProfileSnapshot(BaseModel):
    """画像结果快照（持久化内容）。"""

    total_documents: int
    valid_documents: int
    invalid_documents: int
    compliance_rate: float = Field(..., description="合规率：valid_documents / total_documents")
    failures: list[ProfileFailure]


class ProfileResponse(ProfileSnapshot):
    """POST 画像 / GET 查询画像的响应。"""

    id: int
    analysis_id: int
    version: int = Field(..., description="该分析下的画像版本号（从 1 开始递增）")
    created_at: str


class ProfileSummary(BaseModel):
    """画像列表中的摘要条目。"""

    id: int
    version: int
    created_at: str
    total_documents: int
    valid_documents: int
    invalid_documents: int
    compliance_rate: float
    failure_groups: int


class ProfileListResponse(BaseModel):
    analysis_id: int
    total: int
    items: list[ProfileSummary]


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay 样本回放诊断
# ---------------------------------------------------------------------------


class ReplayRequest(BaseModel):
    """POST /api/analyses/{id}/replay 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_REPLAY_DOCUMENTS,
        description=(
            f"待回放的 JSON 文档列表（任意 JSON 类型），数量 1..{MAX_REPLAY_DOCUMENTS}"
        ),
    )


class ReplayCounts(BaseModel):
    """回放文档按校验结果的分类计数（四类互斥，总和为文档总数）。"""

    valid: int = Field(..., description="旧、新两版 schema 均校验通过的文档数")
    backward: int = Field(..., description="旧版合法、新版拒绝（向后破坏实锤）的文档数")
    forward: int = Field(..., description="新版合法、旧版拒绝（向前破坏实锤）的文档数")
    invalid_both: int = Field(..., description="旧、新两版均拒绝（无法归因）的文档数")


class ReplayIssueHit(BaseModel):
    """报告中一条 issue 在回放样本上的命中情况。"""

    direction: Literal["backward", "forward"]
    path: str
    code: str
    severity: Literal["error", "warning"]
    count: int = Field(..., ge=0, description="命中该 issue 的文档数（每文档每 issue 仅计一次）")
    example_indices: list[int] = Field(
        ..., description="示例文档下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class ReplayUnattributedError(BaseModel):
    """无法归因到报告 issue 的校验错误（按方向 / 位置 / 规则归并）。"""

    direction: Literal["backward", "forward", "both"] = Field(
        ..., description="失败方向：单向失败但无匹配 issue，或 both（两版均拒绝）"
    )
    reason: Literal["no_matching_issue", "rejected_by_both"]
    pointer: str = Field(..., description="失败所在的 schema 节点位置（JSON Pointer）")
    validator: str = Field(..., description="jsonschema 校验规则名（如 required / type）")
    schema_pointer: str = Field(..., description="schema 中触发规则的实际位置（JSON Pointer）")
    message: str = Field(..., description="确定性的规则说明（不随实例取值变化）")
    count: int = Field(..., ge=0, description="命中该错误的文档数（每文档同类错误仅计一次）")
    example_indices: list[int]


class ReplayResponse(BaseModel):
    """样本回放诊断结果（接口只读，不持久化）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=0)
    counts: ReplayCounts
    issue_hits: list[ReplayIssueHit] = Field(
        ..., description="报告全部 issue 的命中情况，按报告内 issue 顺序排列（未命中 count=0）"
    )
    unattributed: list[ReplayUnattributedError] = Field(
        ..., description="无法归因到报告 issue 的校验错误分组"
    )


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/mutation-coverage 样本变异覆盖评估
# ---------------------------------------------------------------------------


class MutationCoverageRequest(BaseModel):
    """POST /api/analyses/{id}/mutation-coverage 请求体。"""

    documents: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_MUTATION_DOCUMENTS,
        description=(
            f"待评估的 JSON 文档列表（任意 JSON 类型），数量 1..{MAX_MUTATION_DOCUMENTS}"
        ),
    )
    max_mutations: dict[str, int] | None = Field(
        default=None,
        description=(
            "每类变异规则的生成上限；未声明的规则使用缺省上限 "
            f"{DEFAULT_MAX_MUTATIONS_PER_RULE}，单类上限不得超过 "
            f"{MAX_MUTATIONS_PER_RULE_LIMIT}"
        ),
    )

    @model_validator(mode="after")
    def _validate_max_mutations(self):
        if self.max_mutations:
            valid_rules = set(MUTATION_RULES)
            for rule, cap in self.max_mutations.items():
                if rule not in valid_rules:
                    raise ValueError(
                        f"未知变异规则: {rule}（合法值: {', '.join(MUTATION_RULES)}）"
                    )
                if cap < 0:
                    raise ValueError(f"max_mutations.{rule} 不能为负数")
                if cap > MAX_MUTATIONS_PER_RULE_LIMIT:
                    raise ValueError(
                        f"max_mutations.{rule} 不能超过 {MAX_MUTATIONS_PER_RULE_LIMIT}"
                    )
        return self


class MutationRuleScore(BaseModel):
    """按变异规则分类的覆盖统计。"""

    rule: str = Field(..., description="变异规则代码")
    generated: int = Field(..., ge=0, description="该规则实际生成的变异数量（已按上限截断）")
    killed: int = Field(..., ge=0, description="被待评估文档发现（杀死）的变异数量")
    survived: int = Field(..., ge=0, description="未被任何文档发现的变异数量")
    mutation_score: float = Field(..., description="该规则的 mutation score：killed / generated")
    cap: int = Field(..., ge=0, description="该规则本次评估使用的生成上限")


class MutationResult(BaseModel):
    """单个变异的评估结果。"""

    rule: str = Field(..., description="变异规则代码")
    path: str = Field(..., description="变异施加位置（schema JSON Pointer，根为 #）")
    description: str = Field(..., description="单点变异的确定性中文说明")
    killed: bool = Field(..., description="是否被待评估文档发现（合法性发生翻转）")
    sample_indices: list[int] = Field(
        ..., description="发现该变异的样例下标（基于提交顺序，从 0 开始，升序且有界）"
    )


class UncoveredMutation(BaseModel):
    """未被任何样例发现的变异（只含定位信息，不含样例下标）。"""

    rule: str
    path: str
    description: str


class MutationCoverageResponse(BaseModel):
    """样本变异覆盖评估结果（接口只读，不持久化）。"""

    analysis_id: int
    total_documents: int = Field(..., ge=1)
    valid_documents: int = Field(..., ge=0, description="在原 schema 下合法的文档数")
    invalid_documents: int = Field(..., ge=0, description="在原 schema 下非法的文档数")
    total_mutations: int = Field(..., ge=0, description="全部规则实际生成的变异总数")
    killed_mutations: int = Field(..., ge=0, description="被样例发现的变异总数")
    survived_mutations: int = Field(..., ge=0, description="未被发现的变异总数")
    mutation_score: float = Field(..., description="整体 mutation score：killed / total")
    rules: list[MutationRuleScore] = Field(
        ..., description="按规则分类的 mutation score（仅包含实际生成过变异的规则）"
    )
    mutations: list[MutationResult] = Field(
        ..., description="全部变异的逐变异评估结果（规则 -> path -> 描述 的确定顺序）"
    )
    uncovered: list[UncoveredMutation] = Field(
        ..., description="未覆盖变异清单（规则 + schema 路径 + 说明）"
    )
