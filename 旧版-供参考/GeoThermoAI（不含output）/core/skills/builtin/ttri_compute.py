"""
TTRI 计算 Skill

计算地形热响应指数（Terrain Thermal Response Index）：
    - 对训练/验证/测试集各自独立拟合多元线性回归 TTRI = a*DEM + b*Slope + c*cos(Aspect)
    - 对10m预测数据通过30m规则网格双线性插值计算TTRI
"""

import os
import pandas as pd
from typing import Any, Dict, List

from ..base_skill import BaseSkill, SkillParameter, SkillResult


class TTRIComputeSkill(BaseSkill):
    """计算TTRI（地形热响应指数）"""

    @property
    def name(self) -> str:
        return "ttri_compute"

    @property
    def group(self) -> str:
        return "ttri_compute"

    @property
    def description(self) -> str:
        return "对训练/验证/测试集拟合TTRI多元线性回归系数（DEM, Slope, cos(Aspect) → LST），并计算TTRI列；对10m预测数据通过双线性插值计算TTRI。"

    @property
    def parameters(self) -> List[SkillParameter]:
        return [
            SkillParameter(
                name="train_csv",
                type="file_path",
                description="训练集CSV路径（需包含DEM, Slope, cos(Aspect), LST列）",
                required=True,
            ),
            SkillParameter(
                name="val_csv",
                type="file_path",
                description="验证集CSV路径",
                required=True,
            ),
            SkillParameter(
                name="test_csv",
                type="file_path",
                description="测试集CSV路径",
                required=True,
            ),
            SkillParameter(
                name="data_30m_csv",
                type="file_path",
                description="30m全量特征CSV路径（用于构建TTRI网格），默认 output_dir/../processed/30m_features_step2.csv",
                required=False,
            ),
            SkillParameter(
                name="predict_10m_csv",
                type="file_path",
                description="10m预测特征CSV路径，默认 output_dir/../processed/10m_predict_features.csv",
                required=False,
            ),
            SkillParameter(
                name="output_dir",
                type="file_path",
                description="输出目录路径",
                required=True,
            ),
            SkillParameter(
                name="batch_size",
                type="number",
                description="批处理大小（10m预测数据插值时使用），默认 500000",
                required=False,
                default=500000,
            ),
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
            "coefficients": "TTRI回归系数",
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

        # 默认路径：和 data_pipeline 输出路径一致
        data_30m_csv = params.get("data_30m_csv",
                                   os.path.join(output_dir, "30m_features_step2.csv"))
        predict_10m_csv = params.get("predict_10m_csv",
                                     os.path.join(output_dir, "10m_predict_features.csv"))

        for name, val in [
            ("train_csv", train_csv),
            ("val_csv", val_csv),
            ("test_csv", test_csv),
            ("output_dir", output_dir),
        ]:
            if not val:
                return SkillResult(success=False, message=f"参数 {name} 不能为空")

        try:
            from ...ttri import compute_ttri_train, compute_ttri_predict
        except ImportError:
            return SkillResult(success=False, message="无法导入TTRI计算模块")

        # ── 训练/验证/测试集TTRI ────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始计算训练/验证/测试集 TTRI...")

        try:
            train_result = compute_ttri_train(
                train_csv=train_csv,
                val_csv=val_csv,
                test_csv=test_csv,
                output_dir=output_dir,
                progress_callback=progress_callback,
            )
        except Exception as e:
            return SkillResult(success=False, message=f"TTRI训练集计算失败: {e}")

        output_files = train_result.get("output_files", {})
        train_coef = train_result.get("train", {})

        result_data = {
            "train_with_ttri": output_files.get("train", ""),
            "val_with_ttri": output_files.get("validate", ""),
            "test_with_ttri": output_files.get("test", ""),
            "predict_with_ttri": "",
            "coefficients": {
                "train": train_coef,
                "validate": train_result.get("validate", {}),
                "test": train_result.get("test", {}),
            },
        }

        artifacts = [v for v in output_files.values() if v]

        # ── 10m预测数据TTRI ──────────────────────────────────────────
        if log_callback:
            log_callback("INFO", "开始计算10m预测数据 TTRI...")

        if not os.path.isfile(data_30m_csv):
            if log_callback:
                log_callback("WARN", f"30m全量数据不存在，跳过10m TTRI计算: {data_30m_csv}")
        elif not os.path.isfile(predict_10m_csv):
            if log_callback:
                log_callback("WARN", f"10m预测数据不存在，跳过10m TTRI计算: {predict_10m_csv}")
        else:
            # 检查是否已有有效 TTRI 列
            _has_ttri = False
            try:
                _head = pd.read_csv(predict_10m_csv, nrows=1)
                _has_ttri = "TTRI" in _head.columns
                if _has_ttri:
                    # 验证 TTRI 值是否有效（不全为 NaN）
                    _sample = pd.read_csv(predict_10m_csv, usecols=["TTRI"], nrows=1000)
                    _has_ttri = _sample["TTRI"].notna().any() > 0
            except Exception:
                pass

            if _has_ttri:
                if log_callback:
                    log_callback("INFO", "10m预测数据已含有效TTRI，跳过计算")
            else:
                # 清理可能残留的旧 TTRI 列
                try:
                    _sample_cols = pd.read_csv(predict_10m_csv, nrows=1).columns.tolist()
                    if "TTRI" in _sample_cols:
                        _tmp_clean = predict_10m_csv + ".clean_tmp"
                        _keep_cols = [c for c in _sample_cols if c != "TTRI"]
                        _chunks = pd.read_csv(predict_10m_csv, chunksize=500000, usecols=_keep_cols)
                        for i, c in enumerate(_chunks):
                            c.to_csv(_tmp_clean, mode="w" if i == 0 else "a", header=i == 0, index=False)
                        import shutil
                        shutil.move(_tmp_clean, predict_10m_csv)
                        if log_callback:
                            log_callback("INFO", "已清理残留TTRI列")
                except Exception:
                    pass
                try:
                    _temp_output = predict_10m_csv + ".ttri_tmp"
                    predict_result = compute_ttri_predict(
                        data_30m_csv=data_30m_csv,
                        predict_10m_csv=predict_10m_csv,
                        output_path=_temp_output,
                        train_csv=train_csv,
                        batch_size=batch_size,
                        progress_callback=progress_callback,
                    )
                    # 完成后替换原始文件
                    import shutil
                    shutil.move(_temp_output, predict_10m_csv)
                    result_data["predict_with_ttri"] = predict_10m_csv
                    artifacts.append(predict_10m_csv)
                    if log_callback:
                        log_callback("INFO",
                            f"10m TTRI计算完成: {predict_result['total_valid']:,} 有效行"
                            f"（耗时 {predict_result['elapsed_seconds']}s）")
                except Exception as e:
                    if log_callback:
                        log_callback("WARN", f"10m TTRI计算失败（不影响训练）: {e}")

        if progress_callback:
            progress_callback("ttri_compute", 1.0, "TTRI计算完成")

        return SkillResult(
            success=True,
            message=(
                f"TTRI计算完成: 训练集 R²={train_coef.get('r2', 'N/A')}, "
                f"系数 a(DEM)={train_coef.get('coefficients', [0])[0]:.6f}"
            ),
            data=result_data,
            artifacts=artifacts,
        )
