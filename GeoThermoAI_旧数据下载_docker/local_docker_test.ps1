# 本地测试队友 Vue3 Docker 版（不碰 geothermal_gradio / 修改gdalbug）
# 用法:
#   .\local_docker_test.ps1
#   .\local_docker_test.ps1 -ApiKey "sk-xxx" -Port 7860
#   .\local_docker_test.ps1 -StopOnly

param(
    [string]$ApiKey = $env:LLM_API_KEY,
    [int]$Port = 7860,
    [switch]$StopOnly,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
# This script lives next to Dockerfile
$Root = $PSScriptRoot
Set-Location $Root

$Image = "geothermoai-teammate:test"
$Container = "geothermoai-teammate-test"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "找不到 docker。请先启动 Docker Desktop。"
}
docker info 1>$null 2>$null
if ($LASTEXITCODE -ne 0) { throw "Docker 引擎未就绪，请等 Desktop 变成 Running。" }

if ($StopOnly) {
    docker rm -f $Container 2>$null | Out-Null
    Write-Host "已停止: $Container"
    exit 0
}

if (-not (Test-Path ".\dist\index.html")) {
    throw "缺少 dist\index.html。先在 frontend 执行 npm run build，再复制到项目根 dist\"
}

if (-not $SkipBuild) {
    Write-Host "==> docker build $Image （首次可能 10-30 分钟）"
    docker build `
        --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/ `
        --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn `
        -t $Image .
}

docker rm -f $Container 2>$null | Out-Null
$envArgs = @()
if ($ApiKey) { $envArgs += @("-e", "LLM_API_KEY=$ApiKey") }

Write-Host "==> 启动 http://127.0.0.1:$Port"
docker run -d --rm -p "${Port}:7860" --name $Container @envArgs $Image | Out-Null
Write-Host "日志: docker logs -f $Container"
Write-Host "停止: .\local_docker_test.ps1 -StopOnly"
