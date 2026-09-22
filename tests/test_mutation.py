"""样本变异覆盖评估引擎单元测试。

覆盖：删除必填 / 新增必填 / 枚举收窄 / 数值边界 / 字符串长度 / 数组长度
各规则的杀死判定、收紧型与放松型变异的对称差分、未覆盖路径、按规则
上限截断、确定性顺序、$ref 内联与 $defs 跳过、样例下标有界、原 schema
不被修改。
"""
from app import mutation


def _by_rule(result):
    return {r["rule"]: r for r in result["rules"]}


def _mutations(result, rule):
    return [m for m in result["mutations"] if m["rule"] == rule]


# ---------------------------------------------------------------------------
# 删除必填项（放松型变异：原非法文档可能被变异接受）
# ---------------------------------------------------------------------------


def test_required_removed_killed_by_document_missing_field():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
    }
    # 0 缺 a，1 缺 b：分别杀死删除 a / 删除 b 的变异
    result = mutation.evaluate_mutation_coverage(
        schema, [{"b": 1}, {"a": "x"}, {"a": "x", "b": 2}]
    )
    removed = _mutations(result, mutation.REQUIRED_REMOVED)
    assert {m["description"]: m["sample_indices"] for m in removed} == {
        "移除必填字段 'a'": [0],
        "移除必填字段 'b'": [1],
    }
    # 完整文档不杀死任何删除必填变异
    assert all(2 not in m["sample_indices"] for m in removed)


def test_required_removed_single_clears_keyword():
    # required 只剩一个字段时，删除变异应整段移除 required；缺失该字段
    # 的文档从非法翻转成合法
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"}}}
    result = mutation.evaluate_mutation_coverage(schema, [{}])
    removed = _mutations(result, mutation.REQUIRED_REMOVED)
    assert len(removed) == 1
    assert removed[0]["killed"] is True
    assert removed[0]["sample_indices"] == [0]
    # 原 schema 未被改动
    assert schema["required"] == ["a"]


# ---------------------------------------------------------------------------
# 新增必填项（收紧型变异：原合法文档可能被变异拒绝）
# ---------------------------------------------------------------------------


def test_required_added_killed_by_documents_without_optional_property():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["a"],
    }
    result = mutation.evaluate_mutation_coverage(
        schema, [{"a": "x"}, {"a": "y", "note": "n"}]
    )
    added = _mutations(result, mutation.REQUIRED_ADDED)
    assert len(added) == 1
    assert added[0]["description"] == "新增必填字段 'note'"
    # 缺 note 的文档 0 被变异拒绝；带 note 的文档 1 不受影响
    assert added[0]["sample_indices"] == [0]


# ---------------------------------------------------------------------------
# 收窄枚举（收紧型变异）
# ---------------------------------------------------------------------------


def test_enum_narrowed_each_value_gets_own_mutation():
    schema = {"enum": ["a", "b", "c"]}
    result = mutation.evaluate_mutation_coverage(
        schema, ["a", "b", "c", "x"]
    )
    narrowed = _mutations(result, mutation.ENUM_NARROWED)
    assert len(narrowed) == 3
    by_value = {m["description"]: m for m in narrowed}
    assert by_value["从枚举中移除取值 \"a\""]["sample_indices"] == [0]
    assert by_value["从枚举中移除取值 \"b\""]["sample_indices"] == [1]
    assert by_value["从枚举中移除取值 \"c\""]["sample_indices"] == [2]
    # 原本就非法的 'x' 在任何收窄变异下仍非法，不贡献杀死
    assert all(3 not in m["sample_indices"] for m in narrowed)


def test_enum_narrowed_survives_when_values_absent_from_samples():
    schema = {"type": "string", "enum": ["a", "b"]}
    result = mutation.evaluate_mutation_coverage(schema, ["a"])
    narrowed = _mutations(result, mutation.ENUM_NARROWED)
    # 组内按描述排序：先移除 "a"（被样例杀死），再移除 "b"（存活）
    assert [m["killed"] for m in narrowed] == [True, False]
    uncovered = {(u["rule"], u["path"]) for u in result["uncovered"]}
    assert (mutation.ENUM_NARROWED, "#") in uncovered


# ---------------------------------------------------------------------------
# 收紧数值范围
# ---------------------------------------------------------------------------


def test_numeric_boundary_mutations():
    schema = {"type": "integer", "minimum": 0, "maximum": 10}
    result = mutation.evaluate_mutation_coverage(
        schema, [0, 10, 5, -1, 11]
    )
    raised = _mutations(result, mutation.MINIMUM_RAISED)
    lowered = _mutations(result, mutation.MAXIMUM_LOWERED)
    assert len(raised) == 1 and len(lowered) == 1
    # 下界 0 -> 1：恰好踩下界的 0 翻转；内部值 5 与原非法的 -1/11 不翻转
    assert raised[0]["sample_indices"] == [0]
    # 上界 10 -> 9：恰好踩上界的 10 翻转
    assert lowered[0]["sample_indices"] == [1]
    assert raised[0]["path"] == lowered[0]["path"] == "#"


def test_numeric_mutations_on_non_object_root_document():
    # 根 schema 直接约束整数，待评估文档可以是非对象
    schema = {"type": "integer", "minimum": 5}
    result = mutation.evaluate_mutation_coverage(schema, [5])
    assert result["total_documents"] == 1
    assert result["valid_documents"] == 1
    raised = _mutations(result, mutation.MINIMUM_RAISED)
    assert raised[0]["killed"] is True


def test_exclusive_bounds_are_mutated_when_plain_bounds_absent():
    schema = {"type": "number", "exclusiveMinimum": 1.0, "exclusiveMaximum": 4.0}
    result = mutation.evaluate_mutation_coverage(schema, [2.5])
    # 内部值不踩边界 -> 两条变异都存活
    scores = _by_rule(result)
    assert scores[mutation.MINIMUM_RAISED]["survived"] == 1
    assert scores[mutation.MAXIMUM_LOWERED]["survived"] == 1


# ---------------------------------------------------------------------------
# 字符串 / 数组长度边界
# ---------------------------------------------------------------------------


def test_string_length_mutations():
    schema = {"type": "string", "minLength": 2, "maxLength": 4}
    result = mutation.evaluate_mutation_coverage(
        schema, ["ab", "abcd", "abc"]
    )
    inc = _mutations(result, mutation.MIN_LENGTH_INCREASED)[0]
    dec = _mutations(result, mutation.MAX_LENGTH_DECREASED)[0]
    assert inc["sample_indices"] == [0]
    assert dec["sample_indices"] == [1]


def test_array_length_mutations():
    schema = {"type": "array", "minItems": 1, "maxItems": 3}
    result = mutation.evaluate_mutation_coverage(
        schema, [[1], [1, 2, 3], [1, 2]]
    )
    inc = _mutations(result, mutation.MIN_ITEMS_INCREASED)[0]
    dec = _mutations(result, mutation.MAX_ITEMS_DECREASED)[0]
    assert inc["sample_indices"] == [0]
    assert dec["sample_indices"] == [1]


def test_zero_upper_bound_produces_no_lowering_mutation():
    # maxLength: 0 再减会变成非法负数，该位置不产生变异
    schema = {"type": "string", "maxLength": 0}
    result = mutation.evaluate_mutation_coverage(schema, [""])
    assert mutation.MAX_LENGTH_DECREASED not in _by_rule(result)
    assert mutation.MAXIMUM_LOWERED not in _by_rule(result)


# ---------------------------------------------------------------------------
# 汇总分数与未覆盖清单
# ---------------------------------------------------------------------------


def test_score_and_uncovered_aggregation():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "enum": ["x", "y"]},
            "b": {"type": "integer", "minimum": 0},
        },
        "required": ["a"],
    }
    # {} 原非法：删除必填 a 后合法 -> 杀死 required_removed；
    # 枚举 / 数值变异下 {} 仍非法 -> 这些变异全部存活
    result = mutation.evaluate_mutation_coverage(schema, [{}])
    assert result["total_documents"] == 1
    assert result["invalid_documents"] == 1
    scores = _by_rule(result)
    assert scores[mutation.REQUIRED_REMOVED]["killed"] == 1
    assert scores[mutation.ENUM_NARROWED]["generated"] == 2
    assert scores[mutation.ENUM_NARROWED]["mutation_score"] == 0.0
    assert scores[mutation.MINIMUM_RAISED]["mutation_score"] == 0.0

    total = result["total_mutations"]
    # required_removed(a) + required_added(b) + 2 enum + minimum
    assert total == 1 + 1 + 2 + 1
    # {} 原本因缺 a 非法：只有删除必填 a 让它翻转为合法；新增必填 b 后
    # 它仍因缺 a 非法，不翻转
    assert result["killed_mutations"] == 1
    assert result["survived_mutations"] == total - 1
    assert result["mutation_score"] == 1 / total

    uncovered_rules = {u["rule"] for u in result["uncovered"]}
    assert mutation.ENUM_NARROWED in uncovered_rules
    assert mutation.MINIMUM_RAISED in uncovered_rules
    assert mutation.REQUIRED_REMOVED not in uncovered_rules
    # 未覆盖项带 schema 路径
    enum_uncovered = [u for u in result["uncovered"]
                      if u["rule"] == mutation.ENUM_NARROWED]
    assert {u["path"] for u in enum_uncovered} == {"#/properties/a"}


def test_schema_without_mutable_keywords_scores_zero():
    result = mutation.evaluate_mutation_coverage({"type": "object"}, [{}])
    assert result["total_mutations"] == 0
    assert result["killed_mutations"] == 0
    assert result["mutation_score"] == 0.0
    assert result["rules"] == []
    assert result["uncovered"] == []
    assert result["mutations"] == []


# ---------------------------------------------------------------------------
# 每类上限与确定性
# ---------------------------------------------------------------------------


def test_max_per_rule_caps_enum_mutations():
    schema = {"type": "string", "enum": ["a", "b", "c", "d"]}
    capped = mutation.evaluate_mutation_coverage(
        schema, ["a", "b", "c", "d"],
        max_per_rule={mutation.ENUM_NARROWED: 2},
    )
    narrowed = _mutations(capped, mutation.ENUM_NARROWED)
    assert len(narrowed) == 2
    # 排序截断：保留规范 JSON 最小的两个取值
    assert [m["description"] for m in narrowed] == [
        "从枚举中移除取值 \"a\"", "从枚举中移除取值 \"b\"",
    ]
    rule = _by_rule(capped)[mutation.ENUM_NARROWED]
    assert rule["cap"] == 2
    assert rule["generated"] == 2


def test_cap_zero_skips_rule():
    schema = {"enum": ["a", "b"], "type": "string"}
    result = mutation.evaluate_mutation_coverage(
        schema, ["a"], max_per_rule={mutation.ENUM_NARROWED: 0}
    )
    assert _mutations(result, mutation.ENUM_NARROWED) == []
    assert mutation.ENUM_NARROWED not in _by_rule(result)


def test_generation_is_deterministic_and_order_independent():
    schema = {
        "type": "object",
        "properties": {
            "z": {"enum": ["b", "a"]},
            "a": {"type": "integer", "maximum": 3, "minimum": 1},
        },
        "required": ["z", "a"],
    }
    docs = ["ignored"]
    first = mutation.evaluate_mutation_coverage(schema, docs)
    second = mutation.evaluate_mutation_coverage(schema, docs)
    assert first["mutations"] == second["mutations"]

    # 同一变异在结果内的顺序固定：规则声明顺序 -> path -> description
    rule_order = {rule: i for i, rule in enumerate(mutation.MUTATION_RULES)}
    keys = [(m["rule"], m["path"], m["description"]) for m in first["mutations"]]
    assert keys == sorted(keys, key=lambda k: (rule_order[k[0]], k[1], k[2]))


# ---------------------------------------------------------------------------
# 递归路径 / $ref / 不修改入参
# ---------------------------------------------------------------------------


def test_mutations_reach_nested_and_composition_paths():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["x", "y"]},
                "minItems": 1,
            },
            "choice": {
                "oneOf": [
                    {"type": "object", "properties": {"k": {"const": 1}},
                     "required": ["k"]},
                ],
            },
        },
        "required": ["tags", "choice"],
    }
    result = mutation.evaluate_mutation_coverage(
        schema,
        [
            {"choice": {"k": 1}},                                # 0 缺 tags（原本非法）
            {"tags": ["y"], "choice": {"k": 1}},                # 1 单元素 + enum=y
            {"tags": ["x", "x"], "choice": {}},                 # 2 缺分支必填 k
            {"tags": ["x"], "choice": {"k": 1}},                # 3 合规单元素
        ],
    )
    paths = {(m["rule"], m["path"]) for m in result["mutations"]}
    assert (mutation.ENUM_NARROWED, "#/properties/tags/items") in paths
    assert (mutation.MIN_ITEMS_INCREASED, "#/properties/tags") in paths
    assert (mutation.REQUIRED_REMOVED, "#/properties/choice/oneOf/0") in paths

    # 恰好 1 个元素且原本合法的文档杀死 minItems 1 -> 2（文档 0 缺 tags、
    # 文档 2 因 choice 分支缺 k 原本非法，均不翻转）
    min_items = [m for m in result["mutations"]
                 if m["path"] == "#/properties/tags"
                 and m["rule"] == mutation.MIN_ITEMS_INCREASED][0]
    assert min_items["sample_indices"] == [1, 3]
    # 缺分支必填 k 的文档杀死 oneOf 分支内的 required_removed
    branch_required = [m for m in result["mutations"]
                       if m["path"] == "#/properties/choice/oneOf/0"][0]
    assert branch_required["sample_indices"] == [2]


def test_refs_are_inlined_and_unused_defs_skipped():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"$ref": "#/$defs/kind"},
        },
        "required": ["kind"],
        "$defs": {
            "kind": {"type": "string", "enum": ["a", "b"]},
            # 从未被引用：其中的可变关键字不应产生变异
            "unused": {"type": "string", "enum": ["zzz"]},
        },
    }
    result = mutation.evaluate_mutation_coverage(
        schema, [{"kind": "a"}, {"kind": "b"}]
    )
    enum_paths = {m["path"] for m in _mutations(result, mutation.ENUM_NARROWED)}
    assert enum_paths == {"#/properties/kind"}
    assert all("$defs" not in m["path"] for m in result["mutations"])


def test_input_schema_is_not_modified():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "enum": ["a", "b"]}},
        "required": ["a"],
    }
    import copy

    snapshot = copy.deepcopy(schema)
    mutation.evaluate_mutation_coverage(schema, [{}, {"a": "a"}])
    assert schema == snapshot


# ---------------------------------------------------------------------------
# 样例下标有界
# ---------------------------------------------------------------------------


def test_sample_indices_are_bounded_and_sorted():
    schema = {
        "type": "object",
        "properties": {"note": {"type": "string"}},
    }
    # 25 篇都缺 note 的文档：新增 note 必填会被全部杀死，但只保留前 20 个下标
    documents = [{} for _ in range(25)]
    result = mutation.evaluate_mutation_coverage(schema, documents)
    added = _mutations(result, mutation.REQUIRED_ADDED)
    assert len(added) == 1
    assert added[0]["sample_indices"] == list(range(20))
    assert added[0]["killed"] is True
    assert result["killed_mutations"] == 1
