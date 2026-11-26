# Gigant Buxgalter — hodim kompyuteriga screen time agentini o'rnatish.
# PowerShell ni ADMINISTRATOR sifatida oching va:   irm <server>/agent/install.ps1 | iex
# (Server manzili va bot nomini bot server yuklab berishda o'zi qo'yadi.)
$ErrorActionPreference = 'Stop'
$Server = '__SERVER_URL__'
$Disc   = '__DISC_URL__'
$DiscK  = '__DISC_KEY__'
$Dir    = Join-Path $env:ProgramFiles 'GigantAgent'
$Exe    = Join-Path $Dir 'GigantAgent.exe'
$Conf   = Join-Path $env:ProgramData 'GigantAgent'
$Task   = 'GigantAgent'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    # O'zini administrator sifatida qayta ishga tushiradi (Windows "Ha / Да" deb so'raydi)
    Write-Host "Administrator ruxsati kerak — ochilgan oynada 'Да / Yes' ni bosing..." -ForegroundColor Yellow
    try {
        Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"irm '$Server/agent/install.ps1' | iex`""
    } catch {
        Write-Host "Ruxsat berilmadi. Пуск -> PowerShell -> o'ng tugma -> 'Запуск от имени администратора' orqali oching." -ForegroundColor Red
    }
    return
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 1. Hodim kaliti (hodim botga /key yozsa chiqadi) — tekshiramiz
while ($true) {
    $Key = (Read-Host "Hodim kalitini kiriting (botda /key)").Trim()
    if (-not $Key) { continue }
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Server/api/ping" -ContentType 'application/json' `
             -Body (@{ key = $Key } | ConvertTo-Json) -TimeoutSec 30
        Write-Host "Kalit to'g'ri: $($r.name)" -ForegroundColor Green
        break
    } catch {
        Write-Host "Kalit noto'g'ri yoki server javob bermadi: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# 2. Eski agentni to'xtatib, yangisini yuklab olamiz.
#    Agent — kichik Python skript; Python'ning rasmiy "embeddable" nusxasi (python.org) papka ichiga qo'yiladi,
#    kompyuterga alohida Python o'rnatilmaydi. pythonw.exe GigantAgent.exe nomi bilan nusxalanadi (Диспетчер задач da shu nom).
Get-Process GigantAgent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
New-Item -ItemType Directory -Force -Path $Dir, $Conf | Out-Null
if (-not (Test-Path (Join-Path $Dir 'python311.dll'))) {
    Write-Host "Python (python.org, ~11 MB) yuklab olinmoqda..."
    $zip = Join-Path $env:TEMP 'gigant-python.zip'
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip' -OutFile $zip -UseBasicParsing -TimeoutSec 600
    Expand-Archive -Path $zip -DestinationPath $Dir -Force
    Remove-Item $zip -Force
}
Copy-Item (Join-Path $Dir 'pythonw.exe') $Exe -Force
Write-Host "Agent yuklab olinmoqda..."
Invoke-WebRequest -Uri "$Server/agent/gigant_agent.py" -OutFile (Join-Path $Dir 'gigant_agent.py') -UseBasicParsing -TimeoutSec 120
if ((Get-Item (Join-Path $Dir 'gigant_agent.py')).Length -lt 5000) { throw "Agent fayli to'liq yuklanmadi" }

# 3. Sozlama: hodimlar o'qiy oladi, o'zgartira olmaydi
@{ key = $Key; server = $Server; disc = $Disc; dkey = $DiscK } | ConvertTo-Json | Set-Content -Path (Join-Path $Conf 'config.json') -Encoding UTF8
icacls $Conf /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null

# 4. Har bir foydalanuvchi kirganda ishga tushadi; yopib qo'yilsa 1 daqiqada qayta yonadi
$action  = New-ScheduledTaskAction -Execute $Exe -Argument "`"$Dir\gigant_agent.py`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
# Kirishdagi trigger takrorlanishi faqat keyingi kirishda boshlanadi — shuning uchun hozirdan boshlab
# har daqiqa tekshiradigan ikkinchi trigger ham (agent yopilib qolsa 1 daqiqada qayta yonadi).
$watch = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger = @($trigger, $watch)
$principal = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Gigant Buxgalter: ish vaqti hisobi (screen time)' | Out-Null

# 4b. Avtomatik yangilanish: SYSTEM nomidan har soatda serverdagi yangi versiyani tekshiradi
#     (keyingi o'zgarishlarda kompyuterlarga qayta o'rnatish shart emas)
Invoke-WebRequest -Uri "$Server/agent/update.ps1" -OutFile (Join-Path $Dir 'update.ps1') -UseBasicParsing -TimeoutSec 120
$uact = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\update.ps1`""
$utr1 = New-ScheduledTaskTrigger -AtStartup
$utr2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$upr  = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -RunLevel Highest
$uset = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName "$Task-Update" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "$Task-Update" -Action $uact -Trigger @($utr1, $utr2) -Principal $upr -Settings $uset `
    -Description 'Gigant Buxgalter: agentni avtomatik yangilash' | Out-Null

# 5. Hozir ishga tushiramiz
Start-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4
if (-not (Get-Process GigantAgent -ErrorAction SilentlyContinue)) {
    Start-Process $Exe -ArgumentList "`"$Dir\gigant_agent.py`"" -WorkingDirectory $Dir
}
Write-Host ""
Write-Host "Tayyor! Agent o'rnatildi va ishlayapti. 1 daqiqadan keyin Telegram botda /onlayn yozib tekshiring." -ForegroundColor Green
