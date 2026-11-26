# Gigant agentni o'chirish (PowerShell ADMINISTRATOR):   irm <server>/agent/uninstall.ps1 | iex
Unregister-ScheduledTask -TaskName 'GigantAgent' -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'GigantAgent-Update' -Confirm:$false -ErrorAction SilentlyContinue
Get-Process GigantAgent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Remove-Item -Recurse -Force (Join-Path $env:ProgramFiles 'GigantAgent'), (Join-Path $env:ProgramData 'GigantAgent') -ErrorAction SilentlyContinue
Write-Host "Agent o'chirildi." -ForegroundColor Green
