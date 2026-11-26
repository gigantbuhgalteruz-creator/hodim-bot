# Gigant agent - avtomatik yangilanish (SYSTEM nomidan har soatda ishlaydi, install.ps1 o'rnatadi).
# Serverdagi gigant_agent.py yangi bo'lsa - faylni almashtiradi. Agent yangi versiyani o'zi sezadi va
# (blok bo'lmagan paytda) qayta ishga tushadi. Hodim bu faylni ham, vazifani ham o'zgartira olmaydi.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Dir  = Join-Path $env:ProgramFiles 'GigantAgent'
$conf = Get-Content (Join-Path $env:ProgramData 'GigantAgent\config.json') -Raw | ConvertFrom-Json

function Get-Server {
    # Tunnel manzili o'zgargan bo'lsa - serverning doimiy IP manzilidan (imzosi tekshiriladi)
    try {
        $d = Invoke-RestMethod "$($conf.disc)?_=$([DateTimeOffset]::Now.ToUnixTimeSeconds())" -TimeoutSec 20
        $h = New-Object System.Security.Cryptography.HMACSHA256 (, [Text.Encoding]::UTF8.GetBytes($conf.dkey))
        $sig = -join ($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($d.url)) | ForEach-Object { $_.ToString('x2') })
        if ($sig -eq $d.sig -and $d.url -match '^https://([a-z0-9-]+\.trycloudflare\.com|((panel|www)\.)?gigantbuxgalter\.com)$') { return $d.url }
    } catch {}
    if ($conf.server -match 'trycloudflare\.com|gigantbuxgalter\.com') { return 'https://gigantbuxgalter.com' }
    return $conf.server
}

$srv = Get-Server
$tmp = Join-Path $Dir 'gigant_agent.new'
Invoke-WebRequest "$srv/agent/gigant_agent.py" -OutFile $tmp -UseBasicParsing -TimeoutSec 120
$new = Get-Content $tmp -Raw -Encoding UTF8
if ($new.Length -lt 5000 -or $new -notmatch 'VERSION = "[\d.]+"' -or $new -notmatch 'def main\(\)') {
    Remove-Item $tmp -Force; exit
}
$cur = Join-Path $Dir 'gigant_agent.py'
if ((Test-Path $cur) -and (Get-FileHash $tmp).Hash -eq (Get-FileHash $cur).Hash) { Remove-Item $tmp -Force; exit }
Move-Item $tmp $cur -Force
