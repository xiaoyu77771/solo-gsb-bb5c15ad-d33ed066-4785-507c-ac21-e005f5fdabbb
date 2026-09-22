"""样本变异覆盖评估引擎。

针对一份**已保存的新 schema** 确定性地生成一批**单点变异**（mutant）：
每个变异只改动 schema 的一个关键字位置，例如删除某个必填字段、收窄
枚举取值、抬升数值下界 / 压低上界、收紧字符串长度与数组长度边界等。

对每个变异 schema，用待评估文档逐个做 ``jsonschema`` 校验，并与文档在
**原 schema** 下的校验结果做**对称差分**：

* 原 schema 合法、变异 schema 拒绝（变异收紧约束，命中了踩边界的样本）；
* 原 schema 拒绝、变异 schema 接受（变异放松约束，命中了原本非法的样本）；

只要存在任一文档的合法性发生翻转，就认为该变异被这批样例**发现（杀死）**。
从而统计：

* 按变异规则（rule）分类的 mutation score = 被发现变异数 / 生成变异数；
* 未被任何样例发现的变异及其 schema 路径（``uncovered``）；
* 发现每个变异的样例下标（``sample_indices``，有界升序）。

变异生成完全确定性：``$ref`` 先按文档内引用预解析（带环保护，见
``compat.resolve_refs``），属性名 / 必填名 / 枚举值按固定顺序排列，
每类规则生成数量受 ``max_per_rule`` 上限截断，因此同一 schema 多次
评估的变异集合与顺序稳定。引擎为纯函数，不读取数据库、不修改入参。
"""
from __future__ import annotations

import copy
import json
from collections import OrderedDict
from typing import Any, Callable

from app.compat import escape_pointer, make_validator, resolve_refs
from app.profile import MAX_EXAMPLE_INDICES

# 单次评估允许的最大文档数（与 schemas.MutationCoverageRequest 保持一致）。
MAX_MUTATION_DOCUMENTS = 5000

# 每类变异规则的默认生成上限（调用方可逐类覆盖）。
DEFAULT_MAX_MUTATIONS_PER_RULE = 50

# 每类变异规则允许声明的最大上限，防止一次评估产生过多变异。
MAX_MUTATIONS_PER_RULE_LIMIT = 1000

# 每个变异最多保留的发现样例下标数（仅控制响应体积，不影响是否杀死）。
MAX_SAMPLE_INDICES = MAX_EXAMPLE_INDICES

# ---------------------------------------------------------------------------
# 变异规则定义
# ---------------------------------------------------------------------------
#
# 规则同时覆盖**收紧型**变异（原合法样本可能被变异拒绝）与**放松型**
# 变异（原非法样本可能被变异接受），两类都通过合法性对称差分自动识别，
# 无需为每条规则单独声明方向。
REQUIRED_REMOVED = "required_removed"
REQUIRED_ADDED = "required_added"
ENUM_NARROWED = "enum_narrowed"
MINIMUM_RAISED = "minimum_raised"
MAXIMUM_LOWERED = "maximum_lowered"
MIN_LENGTH_INCREASED = "min_length_increased"
MAX_LENGTH_DECREASED = "max_length_decreased"
MIN_ITEMS_INCREASED = "min_items_increased"
MAX_ITEMS_DECREASED = "max_items_decreased"

# 全部变异规则（固定顺序，也是响应中规则缺省上限的填充顺序）。
MUTATION_RULES: tuple[str, ...] = (
    REQUIRED_REMOVED,
    REQUIRED_ADDED,
    ENUM_NARROWED,
    MINIMUM_RAISED,
    MAXIMUM_LOWERED,
    MIN_LENGTH_INCREASED,
    MAX_LENGTH_DECREASED,
    MIN_ITEMS_INCREASED,
    MAX_ITEMS_DECREASED,
)

# 递归遍历时需要进入的子 schema 位置（$defs/definitions 不进入：其中的
# 子树已经通过 $ref 预解析内联到引用点，再变异会产生语义重复的变异）。
_DICT_CHILD_KEYS = (
    "additionalProperties",
    "propertyNames",
    "items",
    "additionalItems",
    "contains",
    "not",
    "if",
    "then",
    "else",
)
_LIST_CHILD_KEYS = ("prefixItems", "anyOf", "oneOf", "allOf")


def _canonical_json(value: Any) -> str:
    """枚举值等异构 JSON 值的确定性排序键。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _is_number(value: Any) -> bool:
    # bool 是 int 的子类，但不是合法的边界声明，显式排除
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _render_pointer(segments: tuple) -> str:
    """把 schema 内导航段渲染为 ``#/a/0/b`` 风格 JSON Pointer。"""
    if not segments:
        return "#"
    return "#/" + "/".join(escape_pointer(str(segment)) for segment in segments)


# ---------------------------------------------------------------------------
# 单点变异种子收集
# ---------------------------------------------------------------------------
#
# 一个种子 = (规则, 描述, apply_node)；apply_node 在 schema 副本的目标
# 节点 dict 上原地施加单点改动。遍历时只记录种子与导航段，真正深拷贝 /
# 构造变异 schema 在上限截断之后按需进行，避免生成注定被丢弃的副本。
Seed = tuple[str, str, Callable[[dict], None]]


def _required_seeds(node: dict) -> list[Seed]:
    required = node.get("required")
    names = (
        sorted(name for name in required if isinstance(name, str))
        if isinstance(required, list)
        else []
    )
    seeds: list[Seed] = []

    # 删除必填项：每删一个名字一个变异；删空后整段 required 关键字移除
    for name in names:
        def remove_apply(n: dict, name: str = name) -> None:
            remaining = [r for r in n.get("required", []) if r != name]
            if remaining:
                n["required"] = remaining
            else:
                n.pop("required", None)

        seeds.append((REQUIRED_REMOVED, f"移除必填字段 '{name}'", remove_apply))

    # 新增必填项：仅选取 properties 中已声明但当前非必填的字段名，
    # 保证变异确定性且与 schema 自身结构相关
    properties = node.get("properties")
    if isinstance(properties, dict):
        optional = sorted(
            name for name in properties
            if isinstance(name, str) and name not in names
        )
        for name in optional:
            def add_apply(n: dict, name: str = name) -> None:
                n["required"] = sorted(
                    set(r for r in n.get("required", []) if isinstance(r, str)) | {name}
                )

            seeds.append((REQUIRED_ADDED, f"新增必填字段 '{name}'", add_apply))
    return seeds


def _enum_seeds(node: dict) -> list[Seed]:
    enum = node.get("enum")
    if not isinstance(enum, list) or not enum:
        return []
    seeds: list[Seed] = []
    # 同一枚举值（含 list/dict 等不可哈希值）只产生一个变异，按规范化
    # JSON 去重并排序，保证变异集合与声明顺序无关
    seen: set[str] = set()
    ordered: list[Any] = []
    for value in sorted(enum, key=_canonical_json):
        key = _canonical_json(value)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)

    for value in ordered:
        rendered = _canonical_json(value)

        def apply(n: dict, rendered: str = rendered) -> None:
            kept = [
                item for item in n.get("enum", [])
                if _canonical_json(item) != rendered
            ]
            # 空枚举是合法 schema（拒绝一切实例），无需特殊处理
            n["enum"] = kept

        seeds.append((ENUM_NARROWED, f"从枚举中移除取值 {rendered}", apply))
    return seeds


def _bound_seeds(node: dict) -> list[Seed]:
    seeds: list[Seed] = []

    minimum = node.get("minimum")
    if _is_number(minimum):
        new_value = minimum + 1

        def raise_min(n: dict, new_value: Any = new_value) -> None:
            n["minimum"] = new_value

        seeds.append((
            MINIMUM_RAISED,
            f"将数值下界 minimum 由 {minimum} 收紧为 {new_value}",
            raise_min,
        ))
    else:
        # 仅有数值型 exclusiveMinimum（draft-06+）时收紧排他下界
        exclusive = node.get("exclusiveMinimum")
        if _is_number(exclusive):
            new_value = exclusive + 1

            def raise_exclusive_min(n: dict, new_value: Any = new_value) -> None:
                n["exclusiveMinimum"] = new_value

            seeds.append((
                MINIMUM_RAISED,
                f"将排他数值下界 exclusiveMinimum 由 {exclusive} 收紧为 {new_value}",
                raise_exclusive_min,
            ))

    maximum = node.get("maximum")
    if _is_number(maximum):
        new_value = maximum - 1

        def lower_max(n: dict, new_value: Any = new_value) -> None:
            n["maximum"] = new_value

        seeds.append((
            MAXIMUM_LOWERED,
            f"将数值上界 maximum 由 {maximum} 收紧为 {new_value}",
            lower_max,
        ))
    else:
        exclusive = node.get("exclusiveMaximum")
        if _is_number(exclusive):
            new_value = exclusive - 1

            def lower_exclusive_max(n: dict, new_value: Any = new_value) -> None:
                n["exclusiveMaximum"] = new_value

            seeds.append((
                MAXIMUM_LOWERED,
                f"将排他数值上界 exclusiveMaximum 由 {exclusive} 收紧为 {new_value}",
                lower_exclusive_max,
            ))
    return seeds


def _length_seeds(node: dict) -> list[Seed]:
    seeds: list[Seed] = []

    min_length = node.get("minLength")
    if _is_nonneg_int(min_length):
        def inc_min_length(n: dict, old: int = min_length) -> None:
            n["minLength"] = old + 1

        seeds.append((
            MIN_LENGTH_INCREASED,
            f"将字符串最小长度 minLength 由 {min_length} 收紧为 {min_length + 1}",
            inc_min_length,
        ))

    # 0 再减会产生非法的负数长度，跳过（该位置无法生成有效单点变异）
    max_length = node.get("maxLength")
    if _is_nonneg_int(max_length) and max_length > 0:
        def dec_max_length(n: dict, old: int = max_length) -> None:
            n["maxLength"] = old - 1

        seeds.append((
            MAX_LENGTH_DECREASED,
            f"将字符串最大长度 maxLength 由 {max_length} 收紧为 {max_length - 1}",
            dec_max_length,
        ))
    return seeds


def _item_count_seeds(node: dict) -> list[Seed]:
    seeds: list[Seed] = []

    min_items = node.get("minItems")
    if _is_nonneg_int(min_items):
        def inc_min_items(n: dict, old: int = min_items) -> None:
            n["minItems"] = old + 1

        seeds.append((
            MIN_ITEMS_INCREASED,
            f"将数组最小长度 minItems 由 {min_items} 收紧为 {min_items + 1}",
            inc_min_items,
        ))

    max_items = node.get("maxItems")
    if _is_nonneg_int(max_items) and max_items > 0:
        def dec_max_items(n: dict, old: int = max_items) -> None:
            n["maxItems"] = old - 1

        seeds.append((
            MAX_ITEMS_DECREASED,
            f"将数组最大长度 maxItems 由 {max_items} 收紧为 {max_items - 1}",
            dec_max_items,
        ))
    return seeds


def _node_seeds(node: dict) -> list[Seed]:
    """收集一个 schema 节点自身（不含子节点）的全部单点变异种子。"""
    return (
        _required_seeds(node)
        + _enum_seeds(node)
        + _bound_seeds(node)
        + _length_seeds(node)
        + _item_count_seeds(node)
    )


def _collect_seeds(resolved: Any) -> list[dict]:
    """递归遍历预解析后的 schema，收集全部变异种子（未截断）。

    返回每条记录：``rule / path / segments / description / apply``。
    """
    records: list[dict] = []

    def visit(node: Any, segments: tuple, seen: frozenset[int]) -> None:
        if node is True or node is False or not isinstance(node, dict):
            return
        if id(node) in seen:
            return
        seen = seen | {id(node)}

        path = _render_pointer(segments)
        for rule, description, apply in _node_seeds(node):
            records.append({
                "rule": rule,
                "path": path,
                "segments": segments,
                "description": description,
                "apply": apply,
            })

        properties = node.get("properties")
        if isinstance(properties, dict):
            for name in sorted(properties):
                visit(properties[name], segments + ("properties", name), seen)

        pattern_properties = node.get("patternProperties")
        if isinstance(pattern_properties, dict):
            for pattern in sorted(pattern_properties):
                visit(pattern_properties[pattern],
                      segments + ("patternProperties", pattern), seen)

        for key in _DICT_CHILD_KEYS:
            child = node.get(key)
            if isinstance(child, dict):
                visit(child, segments + (key,), seen)

        for key in _LIST_CHILD_KEYS:
            children = node.get(key)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    if isinstance(child, dict):
                        visit(child, segments + (key, index), seen)

    visit(resolved, (), frozenset())
    return records


def _select_seeds(records: list[dict], max_per_rule: dict[str, int]) -> list[dict]:
    """按规则分组，组内按 (path, description) 确定性排序后按上限截断。"""
    grouped: dict[str, list[dict]] = OrderedDict(
        (rule, []) for rule in MUTATION_RULES
    )
    for record in records:
        grouped.setdefault(record["rule"], []).append(record)

    selected: list[dict] = []
    for rule in MUTATION_RULES:
        bucket = grouped.get(rule, [])
        bucket.sort(key=lambda r: (r["path"], r["description"]))
        selected.extend(bucket[:max_per_rule.get(rule, DEFAULT_MAX_MUTATIONS_PER_RULE)])
    return selected


def _build_mutant(resolved: Any, segments: tuple,
                  apply: Callable[[dict], None]) -> dict:
    """深拷贝预解析 schema，在导航段指向的节点上施加单点变异。"""
    mutant = copy.deepcopy(resolved)
    node = mutant
    for segment in segments:
        node = node[segment]
    apply(node)
    return mutant


def _is_valid(validator: Any, document: Any) -> bool:
    return next(validator.iter_errors(document), None) is None


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def evaluate_mutation_coverage(
    schema: Any,
    documents: list[Any],
    max_per_rule: dict[str, int] | None = None,
) -> dict:
    """对 schema 生成确定性单点变异并用文档评估覆盖情况（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_MUTATION_DOCUMENTS 个任意 JSON 值；
    ``max_per_rule`` 为每类变异的生成上限（缺省 DEFAULT_MAX_MUTATIONS_PER_RULE）。
    """
    caps = dict(max_per_rule) if max_per_rule else {}
    resolved = resolve_refs(schema)
    validator = make_validator(resolved)

    # 每篇文档在原 schema 下的合法性只计算一次
    original_valid = [_is_valid(validator, document) for document in documents]
    valid_documents = sum(1 for ok in original_valid if ok)

    seeds = _select_seeds(_collect_seeds(resolved), caps)

    mutations: list[dict] = []
    for seed in seeds:
        mutant_validator = make_validator(
            _build_mutant(resolved, seed["segments"], seed["apply"])
        )
        sample_indices: list[int] = []
        for index, document in enumerate(documents):
            if _is_valid(mutant_validator, document) != original_valid[index]:
                sample_indices.append(index)
                # 已收集足够展示用下标后停止该校验器的剩余校验
                if len(sample_indices) >= MAX_SAMPLE_INDICES:
                    break
        mutations.append({
            "rule": seed["rule"],
            "path": seed["path"],
            "description": seed["description"],
            "killed": bool(sample_indices),
            "sample_indices": sample_indices,
        })

    # 输出顺序与变异选择一致（规则声明顺序 -> path -> description）
    total = len(mutations)
    killed = sum(1 for mutation in mutations if mutation["killed"])
    survived = total - killed

    rule_buckets: "OrderedDict[str, dict]" = OrderedDict()
    for mutation in mutations:
        bucket = rule_buckets.setdefault(mutation["rule"], {"generated": 0, "killed": 0})
        bucket["generated"] += 1
        if mutation["killed"]:
            bucket["killed"] += 1

    rules = [
        {
            "rule": rule,
            "generated": bucket["generated"],
            "killed": bucket["killed"],
            "survived": bucket["generated"] - bucket["killed"],
            "mutation_score": (
                bucket["killed"] / bucket["generated"] if bucket["generated"] else 0.0
            ),
            "cap": caps.get(rule, DEFAULT_MAX_MUTATIONS_PER_RULE),
        }
        for rule, bucket in rule_buckets.items()
    ]

    return {
        "total_documents": len(documents),
        "valid_documents": valid_documents,
        "invalid_documents": len(documents) - valid_documents,
        "total_mutations": total,
        "killed_mutations": killed,
        "survived_mutations": survived,
        "mutation_score": (killed / total) if total else 0.0,
        "rules": rules,
        "mutations": mutations,
        "uncovered": [
            {
                "rule": mutation["rule"],
                "path": mutation["path"],
                "description": mutation["description"],
            }
            for mutation in mutations
            if not mutation["killed"]
        ],
    }
