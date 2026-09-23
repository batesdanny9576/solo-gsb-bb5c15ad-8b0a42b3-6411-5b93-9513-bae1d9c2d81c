"""单份 JSON Schema 的 ``$ref`` 引用依赖诊断引擎。

只处理**文档内片段形式**的本地引用（``#`` 与 ``#/...``，JSON Pointer
RFC 6901），绝不访问网络或外部文件：

* ``https://example.com/schema.json``、``schema.json#/$defs/a`` 等带
  scheme / 非空 document 部分的引用一律记为 ``external_reference_not_allowed``；
* ``#foo`` 不是合法的 JSON Pointer 片段（plain-name fragment），记为
  ``invalid_pointer_syntax``；
* ``#/a~2`` 之类含非法转义的片段记为 ``invalid_pointer_syntax``；
* 指针转义合法但在文档中解析不到目标（中间节点非容器、键不存在、
  数组越界 / 非数字下标）记为 ``unresolvable_target``。

诊断结果包含四类信息：

1. ``edges``：成功解析的引用边（来源指针 -> 目标指针，含原始引用串）；
2. ``invalid_refs``：每个无法作为本地引用解析的 ``$ref`` 的来源定位、
   原始值与失败原因；
3. ``unreachable_defs``：从有效 schema 结构出发、不经任何引用边无法
   到达的 ``$defs`` 成员（``$defs`` 容器本身视为定义区，不参与结构遍历）；
4. ``cycles``：引用有向图上的全部基本环（每条环链首尾指针相同），
   确定性枚举，超过 :data:`MAX_CYCLES` 时截断并置 ``cycles_truncated``。

整个诊断为纯函数，不读不写数据库，也不做任何外部 IO。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

# 单个 schema 报告的环链上限：基本环最坏可达指数级，超出后只报前若干条
MAX_CYCLES = 200

REASON_EXTERNAL = "external_reference_not_allowed"
REASON_SYNTAX = "invalid_pointer_syntax"
REASON_UNRESOLVABLE = "unresolvable_target"
REASON_NOT_SCHEMA = "target_not_a_schema"

INVALID_REASONS = (
    REASON_EXTERNAL,
    REASON_SYNTAX,
    REASON_UNRESOLVABLE,
    REASON_NOT_SCHEMA,
)


def render_pointer(tokens: list[str]) -> str:
    """把未转义的路径段列表渲染成 ``#`` / ``#/a/b`` 风格 JSON Pointer。"""
    if not tokens:
        return "#"
    return "#/" + "/".join(_escape(token) for token in tokens)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_token(token: str) -> str | None:
    """校验单个 pointer token 的转义并反转义；非法转义返回 None。"""
    i = 0
    out: list[str] = []
    while i < len(token):
        ch = token[i]
        if ch == "~":
            if i + 1 >= len(token) or token[i + 1] not in ("0", "1"):
                return None
            out.append("~" if token[i + 1] == "0" else "/")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def classify_ref(ref: Any) -> tuple[str | None, str | None, str | None]:
    """分类一个 ``$ref`` 值。

    返回 ``(状态, 原因, 诊断信息)``：
    合法本地引用时状态为 ``"ok"``；否则状态为 None，后两者给出
    :data:`INVALID_REASONS` 中的原因码与中文说明。
    """
    if not isinstance(ref, str):
        return None, REASON_SYNTAX, "$ref 必须是字符串"

    # URI fragment 必须形如 # 或 #/...（fragment 按 RFC 3986 先百分号解码）
    if not ref.startswith("#"):
        return None, REASON_EXTERNAL, "仅支持文档内片段引用（# 或 #/...），不访问网络或外部文件"
    fragment = unquote(ref[1:])
    if fragment == "":
        return "ok", None, None
    if not fragment.startswith("/"):
        return None, REASON_SYNTAX, "片段必须是 JSON Pointer（空串 # 或以 #/ 开头），不支持 #foo 形式"

    tokens: list[str] = []
    for raw_token in fragment[1:].split("/"):
        token = _unescape_token(raw_token)
        if token is None:
            return None, REASON_SYNTAX, f"非法 JSON Pointer 转义: {raw_token}"
        tokens.append(token)
    return "ok", None, None


def resolve_pointer(root: Any, tokens: list[str]) -> tuple[Any, list[str]] | None:
    """按 JSON Pointer 在 root 内解析目标。

    成功返回 ``(目标节点, 目标节点的规范路径段)``；中途缺键 / 越界 /
    容器类型不匹配返回 None。
    """
    node = root
    canonical: list[str] = []
    for token in tokens:
        canonical.append(token)
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            if not token or not token.isdigit():
                return None
            index = int(token)
            if token != str(index) or index >= len(node):  # 拒绝 01 这类前导零与越界
                return None
            node = node[index]
        else:
            return None
    return node, canonical


def _collect_nodes(
    root: Any,
) -> tuple[list[dict[str, Any]], list[tuple[str, Any, list[str]]]]:
    """遍历整份 schema，收集全部 dict 节点与全部 ``$defs`` 成员。

    返回：
    * 按先序遍历得到的 dict 节点列表，每项为
      ``{"node", "tokens", "id"}``；
    * ``$defs`` 成员列表：``(成员规范指针, 成员节点, 成员路径段)``。

    遍历会穿过 ``$defs`` 与 ``$ref`` 兄弟字段（``$ref`` 不阻断兄弟
    关键字的遍历，覆盖 draft-2019-09+ 的合法写法），以节点 id 去重，
    因此别名 / 自引用结构不会导致无限递归。
    """
    nodes: list[dict[str, Any]] = []
    defs_members: list[tuple[str, Any, list[str]]] = []
    seen: set[int] = set()

    def walk(node: Any, tokens: list[str]) -> None:
        if not isinstance(node, dict):
            return
        node_id = id(node)
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"node": node, "tokens": tokens, "id": node_id})

        defs = node.get("$defs")
        if isinstance(defs, dict):
            for name, member in defs.items():
                member_tokens = [*tokens, "$defs", name]
                defs_members.append((render_pointer(member_tokens), member, member_tokens))
                walk(member, member_tokens)

        for key, value in node.items():
            if key == "$defs":
                continue
            if isinstance(value, dict):
                walk(value, [*tokens, key])
            elif isinstance(value, list):
                _walk_list(value, [*tokens, key])

    def _walk_list(items: list, tokens: list[str]) -> None:
        for index, item in enumerate(items):
            if isinstance(item, dict):
                walk(item, [*tokens, str(index)])
            elif isinstance(item, list):
                _walk_list(item, [*tokens, str(index)])

    walk(root, [])
    return nodes, defs_members


def _reachable_ids(
    root: Any,
    source_outgoing: dict[int, tuple[str, int]],
) -> set[int]:
    """计算从有效 schema 结构可达的节点身份（id）集合（用于 $defs 可达性）。

    步骤：根做结构遍历（所有层级的 ``$defs`` 容器都不进入），再从
    可达节点上的有效 ``$ref`` 出发沿引用边做闭包；每次经引用进入新
    节点后同样做结构展开。引用链有环时以节点 id 集合收敛。布尔型
    schema（True/False）不是 dict/list，只能经引用到达，其 id 同样
    计入集合（内置单例 id 稳定）。以 id 而非指针判定，别名挂载的
    成员只要任一挂载位置可达即视为可达。

    ``source_outgoing`` 为来源节点 id -> (目标指针, 目标节点 id)。
    """
    nodes, _ = _collect_nodes(root)
    node_by_id: dict[int, Any] = {item["id"]: item["node"] for item in nodes}

    reachable: set[int] = {id(root)}
    ref_queue: list[int] = [id(root)]

    def struct_children(current: Any):
        if isinstance(current, dict):
            for key, value in current.items():
                if key == "$defs":
                    continue
                yield value
        elif isinstance(current, list):
            yield from current

    def expand_struct(start: Any) -> None:
        work = [start]
        while work:
            current = work.pop()
            for child in struct_children(current):
                if isinstance(child, (dict, list)) and id(child) not in reachable:
                    reachable.add(id(child))
                    work.append(child)
                    if isinstance(child, dict):
                        ref_queue.append(id(child))

    expand_struct(root)

    while ref_queue:
        source_id = ref_queue.pop()
        outgoing = source_outgoing.get(source_id)
        if outgoing is None:
            continue
        _target_pointer, target_id = outgoing
        if target_id not in reachable:
            reachable.add(target_id)
            target = node_by_id.get(target_id)
            if target is not None:
                # 经引用进入新的对象 schema：其内部结构（跳过 $defs）随之可达
                expand_struct(target)
                ref_queue.append(target_id)

    return reachable


def _find_cycles(adj: dict[str, list[str]]) -> tuple[list[list[str]], bool]:
    """在指针有向图上确定性枚举全部基本环（index-bounded DFS）。

    以节点排序下标为界：只允许经过下标 >= s 的节点、且只枚举起点 s
    为环内最小下标的环，保证每条基本环恰好出现一次。超过
    :data:`MAX_CYCLES` 条时停止并返回截断标记。
    """
    node_set: set[str] = set(adj)
    for targets in adj.values():
        node_set.update(targets)
    nodes = sorted(node_set)
    index = {node: i for i, node in enumerate(nodes)}
    cycles: list[list[str]] = []
    truncated = False

    for start_index, start in enumerate(nodes):
        stack: list[str] = [start]
        on_stack: set[str] = {start}

        def dfs(node: str) -> bool:
            nonlocal truncated
            for nxt in adj.get(node, []):
                nxt_index = index[nxt]
                if nxt_index < start_index:
                    continue
                if nxt == start:
                    cycles.append([*stack, start])
                    if len(cycles) >= MAX_CYCLES:
                        truncated = True
                        return False  # 停止当前 DFS
                    continue
                if nxt in on_stack:
                    continue
                on_stack.add(nxt)
                stack.append(nxt)
                if not dfs(nxt):
                    return False
                stack.pop()
                on_stack.remove(nxt)
            return True

        if not dfs(start):
            break

    cycles.sort(key=lambda cycle: (len(cycle), cycle))
    return cycles, truncated


def diagnose_references(schema: dict[str, Any]) -> dict[str, Any]:
    """对单份 JSON Schema 执行 ``$ref`` 引用依赖诊断。

    返回包含 ``edges`` / ``invalid_refs`` / ``unreachable_defs`` /
    ``cycles`` / ``cycles_truncated`` / ``summary`` 的纯数据字典。
    """
    nodes, defs_members = _collect_nodes(schema)
    pointer_of: dict[int, str] = {item["id"]: render_pointer(item["tokens"]) for item in nodes}

    edges: list[dict[str, str]] = []
    invalid_refs: list[dict[str, str]] = []
    # 来源节点 id -> (有效边目标指针, 目标节点 id)，每个 dict 最多一个 $ref
    source_outgoing: dict[int, tuple[str, int]] = {}

    for item in sorted(nodes, key=lambda entry: pointer_of[entry["id"]]):
        node = item["node"]
        if "$ref" not in node:
            continue
        source_pointer = pointer_of[item["id"]]
        ref = node["$ref"]
        status, reason, message = classify_ref(ref)
        if status != "ok":
            invalid_refs.append({
                "source": source_pointer,
                "ref": ref if isinstance(ref, str) else str(ref),
                "reason": reason,
                "message": message,
            })
            continue

        fragment = unquote(ref[1:]) if isinstance(ref, str) else ""
        tokens = (
            []
            if fragment == ""
            else [_unescape_token(tok) for tok in fragment[1:].split("/")]
        )
        resolved = resolve_pointer(schema, tokens)
        if resolved is None:
            invalid_refs.append({
                "source": source_pointer,
                "ref": ref,
                "reason": REASON_UNRESOLVABLE,
                "message": f"引用目标无法在本文档内解析: {ref}",
            })
            continue

        target_node, canonical_tokens = resolved
        target_pointer = render_pointer(canonical_tokens)
        if not isinstance(target_node, dict) and target_node is not True and target_node is not False:
            invalid_refs.append({
                "source": source_pointer,
                "ref": ref,
                "reason": REASON_NOT_SCHEMA,
                "message": f"引用目标不是 JSON Schema 节点（对象或布尔）: {ref}",
            })
            continue
        edges.append({"source": source_pointer, "target": target_pointer, "ref": ref})
        source_outgoing[item["id"]] = (target_pointer, id(target_node))

    edges.sort(key=lambda edge: (edge["source"], edge["target"], edge["ref"]))
    invalid_refs.sort(key=lambda record: (record["source"], record["ref"]))

    # 不可达 $defs：结构 + 引用闭包之外的 $defs 成员。同一对象别名
    # 挂载在多个位置时，按对象身份判定（任一位置可达即全部可达），
    # 但仍逐成员指针输出不可达位置。
    reachable_ids = _reachable_ids(schema, source_outgoing)
    unreachable: list[dict[str, Any]] = [
        {
            "pointer": member_pointer,
            "kind": "boolean" if member is True or member is False else "schema",
        }
        for member_pointer, member, _tokens in defs_members
        if id(member) not in reachable_ids
    ]
    unreachable.sort(key=lambda record: record["pointer"])

    # 引用图环：仅由有效边构成
    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge["source"], []).append(edge["target"])
    for targets in adj.values():
        targets.sort()
    cycles, cycles_truncated = _find_cycles(adj)

    return {
        "edges": edges,
        "invalid_refs": invalid_refs,
        "unreachable_defs": unreachable,
        "cycles": cycles,
        "cycles_truncated": cycles_truncated,
        "summary": {
            "total_refs": len(edges) + len(invalid_refs),
            "valid_edges": len(edges),
            "invalid_refs": len(invalid_refs),
            "unreachable_defs": len(unreachable),
            "cycles": len(cycles),
        },
    }
