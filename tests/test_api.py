"""FastAPI 接口测试（TestClient + 临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_analysis(client):
    resp = client.post("/api/analyses", json={
        "name": "user-schema-v1-v2",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    analysis_id = data["id"]
    assert data["report"]["backward_compatible"] is False
    assert data["report"]["forward_compatible"] is True
    assert data["report"]["summary"]["errors"] >= 1

    # 反例字段存在
    issues = data["report"]["issues"]
    backward = [i for i in issues if i["direction"] == "backward"]
    assert backward
    assert any(i["counterexample"] is not None for i in backward)

    # 样例结果
    assert data["report"]["sample_results"][0]["valid_against_new"] is False

    # 查询报告
    resp2 = client.get(f"/api/analyses/{analysis_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == analysis_id

    # 列表
    resp3 = client.get("/api/analyses")
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


def test_get_missing_returns_404(client):
    resp = client.get("/api/analyses/999999")
    assert resp.status_code == 404


def test_invalid_schema_returns_400(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "not-a-real-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "old_schema"


def test_compatible_schemas(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "new_schema": {"type": "object",
                       "properties": {"a": {"type": "string"},
                                      "b": {"type": "integer"}}},
    })
    assert resp.status_code == 201
    report = resp.json()["report"]
    assert report["backward_compatible"]
    assert report["forward_compatible"]
    assert report["summary"]["errors"] == 0


def test_conditional_if_then_else_end_to_end(client):
    """draft-07+ 条件分支：then 收紧 -> 向后破坏，issue 定位到 #/then。"""
    if_a = {"required": ["kind"], "properties": {"kind": {"const": "a"}}}
    old = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x"]}}
    new = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x", "y"]}}
    resp = client.post("/api/analyses", json={
        "name": "conditional", "old_schema": old, "new_schema": new,
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()["report"]
    assert report["backward_compatible"] is False
    assert report["forward_compatible"] is True
    issue = [i for i in report["issues"] if i["direction"] == "backward"][0]
    assert issue["code"] == "required_added"
    assert issue["path"] == "#/then"
    assert issue["severity"] == "error"
    assert issue["counterexample"]["kind"] == "a"


# ---------------------------------------------------------------------------
# POST /api/analyses/compare
# ---------------------------------------------------------------------------

COMPARE_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
}
# v2：a 收紧 minLength、新增必填 b
COMPARE_V2 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "b": {"type": "integer"}},
    "required": ["a", "b"],
}
# v3：b 不再必填（修复）、新增必填 c（新引入）、a 的 minLength 保持（仍存在）
COMPARE_V3 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "c": {"type": "string"}},
    "required": ["a", "c"],
}


def _create(client, old_schema, new_schema, name=None):
    resp = client.post("/api/analyses", json={
        "name": name, "old_schema": old_schema, "new_schema": new_schema,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compare_added_fixed_persisting(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2, "baseline")
    current = _create(client, COMPARE_OLD, COMPARE_V3, "current")

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["baseline_id"] == baseline["id"]
    assert data["current_id"] == current["id"]
    assert data["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                              "severity_changed": 0}

    added = data["issues"]["added"]
    fixed = data["issues"]["fixed"]
    persisting = data["issues"]["persisting"]
    assert [i["code"] for i in added] == ["required_added"]
    assert "'c'" in added[0]["reason"]
    assert [i["code"] for i in fixed] == ["required_added"]
    assert "'b'" in fixed[0]["reason"]
    assert [i["code"] for i in persisting] == ["min_length_increased"]

    # 每条对比 issue 都带稳定指纹
    for group in (added, fixed, persisting):
        for entry in group:
            assert len(entry["fingerprint"]) == 16

    # 双向兼容性：两个方向都保持不兼容 -> changed 为 False
    assert data["compatibility"]["backward"] == {
        "baseline": False, "current": False, "changed": False,
    }
    assert data["compatibility"]["forward"] == {
        "baseline": True, "current": True, "changed": False,
    }

    # 错误 / 警告汇总差异
    diff = data["summary_diff"]
    assert diff["errors"]["baseline"] == baseline["report"]["summary"]["errors"]
    assert diff["errors"]["current"] == current["report"]["summary"]["errors"]
    assert diff["errors"]["delta"] == (
        current["report"]["summary"]["errors"]
        - baseline["report"]["summary"]["errors"]
    )
    assert diff["warnings"]["delta"] == 0
    assert diff["backward"]["errors"]["baseline"] == 2


def test_compare_severity_only_change(client):
    # 同一 integer_required 问题：baseline 可构造验证反例（error），
    # current 中 maximum 限制使反例不再成立（降级 warning）
    baseline = _create(client, {"minimum": 0}, {"type": "integer"})
    current = _create(client, {"minimum": 0, "maximum": 1}, {"type": "integer"})

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    changed = data["issues"]["severity_changed"]
    assert data["counts"]["severity_changed"] == 1
    assert changed[0]["code"] == "integer_required"
    assert changed[0]["baseline_severity"] == "error"
    assert changed[0]["current_severity"] == "warning"
    # 仍存在：type_restricted 两份报告都有
    assert any(i["code"] == "type_restricted" for i in data["issues"]["persisting"])


def test_compare_does_not_modify_reports(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2)
    current = _create(client, COMPARE_OLD, COMPARE_V3)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200

    for original in (baseline, current):
        after = client.get(f"/api/analyses/{original['id']}").json()
        assert after["report"] == original["report"]
        assert after["old_schema"] == original["old_schema"]
        assert after["new_schema"] == original["new_schema"]


def test_compare_self_all_persisting(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)
    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": analysis["id"],
    })
    assert resp.status_code == 200
    data = resp.json()
    total = analysis["report"]["summary"]["total"]
    assert data["counts"]["persisting"] == total
    assert data["counts"]["added"] == 0
    assert data["counts"]["fixed"] == 0


def test_compare_missing_analysis_returns_404(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": 999999, "current_id": analysis["id"],
    })
    assert resp.status_code == 404

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": 999999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate
# ---------------------------------------------------------------------------

# backward：required_added error（#）+ 两条 pattern_changed warning
# forward：两条 pattern_changed warning
GATE_OLD = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^x"},
        "b": {"type": "string", "pattern": "^y"},
    },
}
GATE_NEW = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^xx"},
        "b": {"type": "string", "pattern": "^yy"},
    },
    "required": ["b"],
}


def _gate_report(client):
    return _create(client, GATE_OLD, GATE_NEW, "gate-case")


def test_gate_error_blocks_and_counts(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass"] is False
    counts = data["counts"]["backward"]
    assert counts["errors"] == 1
    assert counts["warnings"] == 2
    assert counts["exempted"] == 0
    assert counts["total"] == 3
    assert counts["max_warnings"] == 0
    codes = [(i["code"], i["blocked_reason"]) for i in data["blocking"]]
    assert ("required_added", "error") in codes
    assert sum(1 for _, reason in codes if reason == "warning_limit_exceeded") == 2
    assert data["exempted"] == []


def test_gate_warning_cap_pass_and_overflow(client):
    analysis = _gate_report(client)
    # cap=1：required error 豁免后只剩 2 条 warning，超 1 条
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 1},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is False
    assert [b["path"] for b in data["blocking"]] == ["#/properties/b"]
    assert data["blocking"][0]["blocked_reason"] == "warning_limit_exceeded"
    assert data["counts"]["backward"]["warnings"] == 2
    assert data["counts"]["backward"]["errors"] == 0
    assert data["exempted"][0]["rule_index"] == 0

    # cap=2：全部 warning 在额度内 -> 通过
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 2},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    assert resp.json()["pass"] is True
    assert resp.json()["blocking"] == []


def test_gate_wildcard_exemption_and_rule_index(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            # 下标 0：通配规则命中两条 pattern_changed
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
            # 下标 1：精确规则命中 required_added
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is True
    rule_indices = sorted((e["path"], e["rule_index"]) for e in data["exempted"])
    assert rule_indices == [
        ("#", 1),
        ("#/properties/a", 0),
        ("#/properties/b", 0),
    ]
    # 豁免项不计数
    assert data["counts"]["backward"] == {
        "errors": 0, "warnings": 0, "exempted": 3, "total": 3,
        "max_warnings": 0,
    }


def test_gate_exact_rule_beats_later_wildcard(client):
    analysis = _gate_report(client)
    # 两条规则都命中 #/properties/a 的 pattern_changed：
    # 下标 0 精确 path（3 精确字段），下标 1 通配 path（2 精确字段）；
    # 即使通配规则声明更晚，也必须记到精确规则。
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/a"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    data = resp.json()
    hit_a = [e for e in data["exempted"] if e["path"] == "#/properties/a"]
    hit_b = [e for e in data["exempted"] if e["path"] == "#/properties/b"]
    assert len(hit_a) == 1 and hit_a[0]["rule_index"] == 0
    assert len(hit_b) == 1 and hit_b[0]["rule_index"] == 1
    # required_added 未被豁免 -> 门禁仍失败
    assert data["pass"] is False


def test_gate_unchecked_direction_ignored(client):
    analysis = _gate_report(client)
    # 只检查 forward：backward 的 error 完全不影响结论；forward 2 条 warning，cap=2
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["forward"],
        "max_warnings": {"forward": 2, "backward": 0},
    })
    data = resp.json()
    assert data["pass"] is True
    assert set(data["counts"]) == {"forward"}
    assert data["counts"]["forward"]["warnings"] == 2


def test_gate_clean_direction_passes(client):
    # 新增可选字段：两个方向都没有 issue
    analysis = _create(
        client,
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string"},
                                           "b": {"type": "integer"}}},
    )
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward", "forward"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["pass"] is True
    assert data["blocking"] == []
    assert data["exempted"] == []
    assert data["counts"]["backward"]["total"] == 0
    assert data["counts"]["forward"]["total"] == 0


def test_gate_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_gate_validation_errors(client):
    analysis = _gate_report(client)
    base = f"/api/analyses/{analysis['id']}/gate"

    # directions 不能为空
    resp = client.post(base, json={"directions": []})
    assert resp.status_code == 422

    # 非法方向
    resp = client.post(base, json={"directions": ["sideways"]})
    assert resp.status_code == 422

    # 非法 path（不以 #/ 开头）
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "x", "path": "properties/a"}],
    })
    assert resp.status_code == 422

    # 负的 warning 上限
    resp = client.post(base, json={
        "directions": ["backward"], "max_warnings": {"backward": -1},
    })
    assert resp.status_code == 422

    # code 不能为空
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "", "path": "#"}],
    })
    assert resp.status_code == 422


def test_gate_does_not_modify_report(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    assert resp.status_code == 200

    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 合规画像
# ---------------------------------------------------------------------------

PROFILE_SCHEMA_OLD = {"type": "object"}
PROFILE_SCHEMA_NEW = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "age": {"type": "integer", "minimum": 0},
        "kind": {
            "oneOf": [
                {"type": "object",
                 "properties": {"t": {"const": "a"}, "v": {"type": "string"}},
                 "required": ["t", "v"]},
                {"type": "object",
                 "properties": {"t": {"const": "b"}, "v": {"type": "integer"}},
                 "required": ["t", "v"]},
            ]
        },
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}


def _profile_analysis(client):
    return _create(client, PROFILE_SCHEMA_OLD, PROFILE_SCHEMA_NEW, "profile-case")


def test_profile_validates_against_new_schema(client):
    analysis = _profile_analysis(client)
    documents = [
        {"name": "ok", "age": 3, "kind": {"t": "a", "v": "x"}},  # 合规
        {"age": 1, "name": 2},                                    # name 类型错
        {"name": "z", "age": 1, "kind": {"t": "a", "v": 7}},      # oneOf 失败
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": documents})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["analysis_id"] == analysis["id"]
    assert data["version"] == 1
    assert data["total_documents"] == 3
    assert data["valid_documents"] == 1
    assert data["invalid_documents"] == 2
    assert data["compliance_rate"] == 1 / 3
    for group in data["failures"]:
        assert len(group["fingerprint"]) == 16
        assert group["pointer"].startswith("#")
        assert group["count"] >= 1
        assert group["example_indices"] == sorted(group["example_indices"])

    # oneOf 失败归并到最深公共路径 #/kind
    kind = [g for g in data["failures"] if g["pointer"] == "#/kind"]
    assert len(kind) == 1
    assert kind[0]["validator"] == "oneOf"
    assert kind[0]["count"] == 1
    assert kind[0]["example_indices"] == [2]

    # 关联持久化：GET 最新快照与 POST 返回一致
    resp2 = client.get(f"/api/analyses/{analysis['id']}/profile")
    assert resp2.status_code == 200
    assert resp2.json() == data


def test_profile_versions_increment_and_listing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    first = client.post(base + "/profile", json={"documents": [{"name": "a", "age": 1}]}).json()
    second = client.post(base + "/profile", json={"documents": [{"name": "b", "age": 2}]}).json()
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]

    # GET 最新返回 version=2
    latest = client.get(base + "/profile").json()
    assert latest["version"] == 2

    # 按 ID 取指定快照
    by_id = client.get(f"{base}/profiles/{first['id']}").json()
    assert by_id["version"] == 1

    # 列表
    resp = client.get(base + "/profiles")
    assert resp.status_code == 200
    listing = resp.json()
    assert listing["analysis_id"] == analysis["id"]
    assert listing["total"] == 2
    assert [item["version"] for item in listing["items"]] == [2, 1]
    summary = listing["items"][0]
    assert summary["total_documents"] == 1
    assert summary["valid_documents"] == 1
    assert summary["compliance_rate"] == 1.0
    assert summary["failure_groups"] == 0


def test_profile_order_independent_grouping(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"
    documents = [
        {"name": "y", "age": 1, "kind": {"t": "a", "v": 1}},
        {"name": "z", "age": 2, "kind": {"t": "a", "v": 2}},
        {"age": 3, "name": 4},
    ]
    a = client.post(base + "/profile", json={"documents": documents}).json()
    b = client.post(base + "/profile", json={"documents": list(reversed(documents))}).json()

    def fingerprint(result):
        return [(g["pointer"], g["validator"], g["fingerprint"], g["count"],
                 g["message"]) for g in result["failures"]]

    assert fingerprint(a) == fingerprint(b)


def test_profile_too_many_documents_returns_422_and_writes_nothing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    # 5001 个文档 -> 422，且不写画像记录
    resp = client.post(base + "/profile",
                       json={"documents": [{"name": "x", "age": 1}] * 5001})
    assert resp.status_code == 422

    listing = client.get(base + "/profiles").json()
    assert listing["total"] == 0
    assert client.get(base + "/profile").status_code == 404

    # 空文档列表也 422
    assert client.post(base + "/profile", json={"documents": []}).status_code == 422


def test_profile_non_object_document_returns_422_and_writes_nothing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    for bad in ([1, 2, 3], ["str"], [None], [True], [[]]):
        resp = client.post(base + "/profile", json={"documents": bad})
        assert resp.status_code == 422, bad

    # 混合列表中只要有非对象就 422
    resp = client.post(base + "/profile",
                       json={"documents": [{"name": "x", "age": 1}, "not-an-object"]})
    assert resp.status_code == 422

    listing = client.get(base + "/profiles").json()
    assert listing["total"] == 0


def test_profile_does_not_modify_analysis(client):
    analysis = _profile_analysis(client)
    client.post(f"/api/analyses/{analysis['id']}/profile",
                json={"documents": [{"name": "x", "age": 1}]})
    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["new_schema"] == analysis["new_schema"]
    assert after["old_schema"] == analysis["old_schema"]


def test_profile_missing_analysis_returns_404(client):
    assert client.post("/api/analyses/999999/profile",
                       json={"documents": [{}]}).status_code == 404
    assert client.get("/api/analyses/999999/profile").status_code == 404
    assert client.get("/api/analyses/999999/profiles").status_code == 404


def test_profile_cross_analysis_id_is_404(client):
    a1 = _profile_analysis(client)
    a2 = _profile_analysis(client)
    created = client.post(f"/api/analyses/{a1['id']}/profile",
                          json={"documents": [{"name": "x", "age": 1}]}).json()
    # 画像属于 a1，用 a2 的命名空间访问 -> 404
    resp = client.get(f"/api/analyses/{a2['id']}/profiles/{created['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay 样本回放诊断
# ---------------------------------------------------------------------------

REPLAY_OLD = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["active", "disabled", "pending"]}},
}
REPLAY_NEW = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def _replay_analysis(client):
    return _create(client, REPLAY_OLD, REPLAY_NEW, "replay-case")


def test_replay_classifies_and_attributes_hits(client):
    analysis = _replay_analysis(client)
    documents = [
        {"status": "pending"},                    # 0 旧合法新拒绝 -> enum + required(owner)
        {"status": "active"},                     # 1 旧合法新拒绝 -> required(owner)
        {"status": "active", "owner": "me"},      # 2 两版合法
        42,                                       # 3 两版都拒绝 -> invalid_both
        "bad",                                    # 4 两版都拒绝 -> invalid_both
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": documents})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["analysis_id"] == analysis["id"]
    assert data["total_documents"] == 5
    assert data["counts"] == {
        "valid": 1, "backward": 2, "forward": 0, "invalid_both": 2,
    }

    hits = {(h["direction"], h["path"], h["code"]): h for h in data["issue_hits"]}
    enum_hit = hits[("backward", "#/properties/status", "enum_value_removed")]
    assert enum_hit["count"] == 1
    assert enum_hit["example_indices"] == [0]
    # doc0 与 doc1 都缺少 owner（同一文档对同一 issue 仅计一次）
    required_hit = hits[("backward", "#", "required_added")]
    assert required_hit["count"] == 2
    assert required_hit["example_indices"] == [0, 1]

    # 报告中的每条 issue 都会输出（未命中 count=0）
    assert len(data["issue_hits"]) == len(analysis["report"]["issues"])

    # 两版都拒绝的文档进入 unattributed
    both = data["unattributed"]
    assert all(u["reason"] == "rejected_by_both" and u["direction"] == "both"
               for u in both)
    root_type = [u for u in both if u["pointer"] == "#" and u["validator"] == "type"]
    assert root_type and root_type[0]["count"] == 2
    assert root_type[0]["example_indices"] == [3, 4]


def test_replay_forward_direction(client):
    # 旧版必填 email、新版删除 -> 新版合法文档被旧版拒绝（forward）
    old = {"type": "object", "required": ["email"],
           "properties": {"email": {"type": "string"}}}
    new = {"type": "object", "properties": {"email": {"type": "string"}}}
    analysis = _create(client, old, new)

    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": [{"email": "a@b.c"}, {}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["counts"] == {
        "valid": 1, "backward": 0, "forward": 1, "invalid_both": 0,
    }
    hit = [h for h in data["issue_hits"] if h["count"] == 1]
    assert len(hit) == 1
    assert hit[0]["direction"] == "forward"
    assert hit[0]["path"] == "#"
    assert hit[0]["code"] == "required_added"


def test_replay_accepts_non_object_documents(client):
    # 与画像接口不同：回放接受任意 JSON 类型文档
    analysis = _replay_analysis(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": [1, "str", None, True, [], {"status": "x"}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_documents"] == 6
    assert data["counts"]["invalid_both"] + data["counts"]["backward"] \
        + data["counts"]["forward"] + data["counts"]["valid"] == 6


def test_replay_does_not_modify_analysis_or_profiles(client):
    analysis = _replay_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    resp = client.post(base + "/replay", json={"documents": [
        {"status": "pending"}, {"status": "active"}, 42,
    ]})
    assert resp.status_code == 200

    after = client.get(base).json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]
    assert after["samples"] == analysis["samples"]

    # 回放不产生任何画像快照
    assert client.get(base + "/profiles").json()["total"] == 0

    # 多次回放结果一致（无持久化副作用）
    again = client.post(base + "/replay",
                        json={"documents": [{"status": "pending"}]}).json()
    assert again["counts"]["backward"] == 1


def test_replay_validation_errors(client):
    analysis = _replay_analysis(client)
    base = f"/api/analyses/{analysis['id']}/replay"

    # 空列表 -> 422
    assert client.post(base, json={"documents": []}).status_code == 422
    # 5001 个文档 -> 422
    assert client.post(base, json={"documents": [{}] * 5001}).status_code == 422
    # 缺少 documents 字段 -> 422
    assert client.post(base, json={}).status_code == 422


def test_replay_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/replay", json={"documents": [{}]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_replay_after_profile_still_readonly(client):
    analysis = _replay_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    # 先写一张画像，再回放；画像快照数量与内容不受回放影响
    profile_resp = client.post(base + "/profile",
                               json={"documents": [{"status": "active", "owner": "me"}]})
    assert profile_resp.status_code == 201
    client.post(base + "/replay", json={"documents": [{"status": "pending"}] * 3})
    listing = client.get(base + "/profiles").json()
    assert listing["total"] == 1
    assert listing["items"][0]["total_documents"] == 1


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay-coverage 回放样本最小覆盖
# ---------------------------------------------------------------------------

# 两个根级必填字段 status / owner + 枚举收窄：
# {"status": "pending"} 同时覆盖 required(owner) 与 enum_value_removed
COVERAGE_OLD = {
    "type": "object",
    "properties": {"status": {"type": "string",
                              "enum": ["active", "disabled", "pending"]}},
}
COVERAGE_NEW = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def _coverage_analysis(client):
    return _create(client, COVERAGE_OLD, COVERAGE_NEW, "coverage-case")


def test_replay_coverage_greedy_selection(client):
    analysis = _coverage_analysis(client)
    documents = [
        {"status": "active", "owner": "me"},  # 0 valid，零覆盖
        {"status": "pending"},                # 1 一次覆盖 enum + required(owner)
        {"status": "active"},                 # 2 只覆盖 required(owner)
        42,                                   # 3 两版都拒绝，零覆盖
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/replay-coverage",
                       json={"documents": documents, "directions": ["backward"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["analysis_id"] == analysis["id"]
    assert data["directions"] == ["backward"]
    assert data["total_documents"] == 4
    # doc1 一轮覆盖两条 backward issue，是唯一代表文档
    assert data["selected_indices"] == [1]
    selected = data["selected_documents"]
    assert len(selected) == 1
    assert selected[0]["index"] == 1
    assert selected[0]["classification"] == "backward"
    assert selected[0]["newly_covered_issues"] == 2
    assert selected[0]["covered_issues"] == 2

    # 报告共 3 条 backward issue（required owner/status + enum），缺 status 的
    # 一条没有文档能覆盖 -> uncovered
    assert data["total_issues"] == 3
    assert data["covered_issue_count"] == 2
    assert data["uncovered_issue_count"] == 1
    covered_codes = sorted(c["code"] for c in data["covered_issues"])
    assert covered_codes == ["enum_value_removed", "required_added"]
    for issue in data["covered_issues"]:
        assert issue["covered_by"] == [1]
    assert data["uncovered_issues"][0]["code"] == "required_added"
    assert "'status'" in data["uncovered_issues"][0]["reason"]

    # 原始四向分类统计
    assert data["counts"] == {
        "valid": 1, "backward": 2, "forward": 0, "invalid_both": 1,
    }


def test_replay_coverage_tie_break_by_index_and_multi_round(client):
    # 旧版无必填，新版必填 a / b：单篇文档无法同时缺 a 又缺 b 时需多轮选择
    analysis = _create(
        client,
        {"type": "object", "properties": {
            "a": {"type": "string"}, "b": {"type": "string"}}},
        {"type": "object", "properties": {
            "a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"]},
    )
    documents = [
        {"a": "0"},   # 0 只缺 b
        {"a": "1"},   # 1 只缺 b，与 doc0 并列
        {"b": "2"},   # 2 只缺 a
        {"a": "3", "b": "3"},  # 3 valid
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/replay-coverage",
                       json={"documents": documents,
                             "directions": ["backward", "forward"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 并列取最小索引 doc0，再选 doc2 覆盖另一条 issue
    assert data["selected_indices"] == [0, 2]
    assert [d["newly_covered_issues"] for d in data["selected_documents"]] == [1, 1]
    assert data["covered_issue_count"] == 2
    assert data["uncovered_issues"] == []


def test_replay_coverage_direction_filter(client):
    # 旧版必填 email（forward），新版必填 name（backward）
    old = {"type": "object", "required": ["email"], "properties": {
        "email": {"type": "string"}, "name": {"type": "string"}}}
    new = {"type": "object", "required": ["name"], "properties": {
        "email": {"type": "string"}, "name": {"type": "string"}}}
    analysis = _create(client, old, new)
    documents = [{"name": "n"}, {"email": "e"}]  # forward, backward

    resp = client.post(f"/api/analyses/{analysis['id']}/replay-coverage",
                       json={"documents": documents, "directions": ["forward"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["directions"] == ["forward"]
    assert data["total_issues"] == 1
    assert data["selected_indices"] == [0]
    assert data["selected_documents"][0]["classification"] == "forward"
    # 分类统计仍是全部文档的原始统计
    assert data["counts"] == {
        "valid": 0, "backward": 1, "forward": 1, "invalid_both": 0,
    }

    # 重复方向自动去重并按固定顺序输出
    resp2 = client.post(f"/api/analyses/{analysis['id']}/replay-coverage",
                        json={"documents": documents,
                              "directions": ["forward", "backward", "forward"]})
    assert resp2.status_code == 200
    assert resp2.json()["directions"] == ["backward", "forward"]


def test_replay_coverage_readonly_and_deterministic(client):
    analysis = _coverage_analysis(client)
    base = f"/api/analyses/{analysis['id']}"
    payload = {"documents": [{"status": "pending"}, 42],
               "directions": ["backward"]}

    first = client.post(base + "/replay-coverage", json=payload)
    assert first.status_code == 200, first.text
    second = client.post(base + "/replay-coverage", json=payload)
    assert second.json() == first.json()

    after = client.get(base).json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]
    assert client.get(base + "/profiles").json()["total"] == 0


def test_replay_coverage_validation_errors(client):
    analysis = _coverage_analysis(client)
    base = f"/api/analyses/{analysis['id']}/replay-coverage"

    # 空文档列表 -> 422
    assert client.post(base, json={"documents": [],
                                   "directions": ["backward"]}).status_code == 422
    # 5001 个文档 -> 422
    assert client.post(base, json={"documents": [{}] * 5001,
                                   "directions": ["backward"]}).status_code == 422
    # 缺少 documents -> 422
    assert client.post(base, json={"directions": ["backward"]}).status_code == 422
    # directions 为空 -> 422
    assert client.post(base, json={"documents": [{}],
                                   "directions": []}).status_code == 422
    # 缺少 directions -> 422
    assert client.post(base, json={"documents": [{}]}).status_code == 422
    # 非法方向 -> 422
    assert client.post(base, json={"documents": [{}],
                                   "directions": ["sideways"]}).status_code == 422


def test_replay_coverage_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/replay-coverage",
                       json={"documents": [{}], "directions": ["backward"]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/mutation-coverage 样本变异覆盖评估
# ---------------------------------------------------------------------------

MUTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
        "age": {"type": "integer", "minimum": 0, "maximum": 100},
        "tags": {"type": "array", "minItems": 1,
                 "items": {"type": "string", "maxLength": 8}},
    },
    "required": ["status", "age"],
}


def _mutation_analysis(client):
    return _create(client, {"type": "object"}, MUTATION_SCHEMA, "mutation-case")


def test_mutation_coverage_scores_and_uncovered_paths(client):
    analysis = _mutation_analysis(client)
    documents = [
        {"status": "pending", "age": 0, "tags": ["ok"]},      # 杀 pending 枚举 / minimum
        {"status": "active", "age": 100, "tags": ["x"]},      # 杀 active 枚举 / maximum
        {"status": "active", "tags": ["x"]},                  # 负向：缺 age
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/mutation-coverage",
                       json={"documents": documents})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["analysis_id"] == analysis["id"]
    assert data["total_documents"] == 3
    assert data["valid_documents"] == 2
    assert data["invalid_documents"] == 1
    assert data["total_mutations"] == data["killed_mutations"] \
        + data["surviving_mutations"]
    assert 0 < data["mutation_score"] < 1

    rules = {r["rule"]: r for r in data["rules"]}
    assert rules["enum_narrowed"]["killed"] == 2
    assert rules["enum_narrowed"]["surviving"] == 1
    assert rules["minimum_increased"]["killed"] == 1
    assert rules["maximum_decreased"]["killed"] == 1
    # 缺 age 的负向样本杀死 required_removed(age)；没有缺 status 的样本
    required = rules["required_removed"]
    assert required["killed"] == 1 and required["surviving"] == 1

    # 未覆盖变异路径与示例索引
    surviving_disabled = [
        m for m in data["mutants"]
        if m["rule"] == "enum_narrowed" and not m["killed"]
    ]
    assert len(surviving_disabled) == 1
    assert surviving_disabled[0]["subject"] == '"disabled"'
    assert surviving_disabled[0]["pointer"] == "#/properties/status"
    assert surviving_disabled[0]["example_indices"] == []
    assert surviving_disabled[0]["pointer"] in data["uncovered_paths"]

    killed_age = next(m for m in data["mutants"]
                      if m["rule"] == "required_removed" and m["subject"] == "age")
    assert killed_age["killed"] is True
    assert killed_age["example_indices"] == [2]

    # 每个变异体都有稳定 ID 与确定性描述
    for mutant in data["mutants"]:
        assert len(mutant["id"]) == 12
        assert mutant["pointer"].startswith("#")
        assert mutant["description"]

    # limits 回填缺省值
    assert data["limits"]["enum_narrowed"] == 50


def test_mutation_coverage_per_rule_limits(client):
    analysis = _mutation_analysis(client)
    base = f"/api/analyses/{analysis['id']}/mutation-coverage"

    # 关闭 required_removed，只保留 1 个枚举变异
    resp = client.post(base, json={
        "documents": [{"status": "pending", "age": 1}],
        "max_per_rule": {"required_removed": 0, "enum_narrowed": 1},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    rules = {r["rule"]: r for r in data["rules"]}
    assert rules["required_removed"]["total"] == 0
    assert rules["enum_narrowed"]["total"] == 1
    assert data["limits"]["required_removed"] == 0
    assert data["limits"]["enum_narrowed"] == 1
    # 未声明的规则仍为缺省上限
    assert data["limits"]["minimum_increased"] == 50


def test_mutation_coverage_is_readonly_and_deterministic(client):
    analysis = _mutation_analysis(client)
    base = f"/api/analyses/{analysis['id']}/mutation-coverage"
    payload = {"documents": [
        {"status": "pending", "age": 0, "tags": ["ok"]},
        {"status": "active"},
    ]}

    first = client.post(base, json=payload)
    assert first.status_code == 200, first.text
    second = client.post(base, json=payload)
    assert second.json() == first.json()

    # 不修改分析记录，也不产生画像快照
    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["new_schema"] == analysis["new_schema"]
    assert client.get(f"/api/analyses/{analysis['id']}/profiles").json()["total"] == 0


def test_mutation_coverage_accepts_non_object_documents(client):
    analysis = _mutation_analysis(client)
    # 与画像接口不同：变异评估接受任意 JSON 类型文档（根 schema 无 type 时）
    loose = _create(
        client,
        {"type": "object"},
        {"type": "object", "properties": {"a": {"enum": [1, 2]}},
         "required": ["a"]},
    )
    resp = client.post(f"/api/analyses/{loose['id']}/mutation-coverage",
                       json={"documents": [1, "str", None, True, [], {"a": 1}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_documents"] == 6
    assert data["valid_documents"] + data["invalid_documents"] == 6


def test_mutation_coverage_validation_errors(client):
    analysis = _mutation_analysis(client)
    base = f"/api/analyses/{analysis['id']}/mutation-coverage"

    # 空文档列表 -> 422
    assert client.post(base, json={"documents": []}).status_code == 422
    # 缺少 documents 字段 -> 422
    assert client.post(base, json={}).status_code == 422
    # 超过文档上限 -> 422
    assert client.post(base, json={"documents": [{}] * 5001}).status_code == 422
    # 未知变异规则 -> 422
    resp = client.post(base, json={
        "documents": [{"status": "active", "age": 1}],
        "max_per_rule": {"not_a_rule": 10},
    })
    assert resp.status_code == 422
    # 负的上限 -> 422
    resp = client.post(base, json={
        "documents": [{"status": "active", "age": 1}],
        "max_per_rule": {"enum_narrowed": -1},
    })
    assert resp.status_code == 422


def test_mutation_coverage_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/mutation-coverage",
                       json={"documents": [{}]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"
