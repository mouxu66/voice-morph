<#
.SYNOPSIS
  Minimal Windows desktop-control harness: screenshot / list windows / dump the
  UI Automation tree / click / type / send keys / focus a window.

.DESCRIPTION
  Everything is driven by UI Automation first and raw coordinates second, so the
  agent does not have to guess pixel positions. Results are written to UTF-8 JSON
  files (so non-ASCII window titles survive the console codepage) and only an
  ASCII-safe summary is printed to stdout.

  Source is deliberately pure ASCII: PowerShell 5.1 mis-parses UTF-8 files
  without a BOM, so non-ASCII literals would be a landmine in a file that is
  never run from an editor.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools/desktop-control/dc.ps1 probe
  powershell ... -File dc.ps1 windows
  powershell ... -File dc.ps1 shot
  powershell ... -File dc.ps1 tree -Title Notepad -Depth 3
  powershell ... -File dc.ps1 click -Title Notepad -Name "Save"
  powershell ... -File dc.ps1 type  -Name "Search" -Text "hello"
  powershell ... -File dc.ps1 keys  -Keys "^s"

  For non-ASCII arguments (Chinese window titles, CJK text) pass -ArgsFile with a
  UTF-8 JSON object instead of inline argv; the console codepage mangles those.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('probe', 'shot', 'windows', 'tree', 'click', 'type', 'keys', 'focus', 'sandbox', 'selftest')]
  [string]$Action,

  # UTF-8 JSON file of extra parameters (Unicode-safe alternative to argv).
  [string]$ArgsFile,

  [string]$Out,                          # screenshot/result directory (default: <script>\out)
  [string]$Result,                       # explicit JSON result path
  [string]$Title,                        # window title (substring, case-insensitive)
  [string]$Name,                         # element Name (exact, then substring)
  [string]$AutoId,                       # element AutomationId (exact)
  [string]$Type,                         # element ControlType, e.g. Edit / Button / Document
  [string]$Text,                         # text to type
  [string]$Keys,                         # SendKeys syntax, e.g. "^s"
  [int]$Depth = 4,                       # UIA tree depth
  [int]$Index = 0,                       # disambiguate multiple window/element matches
  [int]$X = [int]::MinValue,             # raw-coordinate fallback
  [int]$Y = [int]::MinValue,
  [int]$After = 350,                     # ms to settle after an action
  [int]$MaxNodes = 3000,                 # tree size cap
  [switch]$All,                          # tree: include off-screen nodes

  # shot: also emit a downscaled JPEG preview. The agent's file reader refuses
  # images over 768 KB, so a full-res 2560x1600 PNG can never be looked at.
  [switch]$Preview,
  [string]$PreviewPath,
  [int]$MaxWidth = 1280,
  [int]$Quality = 72
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------- args file ---
if ($ArgsFile) {
  $cfg = Get-Content -LiteralPath $ArgsFile -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($p in $cfg.PSObject.Properties) {
    if ($p.Name -eq 'Action') { $Action = [string]$p.Value }
    else { Set-Variable -Name $p.Name -Value $p.Value -Scope 0 }
  }
}

# ------------------------------------------------------------- assemblies -----
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

if (-not ('Win32.Native' -as [type])) {
  Add-Type -Namespace Win32 -Name Native -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, int data, uint extra);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
'@
}
# Without this the UIA rects and the captured pixels live in different coordinate
# spaces on scaled displays, and every click lands in the wrong place.
[void][Win32.Native]::SetProcessDPIAware()

$AE = [System.Windows.Automation.AutomationElement]
$TW = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$ScopeChildren = [System.Windows.Automation.TreeScope]::Children
$ScopeDesc = [System.Windows.Automation.TreeScope]::Descendants

$script:PatternMap = [ordered]@{
  Invoke         = [System.Windows.Automation.InvokePattern]::Pattern
  Value          = [System.Windows.Automation.ValuePattern]::Pattern
  Toggle         = [System.Windows.Automation.TogglePattern]::Pattern
  SelectionItem  = [System.Windows.Automation.SelectionItemPattern]::Pattern
  ExpandCollapse = [System.Windows.Automation.ExpandCollapsePattern]::Pattern
  ScrollItem     = [System.Windows.Automation.ScrollItemPattern]::Pattern
  RangeValue     = [System.Windows.Automation.RangeValuePattern]::Pattern
}

# ---------------------------------------------------------------- helpers -----
function Get-OutDir {
  if ($Out) { $d = $Out } else { $d = Join-Path $PSScriptRoot 'out' }
  if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
  return $d
}

function Save-Json($value, $kind) {
  $path = if ($Result) { $Result } else { Join-Path (Get-OutDir) "$kind.json" }
  $json = if ($null -eq $value) { 'null' } else { $value | ConvertTo-Json -Depth 12 }
  $utf8 = New-Object System.Text.UTF8Encoding($false)   # no BOM: friendlier to readers
  [IO.File]::WriteAllText($path, $json, $utf8)
  return $path
}

function Get-Patterns($el) {
  $found = @()
  foreach ($k in $script:PatternMap.Keys) {
    # The [ref] target must be a real variable: passing [ref]$null silently
    # reports "no patterns" for every element, which is a very confusing lie.
    $probe = $null
    try { if ($el.TryGetCurrentPattern($script:PatternMap[$k], [ref]$probe) -and $null -ne $probe) { $found += $k } } catch {}
  }
  return $found
}

function Get-Rect($el) {
  $r = $el.Current.BoundingRectangle
  return [pscustomobject]@{ x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height }
}

$script:ProcNames = @{}
try { foreach ($p in Get-Process) { $script:ProcNames[$p.Id] = $p.ProcessName } } catch {}

function Get-TopWindows {
  $list = @()
  foreach ($e in $AE::RootElement.FindAll($ScopeChildren, [System.Windows.Automation.Condition]::TrueCondition)) {
    try {
      $c = $e.Current
      $r = Get-Rect $e
      $name = ''
      if ($script:ProcNames.ContainsKey($c.ProcessId)) { $name = $script:ProcNames[$c.ProcessId] }
      $list += [pscustomobject]@{
        title     = $c.Name
        process   = $name
        pid       = $c.ProcessId
        cls       = $c.ClassName
        rect      = $r
        offscreen = $c.IsOffscreen
        hwnd      = ('0x{0:X}' -f $c.NativeWindowHandle)
        el        = $e
      }
    } catch {}
  }
  return $list
}

function Get-TargetWindow {
  if (-not $Title) {
    # No title given: use the window the user is currently looking at.
    $f = $AE::FocusedElement
    $w = $f
    while ($null -ne $w) {
      $p = $TW.GetParent($w)
      if ($null -eq $p -or $p -eq $AE::RootElement) { break }
      $w = $p
    }
    if ($null -ne $w -and $w -ne $AE::RootElement) { return $w }
    return $AE::RootElement
  }
  $all = @(Get-TopWindows)
  $hit = @($all | Where-Object { $_.title -like "*$Title*" -and -not $_.offscreen })
  if (-not $hit.Count) { $hit = @($all | Where-Object { $_.title -like "*$Title*" }) }
  if (-not $hit.Count) { throw "no window matching title '*$Title*'" }
  if ($Index -ge $hit.Count) { throw "window index $Index out of range ($($hit.Count) match(es))" }
  return $hit[$Index].el
}

function Get-Node($el, [int]$level, [int]$max) {
  if ($script:NodeCount -ge $MaxNodes) { return $null }
  $c = $el.Current
  if (-not $All -and $c.IsOffscreen) { return $null }
  $script:NodeCount++
  $r = Get-Rect $el
  $node = [ordered]@{
    type = ($c.ControlType.ProgrammaticName -replace '^ControlType\.', '')
    name = $c.Name
    id   = $c.AutomationId
    cls  = $c.ClassName
    rect = @($r.x, $r.y, $r.w, $r.h)
    pat  = (Get-Patterns $el)
    dis  = -not $c.IsEnabled
  }
  $kids = @()
  if ($level -lt $max) {
    $child = $TW.GetFirstChild($el)
    while ($null -ne $child) {
      $n = Get-Node $child ($level + 1) $max
      if ($null -ne $n) { $kids += $n }
      $child = $TW.GetNextSibling($child)
    }
  }
  if ($kids.Count) { $node.children = $kids }
  return [pscustomobject]$node
}

function Get-ControlType([string]$wanted) {
  switch ($wanted) {
    'Button'     { return [System.Windows.Automation.ControlType]::Button }
    'CheckBox'   { return [System.Windows.Automation.ControlType]::CheckBox }
    'ComboBox'   { return [System.Windows.Automation.ControlType]::ComboBox }
    'Document'   { return [System.Windows.Automation.ControlType]::Document }
    'Edit'       { return [System.Windows.Automation.ControlType]::Edit }
    'ListItem'   { return [System.Windows.Automation.ControlType]::ListItem }
    'MenuItem'   { return [System.Windows.Automation.ControlType]::MenuItem }
    'Pane'       { return [System.Windows.Automation.ControlType]::Pane }
    'RadioButton'{ return [System.Windows.Automation.ControlType]::RadioButton }
    'TabItem'    { return [System.Windows.Automation.ControlType]::TabItem }
    'Text'       { return [System.Windows.Automation.ControlType]::Text }
    'TreeItem'   { return [System.Windows.Automation.ControlType]::TreeItem }
    'Window'     { return [System.Windows.Automation.ControlType]::Window }
    default      { throw "unknown ControlType '$wanted'" }
  }
}

function Find-Element($scope) {
  if ($Type) {
    # Most real apps expose no usable names at all, so ControlType is often the
    # only handle available.
    $cond = New-Object System.Windows.Automation.PropertyCondition(
      [System.Windows.Automation.AutomationElement]::ControlTypeProperty, (Get-ControlType $Type))
    $hit = @($scope.FindAll($ScopeDesc, $cond))
    if (-not $hit.Count) { throw "no element of ControlType '$Type'" }
    if ($Index -ge $hit.Count) { throw "element index $Index out of range ($($hit.Count) match(es))" }
    return $hit[$Index]
  }
  if ($AutoId) {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $AutoId)
    $hit = @($scope.FindAll($ScopeDesc, $cond))
    if ($hit.Count) { return $hit[[Math]::Min($Index, $hit.Count - 1)] }
    throw "no element with AutomationId '$AutoId'"
  }
  if ($Name) {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $Name)
    $hit = @($scope.FindAll($ScopeDesc, $cond))
    if (-not $hit.Count) {   # fall back to substring, like Get-TargetWindow does
      $hit = @($scope.FindAll($ScopeDesc, [System.Windows.Automation.Condition]::TrueCondition) |
        Where-Object { $_ -and $_.Current.Name -like "*$Name*" })
    }
    if (-not $hit.Count) { throw "no element named '$Name'" }
    if ($Index -ge $hit.Count) { throw "element index $Index out of range ($($hit.Count) match(es))" }
    return $hit[$Index]
  }
  return $null
}

function Invoke-Click([int]$px, [int]$py) {
  [void][Win32.Native]::SetCursorPos($px, $py)
  Start-Sleep -Milliseconds 70
  [Win32.Native]::mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
  Start-Sleep -Milliseconds 45
  [Win32.Native]::mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
}

function Save-Preview($bmp, $path, [int]$maxWidth, [int]$quality) {
  $ratio = [Math]::Min(1.0, $maxWidth / $bmp.Width)
  $w = [int]($bmp.Width * $ratio)
  $h = [int]($bmp.Height * $ratio)
  $small = New-Object System.Drawing.Bitmap($w, $h)
  $g = [System.Drawing.Graphics]::FromImage($small)
  try {
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($bmp, 0, 0, $w, $h)
  } finally { $g.Dispose() }
  $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
    Where-Object { $_.MimeType -eq 'image/jpeg' }
  $ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
  $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter(
    [System.Drawing.Imaging.Encoder]::Quality, [int64]$quality)
  $small.Save($path, $codec, $ep)
  $small.Dispose()
  $ep.Dispose()
  return "$w x $h"
}

function Wait-Window($pattern, [int]$timeoutMs) {
  $deadline = (Get-Date).AddMilliseconds($timeoutMs)
  while ((Get-Date) -lt $deadline) {
    $hit = @(Get-TopWindows) | Where-Object { $_.title -like $pattern } | Select-Object -First 1
    if ($hit) { return $hit }
    Start-Sleep -Milliseconds 250
  }
  return $null
}

function Set-ClipboardText($value) {
  $saved = $null
  try { $saved = [System.Windows.Forms.Clipboard]::GetText() } catch {}
  [System.Windows.Forms.Clipboard]::SetText($value)
  return $saved
}

# ------------------------------------------------------------------ actions ---
switch ($Action) {

  # NOTE: never name a variable $out here - PowerShell variables are
  # case-insensitive, so $out IS the -Out parameter and the assignment silently
  # repoints the output directory (this bit me once already).
  'probe' {
    $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $payload = [ordered]@{
      userInteractive = [Environment]::UserInteractive
      powershell      = $PSVersionTable.PSVersion.ToString()
      virtualScreen   = [ordered]@{ x = $vs.X; y = $vs.Y; w = $vs.Width; h = $vs.Height }
      monitors        = [System.Windows.Forms.Screen]::AllScreens.Count
      uiaRoot         = ($null -ne $AE::RootElement)
      focused         = $AE::FocusedElement.Current.Name
      topWindows      = @(Get-TopWindows | Select-Object title, process, pid, rect, offscreen)
    }
    $p = Save-Json $payload 'probe'
    Write-Output "OK probe JSON=$p"
  }

  'shot' {
    $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap($vs.Width, $vs.Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try { $g.CopyFromScreen($vs.X, $vs.Y, 0, 0, $bmp.Size) } finally { $g.Dispose() }
    $file = if ($Result) { $Result } else {
      Join-Path (Get-OutDir) ('shot-{0}.png' -f (Get-Date -Format 'HHmmss-fff'))
    }
    $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output "OK shot PNG=$file size=$($vs.Width)x$($vs.Height)"
    if ($Preview) {
      $pv = if ($PreviewPath) { $PreviewPath } else { [IO.Path]::ChangeExtension($file, '.jpg') }
      $dim = Save-Preview $bmp $pv $MaxWidth $Quality
      Write-Output "OK shot PREVIEW=$pv ($dim)"
    }
    $bmp.Dispose()
  }

  'windows' {
    $list = @(Get-TopWindows | Select-Object title, process, pid, cls, rect, offscreen, hwnd)
    $p = Save-Json $list 'windows'
    Write-Output "OK windows count=$($list.Count) JSON=$p"
  }

  'tree' {
    $win = Get-TargetWindow
    $script:NodeCount = 0
    $tree = Get-Node $win 0 $Depth
    $payload = [ordered]@{
      window   = $win.Current.Name
      depth    = $Depth
      nodes    = $script:NodeCount
      truncated = ($script:NodeCount -ge $MaxNodes)
      root     = $tree
    }
    $p = Save-Json $payload 'tree'
    Write-Output "OK tree nodes=$($script:NodeCount) truncated=$($script:NodeCount -ge $MaxNodes) JSON=$p"
  }

  'focus' {
    $win = Get-TargetWindow
    $title = $win.Current.Name
    $ok = [Win32.Native]::SetForegroundWindow([IntPtr]$win.Current.NativeWindowHandle)
    Start-Sleep -Milliseconds $After
    $p = Save-Json ([ordered]@{ window = $title; foreground = [bool]$ok }) 'focus'
    Write-Output "OK focus foreground=$([bool]$ok) JSON=$p"
  }

  'click' {
    $win = Get-TargetWindow
    $el = Find-Element $win
    $mode = ''
    $r = $null
    # Snapshot the names now: invoking can destroy the element, and reading it
    # afterwards just yields null in the report.
    $winTitle = $win.Current.Name
    $targetName = $(if ($el) { $el.Current.Name } else { '' })
    if ($el) {
      $r = Get-Rect $el
      $invoked = $false
      foreach ($p in @('Invoke', 'Toggle', 'SelectionItem', 'ExpandCollapse')) {
        if ($script:PatternMap.Contains($p) -or $script:PatternMap.Keys -contains $p) {
          $pat = $null
          if ($el.TryGetCurrentPattern($script:PatternMap[$p], [ref]$pat) -and $null -ne $pat) {
            switch ($p) {
              'Invoke'         { $pat.Invoke(); $invoked = $true }
              'Toggle'         { $pat.Toggle(); $invoked = $true }
              'SelectionItem'  { $pat.Select(); $invoked = $true }
              'ExpandCollapse' { $pat.Expand(); $invoked = $true }
            }
            if ($invoked) { $mode = "uia:$p"; break }
          }
        }
      }
      if (-not $invoked) {
        Invoke-Click ([int]($r.x + $r.w / 2)) ([int]($r.y + $r.h / 2))
        $mode = 'mouse:center-of-element'
      }
    }
    elseif ($X -ne [int]::MinValue -and $Y -ne [int]::MinValue) {
      Invoke-Click $X $Y
      $mode = 'mouse:coordinates'
      $r = [pscustomobject]@{ x = $X; y = $Y; w = 0; h = 0 }
    }
    else {
      throw 'click needs -Name/-AutoId/-Type, or -X/-Y coordinates'
    }
    Start-Sleep -Milliseconds $After
    $p = Save-Json ([ordered]@{
      window = $winTitle
      target = $targetName
      mode   = $mode
      rect   = @($r.x, $r.y, $r.w, $r.h)
    }) 'click'
    Write-Output "OK click mode=$mode JSON=$p"
  }

  'type' {
    $win = Get-TargetWindow
    $el = Find-Element $win
    if (-not $el) { throw 'type needs -Name/-AutoId/-Type of the target field' }
    $how = ''
    $pat = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat) -and $null -ne $pat -and -not $pat.Current.IsReadOnly) {
      $pat.SetValue($Text)      # Unicode-safe, no keystroke simulation
      $how = 'uia:ValuePattern'
    }
    else {
      $saved = Set-ClipboardText $Text
      try {
        $el.SetFocus()
        Start-Sleep -Milliseconds 120
        [System.Windows.Forms.SendKeys]::SendWait('^a')
        Start-Sleep -Milliseconds 60
        [System.Windows.Forms.SendKeys]::SendWait('^v')
        $how = 'clipboard:paste'
      }
      finally {
        Start-Sleep -Milliseconds 250
        if ($null -ne $saved) { try { [System.Windows.Forms.Clipboard]::SetText($saved) } catch {} }
        else { try { [System.Windows.Forms.Clipboard]::Clear() } catch {} }
      }
    }
    Start-Sleep -Milliseconds $After
    $p = Save-Json ([ordered]@{ window = $win.Current.Name; target = $el.Current.Name; mode = $how }) 'type'
    Write-Output "OK type mode=$how JSON=$p"
  }

  'keys' {
    if (-not $Keys) { throw 'keys needs -Keys (SendKeys syntax, e.g. "^s", "%{F4}")' }
    $win = $null
    if ($Title) {
      $win = Get-TargetWindow
      [void][Win32.Native]::SetForegroundWindow([IntPtr]$win.Current.NativeWindowHandle)
      Start-Sleep -Milliseconds 250
    }
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
    Start-Sleep -Milliseconds $After
    $p = Save-Json ([ordered]@{ keys = $Keys; window = $(if ($win) { $win.Current.Name } else { '(focused)' }) }) 'keys'
    Write-Output "OK keys JSON=$p"
  }

  # A throwaway WPF window with a text box and a Close button. It exists so that
  # click/type/keys can be proven end-to-end without depending on whatever apps
  # happen to be installed - and without touching the user's real windows.
  #
  # WPF on purpose: it publishes first-class UI Automation providers (real
  # ControlTypes, ValuePattern, AutomationId), whereas a WinForms control shows
  # up as a generic unnamed 'Pane' with no patterns at all, which makes it
  # useless as a test target.
  'sandbox' {
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
    $xamlText = '<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" ' +
      'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" ' +
      'Title="DC Sandbox" Width="520" Height="250" WindowStartupLocation="CenterScreen">' +
      '<StackPanel Margin="16">' +
      '<TextBox x:Name="sandboxInput" AutomationProperties.AutomationId="sandboxInput" ' +
      'Height="140" AcceptsReturn="True" TextWrapping="Wrap" VerticalScrollBarVisibility="Auto" />' +
      '<Button x:Name="sandboxClose" AutomationProperties.AutomationId="sandboxClose" ' +
      'Content="Close" Width="120" Height="36" Margin="0,16,0,0" HorizontalAlignment="Left" />' +
      '</StackPanel></Window>'
    $win = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader ([xml]$xamlText)))
    # XamlReader cannot wire Click handlers, so attach it after the load.
    $win.FindName('sandboxClose').Add_Click({ $win.Close() })
    $null = $win.ShowDialog()
    Write-Output 'OK sandbox closed'
  }

  # End-to-end proof that input control works: spawn the sandbox, read it back
  # through UI Automation, write text, click its Close button, confirm it left.
  # Exits non-zero on failure so it can gate a commit.
  'selftest' {
    $checks = [ordered]@{}
    $child = $null
    $psExe = (Get-Command powershell.exe).Source

    # Deliberately drives the public actions as child processes rather than
    # calling the helpers directly, so this test covers the real command line.
    function Invoke-DcAction([string[]]$cliArgs) {
      $saved = $ErrorActionPreference
      $ErrorActionPreference = 'Continue'
      try { $text = (& $psExe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @cliArgs 2>&1) -join ' ' }
      finally { $ErrorActionPreference = $saved }
      return [pscustomobject]@{ code = $LASTEXITCODE; text = $text }
    }

    try {
      $child = Start-Process -FilePath $psExe -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Action', 'sandbox')
      $checks['spawned'] = ('pid {0}' -f $child.Id)

      $win = Wait-Window '*DC Sandbox*' 20000
      $checks['window_visible'] = [bool]$win
      if (-not $win) { throw 'sandbox window never appeared' }
      $winEl = $win.el

      $editCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
      $editEl = $winEl.FindFirst($ScopeDesc, $editCond)
      $checks['edit_found'] = [bool]$editEl
      if (-not $editEl) { throw 'sandbox text box not reachable via UIA' }
      $checks['edit_patterns'] = (Get-Patterns $editEl) -join ','

      # 1) type through the public action, then read the text back off the control
      $stamp = 'dc-selftest ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
      $r = Invoke-DcAction @('-Action', 'type', '-Title', 'DC Sandbox', '-Type', 'Edit', '-Text', $stamp)
      $checks['type_exit'] = $r.code
      Start-Sleep -Milliseconds 500
      $vp = $null
      if ($editEl.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
        $landed = $vp.Current.Value
        $checks['type_seen_via'] = 'ValuePattern'
      }
      else {
        $landed = $editEl.Current.Name
        $checks['type_seen_via'] = 'Name'
      }
      $checks['type_landed'] = ($landed -eq $stamp)
      $checks['type_read_back'] = $landed
      if ($landed -ne $stamp) { throw "typed text did not land (read back '$landed'; dc said: $($r.text))" }

      # 2) click through the public action and confirm the window actually went away
      $r = Invoke-DcAction @('-Action', 'click', '-Title', 'DC Sandbox', '-AutoId', 'sandboxClose')
      $checks['click_exit'] = $r.code
      $gone = $null -eq (Wait-Window '*DC Sandbox*' 8000)
      $checks['window_closed'] = $gone
      if (-not $gone) { throw "sandbox window did not close (dc said: $($r.text))" }

      $checks['result'] = 'PASS'
    }
    catch {
      $checks['result'] = 'FAIL'
      $checks['error'] = $_.Exception.Message
    }
    finally {
      if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
    }
    $p = Save-Json $checks 'selftest'
    Write-Output ("{0} selftest JSON={1}" -f $(if ($checks['result'] -eq 'PASS') { 'OK' } else { 'FAILED' }), $p)
    if ($checks['result'] -ne 'PASS') { exit 1 }
    exit 0
  }
}
