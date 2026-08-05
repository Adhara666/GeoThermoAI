"""
随机森林训练+预测 Skill（B-02 / A-07 重写）

    - 训练参数改为"先拷贝默认值，再用白名单覆盖"，random_state/max_features
      不再因用户传入部分参数而静默丢失；n_jobs 按容器 CPU 配额解析（B-02）；
    - 测试集推理完成后，额外调用 evaluate_independent_prediction() 写出固定
      independent_prediction.json（A-07 协议一）。Agent 的7阶段固定工作流
      （core/agent/geo_thermo_agent.py，未修改）里没有单独的"独立预测评估"
      步骤，因此把该产物作为 rf_model 阶段的附加产物一并写出，不新增 Agent
      工作流步骤名。
"""

import json
import os
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, Hyperparameter, SkillResult
from ... import manifest as run_manifest
from ...intermediate_cleanup import cleanup_stage
from ...stage_rebuild import ensure_stage_inputs


class RFModelSkill(BaseSkill):
    """随机森林回归模型训练与测试集预测 + 独立预测评估"""

    @property
    def name(self) -> str:
        return "rf_model"

    @property
    def group(self) -> str:
        return "model_train_predict"

    @property
    def description(self) -> str:
        return "融合多光谱、遥感指数、地形因子等多特征进行LST降尺度模型训练与预测，输出模型文件、评估指标（R²/RMSE/MAE/MB）、特征重要性排序和独立预测协议JSON。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(name="train_csv", type="file_path", description="训练集CSV路径（需包含TTRI列）", required=True),
            SkillParameter(name="val_csv", type="file_path", description="验证集CSV路径（需包含TTRI列）", required=True),
            SkillParameter(name="test_csv", type="file_path", description="测试集CSV路径（需包含TTRI列）", required=True),
            SkillParameter(name="output_dir", type="file_path", description="输出目录路径", required=True),
            SkillParameter(name="n_estimators", type="number", description="决策树数量", required=False, default=200),
            SkillParameter(name="max_depth", type="number", description="最大深度", required=False, default=25),
            SkillParameter(name="min_samples_split", type="number", description="内部节点分裂所需最小样本数", required=False, default=16),
            SkillParameter(name="min_samples_leaf", type="number", description="叶节点最小样本数", required=False, default=8),
            SkillParameter(name="max_features", type="number", description="每次分裂考虑的最大特征比例", required=False, default=0.5),
            SkillParameter(name="random_state", type="number", description="随机种子（保证可复现）", required=False, default=42),
        ]

    @property
    def hyperparameters(self) -> List[Hyperparameter]:
        return [
            Hyperparameter(name="n_estimators", label="决策树数量", type="number", default=200, min=50, max=1000, step=50,
                          description="随机森林中决策树的数量，越大越稳定但越慢"),
            Hyperparameter(name="max_depth", label="最大深度", type="number", default=25, min=5, max=50, step=1,
                          description="每棵决策树的最大深度，防止过拟合"),
            Hyperparameter(name="min_samples_split", label="最小分裂样本数", type="number", default=16, min=2, max=100, step=2,
                          description="内部节点分裂所需的最小样本数"),
            Hyperparameter(name="min_samples_leaf", label="叶节点最小样本数", type="number", default=8, min=1, max=50, step=1,
                          description="叶节点所需的最小样本数"),
            Hyperparameter(name="max_features", label="最大特征比例", type="number", default=0.5, min=0.1, max=1.0, step=0.1,
                          description="每次分裂考虑的最大特征比例"),
            Hyperparameter(name="random_state", label="随机种子", type="number", default=42, min=0, max=999999, step=1,
                          description="固定后可复现；不填则使用默认值 42，不回退到 sklearn 的 None"),
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
            "model_path": "模型文件路径",
            "train_metrics": "训练集评估指标",
            "test_metrics": "测试集评估指标（含MB）",
            "metrics_path": "指标JSON路径",
            "feature_importance": "特征重要性列表",
            "independent_prediction_path": "独立预测协议JSON路径（A-07）",
        }

    def execute(
        self,
        params: Dict[str, Any],
        progress_callback=None,
        log_callback=None,
    ) -> SkillResult:
        """执行随机森林训练、测试集预测和独立预测评估。"""
        train_csv = params.get("train_csv", "")
        val_csv = params.get("val_csv", "")
        test_csv = params.get("test_csv", "")
        output_dir = params.get("output_dir", "")

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
                ensure_stage_inputs(project_root, "rf_model", log_callback=log_callback)
            except Exception as e:
                return SkillResult(success=False, message=f"重建输入失败: {e}")

        try:
            from ...rf_model import train_random_forest, predict_test_set
            from ...evaluation import evaluate_independent_prediction
        except ImportError:
            return SkillResult(success=False, message="无法导入RF模型/评估模块")

        # ── 构建超参数（B-02：白名单覆盖默认值，包含 random_state/max_features）
        rf_params = {}
        for key in ["n_estimators", "max_depth", "min_samples_split", "min_samples_leaf",
                    "max_features", "random_state"]:
            if key in params and params[key] is not None:
                rf_params[key] = params[key]

        if rf_params and log_callback:
            log_callback("INFO", f"自定义RF超参数（与默认值合并）: {rf_params}")

        # ── 步骤1: 训练 ──────────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始训练随机森林模型...")

        train_results_dir = os.path.join(output_dir, "train")
        os.makedirs(train_results_dir, exist_ok=True)

        try:
            train_result = train_random_forest(
                train_csv=train_csv, val_csv=val_csv,
                output_dir=train_results_dir,
                params=rf_params if rf_params else None,
                progress_callback=lambda sn, pct, msg: (
                    progress_callback(sn, pct * 0.4, f"[训练] {msg}") if progress_callback else None
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"模型训练失败: {e}")

        model_path = train_result.get("model_path", "")

        if log_callback:
            log_callback("INFO", f"模型训练完成: {model_path}；生效参数: {train_result.get('params', {})}")

        # ── 步骤2: 测试集预测 ────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始测试集预测...")

        test_results_dir = os.path.join(output_dir, "test")
        os.makedirs(test_results_dir, exist_ok=True)

        try:
            test_result = predict_test_set(
                test_csv=test_csv, model_path=model_path,
                output_dir=test_results_dir,
                progress_callback=lambda sn, pct, msg: (
                    progress_callback(sn, 0.4 + pct * 0.3, f"[预测] {msg}") if progress_callback else None
                ),
            )
        except Exception as e:
            return SkillResult(success=False, message=f"测试集预测失败: {e}")

        # ── 步骤3: 独立预测评估（A-07 协议一）──────────────────────────
        # Agent 固定7阶段工作流未单列该步骤，作为 rf_model 阶段的附加产物写出。
        split_info = None
        _split_info_path = os.path.join(os.path.dirname(train_csv), "split_info.json")
        if os.path.isfile(_split_info_path):
            try:
                with open(_split_info_path, "r", encoding="utf-8") as f:
                    split_info = json.load(f)
            except Exception:
                pass

        try:
            independent_result = evaluate_independent_prediction(
                test_csv=test_csv, model_path=model_path,
                output_dir=output_dir, split_info=split_info,
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"独立预测评估失败: {e}")

        result_data = {
            "model_path": model_path,
            "train_metrics": train_result.get("metrics", {}),
            "test_metrics": test_result.get("metrics", {}),
            "metrics_path": train_result.get("metrics_path", ""),
            "feature_importance": train_result.get("feature_importance", []),
            "features": train_result.get("features", []),
            "params": train_result.get("params", {}),
            "train_time_seconds": train_result.get("train_time_seconds", 0),
            "independent_prediction_path": independent_result.get("output_path", ""),
            "independent_prediction": {
                k: v for k, v in independent_result.items()
                if k not in ("output_path",)
            },
        }

        artifacts = [model_path, train_result.get("metrics_path", ""),
                    test_result.get("output_path", ""), independent_result.get("output_path", "")]
        artifacts = [a for a in artifacts if a]

        project_root = run_manifest.project_root_from_stage_output_dir(output_dir)
        if project_root:
            run_manifest.record_stage(
                project_root, "rf_model", run_manifest.STATUS_COMPLETED,
                artifacts={
                    "model_path": model_path, "metrics_path": train_result.get("metrics_path", ""),
                    "test_metrics_path": test_result.get("output_path", ""),
                    "independent_prediction_path": independent_result.get("output_path", ""),
                },
                stats={
                    "train_metrics": train_result.get("metrics", {}),
                    "test_metrics": test_result.get("metrics", {}),
                    "params": train_result.get("params", {}),
                },
            )
            # 训练+测试预测+独立评估完成后，train/val/test 划分 CSV 不再被下游读取，立即清理
            cleanup_stage(project_root, "rf_model")

        train_m = train_result.get("metrics", {}).get("train", {})
        test_m = test_result.get("metrics", {})

        if progress_callback:
            progress_callback("rf_model", 1.0, "训练+预测+独立评估完成")

        return SkillResult(
            success=True,
            message=(
                f"模型训练完成: 训练R²={train_m.get('R2', 'N/A')}, "
                f"测试R²={test_m.get('R2', 'N/A')}, "
                f"RMSE={test_m.get('RMSE', 'N/A')}, MB={test_m.get('MB', 'N/A')}"
            ),
            data=result_data,
            artifacts=artifacts,
        )
