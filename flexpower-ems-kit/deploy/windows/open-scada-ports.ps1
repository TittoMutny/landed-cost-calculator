# Allow only the SCADA / PCS 7 station(s) through Windows Firewall to the EMS
# OPC UA and/or Modbus TCP server.
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\open-scada-ports.ps1 -ScadaIp 192.168.10.20
#   (several stations: -ScadaIp 192.168.10.20,192.168.10.21)
# Re-running replaces the two Flexpower rules with the new settings.
param(
    [Parameter(Mandatory = $true)][string[]]$ScadaIp,
    [int]$OpcUaPort = 4840,
    [int]$ModbusPort = 502,   # also used by the PROFINET gateway plugin
    [switch]$NoOpcUa,
    [switch]$NoModbus
)
$ErrorActionPreference = "Stop"
$rules = @(
    @{ Name = "Flexpower EMS - OPC UA"; Port = $OpcUaPort; Skip = $NoOpcUa },
    @{ Name = "Flexpower EMS - Modbus TCP"; Port = $ModbusPort; Skip = $NoModbus }
)
foreach ($rule in $rules) {
    # Replace our own rule of the same name if it already exists.
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) { $existing | Remove-NetFirewallRule }
    if ($rule.Skip) { continue }
    New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $rule.Port -RemoteAddress $ScadaIp | Out-Null
    Write-Host "Allowed TCP $($rule.Port) from $($ScadaIp -join ', ') ($($rule.Name))"
}
