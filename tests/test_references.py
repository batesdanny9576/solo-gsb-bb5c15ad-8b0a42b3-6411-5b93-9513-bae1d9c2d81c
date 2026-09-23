"""引用依赖诊断引擎单元测试。"""
import pytest

from app.references import (
    MAX_CYCLES,
    REASON_EXTERNAL,
    REASON_NOT_SCHEMA,
    REASON_SYNTAX,
    REASON_UNRESOLVABLE,
    classify_ref,
    diagnose_references,
)


# ---------------------------------------------------------------------------
# classify_ref：引用值分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,tokens",
    [
        ("#", []),
        ("#/", [""]),
        ("#/$defs/a", ["$defs", "a"]),
        ("#/$defs/weird~1name", ["$defs", "weird/name"]),
        ("#/$defs/a~0b", ["$defs", "a~b"]),
        ("#/a//b", ["a", "", "b"]),
        ("#/0/1", ["0", "1"]),
    ],
)
def test_classify_ref_accepts_local_pointer(ref, tokens):
    status, reason, _ = classify_ref(ref)
    assert status == "ok"
    assert reason is None


@pytest.mark.parametrize(
    "ref,reason",
    [
        ("https://example.com/s.json#/$defs/a", REASON_EXTERNAL),
        ("other.json#/$defs/a", REASON_EXTERNAL),
        ("//example.com/schema#/$defs/a", REASON_EXTERNAL),
        ("urn:isbn:123#/$defs/a", REASON_EXTERNAL),
        ("#foo", REASON_SYNTAX),
        ("#$defs/a", REASON_SYNTAX),
        ("#/$defs/a~2", REASON_SYNTAX),
        ("#/$defs/a~", REASON_SYNTAX),
        (123, REASON_SYNTAX),
        (None, REASON_SYNTAX),
    ],
)
def test_classify_ref_rejects_external_and_bad_syntax(ref, reason):
    status, actual, _ = classify_ref(ref)
    assert status is None
    assert actual == reason


def test_classify_ref_percent_encoding_is_uri_decoded_before_pointer():
    # %2F 解码成普通斜杠（分段），不是 JSON Pointer 的 ~1 转义
    status, reason, _ = classify_ref("#/$defs/a%2Fb")
    assert status == "ok"
    assert reason is None
    # 指针合法但该路径不存在
    result = diagnose_references({"type": "object",
                                  "properties": {"x": {"$ref": "#/$defs/a%2Fb"}}})
    assert result["invalid_refs"][0]["reason"] == REASON_UNRESOLVABLE


# ---------------------------------------------------------------------------
# 引用边
# ---------------------------------------------------------------------------


def test_edges_cover_properties_items_allof_and_defs():
    schema = {
        "type": "object",
        "properties": {
            "a": {"$ref": "#/$defs/a"},
            "b": {"type": "array", "items": [{"$ref": "#/$defs/a"}]},
        },
        "allOf": [{"$ref": "#/$defs/c"}],
        "$defs": {
            "a": {"type": "string"},
            "c": {"$ref": "#/$defs/a"},
        },
    }
    result = diagnose_references(schema)
    edges = {(e["source"], e["target"]) for e in result["edges"]}
    assert ("#/properties/a", "#/$defs/a") in edges
    assert ("#/properties/b/items/0", "#/$defs/a") in edges
    assert ("#/allOf/0", "#/$defs/c") in edges
    assert ("#/$defs/c", "#/$defs/a") in edges
    assert result["summary"]["valid_edges"] == 4
    assert result["summary"]["invalid_refs"] == 0


def test_ref_to_root_is_valid():
    result = diagnose_references({"$ref": "#", "$defs": {"a": {"type": "string"}}})
    assert result["edges"] == [{"source": "#", "target": "#", "ref": "#"}]


def test_escaped_pointer_key_roundtrip():
    schema = {
        "type": "object",
        "properties": {
            "weird/name": {"type": "integer"},
            "x": {"$ref": "#/properties/weird~1name"},
        },
    }
    result = diagnose_references(schema)
    assert result["edges"][0]["target"] == "#/properties/weird~1name"
    assert result["invalid_refs"] == []


def test_ref_into_array_index():
    schema = {
        "type": "array",
        "prefixItems": [{"type": "string"}, {"$ref": "#/prefixItems/0"}],
    }
    result = diagnose_references(schema)
    assert result["edges"][0]["target"] == "#/prefixItems/0"


def test_ref_siblings_are_traversed():
    # draft 2019-09+：$ref 不再忽略兄弟关键字，兄弟里的 $ref 也要遍历到
    schema = {
        "type": "object",
        "properties": {
            "x": {"$ref": "#/$defs/a", "allOf": [{"$ref": "#/$defs/b"}]},
        },
        "$defs": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    result = diagnose_references(schema)
    targets = {e["target"] for e in result["edges"]}
    assert targets == {"#/$defs/a", "#/$defs/b"}


# ---------------------------------------------------------------------------
# 无效目标定位
# ---------------------------------------------------------------------------


def test_unresolvable_targets_report_source_location():
    schema = {
        "type": "object",
        "properties": {
            "missing": {"$ref": "#/$defs/nope"},
            "deep": {"properties": {"y": {"$ref": "#/$defs/a/sub"}}},
            "oob": {"$ref": "#/type/9"},
        },
        "$defs": {"a": {"type": "string"}},
    }
    result = diagnose_references(schema)
    by_source = {r["source"]: r for r in result["invalid_refs"]}
    assert by_source["#/properties/missing"]["reason"] == REASON_UNRESOLVABLE
    assert by_source["#/properties/deep/properties/y"]["reason"] == REASON_UNRESOLVABLE
    assert by_source["#/properties/oob"]["reason"] == REASON_UNRESOLVABLE
    assert result["edges"] == []


def test_array_leading_zero_index_is_invalid():
    result = diagnose_references(
        {"type": "array", "items": {"$ref": "#/items/00"}}
    )
    assert result["invalid_refs"][0]["reason"] == REASON_UNRESOLVABLE


def test_external_refs_never_resolved():
    schema = {
        "type": "object",
        "properties": {
            "http": {"$ref": "https://example.com/schema.json"},
            "file": {"$ref": "schemas/common.json#/$defs/x"},
            "frag_only_bad": {"$ref": "#/$defs/x"},
        },
        "$defs": {"x": {"type": "string"}},
    }
    result = diagnose_references(schema)
    reasons = {r["source"]: r["reason"] for r in result["invalid_refs"]}
    assert reasons["#/properties/http"] == REASON_EXTERNAL
    assert reasons["#/properties/file"] == REASON_EXTERNAL
    # 外部引用不产生边，也不参与环 / 可达性
    assert all(e["source"] != "#/properties/http" for e in result["edges"])


def test_target_not_a_schema():
    # $ref 解析到了 required 数组 / 具体字符串，不是 schema 节点
    schema = {
        "type": "object",
        "required": ["x"],
        "properties": {
            "to_array": {"$ref": "#/required"},
            "to_string": {"$ref": "#/type"},
            "to_bool_ok": {"$ref": "#/$defs/flag"},
        },
        "$defs": {"flag": False},
    }
    result = diagnose_references(schema)
    by_source = {r["source"]: r for r in result["invalid_refs"]}
    assert by_source["#/properties/to_array"]["reason"] == REASON_NOT_SCHEMA
    assert by_source["#/properties/to_string"]["reason"] == REASON_NOT_SCHEMA
    assert "#/properties/to_bool_ok" not in by_source
    assert any(e["target"] == "#/$defs/flag" for e in result["edges"])


# ---------------------------------------------------------------------------
# 不可达 $defs
# ---------------------------------------------------------------------------


def test_unreachable_defs_detected():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/used"}},
        "$defs": {
            "used": {"type": "string"},
            "orphan": {"type": "integer"},
            "chain": {"$ref": "#/$defs/orphan"},
        },
    }
    result = diagnose_references(schema)
    pointers = {d["pointer"] for d in result["unreachable_defs"]}
    assert pointers == {"#/$defs/orphan", "#/$defs/chain"}


def test_def_reachable_transitively_through_used_def():
    schema = {
        "$ref": "#/$defs/root",
        "$defs": {
            "root": {"allOf": [{"$ref": "#/$defs/leaf"}]},
            "leaf": {"type": "string"},
            "dead": {"type": "null"},
        },
    }
    result = diagnose_references(schema)
    pointers = {d["pointer"] for d in result["unreachable_defs"]}
    assert pointers == {"#/$defs/dead"}


def test_boolean_defs_reachability_kind():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/open"}},
        "$defs": {"open": True, "closed": False},
    }
    result = diagnose_references(schema)
    by_pointer = {d["pointer"]: d for d in result["unreachable_defs"]}
    assert "#/$defs/open" not in by_pointer
    assert by_pointer["#/$defs/closed"]["kind"] == "boolean"


def test_nested_defs_reachability():
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/outer/$defs/inner"}},
        "$defs": {
            "outer": {
                "type": "object",
                "$defs": {"inner": {"type": "string"}, "hidden": {"type": "null"}},
            },
            "ghost": {"type": "integer"},
        },
    }
    result = diagnose_references(schema)
    pointers = {d["pointer"] for d in result["unreachable_defs"]}
    assert "#/$defs/outer/$defs/inner" not in pointers
    # outer 本身只作为定义区容器，内部未被引用的 inner 成员仍不可达
    assert "#/$defs/outer/$defs/hidden" in pointers
    assert "#/$defs/ghost" in pointers


def test_defs_container_never_structurally_reachable():
    # 属性里直接内联一个同名 $defs 键不影响：定义区只能经引用进入
    schema = {
        "type": "object",
        "$defs": {"only_ref": {"$ref": "#/$defs/target"}, "target": {"type": "string"}},
    }
    result = diagnose_references(schema)
    pointers = {d["pointer"] for d in result["unreachable_defs"]}
    assert pointers == {"#/$defs/only_ref", "#/$defs/target"}


# ---------------------------------------------------------------------------
# 循环链
# ---------------------------------------------------------------------------


def test_self_loop_cycle():
    schema = {"$ref": "#/$defs/a", "$defs": {"a": {"$ref": "#/$defs/a"}}}
    result = diagnose_references(schema)
    assert result["cycles"] == [["#/$defs/a", "#/$defs/a"]]


def test_two_node_and_three_node_cycles():
    schema = {
        "$ref": "#/$defs/a",
        "$defs": {
            "a": {"$ref": "#/$defs/b"},
            "b": {"$ref": "#/$defs/a"},
            "c": {"$ref": "#/$defs/d"},
            "d": {"$ref": "#/$defs/e"},
            "e": {"$ref": "#/$defs/c"},
        },
    }
    result = diagnose_references(schema)
    assert ["#/$defs/a", "#/$defs/b", "#/$defs/a"] in result["cycles"]
    assert ["#/$defs/c", "#/$defs/d", "#/$defs/e", "#/$defs/c"] in result["cycles"]
    # 排序：链长短者在前
    assert len(result["cycles"][0]) == 3
    assert result["cycles_truncated"] is False


def test_unreachable_cycles_are_still_reported():
    # 环图本身是结构事实，即使整个环从根不可达也要报告
    schema = {
        "type": "object",
        "$defs": {
            "lonely1": {"$ref": "#/$defs/lonely2"},
            "lonely2": {"$ref": "#/$defs/lonely1"},
        },
    }
    result = diagnose_references(schema)
    assert len(result["cycles"]) == 1
    assert {d["pointer"] for d in result["unreachable_defs"]} == {
        "#/$defs/lonely1",
        "#/$defs/lonely2",
    }


def test_diamond_is_not_a_cycle():
    schema = {
        "$ref": "#/$defs/a",
        "$defs": {
            "a": {"allOf": [{"$ref": "#/$defs/b"}, {"$ref": "#/$defs/c"}]},
            "b": {"$ref": "#/$defs/d"},
            "c": {"$ref": "#/$defs/d"},
            "d": {"type": "string"},
        },
    }
    result = diagnose_references(schema)
    assert result["cycles"] == []
    assert result["unreachable_defs"] == []


def test_cycles_truncation_flag():
    # 环链报告上限为 MAX_CYCLES：构造超过上限的独立自环（每个 $def
    # 成员的 $ref 指向自身），枚举到上限即截断并置 truncated 标记。
    total = MAX_CYCLES + 10
    schema = {
        "$ref": "#/$defs/n0",
        "$defs": {f"n{i}": {"$ref": f"#/$defs/n{i}"} for i in range(total)},
    }
    result = diagnose_references(schema)
    assert len(result["cycles"]) == MAX_CYCLES
    assert result["cycles_truncated"] is True
    assert result["summary"]["cycles"] == MAX_CYCLES


# ---------------------------------------------------------------------------
# 汇总与顺序稳定性
# ---------------------------------------------------------------------------


def test_summary_counts_and_sorting_are_stable():
    schema = {
        "$defs": {
            "z": {"$ref": "#/$defs/z"},
            "a": {"properties": {"x": {"$ref": "#/nope"}}},
        },
        "properties": {"p": {"$ref": "#/$defs/z"}},
    }
    first = diagnose_references(schema)
    second = diagnose_references(schema)
    assert first == second
    assert first["summary"] == {
        "total_refs": 3,
        "valid_edges": 2,
        "invalid_refs": 1,
        "unreachable_defs": 1,
        "cycles": 1,
    }
    # a 自引用成环且被 p 间接... 实际 a 不可达（其内部坏 ref 不产生边）
    unreachable = [d["pointer"] for d in first["unreachable_defs"]]
    assert unreachable == ["#/$defs/a"]


def test_schema_without_refs():
    result = diagnose_references({"type": "object", "properties": {"a": {"type": "string"}}})
    assert result["edges"] == []
    assert result["invalid_refs"] == []
    assert result["unreachable_defs"] == []
    assert result["cycles"] == []
    assert result["summary"]["total_refs"] == 0


def test_aliased_def_member_reachable_via_any_name():
    shared = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/x"}},
        "$defs": {"x": shared, "y": shared},
    }
    result = diagnose_references(schema)
    # 同一对象挂两个名字：经 x 引用可达即按对象身份视为可达，
    # 两个成员位置都不报不可达
    assert result["unreachable_defs"] == []


def test_aliased_def_member_unreachable_when_no_alias_reachable():
    shared = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "$defs": {"x": shared, "y": shared},
    }
    result = diagnose_references(schema)
    # 两个挂载位置都不可达：逐成员指针输出
    assert {d["pointer"] for d in result["unreachable_defs"]} == {
        "#/$defs/x",
        "#/$defs/y",
    }
