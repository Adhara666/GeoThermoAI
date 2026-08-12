# -*- coding: utf-8 -*-
"""
中间过程文件清理模块

按阶段删除全流程运行中不再需要的中间产物，减少磁盘占用（需求：只保留
原始影像、最终 LST 影像与超参数寻优必需的过程文件）。

删除时机与 Agent 7 阶段工作流（core/agent/geo_thermo_agent.py 的 SKILL_PATHS
注入的 project_dir/{raw,processed,results} 目录约定）一致：

    - data_pipeline 完成后：Aligned_* 对齐栅格（4 个）+ 30m_features_step2.parquet
    - rf_model      完成后：processed/train.parquet, validate.parquet, test.parquet
    - tcr_compute   完成后：processed/10m_predict_features.parquet
    - lst_export    完成后：results/rf_10m_predict.parquet
    - accuracy_eval 完成后：processed/30m_constraint_grid.parquet, results/tcr_result.parquet

删除前提已逐一核对：被删文件在该阶段之后不再被任何下游环节（skill、
run_manifest 状态查询、精度接口、地图图层）读取；仅做 dirname 定位的
遗留参数（data_30m_csv/full_30m_csv 等）不依赖文件真实存在。

安全约定：
    - 只删除精确 basename 匹配的文件，绝不按前缀/模糊匹配；
    - 任何删除失败都吞掉（OSError），绝不抛出影响主流程；
    - 同一份集合 INTERMEDIATE_FILENAMES 同时用于前端下载面板过滤与
      下载接口拦截，保证这些文件即使处于产生阶段也不会出现在下载面板。

注意：清理后若单独重跑某个下游 skill（而非整条 7 阶段流水线），会因
缺少中间输入而失败——这是"减少磁盘占用"的预期代价；完整流水线运行不受影响。
"""

import os

# 会被清理的中间产物文件名（精确 basename；下载面板/下载接口按同一集合过滤；
# 升级：中间产物表统一 Parquet 后缀）
INTERMEDIATE_FILENAMES = frozenset({
    "Aligned_S2_30m.tif",
    "Aligned_SCL_30m.tif",
    "Aligned_DEM_30m.tif",
    "Aligned_SCL_to_S2_10m.tif",
    "30m_features_step2.parquet",
    "30m_constraint_grid.parquet",
    "10m_predict_features.parquet",
    "train.parquet",
    "validate.parquet",
    "test.parquet",
    "tcr_result.parquet",
    "tcr_predict.parquet",
    "rf_10m_predict.parquet",
    "_merged_vrt_temp.tif",
})

# 各阶段完成后要删除的文件名（键与 run_manifest stage 名 / skill name 一致）
_STAGE_FILES: dict = {
    "data_pipeline": [
        "Aligned_S2_30m.tif",
        "Aligned_SCL_30m.tif",
        "Aligned_DEM_30m.tif",
        "Aligned_SCL_to_S2_10m.tif",
        "30m_features_step2.parquet",
    ],
    "rf_model": [
        "train.parquet",
        "validate.parquet",
        "test.parquet",
    ],
    "tcr_compute": [
        "10m_predict_features.parquet",
    ],
    "lst_export": [
        "rf_10m_predict.parquet",
    ],
    "accuracy_eval": [
        "30m_constraint_grid.parquet",
        "tcr_result.parquet",
        "tcr_predict.parquet",
    ],
}


def is_intermediate_name(filename: str) -> bool:
    """判断文件名是否属于会被删除的中间产物（供下载面板过滤）"""
    return bool(filename) and os.path.basename(filename) in INTERMEDIATE_FILENAMES


def cleanup_stage(project_dir: str, stage: str):
    """在指定阶段完成后删除该阶段不再需要的中间产物（含子目录）。

    Args:
        project_dir: 项目根目录（与 run_manifest.json 同层）
        stage:       阶段名（data_pipeline / rf_model / tcr_compute /
                     lst_export / accuracy_eval）

    任何异常都不会向外抛出，清理失败不影响主流程。
    """
    if not stage or not project_dir or not os.path.isdir(project_dir):
        return
    names = set(_STAGE_FILES.get(stage, ()))
    if not names:
        return
    removed = []
    for root, _dirs, files in os.walk(project_dir):
        for name in files:
            if name in names:
                full = os.path.join(root, name)
                try:
                    os.remove(full)
                    removed.append(full)
                except OSError:
                    pass
    return removed
