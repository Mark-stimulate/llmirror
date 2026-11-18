param([ValidateSet('install','start','stop','uninstall','status')][string]$Action='status')
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskService = Join-Path $taskRoot 'llmirror-service.exe'
if ($Action -eq 'status') {
    & $taskService status
    exit $LASTEXITCODE
}
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    # Hidden helper process; only the normal UAC permission prompt may be shown.
    $taskScript = $PSCommandPath.Replace("'", "''")
    $taskCommand = "& '$taskScript' -Action '$Action'"
    $taskEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($taskCommand))
    $taskChild = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$taskEncoded) -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    exit $taskChild.ExitCode
}
try {
    if ($Action -eq 'install') {
        if (Get-Service -Name 'WingBench' -ErrorAction SilentlyContinue) {
            throw '检测到旧版服务，请先使用旧版服务管理脚本卸载，再安装 llmirror 服务。'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'data\llmirror.db'))) {
            throw '请先双击启动并完成管理员初始化，再安装后台服务。'
        }
        if (Get-Service -Name 'llmirror' -ErrorAction SilentlyContinue) {
            throw 'llmirror 服务已存在；请使用启动服务，或先卸载旧服务。'
        }
        # The service receives read/execute rights on the bundle and write rights only on data.
        & icacls.exe $taskRoot /grant '*S-1-5-19:(OI)(CI)RX' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '无法授予服务读取程序目录的权限。' }
        & icacls.exe (Join-Path $taskRoot 'data') /grant '*S-1-5-19:(OI)(CI)M' /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '无法授予服务写入数据目录的权限。' }
        & $taskService install
        if ($LASTEXITCODE -ne 0) { throw '服务安装失败；请检查 data/logs 下的日志。' }
        # Verify the effective account instead of relying on version-specific XML interpretation.
        $taskInstalled = Get-CimInstance Win32_Service -Filter "Name='llmirror'"
        if ($taskInstalled.StartName -notmatch '(?i)LocalService|S-1-5-19') {
            & $taskService uninstall | Out-Null
            throw '服务运行账号未被正确设置为 LocalService，已撤回安装。'
        }
        & $taskService start
    } elseif ($Action -eq 'uninstall') {
        $taskExisting = Get-Service -Name 'llmirror' -ErrorAction SilentlyContinue
        if ($taskExisting -and $taskExisting.Status -ne 'Stopped') {
            & $taskService stop
            if ($LASTEXITCODE -ne 0) { throw '服务无法停止，未继续卸载。' }
        }
        & $taskService uninstall
    } else {
        & $taskService $Action
    }
    if ($LASTEXITCODE -ne 0) { throw '服务操作失败；请查看 data/logs 下的日志。' }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show("操作完成：$Action。数据文件保留在原目录。",'词镜服务管理') | Out-Null
    exit 0
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message,'词镜服务管理') | Out-Null
    exit 1
}
