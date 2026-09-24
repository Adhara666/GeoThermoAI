**[English](Algorithm.md)** | [简体中文](算法原理.md)

# Core Algorithms

## 5.1 Data calibration, alignment, and masking

### 5.1.1 Sentinel-2 reflectance calibration

　　Sentinel-2 L2A imagery is converted to surface reflectance using the scene metadata:

$$
\rho_b=\frac{D_b+O_b}{Q}.
$$

　　where $\rho_b$ is the surface reflectance of band $b$, $D_b$ is the digital number of that band, $O_b$ is the additive offset given by the scene metadata, $Q$ is the quantification factor, and $b$ is the spectral band. Invalid raw zeros do not participate in subsequent computation.

### 5.1.2 Landsat temperature conversion and quality control

　　Landsat land surface temperature is converted to Kelvin using the product-specified scale and offset:

$$
T=0.00341802D_T+149.0.
$$

　　where $T$ is the land surface temperature in K, and $D_T$ is the digital number of the thermal infrared band. Quality control excludes fill values, clouds, cirrus, dilated clouds, and cloud shadows. The Sentinel-2 scene classification layer keeps only vegetation, bare soil, and water classes. Continuous variables use bilinear resampling; categorical variables use nearest-neighbor resampling.

## 5.2 Spectral indices and terrain features

　　Spectral indices express differences among vegetation, water, and built-up areas:

$$
\mathrm{NDVI}=\frac{N-R}{N+R},\qquad
\mathrm{NDWI}=\frac{G-N}{G+N},\qquad
\mathrm{NDBI}=\frac{S-N}{S+N}.
$$

　　where $N$ is near-infrared reflectance, $R$ is red reflectance, $G$ is green reflectance, and $S$ is shortwave-infrared reflectance. $NDVI$, $NDWI$, and $NDBI$ describe spectral differences related to vegetation, water, and built-up areas, respectively.

　　Terrain slope and aspect are computed from the horizontal gradients of the digital elevation model:

$$
\mathrm{Slope}=\frac{180}{\pi}\arctan\sqrt{g_x^2+g_y^2},
$$

$$
\mathrm{Aspect}=\left[\frac{180}{\pi}\mathrm{atan2}(-g_x,g_y)+360\right]\bmod 360.
$$

　　where $g_x$ and $g_y$ are the elevation change rates in the east-west and north-south directions, $\pi$ is the circle constant, $\mathrm{atan2}$ is the quadrant-aware arctangent, and $\bmod$ is the modulo operation. $Slope$ and $Aspect$ are expressed in degrees.

## 5.3 Dual data streams and spatial blocks

### 5.3.1 Training and evaluation data stream

　　The training and evaluation stream keeps only 30 m samples that pass the temperature, cloud-mask, spectral, and terrain checks simultaneously. When the sample count is large, rows and columns are sampled by fixed strides to avoid excessive clustering of adjacent pixels. Training, validation, and test regions are then divided into spatial blocks, with buffer zones at the boundaries between sets to reduce information leakage caused by direct adjacency. The exact buffer distance is documented with the experiment settings in the results.

### 5.3.2 Full-constraint data stream

　　The full-constraint stream keeps all valid 30 m parent cells for terrain thermal response spatialization, thermal constraint correction, and coarse-scale closure. It is not replaced by the training subsample. The valid 10 m domain is defined jointly by valid Sentinel-2 spectral pixels and the scene classification, so not every valid 10 m pixel has its own 30 m thermal label.

## 5.4 Terrain thermal response index

　　Because no open global 10 m terrain dataset exists, we designed the terrain thermal response index to compress the combined influence of elevation, slope, and aspect on temperature and to serve as a 10 m-scale substitute for terrain data. The training stage fits:

$$
T_{30}=\beta_0+aH+bS+c\cos A+\varepsilon.
$$

　　where $T_{30}$ is the 30 m land surface temperature, $H$ is elevation, $S$ is slope, $A$ is aspect, $a$, $b$, and $c$ are the regression coefficients of the three terrain variables, $\beta_0$ is the intercept, and $\varepsilon$ is the unexplained residual. The terrain thermal response index is defined as:

$$
I_{\mathrm{TTRI}}=aH+bS+c\cos A.
$$

　　where $I_{\mathrm{TTRI}}$ is the terrain thermal response index and the remaining symbols are as above. The coefficients are fitted once on the training set and applied unchanged to the validation region, test region, the full 30 m constraint grid, and the 10 m prediction grid, so that terrain relationships are never adjusted using validation or test information.

## 5.5 Random Forest temperature mapping

　　The Random Forest combines red, green, blue, near-infrared, and shortwave-infrared reflectance together with NDVI, NDWI, NDBI, and the terrain thermal response index — nine features in total. Its prediction is expressed as:

$$
\widehat{T}^{\mathrm{RF}}(\mathbf{x})=\frac{1}{M}\sum_{m=1}^{M}f_m(\mathbf{x}).
$$

　　where $\widehat{T}^{\mathrm{RF}}$ is the Random Forest predicted temperature, $\mathbf{x}$ is the nine-dimensional input feature vector, $M$ is the total number of regression trees, $m$ is the tree index, and $f_m$ is the prediction function of that tree. The tree count and random state are fixed by the experiment configuration and stay unchanged across the same training, validation, and test procedure.

## 5.6 Thermal constraint residual correction

　　The Random Forest initial values carry 10 m spatial detail, but the mean of the fine pixels within a 30 m parent cell is not necessarily equal to the thermal infrared reference. For a parent cell $g$, the mean predicted temperature of its valid 10 m children is first computed:

$$
\overline{T}^{\mathrm{RF}}_g=\frac{1}{n_g}\sum_{i\in g}\widehat{T}^{\mathrm{RF}}_i.
$$

　　where $g$ is a 30 m parent cell, $n_g$ is the number of valid 10 m child pixels in that cell, $i$ is one child pixel, $\widehat{T}^{\mathrm{RF}}_i$ is its Random Forest predicted temperature, and $\overline{T}^{\mathrm{RF}}_g$ is the mean predicted temperature within the parent cell.

　　The parent-cell thermal constraint residual is:

$$
R_g=T^{30}_g-\overline{T}^{\mathrm{RF}}_g.
$$

　　where $R_g$ is the thermal constraint residual of parent cell $g$, and $T^{30}_g$ is the 30 m reference temperature of that cell. Adding the same residual to every valid child pixel in the cell gives the final temperature:

$$
\widehat{T}^{10}_i=\widehat{T}^{\mathrm{RF}}_i+R_g,\qquad i\in g.
$$

　　where $\widehat{T}^{10}_i$ is the final 10 m temperature of child pixel $i$ and the remaining symbols are as above. It follows that:

$$
\frac{1}{n_g}\sum_{i\in g}\widehat{T}^{10}_i=T^{30}_g.
$$

　　This equation expresses the arithmetic-mean constraint within each parent cell; it does not represent energy conservation, nor does it mean the result has been validated against independent 10 m ground truth.

## 5.7 Export, evaluation, and optional gap filling

　　The final product is exported as a GeoTIFF preserving the coordinate reference, affine transform, and no-data information. The default output name is `rf_10m_lst_final.tif`. Model evaluation uses the coefficient of determination, root mean square error, mean absolute error, and mean bias:

$$
R^2=1-\frac{\sum_{i=1}^{n}(y_i-\widehat{y}_i)^2}{\sum_{i=1}^{n}(y_i-\overline{y})^2},
$$

$$
\mathrm{RMSE}=\sqrt{\frac{1}{n}\sum_{i=1}^{n}(\widehat{y}_i-y_i)^2},
$$

$$
\mathrm{MAE}=\frac{1}{n}\sum_{i=1}^{n}\left|\widehat{y}_i-y_i\right|,
\qquad
\mathrm{MB}=\frac{1}{n}\sum_{i=1}^{n}(\widehat{y}_i-y_i).
$$

　　where $n$ is the number of samples, $i$ is the sample index, $y_i$ is the observed temperature, $\widehat{y}_i$ is the predicted temperature, and $\overline{y}$ is the mean of all observed temperatures. $R^2$ closer to 1 indicates better overall fit; smaller $RMSE$ and $MAE$ indicate lower error; $MB$ close to 0 indicates small overall systematic bias.

　　When gaps are few, the system fills them by distance weighting. When gaps are many, the system estimates in a multi-scale space and restores to the original resolution. Filled values are for continuous display only and do not participate in thermal constraint closure or accuracy claims.
