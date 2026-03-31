#!/usr/bin/env python3
"""
task_tools.py — .task 文件 YAML 配置管理工具

功能:
  extract        从 .task 文件提取所有节点属性到 YAML
                 - nodes_all.yaml 始终全量生成
                 - nodes_overrides.yaml: 若已存在则按已有字段结构刷新值；若不存在则不自动创建
  update         对比当前 task 与 nodes_all.yaml 的差异，将改动的字段追加到 nodes_overrides.yaml，
                 并用当前 task 更新 nodes_all.yaml；自动生成 nodes_overrides.yaml.bak 并备份到备份目录
  apply          将 YAML 配置应用回 .task 文件（overrides 优先级高于 all）；自动备份 task 到备份目录
                 --trust-task true（默认）：以当前 task 为基准叠加 overrides，并同步更新 nodes_all.yaml
                 --trust-task false：以 nodes_all.yaml 为基准（原逻辑）
  diff           对比两个 YAML 配置文件的差异
  backup         全量备份 .task 文件和 YAML 配置（手动触发）
  restore        从备份还原（自动根据备份类型决定还原范围；支持 --only-yaml / --only-task）
  list-backups   列出所有备份槽
  remove-backups 删除备份槽（按名称/最近N个/超过N天）

默认目录: {task名称}_config  (例: 卸车标图_config)

用法:
  python task_tools.py extract        [--task 卸车标图] [--output-dir 卸车标图_config]
  python task_tools.py update         [--task 卸车标图] [--input-dir ...] [--output-dir ...]
  python task_tools.py apply          [--task 卸车标图] [--input-dir ...] [--yaml nodes_overrides.yaml]
  python task_tools.py diff           <yaml_a> <yaml_b> [--keys k1,k2]
  python task_tools.py backup         [--task 卸车标图] [--input-dir 卸车标图_config]
  python task_tools.py restore        [--task 卸车标图] [--slot latest|<name>] [--only-yaml] [--only-task]
  python task_tools.py list-backups   [--task 卸车标图]
  python task_tools.py remove-backups [--task 卸车标图] [--slot <name> | --count N | --days N]
"""

import argparse
import copy
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_TASK_NAME = "卸车标图"


def default_config_dir(task_name: str) -> str:
    """默认配置目录名: {task名称}_config"""
    return f"{task_name}_config"


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

import sys as _sys
# Nuitka onefile 模式下 __file__ 指向 /tmp 解压临时目录；
# 用 sys.argv[0] 定位实际可执行文件所在目录。
_exe = Path(_sys.argv[0]).resolve()
SCRIPT_DIR = _exe.parent if _exe.is_file() else Path(__file__).parent.resolve()


def get_task_path(task_name: str) -> Path:
    path = SCRIPT_DIR / f"{task_name}.task"
    if not path.exists():
        raise FileNotFoundError(f"Task 文件不存在: {path}")
    return path


def get_config_dir(dir_name: str) -> Path:
    return SCRIPT_DIR / dir_name


def get_backup_dir() -> Path:
    """备份目录放在工程上级隐藏目录中"""
    return SCRIPT_DIR.parent / ".task_yaml_backup"


# ---------------------------------------------------------------------------
# 备份 latest 指针辅助（跨平台：Unix 用软链接，Windows 用 .txt 文件）
# ---------------------------------------------------------------------------

def _set_latest(backup_base: Path, task_name: str, slot_dir: Path) -> None:
    """更新 latest 指针（Unix=软链接，Windows=纯文本文件）"""
    latest_link = backup_base / f"{task_name}_latest"
    if latest_link.is_symlink() or (latest_link.exists() and not latest_link.is_dir()):
        latest_link.unlink()
    try:
        latest_link.symlink_to(slot_dir.name)
    except (OSError, NotImplementedError):
        # Windows 无开发者模式时不支持软链接，回退为纯文本文件
        (backup_base / f"{task_name}_latest.txt").write_text(slot_dir.name, encoding="utf-8")


def _get_latest(backup_base: Path, task_name: str) -> Path | None:
    """解析 latest 指针，返回备份槽目录或 None"""
    latest_link = backup_base / f"{task_name}_latest"
    if latest_link.is_symlink():
        target = latest_link.resolve()
        return target if target.exists() else None
    # Windows 纯文本文件回退
    latest_txt = backup_base / f"{task_name}_latest.txt"
    if latest_txt.exists():
        slot_name = latest_txt.read_text(encoding="utf-8").strip()
        candidate = backup_base / slot_name
        return candidate if candidate.exists() else None
    return None


# ---------------------------------------------------------------------------
# YAML 辅助
# ---------------------------------------------------------------------------

def _yaml_load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _yaml_dump(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=True,
                  default_flow_style=False, width=120)


def _json_normalize(v):
    """通过 JSON 序列化规范化值，消除 YAML/JSON 加载时的类型差异（如 int/float、None 等）。"""
    return json.loads(json.dumps(v, ensure_ascii=False))


_FLOAT_TOL = 1e-6  # 浮点数差值绝对值小于此值视为相同


def _values_equal(a, b) -> bool:
    """递归比较两个值是否相等；数字使用 1e-6 容差；list/dict 递归比较。"""
    if type(a) in (int, float) and type(b) in (int, float):
        return abs(float(a) - float(b)) < _FLOAT_TOL
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a.keys()) == set(b.keys()) and all(_values_equal(a[k], b[k]) for k in a)
    return a == b


def _deep_diff_minimal(baseline, current):
    """
    递归比较 baseline 和 current，返回最小变化子结构。
    - 两侧都是 dict：递归进入，只返回有差异的子字段
    - 其他类型：值不同则返回 current，相同则返回 None（无变化）
    - 数字类型：差值绝对值 < 1e-6 视为相同
    """
    # 规范化类型，防止 YAML/JSON 往返引入差异
    baseline = _json_normalize(baseline)
    current = _json_normalize(current)

    if isinstance(baseline, dict) and isinstance(current, dict):
        result = {}
        for k, cv in current.items():
            if k not in baseline:
                result[k] = cv          # 新增字段
            else:
                sub = _deep_diff_minimal(baseline[k], cv)
                if sub is not None:
                    result[k] = sub     # 有变化的子字段
        return result if result else None
    else:
        return None if _values_equal(baseline, current) else current


def _deep_merge(base, override):
    """
    将 override 深度合并到 base 中，返回新对象（不修改原对象）。
    - 两侧都是 dict：递归合并
    - 其他类型：override 覆盖 base
    """
    if isinstance(base, dict) and isinstance(override, dict):
        result = copy.deepcopy(base)
        for k, v in override.items():
            if k in result:
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = copy.deepcopy(v)
        return result
    return copy.deepcopy(override)


# ---------------------------------------------------------------------------
# extract 命令
# ---------------------------------------------------------------------------

# 端口/连线相关字段，不参与 extract/apply（修改会破坏连线）
_PORT_FIELDS = {"func_def"}

# 不追踪到 nodes_overrides.yaml 的字段（端口定义，由程序员维护，非站点差异参数）
_OVERRIDES_EXCLUDED_FIELDS = {"signature"}


def _build_all_nodes(task_data: dict) -> tuple[dict, dict, dict]:
    """从 task_data 构建 all_nodes 字典，同时返回 service 和 robot_scales。"""
    nodes = task_data.get("nodes", [])
    all_nodes: dict = {}
    for node in nodes:
        uid = node.get("id", "")
        model = node.get("model", {})
        position = node.get("position", {})
        all_nodes[uid] = {
            "_class": model.get("_CLASS_", ""),
            "position": position,
            "model": {k: v for k, v in model.items()
                      if k != "_CLASS_" and k not in _PORT_FIELDS},
        }
    return all_nodes, task_data.get("service", {}), task_data.get("robotGlobalSpeedScales", {})


def cmd_extract(task_name: str, output_dir: str) -> None:
    task_path = get_task_path(task_name)
    config_dir = get_config_dir(output_dir)
    all_path = config_dir / "nodes_all.yaml"
    overrides_path = config_dir / "nodes_overrides.yaml"

    print(f"[extract] 读取 task: {task_path}")
    with open(task_path, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    all_nodes, service, robot_scales = _build_all_nodes(task_data)

    metadata = {
        "task_name": task_name,
        "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "node_count": len(all_nodes),
        "task_file": str(task_path),
    }

    # 写全量 YAML
    all_data = {
        "metadata": metadata,
        "service": service,
        "robotGlobalSpeedScales": robot_scales,
        "nodes": all_nodes,
    }
    _yaml_dump(all_data, all_path)
    print(f"[extract] 全量 YAML  → {all_path}  ({len(all_nodes)} 节点)")

    # overrides: 按已有文件的字段结构刷新值；若不存在则不创建
    if overrides_path.exists():
        existing_overrides = _yaml_load(overrides_path)
        existing_nodes = existing_overrides.get("nodes", {})
        refreshed_nodes: dict = {}
        for uid, ov_entry in existing_nodes.items():
            if uid not in all_nodes:
                continue  # task 中已不存在此节点，跳过
            tracked_fields = set(ov_entry.get("model", {}).keys()) - _OVERRIDES_EXCLUDED_FIELDS
            fresh_model = {k: v for k, v in all_nodes[uid]["model"].items()
                           if k in tracked_fields}
            if fresh_model:
                refreshed_nodes[uid] = {
                    "_class": all_nodes[uid]["_class"],
                    "model": fresh_model,
                }
        existing_overrides["nodes"] = refreshed_nodes
        existing_overrides.setdefault("metadata", {}).update({
            "task_name": task_name,
            "extracted_at": metadata["extracted_at"],
        })
        _yaml_dump(existing_overrides, overrides_path)
        print(f"[extract] 差异 YAML 已刷新 → {overrides_path}  ({len(refreshed_nodes)} 节点)")
    else:
        print(f"[extract] 差异 YAML 不存在，跳过（如需创建请先手动编辑或运行 update）")


# ---------------------------------------------------------------------------
# apply 命令（拆分为 dry-run 计算 + 实际写入两个函数，便于 TUI 交互）
# ---------------------------------------------------------------------------

def _compute_apply_changes(
    task_name: str,
    input_dir: str,
    yaml_files: list[str] | None = None,
    trust_task: bool = True,
) -> tuple[dict, Path, list[tuple]]:
    """
    dry-run 版 apply：计算所有待改动，不写入任何文件。

    返回:
        task_data       - 已加载（但未写入）的 task JSON 数据（含计算后的改动）
        all_path        - nodes_all.yaml 的 Path（可能不存在）
        changed_details - 每项: (cls, uid, field, old_val, new_val)

    供 TUI 交互式确认使用：先调用此函数展示改动，用户确认后再调用 _execute_apply。
    """
    task_path = get_task_path(task_name)
    config_dir = get_config_dir(input_dir)
    all_path = config_dir / "nodes_all.yaml"
    overrides_path = config_dir / "nodes_overrides.yaml"

    # 加载 task 数据
    with open(task_path, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    _yaml_set = set(yaml_files) if yaml_files else None
    all_yaml: dict = {}
    overrides_yaml: dict = {}

    if trust_task:
        # 以当前 task 为基准：all_yaml 直接来自 task
        current_nodes, _, _ = _build_all_nodes(task_data)
        all_yaml = current_nodes
        # 仍加载 overrides（受 --yaml 过滤）
        if overrides_path.exists() and (_yaml_set is None or "nodes_overrides.yaml" in _yaml_set):
            overrides_yaml = _yaml_load(overrides_path).get("nodes", {})
    else:
        # 原逻辑：从文件加载 all + overrides
        if all_path.exists() and (_yaml_set is None or "nodes_all.yaml" in _yaml_set):
            all_yaml = _yaml_load(all_path).get("nodes", {})
        if overrides_path.exists() and (_yaml_set is None or "nodes_overrides.yaml" in _yaml_set):
            overrides_yaml = _yaml_load(overrides_path).get("nodes", {})

    # 合并: overrides 深度合并覆盖 all（支持嵌套字段）
    merged: dict = {}
    for uid, entry in all_yaml.items():
        merged[uid] = copy.deepcopy(entry)
    for uid, entry in overrides_yaml.items():
        if uid in merged:
            for field, ov_val in entry.get("model", {}).items():
                base_val = merged[uid]["model"].get(field)
                if base_val is not None:
                    merged[uid]["model"][field] = _deep_merge(base_val, ov_val)
                else:
                    merged[uid]["model"][field] = copy.deepcopy(ov_val)
        else:
            merged[uid] = copy.deepcopy(entry)

    # 计算改动（修改 task_data 中的 model，但不写文件）
    changed_details = []
    nodes = task_data.get("nodes", [])
    for node in nodes:
        uid = node.get("id", "")
        if uid not in merged:
            continue
        yaml_entry = merged[uid]
        yaml_model = yaml_entry.get("model", {})
        orig_model = node.get("model", {})
        cls = yaml_entry.get("_class", "")
        for field, value in yaml_model.items():
            if not _values_equal(orig_model.get(field), value):
                changed_details.append((cls, uid, field, orig_model.get(field), value))
                orig_model[field] = value  # 写入内存中的 task_data，但不落盘

    return task_data, all_path, changed_details


def _execute_apply(
    task_name: str,
    task_data: dict,
    all_path: Path,
    input_dir: str,
    approved_changes: list[tuple],
    trust_task: bool = True,
    overwrite_python: bool = False,
) -> str:
    """
    执行实际写入：只应用 approved_changes 中的改动（由 TUI 用户筛选后传入）。

    参数:
        task_name        - task 名称
        task_data        - 已由 _compute_apply_changes 计算过的 task JSON（含全部改动）
        all_path         - nodes_all.yaml 路径
        input_dir        - 配置目录名（用于 _auto_backup）
        approved_changes - 用户接受的改动列表，格式同 changed_details: (cls, uid, field, old_val, new_val)
        trust_task       - 是否同步更新 nodes_all.yaml
        overwrite_python - 是否将 PythonNodeModel 的 script 写回 .py 文件

    返回:
        结果文本字符串（summary）
    """
    task_path = get_task_path(task_name)
    lines = []
    lines.append(f"[apply] 写入 task: {task_path}  (trust_task={trust_task})")

    # 构建 approved_changes 的快速查找集合：{(uid, field)}
    approved_set = {(uid, field) for (cls, uid, field, old_val, new_val) in approved_changes}

    # 若 approved_changes 比全量改动少，需要把未批准的改动从 task_data 中回滚
    # 遍历 task_data nodes，找出不在 approved_set 中的改动，还原为 old_val
    old_val_map = {}  # (uid, field) -> old_val（来自 approved_changes 之外的改动）
    # 注意：_compute_apply_changes 传入的 task_data 已经包含了全部改动（含未批准的）
    # 我们需要一个"全量改动"来知道哪些未被批准。但此函数只收到 approved_changes。
    # 解决方案：对于未被批准的字段，我们不知道 old_val，因此调用者需保证：
    #   task_data 仅包含 approved_changes 对应的改动，
    #   或者在 TUI 层面重新加载 task 仅应用 approved_changes。
    # 实际处理：重新从磁盘加载 task，仅应用 approved_changes 中的字段。
    with open(task_path, "r", encoding="utf-8") as f:
        fresh_task_data = json.load(f)

    # 建立 uid -> node 索引
    node_by_id = {n.get("id", ""): n for n in fresh_task_data.get("nodes", [])}

    # 只应用 approved_changes 中的改动
    for cls, uid, field, old_val, new_val in approved_changes:
        node = node_by_id.get(uid)
        if node is None:
            continue
        node.get("model", {})[field] = new_val

    # 应用顶层字段（service, robotGlobalSpeedScales）：trust_task=False 时才从文件覆盖
    if not trust_task and all_path.exists():
        all_top = _yaml_load(all_path)
        if "service" in all_top:
            fresh_task_data["service"] = all_top["service"]
        if "robotGlobalSpeedScales" in all_top:
            fresh_task_data["robotGlobalSpeedScales"] = all_top["robotGlobalSpeedScales"]

    # 备份原文件后写入 task
    backup_path = task_path.with_suffix(".task.bak")
    shutil.copy2(task_path, backup_path)
    lines.append(f"[apply] 原文件已备份到: {backup_path}")

    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(fresh_task_data, f, ensure_ascii=False, indent=2, sort_keys=True)

    # --overwrite-python: 将 PythonNodeModel 的 script 改动同步写回对应 .py 文件
    py_written = []
    py_skipped = []
    if overwrite_python:
        project_root = task_path.parent.parent  # tasks/ 的上级目录
        for cls, uid, field, old_val, new_val in approved_changes:
            if cls != "PythonNodeModel" or field != "script":
                continue
            node = node_by_id.get(uid, {})
            file_path_str = node.get("model", {}).get("file_path", "")
            if not file_path_str:
                py_skipped.append(f"  [{uid}] 无 file_path，跳过")
                continue
            py_path = project_root / file_path_str
            py_path.parent.mkdir(parents=True, exist_ok=True)
            py_path.write_text(new_val, encoding="utf-8")
            py_written.append(f"  {file_path_str}")
        if py_written:
            lines.append(f"\n同步写入 Python 文件 ({len(py_written)} 个):")
            lines.extend(py_written)
        if py_skipped:
            lines.append(f"\n跳过 Python 文件 ({len(py_skipped)} 个):")
            lines.extend(py_skipped)

    # 输出改动明细
    if approved_changes:
        lines.append(f"\n改动明细 ({len(approved_changes)} 个字段):")
        for cls, uid, field, old_val, new_val in approved_changes:
            lines.append(f"  [{cls}] {uid}")
            lines.append(f"    {field}: {old_val!r} -> {new_val!r}")
    else:
        lines.append("\n无字段改动。")

    lines.append(f"\n[apply] 完成，共修改 {len(approved_changes)} 个字段，task 已保存: {task_path}")

    # trust_task=True：同步更新 nodes_all.yaml 为当前 task 快照
    if trust_task:
        new_nodes, new_service, new_robot_scales = _build_all_nodes(fresh_task_data)
        all_data: dict = {}
        if all_path.exists():
            all_data = _yaml_load(all_path)
        all_data["nodes"] = new_nodes
        all_data["service"] = new_service
        all_data["robotGlobalSpeedScales"] = new_robot_scales
        all_data.setdefault("metadata", {}).update({
            "task_name": task_name,
            "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "node_count": len(new_nodes),
        })
        _yaml_dump(all_data, all_path)
        lines.append(f"[apply] nodes_all.yaml 已同步更新为当前 task 快照")

    # 写结果到编号 txt 文件
    output = "\n".join(lines)
    out_path = _next_output_path("apply")
    out_path.write_text(output + "\n", encoding="utf-8")
    lines.append(f"[apply] 结果已写入: {out_path}")

    # 自动备份 task 到备份目录
    auto_slot = _auto_backup(task_name, input_dir, "task_only")
    lines.append(f"[apply] 已自动备份 task → {auto_slot}")

    return "\n".join(lines)


def cmd_apply(task_name: str, input_dir: str, overwrite_python: bool = False,
              yaml_files: list[str] | None = None, trust_task: bool = True) -> None:
    """
    将 YAML 配置应用回 .task 文件。

    trust_task=True（默认）：以当前 task 为基准，只叠加 nodes_overrides.yaml，
                             完成后同步更新 nodes_all.yaml 为当前 task 快照。
    trust_task=False        ：以 nodes_all.yaml 为基准（原逻辑），适用于多站统一下发。

    内部通过 _compute_apply_changes + _execute_apply 实现，保持原有行为不变。
    """
    config_dir = get_config_dir(input_dir)
    all_path = config_dir / "nodes_all.yaml"
    overrides_path = config_dir / "nodes_overrides.yaml"

    # 参数校验（与原逻辑一致）
    if not trust_task and not all_path.exists() and not overrides_path.exists():
        print("[apply] 错误: 未找到 nodes_all.yaml 或 nodes_overrides.yaml，请先运行 extract", file=sys.stderr)
        sys.exit(1)
    if trust_task and not overrides_path.exists() and not all_path.exists():
        print("[apply] 警告: 未找到任何 YAML 文件，task 将原样写回（仅更新 nodes_all.yaml）")

    # 第一步：dry-run 计算所有改动
    task_data, all_path, changed_details = _compute_apply_changes(
        task_name, input_dir, yaml_files=yaml_files, trust_task=trust_task
    )

    # 第二步：执行全量写入（approved_changes = 全部 changed_details）
    result = _execute_apply(
        task_name, task_data, all_path, input_dir,
        approved_changes=changed_details,
        trust_task=trust_task,
        overwrite_python=overwrite_python,
    )
    print(result)


# ---------------------------------------------------------------------------
# update 命令
# ---------------------------------------------------------------------------

def cmd_update(task_name: str, input_dir: str, output_dir: str) -> None:
    """
    对比当前 task 与 nodes_all.yaml（上次 extract/update 的快照），
    将发生变化的字段追加到 nodes_overrides.yaml，
    然后用当前 task 更新 nodes_all.yaml 作为新基线。

    input_dir:  读取 nodes_all.yaml（基线）的目录
    output_dir: 写入 nodes_overrides.yaml 和更新后 nodes_all.yaml 的目录
    """
    task_path = get_task_path(task_name)
    all_path = get_config_dir(input_dir) / "nodes_all.yaml"
    overrides_path = get_config_dir(output_dir) / "nodes_overrides.yaml"
    out_all_path = get_config_dir(output_dir) / "nodes_all.yaml"

    if not all_path.exists():
        print("[update] 错误: nodes_all.yaml 不存在，请先运行 extract", file=sys.stderr)
        sys.exit(1)

    print(f"[update] 读取 task: {task_path}")
    with open(task_path, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    current_nodes, service, robot_scales = _build_all_nodes(task_data)
    baseline_nodes: dict = _yaml_load(all_path).get("nodes", {})

    # 加载现有 overrides（若有）
    existing_overrides_data = _yaml_load(overrides_path) if overrides_path.exists() else {}
    overrides_nodes: dict = copy.deepcopy(existing_overrides_data.get("nodes", {}))

    added_fields = 0
    changed_nodes_count = 0

    for uid, current_entry in current_nodes.items():
        if uid not in baseline_nodes:
            continue  # 新增节点，不自动加入 overrides
        baseline_model = baseline_nodes[uid].get("model", {})
        current_model = current_entry.get("model", {})

        # 对每个顶层字段做递归 diff，只提取最小变化子结构
        for field, current_val in current_model.items():
            if field not in baseline_model:
                continue  # 新增字段暂不自动追踪
            if field in _OVERRIDES_EXCLUDED_FIELDS:
                continue  # 端口定义等字段不追踪到 overrides
            diff = _deep_diff_minimal(baseline_model[field], current_val)
            if diff is None:
                continue  # 此字段无变化

            if uid not in overrides_nodes:
                overrides_nodes[uid] = {
                    "_class": current_entry["_class"],
                    "model": {},
                }
            # 与已有 overrides 做深度合并，保留用户已有的跟踪字段
            existing_field_val = overrides_nodes[uid]["model"].get(field)
            if existing_field_val is not None:
                overrides_nodes[uid]["model"][field] = _deep_merge(existing_field_val, diff)
            else:
                overrides_nodes[uid]["model"][field] = diff
            added_fields += 1
        if uid in overrides_nodes:
            changed_nodes_count += 1

    # 写回 overrides（先备份旧 overrides 为 .bak）
    if overrides_path.exists():
        shutil.copy2(overrides_path, Path(str(overrides_path) + ".bak"))
    metadata = {
        "task_name": task_name,
        "extracted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "description": "频繁修改的站点差异参数（优先级高于 nodes_all.yaml）",
    }
    existing_overrides_data["metadata"] = metadata
    existing_overrides_data["nodes"] = overrides_nodes
    _yaml_dump(existing_overrides_data, overrides_path)

    # 更新 nodes_all.yaml 为当前 task 快照（新基线），写到 output_dir
    all_data = _yaml_load(all_path)
    all_data["nodes"] = current_nodes
    all_data["service"] = service
    all_data["robotGlobalSpeedScales"] = robot_scales
    all_data["metadata"]["extracted_at"] = metadata["extracted_at"]
    _yaml_dump(all_data, out_all_path)

    print(f"[update] 发现 {changed_nodes_count} 个节点有变化，共 {added_fields} 个字段追加到 nodes_overrides.yaml")
    print(f"[update] nodes_all.yaml 已更新为当前 task 快照")

    # 自动备份 overrides 到备份目录
    auto_slot = _auto_backup(task_name, output_dir, "overrides_only")
    print(f"[update] 已自动备份 overrides → {auto_slot}")


# ---------------------------------------------------------------------------
# diff 命令
# ---------------------------------------------------------------------------

def _flatten(d: dict, prefix: str = "") -> dict:
    """递归展平嵌套 dict，用 '.' 连接 key"""
    result = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, full_key))
        else:
            result[full_key] = v
    return result


def _next_output_path(prefix: str) -> Path:
    """在当前工作目录中找下一个可用的 {prefix}_N.txt 路径"""
    cwd = Path.cwd()
    n = 1
    while (cwd / f"{prefix}_{n}.txt").exists():
        n += 1
    return cwd / f"{prefix}_{n}.txt"


def cmd_diff(yaml_a: str, yaml_b: str, filter_keys: list[str] | None = None) -> None:
    path_a = Path(yaml_a)
    path_b = Path(yaml_b)

    if not path_a.exists():
        print(f"[diff] 错误: 文件不存在: {path_a}", file=sys.stderr)
        sys.exit(1)
    if not path_b.exists():
        print(f"[diff] 错误: 文件不存在: {path_b}", file=sys.stderr)
        sys.exit(1)

    data_a = _yaml_load(path_a)
    data_b = _yaml_load(path_b)

    nodes_a: dict = data_a.get("nodes", {})
    nodes_b: dict = data_b.get("nodes", {})

    all_uids = sorted(set(nodes_a.keys()) | set(nodes_b.keys()))

    only_in_a = []
    only_in_b = []
    changed_nodes = []

    for uid in all_uids:
        if uid not in nodes_b:
            only_in_a.append(uid)
            continue
        if uid not in nodes_a:
            only_in_b.append(uid)
            continue

        model_a = nodes_a[uid].get("model", {})
        model_b = nodes_b[uid].get("model", {})

        flat_a = _flatten(model_a)
        flat_b = _flatten(model_b)

        all_keys = set(flat_a.keys()) | set(flat_b.keys())
        if filter_keys:
            all_keys = {k for k in all_keys
                        if any(k.startswith(fk) or fk in k for fk in filter_keys)}

        diffs = []
        for key in sorted(all_keys):
            va = flat_a.get(key, "<missing>")
            vb = flat_b.get(key, "<missing>")
            if not _values_equal(va, vb):
                diffs.append((key, va, vb))

        if diffs:
            changed_nodes.append((uid, nodes_a[uid].get("_class", ""), diffs))

    # 构建输出内容
    lines = []
    lines.append(f"\n=== DIFF: {path_a.name}  vs  {path_b.name} ===\n")

    if only_in_a:
        lines.append(f"仅在 A 中存在的节点: {len(only_in_a)} 个")
        lines.append("")

    if only_in_b:
        lines.append(f"仅在 B 中存在的节点: {len(only_in_b)} 个")
        lines.append("")

    if changed_nodes:
        # 分成两类：有真实值差异 vs 纯字段缺失
        value_diff_nodes = []
        pure_missing_nodes = []
        for uid, cls, diffs in changed_nodes:
            real_diffs = [(k, va, vb) for k, va, vb in diffs
                          if va != "<missing>" and vb != "<missing>"]
            if real_diffs:
                value_diff_nodes.append((uid, cls, diffs))
            else:
                pure_missing_nodes.append((uid, cls, diffs))

        # 纯字段缺失：只统计，不逐条展开
        if pure_missing_nodes:
            lines.append(f"字段数量不同（无值变化）的节点: {len(pure_missing_nodes)} 个")
            lines.append("")

        # 有真实值差异：逐条展开
        if value_diff_nodes:
            lines.append(f"有值差异的节点 ({len(value_diff_nodes)}):")
            for uid, cls, diffs in value_diff_nodes:
                real_diffs = [(k, va, vb) for k, va, vb in diffs
                              if va != "<missing>" and vb != "<missing>"]
                only_in_a_fields = [k for k, va, vb in diffs if vb == "<missing>"]
                only_in_b_fields = [k for k, va, vb in diffs if va == "<missing>"]

                lines.append(f"\n  [{cls}] {uid}")
                for key, va, vb in real_diffs:
                    lines.append(f"    {key}:")
                    lines.append(f"      A: {va!r}")
                    lines.append(f"      B: {vb!r}")
                if only_in_b_fields:
                    if len(only_in_b_fields) <= 3:
                        lines.append(f"    B 比 A 多字段: {only_in_b_fields}")
                    else:
                        lines.append(f"    B 比 A 多 {len(only_in_b_fields)} 个字段")
                if only_in_a_fields:
                    if len(only_in_a_fields) <= 3:
                        lines.append(f"    A 比 B 多字段: {only_in_a_fields}")
                    else:
                        lines.append(f"    A 比 B 多 {len(only_in_a_fields)} 个字段")

        if not value_diff_nodes and not pure_missing_nodes:
            lines.append("无差异。")
    else:
        lines.append("无差异。")

    lines.append("")
    lines.append(f"汇总: {len(only_in_a)} 仅A / {len(only_in_b)} 仅B / {len(changed_nodes)} 有差异")

    output = "\n".join(lines)
    print(output)

    # 写入自动编号的 txt 文件
    out_path = _next_output_path("diff")
    out_path.write_text(output + "\n", encoding="utf-8")
    print(f"\n[diff] 结果已写入: {out_path}")


# ---------------------------------------------------------------------------
# 自动备份辅助（update/apply 后自动触发）
# ---------------------------------------------------------------------------

def _parse_slot_time(slot_dir: Path) -> datetime.datetime | None:
    """从备份槽解析创建时间（优先 backup_meta.json，其次目录名中的 YYYYMMDD_HHMMSS）"""
    meta_path = slot_dir / "backup_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            s = meta.get("backed_up_at", "")
            if s:
                return datetime.datetime.fromisoformat(s)
        except Exception:
            pass
    # 兜底：扫描目录名中相邻的 8位_6位 数字段
    parts = slot_dir.name.split("_")
    for i in range(len(parts) - 1):
        if len(parts[i]) == 8 and len(parts[i + 1]) == 6:
            try:
                return datetime.datetime.strptime(f"{parts[i]}_{parts[i+1]}", "%Y%m%d_%H%M%S")
            except ValueError:
                pass
    return None


def _auto_backup(task_name: str, config_dir_name: str | None, backup_type: str) -> Path:
    """
    update 后自动备份 overrides（backup_type="overrides_only"），
    apply 后自动备份 task（backup_type="task_only"）。
    返回备份槽目录。
    """
    backup_base = get_backup_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slot_name = f"{task_name}_{ts}"
    slot_dir = backup_base / slot_name
    # 防止同秒内命名冲突
    n = 1
    while slot_dir.exists():
        slot_dir = backup_base / f"{slot_name}_{n}"
        n += 1
    slot_dir.mkdir(parents=True, exist_ok=True)

    if backup_type == "task_only":
        task_path = get_task_path(task_name)
        shutil.copy2(task_path, slot_dir / task_path.name)
    elif backup_type == "overrides_only" and config_dir_name:
        overrides_path = get_config_dir(config_dir_name) / "nodes_overrides.yaml"
        if overrides_path.exists():
            shutil.copy2(overrides_path, slot_dir / "nodes_overrides.yaml")

    meta = {
        "task_name": task_name,
        "backed_up_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "backup_type": backup_type,
        "config_dir": str(get_config_dir(config_dir_name)) if config_dir_name else "",
    }
    with open(slot_dir / "backup_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    _set_latest(backup_base, task_name, slot_dir)
    return slot_dir


# ---------------------------------------------------------------------------
# backup 命令
# ---------------------------------------------------------------------------

def cmd_backup(task_name: str, input_dir: str) -> None:
    task_path = get_task_path(task_name)
    config_dir = get_config_dir(input_dir)
    backup_base = get_backup_dir()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    slot_name = f"{task_name}_{ts}"
    slot_dir = backup_base / slot_name
    # 防止同秒内命名冲突
    n = 1
    while slot_dir.exists():
        slot_dir = backup_base / f"{slot_name}_{n}"
        n += 1
    slot_dir.mkdir(parents=True, exist_ok=True)

    # 备份 task 文件
    shutil.copy2(task_path, slot_dir / task_path.name)
    print(f"[backup] task → {slot_dir / task_path.name}")

    # 备份 YAML 配置文件夹
    if config_dir.exists():
        dst_config = slot_dir / config_dir.name
        shutil.copytree(config_dir, dst_config)
        print(f"[backup] config → {dst_config}")
    else:
        print(f"[backup] config 目录不存在，跳过: {config_dir}")

    # 写入备份元信息
    meta = {
        "task_name": task_name,
        "backed_up_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "backup_type": "full",
        "task_file": str(task_path),
        "config_dir": str(config_dir),
    }
    with open(slot_dir / "backup_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    _set_latest(backup_base, task_name, slot_dir)
    print(f"[backup] 完成，备份槽: {slot_dir}")
    print(f"[backup] latest 指向: {backup_base / (task_name + '_latest')}")


# ---------------------------------------------------------------------------
# restore 命令
# ---------------------------------------------------------------------------

def _compute_restore_python_changes(
    backup_task_path: Path, current_task_path: Path
) -> list[tuple[str, str, str, str]]:
    """
    比较备份 task 与当前 task 中 PythonNodeModel 的 script 字段。
    返回有差异的条目列表: [(uid, file_path, current_script, backup_script), ...]
    file_path 取自备份 task（还原目标路径）。
    """
    def _load(p: Path) -> dict:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    try:
        backup_nodes  = {n["id"]: n for n in _load(backup_task_path).get("nodes", [])}
        current_nodes = {n["id"]: n for n in _load(current_task_path).get("nodes", [])}
    except Exception:
        return []

    changes = []
    for uid, bnode in backup_nodes.items():
        bmodel = bnode.get("model", {})
        if bmodel.get("_CLASS_") != "PythonNodeModel":
            continue
        bscript       = bmodel.get("script", "")
        file_path_str = bmodel.get("file_path", "")
        if not file_path_str:
            continue
        cnode = current_nodes.get(uid)
        if cnode is None:
            continue
        cscript = cnode.get("model", {}).get("script", "")
        if bscript != cscript:
            changes.append((uid, file_path_str, cscript, bscript))
    return changes


def cmd_restore(task_name: str, output_dir: str, slot: str = "latest",
                only_yaml: bool = False, only_task: bool = False,
                overwrite_python: bool = False) -> None:
    task_path = SCRIPT_DIR / f"{task_name}.task"
    config_dir = get_config_dir(output_dir)
    backup_base = get_backup_dir()

    if slot == "latest":
        slot_dir = _get_latest(backup_base, task_name)
        if slot_dir is None:
            print(f"[restore] 错误: 未找到 latest 备份，请先运行 backup", file=sys.stderr)
            sys.exit(1)
    else:
        slot_dir = backup_base / slot
        if not slot_dir.exists():
            print(f"[restore] 错误: 备份槽不存在: {slot_dir}", file=sys.stderr)
            sys.exit(1)

    # 读取备份类型，自动决定还原范围
    backup_type = "full"
    meta_path = slot_dir / "backup_meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                bk_meta = json.load(f)
            backup_type = bk_meta.get("backup_type", "full")
        except Exception:
            pass

    do_restore_task = (not only_yaml) and backup_type in ("full", "task_only")
    do_restore_yaml = (not only_task) and backup_type in ("full", "overrides_only")

    # 还原 task
    task_backup = slot_dir / f"{task_name}.task"
    # 在覆盖 task 文件之前，先计算 Python 脚本差异（此时 task_path 仍是当前版本）
    py_changes: list[tuple[str, str, str, str]] = []
    if do_restore_task and overwrite_python and task_backup.exists() and task_path.exists():
        py_changes = _compute_restore_python_changes(task_backup, task_path)

    if do_restore_task:
        if not task_backup.exists():
            print(f"[restore] 警告: 备份中无 task 文件，跳过 task 还原")
        else:
            shutil.copy2(task_backup, task_path)
            print(f"[restore] task ← {task_backup}")
    else:
        print(f"[restore] 跳过 task 还原（backup_type={backup_type}, only_yaml={only_yaml}）")

    # 还原 YAML 配置
    if do_restore_yaml:
        if backup_type == "overrides_only":
            # 只有 nodes_overrides.yaml 直接在 slot_dir 下
            overrides_bak = slot_dir / "nodes_overrides.yaml"
            if overrides_bak.exists():
                config_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(overrides_bak, config_dir / "nodes_overrides.yaml")
                print(f"[restore] nodes_overrides.yaml ← {overrides_bak}")
            else:
                print(f"[restore] 备份中无 nodes_overrides.yaml，跳过")
        else:
            # full 备份：还原整个 config 目录
            config_backup = slot_dir / Path(output_dir).name
            if not config_backup.exists():
                candidates = [d for d in slot_dir.iterdir()
                              if d.is_dir() and d.name != "__pycache__"
                              and not d.name.startswith(".")]
                if candidates:
                    config_backup = candidates[0]
                    print(f"[restore] 备份中未找到 '{Path(output_dir).name}'，使用: {config_backup.name}")

            if config_backup.exists():
                if config_dir.exists():
                    shutil.rmtree(config_dir)
                shutil.copytree(config_backup, config_dir)
                print(f"[restore] config ← {config_backup}")
            else:
                print(f"[restore] 备份中无 config 目录，跳过")
    else:
        print(f"[restore] 跳过 YAML 还原（backup_type={backup_type}, only_task={only_task}）")

    # 写回 Python 脚本（overwrite_python=True 时）
    if py_changes:
        project_root = task_path.parent.parent
        for uid, file_path_str, _cur, backup_script in py_changes:
            py_path = project_root / file_path_str
            py_path.parent.mkdir(parents=True, exist_ok=True)
            py_path.write_text(backup_script, encoding="utf-8")
            print(f"[restore] python ← {file_path_str}")

    print(f"[restore] 完成，从备份槽: {slot_dir}（backup_type={backup_type}）")


# ---------------------------------------------------------------------------
# list-backups 命令
# ---------------------------------------------------------------------------

def cmd_list_backups(task_name: str) -> None:
    backup_base = get_backup_dir()
    if not backup_base.exists():
        print("[list-backups] 暂无备份记录")
        return

    slots = sorted(
        [d for d in backup_base.iterdir()
         if d.is_dir() and d.name.startswith(task_name + "_")
         and not d.name.endswith("_latest")],
        reverse=True
    )

    if not slots:
        print(f"[list-backups] 任务 '{task_name}' 暂无备份")
        return

    print(f"[list-backups] 任务 '{task_name}' 的备份列表:")
    _latest = _get_latest(backup_base, task_name)
    latest_target = _latest.name if _latest else None

    for slot_dir in slots:
        meta_path = slot_dir / "backup_meta.json"
        backed_at = ""
        btype = ""
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            backed_at = meta.get("backed_up_at", "")
            btype = meta.get("backup_type", "")
        flag = " ← latest" if slot_dir.name == latest_target else ""
        type_tag = f"  [{btype}]" if btype and btype != "full" else ""
        print(f"  {slot_dir.name}  {backed_at}{type_tag}{flag}")


# ---------------------------------------------------------------------------
# remove-backups 命令
# ---------------------------------------------------------------------------

def cmd_remove_backups(task_name: str, slot: str | None = None,
                       days: int | None = None, count: int | None = None,
                       remove_all: bool = False) -> None:
    """
    删除备份槽。四种模式（互斥）：
      --all           删除全部备份槽
      --slot <name>   删除指定槽
      --count <n>     删除最近 N 个（默认 1）
      --days <n>      删除超过 N 天的旧备份（默认 7）
    四者均未指定时，默认等同 --days 7。
    """
    backup_base = get_backup_dir()
    if not backup_base.exists():
        print("[remove-backups] 暂无备份记录")
        return

    all_slots = sorted(
        [d for d in backup_base.iterdir()
         if d.is_dir() and d.name.startswith(task_name + "_")
         and not d.name.endswith("_latest")],
        key=lambda p: p.name,  # 按目录名字母顺序（等同时间顺序）旧→新
    )

    to_remove: list[Path] = []

    if remove_all:
        to_remove = list(all_slots)
    elif slot:
        matched = [s for s in all_slots if s.name == slot]
        if not matched:
            print(f"[remove-backups] 未找到备份槽: {slot}", file=sys.stderr)
            sys.exit(1)
        to_remove = matched
    elif count is not None:
        # 删除最近 N 个（newest first = 倒序，取前 N）
        to_remove = list(reversed(all_slots))[:count]
    else:
        # 删除超过 n 天的旧备份；未指定则默认 7 天
        if days is None:
            days = 7
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        for s in all_slots:
            t = _parse_slot_time(s)
            if t is not None and t < cutoff:
                to_remove.append(s)

    if not to_remove:
        print("[remove-backups] 无符合条件的备份槽")
        return

    latest_target = _get_latest(backup_base, task_name)

    for s in to_remove:
        shutil.rmtree(s)
        print(f"[remove-backups] 已删除: {s.name}")

    print(f"[remove-backups] 共删除 {len(to_remove)} 个备份槽")

    # 若 latest 指向了被删除的槽，更新 latest
    if latest_target and not latest_target.exists():
        remaining = sorted(
            [d for d in backup_base.iterdir()
             if d.is_dir() and d.name.startswith(task_name + "_")
             and not d.name.endswith("_latest")],
            key=lambda p: p.name,
        )
        if remaining:
            _set_latest(backup_base, task_name, remaining[-1])
            print(f"[remove-backups] latest 已更新为: {remaining[-1].name}")
        else:
            for p in [backup_base / f"{task_name}_latest",
                      backup_base / f"{task_name}_latest.txt"]:
                if p.is_symlink() or p.exists():
                    p.unlink(missing_ok=True)
            print(f"[remove-backups] 所有备份已删除，latest 链接已移除")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SiteSync_tools — 卸车站点配置同步管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    def _add_task(p):
        p.add_argument("--task", default=DEFAULT_TASK_NAME,
                       help=f"task 名称（默认: {DEFAULT_TASK_NAME}）")

    def _add_input(p, task_ref="task"):
        p.add_argument("--input-dir", default=None,
                       help="读取 YAML 配置的目录（默认: {task}_config）")

    def _add_output(p):
        p.add_argument("--output-dir", default=None,
                       help="写入 YAML 配置的目录（默认: {task}_config）")

    # extract
    p_ext = sub.add_parser("extract", help="从 .task 文件提取节点属性到 YAML")
    _add_task(p_ext); _add_output(p_ext)

    # update
    p_upd = sub.add_parser("update", help="将 task 相对 nodes_all 的改动追加到 nodes_overrides，并更新 nodes_all 基线")
    _add_task(p_upd); _add_input(p_upd); _add_output(p_upd)

    # apply
    p_app = sub.add_parser("apply", help="将 YAML 配置应用回 .task 文件")
    _add_task(p_app); _add_input(p_app)
    p_app.add_argument("--overwrite-python", type=lambda x: x.lower() == "true",
                       default=False, metavar="true|false",
                       help="同步将 PythonNodeModel 的 script 改动写回对应 .py 文件（默认: false）")
    p_app.add_argument("--yaml", dest="yaml_files", action="append", default=None,
                       metavar="FILENAME",
                       help="只 apply 指定的 yaml 文件名（可重复），如 --yaml nodes_overrides.yaml")
    p_app.add_argument("--trust-task", dest="trust_task",
                       type=lambda x: x.lower() not in ("false", "0", "no"),
                       default=True, metavar="true|false",
                       help="以当前 task 为基准叠加 overrides 并更新 nodes_all（默认: true）")

    # diff
    p_diff = sub.add_parser("diff", help="对比两个 YAML 配置文件")
    p_diff.add_argument("yaml_a", help="YAML 文件 A")
    p_diff.add_argument("yaml_b", help="YAML 文件 B")
    p_diff.add_argument("--keys", default="", help="只比较包含指定关键词的字段，逗号分隔")

    # backup
    p_bak = sub.add_parser("backup", help="备份 .task 文件和 YAML 配置（全量）")
    _add_task(p_bak); _add_input(p_bak)

    # restore
    p_res = sub.add_parser("restore", help="从备份还原（自动根据备份类型决定还原范围）")
    _add_task(p_res); _add_output(p_res)
    p_res.add_argument("--slot", default="latest", help="备份槽名称（默认: latest）")
    p_res.add_argument("--only-yaml", action="store_true", default=False,
                       help="只还原 YAML 配置，不还原 task 文件")
    p_res.add_argument("--only-task", action="store_true", default=False,
                       help="只还原 task 文件，不还原 YAML 配置")

    # list-backups
    p_lb = sub.add_parser("list-backups", help="列出所有备份")
    _add_task(p_lb)

    # remove-backups
    p_rm = sub.add_parser("remove-backups", help="删除指定备份槽")
    _add_task(p_rm)
    grp = p_rm.add_mutually_exclusive_group()
    grp.add_argument("--all", dest="remove_all", action="store_true", default=False,
                     help="删除全部备份槽")
    grp.add_argument("--slot", default=None, metavar="SLOT_NAME", help="删除指定备份槽")
    grp.add_argument("--count", type=int, default=None, metavar="N",
                     help="删除最近 N 个备份（默认 1，与 --days/--slot 互斥）")
    grp.add_argument("--days", type=int, default=None, metavar="N",
                     help="删除超过 N 天的旧备份（默认 7，与 --count/--slot 互斥）")

    args = parser.parse_args()

    # 无子命令时启动 TUI
    if args.cmd is None:
        from SiteSync_tools_ui import main as ui_main
        ui_main()
        return

    if args.cmd == "extract":
        out = args.output_dir or default_config_dir(args.task)
        cmd_extract(args.task, out)
    elif args.cmd == "update":
        inp = args.input_dir or default_config_dir(args.task)
        out = args.output_dir or default_config_dir(args.task)
        cmd_update(args.task, inp, out)
    elif args.cmd == "apply":
        inp = args.input_dir or default_config_dir(args.task)
        cmd_apply(args.task, inp, overwrite_python=args.overwrite_python,
                  yaml_files=args.yaml_files, trust_task=args.trust_task)
    elif args.cmd == "diff":
        filter_keys = [k.strip() for k in args.keys.split(",") if k.strip()] if args.keys else None
        cmd_diff(args.yaml_a, args.yaml_b, filter_keys)
    elif args.cmd == "backup":
        inp = args.input_dir or default_config_dir(args.task)
        cmd_backup(args.task, inp)
    elif args.cmd == "restore":
        out = args.output_dir or default_config_dir(args.task)
        cmd_restore(args.task, out, args.slot,
                    only_yaml=args.only_yaml, only_task=args.only_task)
    elif args.cmd == "list-backups":
        cmd_list_backups(args.task)
    elif args.cmd == "remove-backups":
        cmd_remove_backups(args.task, slot=args.slot, days=args.days, count=args.count,
                           remove_all=args.remove_all)


if __name__ == "__main__":
    main()
