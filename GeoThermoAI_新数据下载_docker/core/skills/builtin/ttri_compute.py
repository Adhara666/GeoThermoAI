"""
TTRI 计算 Skill

    - 仅用训练集（train split）拟合一次 TTRI 回归系数，固定保存 ttri_coefficients.json；
      validate/test 复用同一组系数做无标签变换；
    - 10m 预测数据的空间化插值基于完整 30m 约束层 + 统一仿射映射双线性插值；
    - 每次执行都完整重算并通过原子替换写回；10m TTRI 计算失败时直接返回
      success=False，使依赖链失败。

Agent 的 SKILL_PATHS 只注入 output_dir / data_30m_csv / predict_10m_csv /
train_csv / val_csv / test_csv，不包含 constraint_csv / constraint_meta /
predict_10m_meta；本 Skill 按固定命名约定从 output_dir（即预处理阶段的
processed_dir）自动推导这些路径，不需要 Agent 额外注入。
"""

import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult
from ...atomic_io import atomic_replace
from ... import manifest as run_manifest
from ...stage_rebuild import ensure_stage_inputs


class TTRIComputeSkill(BaseSkill):
    """计算TTRI（地形热响应指数）：仅 train 拟合一次 + 统一仿射映射空间化"""

    @property
    def name(self) -> str:
        return "ttri_compute"

    @property
    def group(self) -> str:
        return "ttri_compute"

    @property
    def description(self) -> str:
        return "仅用训练集拟合一次TTRI多元线性回归系数（DEM, Slope, cos(Aspect) → LST），固定保存系数；validate/test/完整30m约束层/10m预测格网复用同一组系数做无标签变换。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="train_csv", type="file_path", description="训练集CSV路径（需包含DEM, Slope, cos(Aspect), LST列）", required=True),
            SkillParameter(name="val_csv", type="file_path", description="验证集CSV路径", required=True),
            SkillParameter(name="test_csv", type="file_path", description="测试集CSV路径", required=True),
            SkillParameter(name="output_dir", type="file_path", description="输出目录路径（预处理阶段的processed目录）", required=True),
            SkillParameter(name="constraint_csv", type="file_path", description="完整30m约束层CSV路径，默认 output_dir/30m_constraint_grid.csv", required=False),
            SkillParameter(name="predict_10m_csv", type="file_path", description="10m预测特征CSV路径，默认 output_dir/10m_predict_features.csv", required=False),
            SkillParameter(name="batch_size", type="number", description="批处理大小（10m预测数据插值时使用），默认 500000", required=False, default=500000),
        ]

    @property
    def input_schema(self) -> Dict[str, str]:
        return {
            "train_csv": "训练集CSV路径",
            "val_csv": "验证集CSV路径",
            "test_csv": "测试集CSV路径",
            "output_dir": "输出目录",
        }

    @property
    def output_schema(self) -> Dict[str, str]:
        return {
            "train_with_ttri": "训练集含TTRI的CSV路径",
            "val_with_ttri": "验证集含TTRI的CSV路径",
            "test_with_ttri": "测试集含TTRI的CSV路径",
            "predict_with_ttri": "10m预测数据含TTRI的CSV路径",
            "coefficients_path": "固定 ttri_coefficients.json 路径",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行TTRI计算。"""
        train_csv = params.get("train_csv", "")
        val_csv = params.get("val_csv", "")
        test_csv = params.get("test_csv", "")
        output_dir = params.get("output_dir", "")
        batch_size = params.get("batch_size", 500000)

        for name, val in [
            ("train_csv", train_csv), ("val_csv", val_csv),
            ("test_csv", test_csv), ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        # 单步重跑支持：输入被阶段清理删除时自动重建（失败则明确报错，不静默）
        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            try:
                ensure_stage_inputs(project_root, "ttri_compute", log_callback=log_callback)
            except Exception as e:
                return SkillResult(success=False, message=f"重建输入失败: {e}")

        # 固定命名推导（Agent 不会显式注入，见模块顶部说明）
        constraint_csv = params.get("constraint_csv") or os.path.join(output_dir, "30m_constraint_grid.csv")
        constraint_meta = params.get("constraint_meta") or os.path.join(output_dir, "30m_constraint_grid_meta.json")
        predict_10m_csv = params.get("predict_10m_csv") or os.path.join(output_dir, "10m_predict_features.csv")
        predict_10m_meta = params.get("predict_10m_meta") or os.path.join(output_dir, "10m_predict_features_meta.json")

        for label, path in [
            ("完整30m约束层", constraint_csv), ("完整30m约束层元数据", constraint_meta),
            ("10m预测特征", predict_10m_csv), ("10m预测元数据", predict_10m_meta),
        ]:
            if not os.path.isfile(path):
                return SkillResult(
                    success=False,
                    message=f"缺少必需输入文件（{label}）: {path}；请确认已成功完成 data_pipeline 阶段（fail-fast）",
                )

        try:
            from ...ttri import compute_ttri_for_splits, compute_ttri_predict, compute_ttri_for_constraint_grid
        except ImportError:
            return SkillResult(success=False, message="无法导入TTRI计算模块")

        # ── 步骤1: 仅 train 拟合一次，train/validate/test 复用同一组系数 ────
        if log_callback:
            log_callback("INFO", "开始计算 TTRI（仅 train 拟合一次，validate/test 无标签复用）...")

        try:
            train_result = compute_ttri_for_splits(
                train_csv=train_csv, val_csv=val_csv, test_csv=test_csv,
                output_dir=output_dir, progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"TTRI训练集计算失败: {e}")

        coefficients_path = train_result["coefficients_path"]
        output_files = train_result.get("output_files", {})
        coef_summary = train_result.get("coefficients", {})

        # ── 步骤2: 完整30m约束层上应用同一组系数（供 TCR/调试使用）──────────
        try:
            compute_ttri_for_constraint_grid(constraint_csv, coefficients_path)
        except Exception as e:
            return SkillResult(success=False, message=f"完整30m约束层 TTRI 计算失败: {e}")

        # ── 步骤3: 10m预测数据TTRI（完整约束层 + 统一仿射映射插值）──
        if log_callback:
            log_callback("INFO", "开始计算10m预测数据 TTRI（统一仿射映射双线性插值）...")

        tmp_output = predict_10m_csv + ".ttri_tmp"
        try:
            predict_result = compute_ttri_predict(
                constraint_csv=constraint_csv,
                constraint_meta_json=constraint_meta,
                predict_10m_csv=predict_10m_csv,
                predict_10m_meta_json=predict_10m_meta,
                coefficients=coefficients_path,
                output_path=tmp_output,
                batch_size=batch_size,
                progress_callback=progress_callback,
            )
        except Exception as e:
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            # 10m TTRI 失败必须使依赖链失败，不能只记 WARN 后仍返回成功
            return SkillResult(success=False, message=f"10m预测数据 TTRI 计算失败: {e}")

        if predict_result.get("total_valid", 0) == 0:
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            return SkillResult(
                success=False,
                message="10m预测数据 TTRI 计算完成但有效行数为0，可能约束层与预测格网完全不重叠，拒绝返回成功",
            )

        # 校验通过后原子替换（避免读写同一文件的竞态）
        atomic_replace(tmp_output, predict_10m_csv)

        result_data = {
            "train_with_ttri": output_files.get("train", ""),
            "val_with_ttri": output_files.get("validate", ""),
            "test_with_ttri": output_files.get("test", ""),
            "predict_with_ttri": predict_10m_csv,
            "coefficients_path": coefficients_path,
            "coefficients": coef_summary,
            "constraint_csv": constraint_csv,
            "constraint_meta": constraint_meta,
            "ttri_predict_stats": {
                "total_valid": predict_result.get("total_valid", 0),
                "total_invalid": predict_result.get("total_invalid", 0),
                "out_of_grid": predict_result.get("out_of_grid", 0),
            },
        }

        artifacts = [v for v in output_files.values() if v] + [coefficients_path, predict_10m_csv]

        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            run_manifest.record_stage(
                project_root, "ttri_compute", run_manifest.STATUS_COMPLETED,
                artifacts={k: v for k, v in result_data.items() if isinstance(v, str) and v},
                stats={"coefficients": coef_summary, "predict": result_data["ttri_predict_stats"]},
            )

        if progress_callback:
            progress_callback("ttri_compute", 1.0, "TTRI计算完成")

        coef_list = coef_summary.get("coefficients", [0, 0, 0])
        return SkillResult(
            success=True,
            message=(
                f"TTRI计算完成（仅train拟合一次）: R²={coef_summary.get('r2', 'N/A')}, "
                f"系数 a(DEM)={coef_list[0]:.6f}；10m有效 {predict_result.get('total_valid', 0):,} 行，"
                f"约束层覆盖范围外 {predict_result.get('out_of_grid', 0):,} 行"
            ),
            data=result_data,
            artifacts=artifacts,
        )
