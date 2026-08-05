# Local smoke test for teammate Vue3 Docker app.
# Does NOT touch geothermal_gradio or 修改gdalbug.
# Usage:
#   .\scripts\local_docker_test.ps1
#   .\scripts\local_docker_test.ps1 -ApiKey "sk-..." -Port 7861
#   .\scripts\local_docker_test.ps1 -RebuildFrontend
#   .\scripts\local_docker_test.ps1 -StopOnly

param(
    [string]$ApiKey = $env:LLM_API_KEY,
    [int]$Port = 7860,
    [string]$Image = "geothermoai-teammate:test",
    [string]$Container = "geothermoai-teammate-test",
    [switch]$RebuildFrontend,
    [switch]$StopOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker 命令不可用。请先安装并启动 Docker Desktop，然后重新打开终端。"
    }
    docker info 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 引擎未就绪。请打开 Docker Desktop，等到状态变为 Running 后再试。"
    }
}

function Stop-TestContainer {
    docker rm -f $Container 2>$null | Out-Null
}

if ($StopOnly) {
    Assert-Docker
    Stop-TestContainer
    Write-Host "已停止并删除容器: $Container"
    exit 0
}

Assert-Docker

if ($RebuildFrontend -or -not (Test-Path (Join-Path $Root "dist\index.html"))) {
    Write-Host "==> 构建前端 dist ..."
    Push-Location (Join-Path $Root "frontend")
    npm install
    npm run build
    Pop-Location
    if (Test-Path (Join-Path $Root "dist")) {
        Remove-Item -Recurse -Force (Join-Path $Root "dist")
    }
    Copy-Item -Recurse (Join-Path $Root "frontend\dist") (Join-Path $Root "dist")
}

if (-not (Test-Path (Join-Path $Root "dist\index.html"))) {
    throw "缺少 dist/index.html。请先在 frontend 目录执行 npm run build，再复制到项目根 dist/"
}

Write-Host "==> 构建镜像 $Image ..."
docker build `
    --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/ `
    --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn `
    -t $Image .

Stop-TestContainer

$envArgs = @()
if ($ApiKey) {
    $envArgs += @("-e", "LLM_API_KEY=$ApiKey")
} else {
    Write-Host "提示: 未传入 -ApiKey / LLM_API_KEY，对话功能可能不可用，但页面应能打开。"
}

Write-Host "==> 启动容器 $Container （宿主机端口 $Port -> 容器 7860）..."
docker run -d --rm -p "${Port}:7860" --name $Container @envArgs $Image | Out-Null

Write-Host ""
Write-Host "打开浏览器: http://127.0.0.1:$Port"
Write-Host "看日志:     docker logs -f $Container"
Write-Host "停止测试:   .\scripts\local_docker_test.ps1 -StopOnly"
Write-Host ""
