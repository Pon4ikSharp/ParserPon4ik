$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Desktop = [Environment]::GetFolderPath('Desktop')
$Target = Join-Path $ProjectRoot 'start_panel.cmd'
$Icon = Join-Path $ProjectRoot 'assets\ponchyk.ico'
$Shell = New-Object -ComObject WScript.Shell

# Use an ASCII filename because old Windows PowerShell/WScript combinations
# may replace Unicode shortcut names with question marks.
$ShortcutName = 'Golden Donut'

# Remove only earlier shortcuts that point to this exact launcher, including
# the incorrectly encoded one created by the previous script version.
Get-ChildItem -LiteralPath $Desktop -Filter '*.lnk' -File | ForEach-Object {
    try {
        $Existing = $Shell.CreateShortcut($_.FullName)
        if ($Existing.TargetPath -and
            ([IO.Path]::GetFullPath($Existing.TargetPath) -ieq [IO.Path]::GetFullPath($Target))) {
            Remove-Item -LiteralPath $_.FullName -Force
        }
    } catch {
        # Ignore unrelated or unreadable shortcuts.
    }
}

$ShortcutPath = Join-Path $Desktop ($ShortcutName + '.lnk')
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = 'Golden Donut local control panel'
$Shortcut.IconLocation = $Icon + ',0'
$Shortcut.Save()
Write-Host ('Shortcut created: ' + $ShortcutPath)
