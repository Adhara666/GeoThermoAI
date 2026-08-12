# 本轮审查修复的回归测试

这些脚本是 2026-08-03 按后端算法技术审查的确认结论
落地修改时编写的验证脚本，用于证明各项修复（A-01~A-08、B-01/B-02/B-07/B-10、
用户确认的14条决策）确实生效，不是"看起来改了但没测过"。

## 运行环境

这些脚本依赖 GDAL/rasterio/pandas/scikit-learn/geopandas/pystac-client 等完整
依赖，宿主机 Python 通常没有全部安装。推荐在项目自带的 Docker 镜像里运行：

```powershell
# 在项目根目录下（Dockerfile 所在目录）
docker build -t geothermoai-reviewed:test .

# 运行单个测试（挂载当前目录，直接用容器内的完整依赖）
docker run --rm -v "${PWD}:/ws" -w /ws geothermoai-reviewed:test python3 tests/test_ttri.py
```

## 测试清单

| 文件 | 验证内容 | 是否需要网络 |
|---|---|---|
| `test_geo_transform.py` | A-01 坐标轴序修复：武汉/北京/南半球 bbox → UTM 有限值；并复现原始 bug 证明修复必要 | 否 |
| `test_sentinel2_calibration.py` | A-03 Sentinel-2 按景定标：用真实 Planetary Computer STAC + MTD_MSIL2A.xml 核验 offset=-1000 | 是 |
| `test_ttri.py` | A-04 TTRI 仅 train 拟合一次、无标签泄漏；A-06 统一仿射插值精确匹配解析解；B-04 秩亏检测 | 否 |
| `test_tcr.py` | A-06 统一格网映射；TCR block_constant 精确闭合、smooth_recentered 闭合+边界跳变诊断 | 否 |
| `test_skill_chain_synthetic.py` | 完整6阶段 Skill 调用链（data_pipeline→ttri_compute→rf_model→tcr_compute→lst_export→accuracy_eval），严格复现 `core/agent/geo_thermo_agent.py` 的 SKILL_PATHS 参数注入方式；B-07 GeoTIFF row/col 校验；run_manifest.json 聚合 | 否（合成栅格） |
| `test_easylst_pipeline_synthetic.py` | `core.pipeline.EasyLSTPipeline` 全流程 + fail-fast（人为制造上游失败，验证下游全部标记 skipped_upstream） | 否（合成栅格） |
| `test_real_e2e_manual.py` | 用真实 Planetary Computer 下载数据跑通完整7阶段（含 data_acquisition），验证"全流程跑通"；耗时较长（视网络约10-15分钟），供人工按需运行，不建议纳入自动化 CI | 是 |

## 已知限制

- 这些是本轮手写的验证脚本，不是 pytest 套件（没有 `assert` 之外的 fixture/参数化），
  按顺序 `python3 tests/xxx.py` 直接跑，输出里有断言失败就是真的失败。
- `test_sentinel2_calibration.py`、`test_real_e2e_manual.py` 需要能访问
  `planetarycomputer.microsoft.com`；网络不可达时会失败，不代表代码本身有问题。
- 未在 ModelScope Studio 平台本身跑过；以上验证均为本地 Docker（`geothermoai-teammate:test` /
  新建的 `geothermoai-reviewed:test`），网络与 GDAL/PROJ 依赖版本与 Studio 实际环境
  可能存在差异，仍需在 Studio 上做一次真实验证。
