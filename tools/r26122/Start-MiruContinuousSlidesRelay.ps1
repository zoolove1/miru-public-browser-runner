[CmdletBinding()]
param(
    [ValidateRange(100, 5000)]
    [int]$TargetIntervalMilliseconds = 200,

    [ValidateRange(320, 1280)]
    [int]$ThumbnailWidth = 640,

    [ValidateRange(10, 90)]
    [int]$JpegQuality = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$stateDir = Join-Path $root 'control-agent-state'
$configPath = Join-Path $root 'slides-bridge-local.json'
$focusStatePath = Join-Path $stateDir 'focus-view-state.json'
$stopPath = Join-Path $stateDir 'continuous-slides-relay-stop.flag'
$statusPath = Join-Path $stateDir 'continuous-slides-relay-status.json'
$logPath = Join-Path $stateDir 'continuous-slides-relay.log'
$version = '0.9.9.3-r26122'
$mode = 'SLIDES_THUMBNAIL_LATEST_ONLY'

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
Remove-Item $stopPath -Force -ErrorAction SilentlyContinue

$mutex = [Threading.Mutex]::new($false, 'Local\MIRU_PC_CONTINUOUS_SLIDES_RELAY_R26122')
$lockTaken = $false
$http = $null
$successCount = 0
$failureCount = 0
$consecutiveFailures = 0
$overrunCount = 0
$startedAt = Get-Date
$lastStatusWrite = [datetime]::MinValue
$cycleTimes = New-Object 'System.Collections.Generic.Queue[double]'
$serverTimes = New-Object 'System.Collections.Generic.Queue[double]'
$stateStaleFallbackMs = 5000

function Write-RelayLog([string]$Message) {
    $line = (Get-Date).ToString('o') + ' ' + $Message
    Add-Content -Path $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Write-JsonAtomic([string]$Path, $Object) {
    $tmp = $Path + '.tmp.' + [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($tmp, ($Object | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try { Move-Item $tmp $Path -Force; return }
        catch {
            if ($attempt -eq 10) { throw }
            Start-Sleep -Milliseconds 10
        }
    }
}

function Get-Average([System.Collections.Generic.Queue[double]]$Values) {
    if ($Values.Count -eq 0) { return 0.0 }
    $sum = 0.0
    foreach ($value in $Values) { $sum += [double]$value }
    return $sum / $Values.Count
}

function New-Rect([int]$X, [int]$Y, [int]$RectWidth, [int]$RectHeight) {
    [pscustomobject][ordered]@{ x=$X; y=$Y; width=$RectWidth; height=$RectHeight }
}

function Initialize-Runtime {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Net.Http
    try {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MiruRelayDpiR26122 {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
}
'@ -ErrorAction SilentlyContinue
        [MiruRelayDpiR26122]::SetProcessDPIAware() | Out-Null
    } catch {}
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

function Get-VirtualBounds {
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    New-Rect -X $bounds.Left -Y $bounds.Top -RectWidth $bounds.Width -RectHeight $bounds.Height
}

function Clamp-Rect($Rect, $Bounds) {
    $width = [math]::Min([math]::Max(1,[int]$Rect.width), [int]$Bounds.width)
    $height = [math]::Min([math]::Max(1,[int]$Rect.height), [int]$Bounds.height)
    $x = [math]::Min([math]::Max([int]$Rect.x,[int]$Bounds.x), [int]$Bounds.x+[int]$Bounds.width-$width)
    $y = [math]::Min([math]::Max([int]$Rect.y,[int]$Bounds.y), [int]$Bounds.y+[int]$Bounds.height-$height)
    New-Rect -X $x -Y $y -RectWidth $width -RectHeight $height
}

function Get-CaptureView {
    $virtual = Get-VirtualBounds
    $fallback = [pscustomobject][ordered]@{
        view_mode='FALLBACK_FULL'; reason='focus state unavailable'; generation=0; rect=$virtual; focus_state_age_ms=-1
    }
    if (-not (Test-Path $focusStatePath)) { return $fallback }
    try {
        $state = Get-Content -Raw $focusStatePath | ConvertFrom-Json
        if (-not $state.PSObject.Properties['source_rect'] -or -not $state.source_rect) { return $fallback }
        $ageMs = -1
        $manualMode = if ($state.PSObject.Properties['manual_mode']) { ([string]$state.manual_mode).ToUpperInvariant() } else { '' }
        $viewMode = if ($state.PSObject.Properties['view_mode']) { ([string]$state.view_mode).ToUpperInvariant() } else { '' }
        $manualPersistent = ($manualMode -in @('REGION','FULL','CURSOR','ACTIVE') -or $viewMode.StartsWith('MANUAL_'))
        if ($state.PSObject.Properties['updated_at']) {
            $updated = [DateTimeOffset]::Parse([string]$state.updated_at)
            $ageMs = ([DateTimeOffset]::Now - $updated).TotalMilliseconds
            if (-not $manualPersistent -and $ageMs -gt $stateStaleFallbackMs) {
                $fallback.reason='automatic focus state stale'; $fallback.focus_state_age_ms=[math]::Round($ageMs,1); return $fallback
            }
        }
        $rect = Clamp-Rect -Rect (New-Rect -X ([int]$state.source_rect.x) -Y ([int]$state.source_rect.y) -RectWidth ([int]$state.source_rect.width) -RectHeight ([int]$state.source_rect.height)) -Bounds $virtual
        [pscustomobject][ordered]@{
            view_mode=if($state.PSObject.Properties['view_mode']){[string]$state.view_mode}else{'FOCUS'}
            reason=if($state.PSObject.Properties['reason']){[string]$state.reason}else{'focus state'}
            generation=if($state.PSObject.Properties['generation']){[int]$state.generation}else{0}
            rect=$rect
            focus_state_age_ms=[math]::Round($ageMs,1)
        }
    } catch { $fallback.reason='focus state read failed'; return $fallback }
}

function Capture-ThumbnailJpeg($View) {
    $rect = $View.rect
    $source = $null; $sourceGraphics = $null; $scaled = $null; $scaledGraphics = $null; $memory = $null; $parameters = $null
    try {
        $targetWidth = [math]::Min($ThumbnailWidth, [int]$rect.width)
        $targetHeight = [math]::Max(180, [int][math]::Round($targetWidth * [int]$rect.height / [int]$rect.width))
        $source = [Drawing.Bitmap]::new([int]$rect.width,[int]$rect.height,[Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $sourceGraphics = [Drawing.Graphics]::FromImage($source)
        $scaled = [Drawing.Bitmap]::new($targetWidth,$targetHeight,[Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $scaledGraphics = [Drawing.Graphics]::FromImage($scaled)
        $memory = [IO.MemoryStream]::new()
        $sourceGraphics.CopyFromScreen([int]$rect.x,[int]$rect.y,0,0,[Drawing.Size]::new([int]$rect.width,[int]$rect.height),[Drawing.CopyPixelOperation]::SourceCopy)
        $scaledGraphics.CompositingMode=[Drawing.Drawing2D.CompositingMode]::SourceCopy
        $scaledGraphics.CompositingQuality=[Drawing.Drawing2D.CompositingQuality]::HighSpeed
        $scaledGraphics.InterpolationMode=[Drawing.Drawing2D.InterpolationMode]::Low
        $scaledGraphics.PixelOffsetMode=[Drawing.Drawing2D.PixelOffsetMode]::HighSpeed
        $scaledGraphics.SmoothingMode=[Drawing.Drawing2D.SmoothingMode]::HighSpeed
        $scaledGraphics.DrawImage($source,0,0,$targetWidth,$targetHeight)
        $codec=[Drawing.Imaging.ImageCodecInfo]::GetImageEncoders()|Where-Object{$_.MimeType -eq 'image/jpeg'}|Select-Object -First 1
        if(-not $codec){throw 'JPEG encoder was not found.'}
        $parameters=[Drawing.Imaging.EncoderParameters]::new(1)
        $parameters.Param[0]=[Drawing.Imaging.EncoderParameter]::new([Drawing.Imaging.Encoder]::Quality,[int64]$JpegQuality)
        $scaled.Save($memory,$codec,$parameters)
        [pscustomobject][ordered]@{
            bytes=$memory.ToArray(); mime_type='image/jpeg'; source_width=[int]$rect.width; source_height=[int]$rect.height; width=$targetWidth; height=$targetHeight
        }
    } finally {
        if($parameters){try{$parameters.Dispose()}catch{}}
        if($memory){try{$memory.Dispose()}catch{}}
        if($scaledGraphics){try{$scaledGraphics.Dispose()}catch{}}
        if($scaled){try{$scaled.Dispose()}catch{}}
        if($sourceGraphics){try{$sourceGraphics.Dispose()}catch{}}
        if($source){try{$source.Dispose()}catch{}}
    }
}

function New-HttpRuntime {
    $handler=[Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect=$true; $handler.UseCookies=$false; $handler.MaxConnectionsPerServer=1
    $handler.AutomaticDecompression=[Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate
    $client=[Net.Http.HttpClient]::new($handler)
    $client.Timeout=[TimeSpan]::FromSeconds(20)
    $client.DefaultRequestHeaders.ExpectContinue=$false
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('MIRU-PC-Slides-Thumbnail-Relay/2.6.12.2')
    [pscustomobject]@{Client=$client;Handler=$handler}
}

function Send-Frame([Net.Http.HttpClient]$Client,[string]$Url,[string]$Secret,[byte[]]$Bytes,[string]$CapturedAt) {
    $payload=[ordered]@{key=$Secret;mime_type='image/jpeg';image_base64=[Convert]::ToBase64String($Bytes);captured_at=$CapturedAt}|ConvertTo-Json -Compress
    $content=[Net.Http.StringContent]::new($payload,[Text.Encoding]::UTF8,'application/json')
    $response=$null
    try {
        $response=$Client.PostAsync($Url,$content).GetAwaiter().GetResult()
        $text=$response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if(-not $response.IsSuccessStatusCode){throw ('HTTP '+[int]$response.StatusCode+': '+$text)}
        return ($text|ConvertFrom-Json)
    } finally { $content.Dispose(); if($response){$response.Dispose()} }
}

function Save-Status([string]$State,[string]$Message,[double]$LastCycleMs,[double]$LastServerMs,[int]$LastBytes,[int]$SourceWidth,[int]$SourceHeight,[int]$LastWidth,[int]$LastHeight,$View) {
    $elapsed=[math]::Max(0.001,((Get-Date)-$startedAt).TotalSeconds)
    $rect=if($View){$View.rect}else{$null}
    Write-JsonAtomic -Path $statusPath -Object ([ordered]@{
        version=$version;state=$State;mode=$mode;message=$Message
        channel_independent=$true;latest_frame_only=$true;backpressure_mode='SINGLE_IN_FLIGHT_NO_QUEUE'
        slide_payload='DOWNSCALED_JPEG_THUMBNAIL';original_frame_route='DRIVE_RAW_FRAME';original_resolution_available=$true
        source_width=$SourceWidth;source_height=$SourceHeight;thumbnail_width=$LastWidth;thumbnail_height=$LastHeight
        image_format='JPEG';mime_type='image/jpeg';jpeg_quality=$JpegQuality;target_interval_ms=$TargetIntervalMilliseconds
        maximum_target_fps=[math]::Round((1000.0/$TargetIntervalMilliseconds),3);overrun_count=$overrunCount
        view_mode=if($View){[string]$View.view_mode}else{''};view_generation=if($View){[int]$View.generation}else{0};view_reason=if($View){[string]$View.reason}else{''}
        source_rect=if($rect){[ordered]@{x=[int]$rect.x;y=[int]$rect.y;width=[int]$rect.width;height=[int]$rect.height}}else{$null}
        focus_state_age_ms=if($View){[double]$View.focus_state_age_ms}else{-1}
        success_count=$successCount;failure_count=$failureCount;consecutive_failures=$consecutiveFailures;elapsed_seconds=[math]::Round($elapsed,3)
        achieved_fps=[math]::Round(($successCount/$elapsed),3);last_cycle_ms=[math]::Round($LastCycleMs,1);rolling_average_cycle_ms=[math]::Round((Get-Average $cycleTimes),1)
        last_server_ms=[math]::Round($LastServerMs,1);rolling_average_server_ms=[math]::Round((Get-Average $serverTimes),1);last_frame_bytes=$LastBytes
        last_frame_at=(Get-Date).ToString('o');updated_at=(Get-Date).ToString('o')
    })
    $script:lastStatusWrite=Get-Date
}

try {
    try{$lockTaken=$mutex.WaitOne(0)} catch [System.Threading.AbandonedMutexException] {$lockTaken=$true}
    if(-not $lockTaken){Write-Host 'MIRU_CONTINUOUS_SLIDES_RELAY_ALREADY_RUNNING';exit 0}
    if(-not(Test-Path $configPath)){throw 'slides-bridge-local.json was not found.'}
    $config=Get-Content -Raw $configPath|ConvertFrom-Json
    $url=[string]$config.web_app_url; $secret=[string]$config.secret
    if([string]::IsNullOrWhiteSpace($url)){throw 'slides-bridge-local.json web_app_url is missing.'}
    if([string]::IsNullOrWhiteSpace($secret)){throw 'slides-bridge-local.json secret is missing.'}
    Initialize-Runtime
    $http=New-HttpRuntime
    $initialView=Get-CaptureView
    Save-Status -State 'STARTING' -Message 'Initializing latest-only Slides thumbnail relay.' -LastCycleMs 0 -LastServerMs 0 -LastBytes 0 -SourceWidth 0 -SourceHeight 0 -LastWidth 0 -LastHeight 0 -View $initialView
    Write-RelayLog ('RELAY_START mode='+$mode+' width='+$ThumbnailWidth+' jpeg_quality='+$JpegQuality+' target_interval_ms='+$TargetIntervalMilliseconds)
    while(-not(Test-Path $stopPath)){
        $cycle=[Diagnostics.Stopwatch]::StartNew();$serverMs=0.0;$byteCount=0;$sourceWidth=0;$sourceHeight=0;$frameWidth=0;$frameHeight=0;$view=$null
        try {
            $view=Get-CaptureView
            $frame=Capture-ThumbnailJpeg -View $view
            $bytes=[byte[]]$frame.bytes;$byteCount=$bytes.Length;$sourceWidth=[int]$frame.source_width;$sourceHeight=[int]$frame.source_height;$frameWidth=[int]$frame.width;$frameHeight=[int]$frame.height
            $response=Send-Frame -Client $http.Client -Url $url -Secret $secret -Bytes $bytes -CapturedAt ((Get-Date).ToString('o'))
            $serverMs=if($response.PSObject.Properties['elapsed_ms']){[double]$response.elapsed_ms}else{0.0}
            if($response.PSObject.Properties['status'] -and [string]$response.status -ne 'SLIDE_UPDATED'){throw ('Unexpected bridge status: '+[string]$response.status)}
            $cycle.Stop();$successCount++;$consecutiveFailures=0
            $cycleMs=[double]$cycle.Elapsed.TotalMilliseconds;if($cycleMs -gt $TargetIntervalMilliseconds){$overrunCount++}
            $cycleTimes.Enqueue($cycleMs);$serverTimes.Enqueue($serverMs);while($cycleTimes.Count-gt30){[void]$cycleTimes.Dequeue()};while($serverTimes.Count-gt30){[void]$serverTimes.Dequeue()}
            if($successCount-eq1 -or ((Get-Date)-$lastStatusWrite).TotalSeconds-ge1){
                Save-Status -State 'READY' -Message 'Slides thumbnail relay is running; original PNG remains on the Drive raw-frame plane.' -LastCycleMs $cycleMs -LastServerMs $serverMs -LastBytes $byteCount -SourceWidth $sourceWidth -SourceHeight $sourceHeight -LastWidth $frameWidth -LastHeight $frameHeight -View $view
            }
            $remainingMs=[int][math]::Ceiling($TargetIntervalMilliseconds-$cycle.Elapsed.TotalMilliseconds);if($remainingMs-gt0){Start-Sleep -Milliseconds $remainingMs}
        } catch {
            $cycle.Stop();$failureCount++;$consecutiveFailures++
            Write-RelayLog ('FRAME_FAILED count='+$failureCount+' consecutive='+$consecutiveFailures+' message='+$_.Exception.Message)
            Save-Status -State 'DEGRADED' -Message $_.Exception.Message -LastCycleMs $cycle.Elapsed.TotalMilliseconds -LastServerMs $serverMs -LastBytes $byteCount -SourceWidth $sourceWidth -SourceHeight $sourceHeight -LastWidth $frameWidth -LastHeight $frameHeight -View $view
            Start-Sleep -Milliseconds ([math]::Min(2000,250*$consecutiveFailures))
        }
    }
    Save-Status -State 'STOPPED' -Message 'Stop requested.' -LastCycleMs 0 -LastServerMs 0 -LastBytes 0 -SourceWidth 0 -SourceHeight 0 -LastWidth 0 -LastHeight 0 -View $null
    Write-RelayLog 'RELAY_STOP_REQUESTED'
} catch {
    try{Save-Status -State 'FAILED' -Message $_.Exception.Message -LastCycleMs 0 -LastServerMs 0 -LastBytes 0 -SourceWidth 0 -SourceHeight 0 -LastWidth 0 -LastHeight 0 -View $null}catch{}
    Write-RelayLog ('RELAY_FAILED message='+$_.Exception.Message);exit 1
} finally {
    if($http){try{$http.Client.Dispose()}catch{};try{$http.Handler.Dispose()}catch{}}
    Remove-Item $stopPath -Force -ErrorAction SilentlyContinue
    if($lockTaken){try{$mutex.ReleaseMutex()}catch{}}
    $mutex.Dispose();Write-RelayLog 'RELAY_STOPPED'
}
