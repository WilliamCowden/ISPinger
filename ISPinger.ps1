#Requires -Version 5.1
<#
.SYNOPSIS
    ISPinger - a tiny system-tray app that watches IPs and emails you when they respond.

.DESCRIPTION
    Each enabled ping entry is pinged every 30 seconds. The first time it replies,
    an email is sent to that entry's address and the entry disables itself.
    Re-enable it any time to start watching again.

    Data (entries, contacts, settings) is stored in %APPDATA%\ISPinger\data.json.
    The SMTP password is encrypted with Windows DPAPI (only your Windows account can read it).

.NOTES
    Run via Launch-ISPinger.vbs to start without a console window.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12

# ============================================================== CONFIG
$script:PingIntervalSec = 30      # seconds between pings for each entry
$script:PingTimeoutMs   = 2000    # how long to wait for a single reply
$script:DataDir         = Join-Path $env:APPDATA 'ISPinger'
$script:DataFile        = Join-Path $script:DataDir 'data.json'

# ============================================================== SINGLE INSTANCE
$createdNew = $false
$script:Mutex = New-Object System.Threading.Mutex($true, 'Local\ISPinger_SingleInstance', [ref]$createdNew)
if (-not $createdNew) {
    [void][System.Windows.Forms.MessageBox]::Show('ISPinger is already running - look for it in the system tray.', 'ISPinger')
    exit
}

# ============================================================== DATA
$script:Entries  = New-Object System.Collections.ArrayList
$script:Contacts = New-Object System.Collections.ArrayList
$script:Settings = [pscustomobject]@{
    SmtpHost       = 'smtp.gmail.com'
    SmtpPort       = 587
    UseSsl         = $true
    Username       = ''
    PasswordEnc    = ''
    FromAddress    = ''
    StartMinimized = $false
}
$script:Runtime = @{}   # per-entry, not saved: next ping time, in-flight ping/email tasks

function New-EntryObject($name, $target, $email, $enabled) {
    [pscustomobject]@{
        Id          = [guid]::NewGuid().ToString()
        Name        = $name
        Target      = $target
        Email       = $email
        Enabled     = [bool]$enabled
        Status      = 'Idle'
        LastChecked = ''
    }
}

function Import-Data {
    if (-not (Test-Path $script:DataFile)) { return }
    try {
        $raw = Get-Content -Path $script:DataFile -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show("Could not read $($script:DataFile)`n`n$($_.Exception.Message)", 'ISPinger')
        return
    }
    foreach ($e in @($raw.Entries)) {
        if ($null -eq $e) { continue }
        [void]$script:Entries.Add([pscustomobject]@{
            Id          = if ($e.Id) { [string]$e.Id } else { [guid]::NewGuid().ToString() }
            Name        = [string]$e.Name
            Target      = [string]$e.Target
            Email       = [string]$e.Email
            Enabled     = [bool]$e.Enabled
            Status      = if ($e.Enabled) { 'Pinging...' } else { [string]$e.Status }
            LastChecked = [string]$e.LastChecked
        })
    }
    foreach ($c in @($raw.Contacts)) {
        if ($null -eq $c) { continue }
        [void]$script:Contacts.Add([pscustomobject]@{ Name = [string]$c.Name; Email = [string]$c.Email })
    }
    if ($raw.Settings) {
        foreach ($p in @($script:Settings.PSObject.Properties.Name)) {
            if ($null -ne $raw.Settings.$p) { $script:Settings.$p = $raw.Settings.$p }
        }
    }
}

function Save-Data {
    try {
        if (-not (Test-Path $script:DataDir)) { New-Item -ItemType Directory -Path $script:DataDir -Force | Out-Null }
        $obj = [ordered]@{
            Entries  = @($script:Entries)
            Contacts = @($script:Contacts)
            Settings = $script:Settings
        }
        ConvertTo-Json -InputObject $obj -Depth 5 | Set-Content -Path $script:DataFile -Encoding UTF8 -ErrorAction Stop
    } catch {
        Show-Balloon 'Save failed' $_.Exception.Message 'Error'
    }
}

function Get-EntryIndex($id) {
    for ($i = 0; $i -lt $script:Entries.Count; $i++) { if ($script:Entries[$i].Id -eq $id) { return $i } }
    return -1
}

function Get-Rt($id) {
    if (-not $script:Runtime.ContainsKey($id)) {
        $script:Runtime[$id] = @{ NextPing = [datetime]::MinValue; Ping = $null; PingTask = $null
                                  MailClient = $null; MailMsg = $null; MailTask = $null; ReachedAt = '' }
    }
    return $script:Runtime[$id]
}

# ============================================================== HELPERS
function Format-Contact($c) { "$($c.Name) <$($c.Email)>" }

function Resolve-Email([string]$text) {
    $t = "$text".Trim()
    $m = [regex]::Match($t, '<([^>]+)>')
    if ($m.Success) { $t = $m.Groups[1].Value.Trim() }
    return $t
}

function Test-Email([string]$addr) { return ($addr -match '^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$') }

function Get-ErrorText($ex) {
    # Pull a readable message out of a (possibly aggregate) exception
    if ($ex -is [System.AggregateException] -and $ex.InnerException) { $ex = $ex.InnerException }
    $msg = $ex.Message
    if ($ex.InnerException -and $ex.InnerException.Message -ne $msg) { $msg += " ($($ex.InnerException.Message))" }
    return $msg
}

function Get-SmtpPassword {
    if ([string]::IsNullOrEmpty($script:Settings.PasswordEnc)) { return '' }
    try {
        $sec = ConvertTo-SecureString $script:Settings.PasswordEnc -ErrorAction Stop
        return (New-Object System.Net.NetworkCredential('', $sec)).Password
    } catch { return '' }
}

function New-SmtpMessage($to, $subject, $body) {
    $s = $script:Settings
    if ([string]::IsNullOrWhiteSpace($s.SmtpHost) -or [string]::IsNullOrWhiteSpace($s.FromAddress)) {
        throw 'Email is not set up yet (see the Settings tab).'
    }
    $msg = New-Object System.Net.Mail.MailMessage
    $msg.From = New-Object System.Net.Mail.MailAddress($s.FromAddress, 'ISPinger')
    $msg.To.Add($to)
    $msg.Subject = $subject
    $msg.Body    = $body
    $client = New-Object System.Net.Mail.SmtpClient($s.SmtpHost, [int]$s.SmtpPort)
    $client.EnableSsl = [bool]$s.UseSsl
    $client.Timeout   = 20000
    if ($s.Username) { $client.Credentials = New-Object System.Net.NetworkCredential($s.Username, (Get-SmtpPassword)) }
    return @{ Client = $client; Message = $msg }
}

function Show-Balloon($title, $text, $icon = 'Info') {
    if ($script:Tray -and $script:Tray.Visible) {
        $script:Tray.ShowBalloonTip(5000, $title, $text, [System.Windows.Forms.ToolTipIcon]::$icon)
    }
}

function Update-TrayText {
    $n = @($script:Entries | Where-Object { $_.Enabled }).Count
    $script:Tray.Text = "ISPinger - $n watching"
}

function New-AppIcon {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(30, 136, 229))), 1, 1, 30, 30)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 2.6
    $g.DrawArc($pen, 4, 9, 24, 24, 225, 90)
    $g.DrawArc($pen, 9, 14, 14, 14, 225, 90)
    $g.FillEllipse([System.Drawing.Brushes]::White, 13.5, 18.5, 5, 5)
    $g.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

function Add-Label($parent, $text, $x, $y, $color = $null) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text
    $l.AutoSize = $true
    $l.Location = New-Object System.Drawing.Point($x, $y)
    if ($color) { $l.ForeColor = $color }
    $parent.Controls.Add($l)
}

function Add-TextBox($parent, $x, $y, $w) {
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location = New-Object System.Drawing.Point($x, $y)
    $t.Width = $w
    $parent.Controls.Add($t)
    return $t
}

function New-Button($text, $width = 95) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Width = $width
    $b.Height = 28
    return $b
}

function New-ButtonBar {
    $p = New-Object System.Windows.Forms.FlowLayoutPanel
    $p.Dock = 'Bottom'
    $p.Height = 42
    $p.Padding = New-Object System.Windows.Forms.Padding(4, 6, 4, 4)
    return $p
}

function New-ListView($columns) {
    $lv = New-Object System.Windows.Forms.ListView
    $lv.View = 'Details'
    $lv.FullRowSelect = $true
    $lv.MultiSelect = $false
    $lv.HideSelection = $false
    $lv.GridLines = $true
    $lv.Dock = 'Fill'
    foreach ($c in $columns) { [void]$lv.Columns.Add($c[0], $c[1]) }
    return $lv
}

function New-Dialog($title, $w, $h) {
    $d = New-Object System.Windows.Forms.Form
    $d.Text = $title
    $d.FormBorderStyle = 'FixedDialog'
    $d.MaximizeBox = $false
    $d.MinimizeBox = $false
    $d.ShowInTaskbar = $false
    $d.StartPosition = 'CenterParent'
    $d.Font = $script:UiFont
    $d.Icon = $script:AppIcon
    $d.ClientSize = New-Object System.Drawing.Size($w, $h)
    return $d
}

# ============================================================== PING ENGINE
function Complete-Entry($e, $rt, $detail, $now) {
    # Target replied: disable the entry and send the email
    $e.Enabled = $false
    $rt.ReachedAt = $now.ToString('g')
    $subject = "ISPinger: $($e.Name) ($($e.Target)) is responding"
    $body = @"
The IP address for "$($e.Name)" ($($e.Target)) successfully pinged at $($now.ToString('F')).

$detail

This ping entry has now been disabled. Re-enable it in ISPinger to watch it again.

- Sent by ISPinger on $env:COMPUTERNAME
"@
    try {
        $m = New-SmtpMessage $e.Email $subject $body
        $rt.MailClient = $m.Client
        $rt.MailMsg    = $m.Message
        $rt.MailTask   = $m.Client.SendMailAsync($m.Message)
        $e.Status = "Reached $($rt.ReachedAt) - sending email..."
    } catch {
        $e.Status = "Reached $($rt.ReachedAt) - EMAIL FAILED: $(Get-ErrorText $_.Exception)"
    }
    Show-Balloon 'Host is up' "$($e.Name) ($($e.Target)) replied to ping."
    Save-Data
    Update-TrayText
}

function Invoke-Tick {
    $now = Get-Date
    foreach ($e in @($script:Entries)) {
        $rt = Get-Rt $e.Id
        $changed = $false

        # 1) A ping finished?
        if ($rt.PingTask -and $rt.PingTask.IsCompleted) {
            $ok = $false
            if ($rt.PingTask.IsFaulted) {
                $detail = "Error: $(Get-ErrorText $rt.PingTask.Exception)"
            } else {
                $reply = $rt.PingTask.Result
                if ($reply.Status -eq [System.Net.NetworkInformation.IPStatus]::Success) {
                    $ok = $true
                    $detail = "Reply from $($reply.Address) in $($reply.RoundtripTime) ms."
                } else {
                    $detail = "No reply ($($reply.Status))"
                }
            }
            $rt.Ping.Dispose()
            $rt.Ping = $null
            $rt.PingTask = $null

            if ($e.Enabled) {      # ignore results for entries disabled mid-ping
                $e.LastChecked = $now.ToString('g')
                if ($ok) { Complete-Entry $e $rt $detail $now }
                else     { $e.Status = "$detail - retrying every $($script:PingIntervalSec)s" }
                $changed = $true
            }
        }

        # 2) Time to send a new ping?
        if ($e.Enabled -and -not $rt.PingTask -and $now -ge $rt.NextPing) {
            $rt.NextPing = $now.AddSeconds($script:PingIntervalSec)
            try {
                $rt.Ping = New-Object System.Net.NetworkInformation.Ping
                $rt.PingTask = $rt.Ping.SendPingAsync($e.Target, $script:PingTimeoutMs)
            } catch {
                if ($rt.Ping) { $rt.Ping.Dispose(); $rt.Ping = $null }
                $rt.PingTask = $null
                $e.Status = "Error: $(Get-ErrorText $_.Exception)"
                $e.LastChecked = $now.ToString('g')
                $changed = $true
            }
        }

        # 3) An email finished sending?
        if ($rt.MailTask -and $rt.MailTask.IsCompleted) {
            if ($rt.MailTask.IsFaulted) {
                $err = Get-ErrorText $rt.MailTask.Exception
                $e.Status = "Reached $($rt.ReachedAt) - EMAIL FAILED: $err"
                Show-Balloon 'Email failed' "$($e.Name): $err" 'Error'
            } else {
                $e.Status = "Reached $($rt.ReachedAt) - email sent to $($e.Email)"
            }
            $rt.MailMsg.Dispose(); $rt.MailClient.Dispose()
            $rt.MailMsg = $null; $rt.MailClient = $null; $rt.MailTask = $null
            Save-Data
            $changed = $true
        }

        if ($changed) { Update-EntryRow $e }
    }
}

# ============================================================== ENTRY LIST UI
function Set-RowValues($item, $e) {
    $item.SubItems[0].Text = if ($e.Enabled) { 'Yes' } else { 'No' }
    $item.SubItems[1].Text = $e.Name
    $item.SubItems[2].Text = $e.Target
    $item.SubItems[3].Text = $e.Email
    $item.SubItems[4].Text = $e.Status
    $item.SubItems[5].Text = $e.LastChecked
    $color = if ($e.Enabled) { [System.Drawing.SystemColors]::WindowText } else { [System.Drawing.Color]::Gray }
    if     ($e.Status -like '*FAILED*' -or $e.Status -like 'Error*') { $color = [System.Drawing.Color]::Firebrick }
    elseif ($e.Status -like 'Reached*email sent*')                   { $color = [System.Drawing.Color]::ForestGreen }
    $item.ForeColor = $color
}

function Update-EntryList {
    $selId = if ($script:lvEntries.SelectedItems.Count) { $script:lvEntries.SelectedItems[0].Tag } else { $null }
    $script:lvEntries.BeginUpdate()
    $script:lvEntries.Items.Clear()
    foreach ($e in $script:Entries) {
        $item = New-Object System.Windows.Forms.ListViewItem('')
        for ($i = 0; $i -lt 5; $i++) { [void]$item.SubItems.Add('') }
        $item.Tag = $e.Id
        Set-RowValues $item $e
        [void]$script:lvEntries.Items.Add($item)
        if ($e.Id -eq $selId) { $item.Selected = $true }
    }
    $script:lvEntries.EndUpdate()
    Update-ToggleButton
    Update-TrayText
}

function Update-EntryRow($e) {
    foreach ($item in $script:lvEntries.Items) {
        if ($item.Tag -eq $e.Id) { Set-RowValues $item $e; break }
    }
    Update-ToggleButton
}

function Get-SelectedEntry {
    if ($script:lvEntries.SelectedItems.Count -eq 0) { return $null }
    $i = Get-EntryIndex $script:lvEntries.SelectedItems[0].Tag
    if ($i -ge 0) { return $script:Entries[$i] }
    return $null
}

function Update-ToggleButton {
    $e = Get-SelectedEntry
    $script:btnToggle.Enabled = [bool]$e
    $script:btnEdit.Enabled   = [bool]$e
    $script:btnRemove.Enabled = [bool]$e
    $script:btnToggle.Text = if ($e -and $e.Enabled) { 'Disable' } else { 'Enable' }
}

function Set-EntryEnabled($e, [bool]$on) {
    $e.Enabled = $on
    $rt = Get-Rt $e.Id
    if ($on) { $rt.NextPing = [datetime]::MinValue; $e.Status = 'Pinging...' }
    else     { $e.Status = 'Disabled' }
    Save-Data
    Update-EntryRow $e
    Update-TrayText
}

function Show-EntryDialog($entry) {
    $title = if ($entry) { 'Edit Ping Entry' } else { 'New Ping Entry' }
    $dlg = New-Dialog $title 450 210

    Add-Label $dlg 'Name:' 14 18
    $tbName = Add-TextBox $dlg 125 15 305
    Add-Label $dlg 'IP / hostname:' 14 53
    $tbIp = Add-TextBox $dlg 125 50 305
    Add-Label $dlg 'Email to notify:' 14 88

    $cbEmail = New-Object System.Windows.Forms.ComboBox
    $cbEmail.DropDownStyle = 'DropDown'
    $cbEmail.Location = New-Object System.Drawing.Point(125, 85)
    $cbEmail.Width = 305
    foreach ($c in $script:Contacts) { [void]$cbEmail.Items.Add((Format-Contact $c)) }
    $dlg.Controls.Add($cbEmail)
    Add-Label $dlg 'Pick a contact or type any email address.' 125 112 ([System.Drawing.Color]::Gray)

    $chk = New-Object System.Windows.Forms.CheckBox
    $chk.Text = 'Enabled (start pinging when saved)'
    $chk.AutoSize = $true
    $chk.Location = New-Object System.Drawing.Point(125, 138)
    $chk.Checked = $true
    $dlg.Controls.Add($chk)

    if ($entry) {
        $tbName.Text = $entry.Name
        $tbIp.Text = $entry.Target
        $chk.Checked = $entry.Enabled
        $match = $script:Contacts | Where-Object { $_.Email -eq $entry.Email } | Select-Object -First 1
        $cbEmail.Text = if ($match) { Format-Contact $match } else { $entry.Email }
    }

    $btnOk = New-Button 'Save' 85
    $btnOk.Location = New-Object System.Drawing.Point(254, 172)
    $btnCancel = New-Button 'Cancel' 85
    $btnCancel.Location = New-Object System.Drawing.Point(345, 172)
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $dlg.Controls.AddRange(@($btnOk, $btnCancel))
    $dlg.AcceptButton = $btnOk
    $dlg.CancelButton = $btnCancel

    $btnOk.Add_Click({
        $err = $null
        $email = Resolve-Email $cbEmail.Text
        $target = $tbIp.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($tbName.Text))   { $err = 'Please enter a name.' }
        elseif ([string]::IsNullOrWhiteSpace($target))    { $err = 'Please enter an IP address or hostname.' }
        elseif ($target -match '\s')                      { $err = 'The IP / hostname cannot contain spaces.' }
        elseif (-not (Test-Email $email))                 { $err = 'Please pick a contact or enter a valid email address.' }
        if ($err) { [void][System.Windows.Forms.MessageBox]::Show($dlg, $err, 'ISPinger', 'OK', 'Warning') }
        else      { $dlg.DialogResult = [System.Windows.Forms.DialogResult]::OK }
    })

    $result = $null
    if ($dlg.ShowDialog($script:MainForm) -eq [System.Windows.Forms.DialogResult]::OK) {
        $result = @{
            Name    = $tbName.Text.Trim()
            Target  = $tbIp.Text.Trim()
            Email   = Resolve-Email $cbEmail.Text
            Enabled = $chk.Checked
        }
    }
    $dlg.Dispose()
    return $result
}

# ============================================================== CONTACTS UI
function Update-ContactList {
    $script:lvContacts.BeginUpdate()
    $script:lvContacts.Items.Clear()
    for ($i = 0; $i -lt $script:Contacts.Count; $i++) {
        $c = $script:Contacts[$i]
        $item = New-Object System.Windows.Forms.ListViewItem($c.Name)
        [void]$item.SubItems.Add($c.Email)
        $item.Tag = $i
        [void]$script:lvContacts.Items.Add($item)
    }
    $script:lvContacts.EndUpdate()
}

function Show-ContactDialog($contact) {
    $title = if ($contact) { 'Edit Contact' } else { 'New Contact' }
    $dlg = New-Dialog $title 400 120
    Add-Label $dlg 'Name:' 14 18
    $tbName = Add-TextBox $dlg 80 15 300
    Add-Label $dlg 'Email:' 14 53
    $tbEmail = Add-TextBox $dlg 80 50 300
    if ($contact) { $tbName.Text = $contact.Name; $tbEmail.Text = $contact.Email }

    $btnOk = New-Button 'Save' 85
    $btnOk.Location = New-Object System.Drawing.Point(204, 84)
    $btnCancel = New-Button 'Cancel' 85
    $btnCancel.Location = New-Object System.Drawing.Point(295, 84)
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $dlg.Controls.AddRange(@($btnOk, $btnCancel))
    $dlg.AcceptButton = $btnOk
    $dlg.CancelButton = $btnCancel

    $btnOk.Add_Click({
        $err = $null
        if ([string]::IsNullOrWhiteSpace($tbName.Text))      { $err = 'Please enter a name.' }
        elseif (-not (Test-Email $tbEmail.Text.Trim()))      { $err = 'Please enter a valid email address.' }
        if ($err) { [void][System.Windows.Forms.MessageBox]::Show($dlg, $err, 'ISPinger', 'OK', 'Warning') }
        else      { $dlg.DialogResult = [System.Windows.Forms.DialogResult]::OK }
    })

    $result = $null
    if ($dlg.ShowDialog($script:MainForm) -eq [System.Windows.Forms.DialogResult]::OK) {
        $result = [pscustomobject]@{ Name = $tbName.Text.Trim(); Email = $tbEmail.Text.Trim() }
    }
    $dlg.Dispose()
    return $result
}

# ============================================================== SETTINGS UI
function Update-PasswordLabel {
    $script:lblPassState.Text = if ($script:Settings.PasswordEnc) { '(saved - leave blank to keep)' } else { '(not set)' }
}

function Save-SettingsFromUi {
    $s = $script:Settings
    $s.SmtpHost       = $script:tbHost.Text.Trim()
    $s.SmtpPort       = [int]$script:numPort.Value
    $s.UseSsl         = $script:chkSsl.Checked
    $s.Username       = $script:tbUser.Text.Trim()
    $s.FromAddress    = $script:tbFrom.Text.Trim()
    $s.StartMinimized = $script:chkMin.Checked
    if ($script:tbPass.Text) {
        $pw = $script:tbPass.Text
        if ($s.SmtpHost -like '*gmail*') { $pw = $pw -replace '\s', '' }   # Gmail app passwords are shown with spaces
        $s.PasswordEnc = ConvertTo-SecureString $pw -AsPlainText -Force | ConvertFrom-SecureString
        $script:tbPass.Text = ''
    }
    Save-Data
    Update-PasswordLabel
}

# ============================================================== BUILD MAIN WINDOW
$script:UiFont  = New-Object System.Drawing.Font('Segoe UI', 9)
$script:AppIcon = New-AppIcon

$form = New-Object System.Windows.Forms.Form
$script:MainForm = $form
$form.Text = 'ISPinger'
$form.Size = New-Object System.Drawing.Size(940, 480)
$form.MinimumSize = New-Object System.Drawing.Size(640, 360)
$form.StartPosition = 'CenterScreen'
$form.Font = $script:UiFont
$form.Icon = $script:AppIcon

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$tabEntries  = New-Object System.Windows.Forms.TabPage 'Ping Entries'
$tabContacts = New-Object System.Windows.Forms.TabPage 'Contacts'
$tabSettings = New-Object System.Windows.Forms.TabPage 'Email Settings'
$tabs.TabPages.AddRange(@($tabEntries, $tabContacts, $tabSettings))
$form.Controls.Add($tabs)

# ---- Ping Entries tab
$script:lvEntries = New-ListView @(
    @('Enabled', 65), @('Name', 150), @('IP / Host', 120), @('Email', 180), @('Status', 280), @('Last Checked', 120)
)
$barEntries = New-ButtonBar
$btnAdd = New-Button 'Add'
$script:btnEdit   = New-Button 'Edit'
$script:btnToggle = New-Button 'Enable'
$script:btnRemove = New-Button 'Remove'
$barEntries.Controls.AddRange(@($btnAdd, $script:btnEdit, $script:btnToggle, $script:btnRemove))
$tabEntries.Controls.Add($barEntries)
$tabEntries.Controls.Add($script:lvEntries)
$script:lvEntries.BringToFront()

$btnAdd.Add_Click({
    $r = Show-EntryDialog $null
    if ($r) {
        $e = New-EntryObject $r.Name $r.Target $r.Email $r.Enabled
        $e.Status = if ($r.Enabled) { 'Pinging...' } else { 'Disabled' }
        [void]$script:Entries.Add($e)
        Save-Data
        Update-EntryList
    }
})

$editEntry = {
    $e = Get-SelectedEntry
    if (-not $e) { return }
    $r = Show-EntryDialog $e
    if ($r) {
        $e.Name = $r.Name; $e.Target = $r.Target; $e.Email = $r.Email
        if ($r.Enabled) { Set-EntryEnabled $e $true }     # (re)start pinging with new details
        elseif ($e.Enabled) { Set-EntryEnabled $e $false }
        else { Save-Data; Update-EntryRow $e }
    }
}
$script:btnEdit.Add_Click($editEntry)
$script:lvEntries.Add_DoubleClick($editEntry)

$script:btnToggle.Add_Click({
    $e = Get-SelectedEntry
    if ($e) { Set-EntryEnabled $e (-not $e.Enabled) }
})

$script:btnRemove.Add_Click({
    $e = Get-SelectedEntry
    if (-not $e) { return }
    $ans = [System.Windows.Forms.MessageBox]::Show($form, "Remove '$($e.Name)'?", 'ISPinger', 'YesNo', 'Question')
    if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
        $i = Get-EntryIndex $e.Id
        if ($i -ge 0) { $script:Entries.RemoveAt($i) }
        $script:Runtime.Remove($e.Id)
        Save-Data
        Update-EntryList
    }
})

$script:lvEntries.Add_SelectedIndexChanged({ Update-ToggleButton })

# ---- Contacts tab
$script:lvContacts = New-ListView @(@('Name', 200), @('Email', 300))
$barContacts = New-ButtonBar
$btnCAdd    = New-Button 'Add'
$btnCEdit   = New-Button 'Edit'
$btnCRemove = New-Button 'Remove'
$barContacts.Controls.AddRange(@($btnCAdd, $btnCEdit, $btnCRemove))
$tabContacts.Controls.Add($barContacts)
$tabContacts.Controls.Add($script:lvContacts)
$script:lvContacts.BringToFront()

$btnCAdd.Add_Click({
    $c = Show-ContactDialog $null
    if ($c) { [void]$script:Contacts.Add($c); Save-Data; Update-ContactList }
})

$editContact = {
    if ($script:lvContacts.SelectedItems.Count -eq 0) { return }
    $i = [int]$script:lvContacts.SelectedItems[0].Tag
    $c = Show-ContactDialog $script:Contacts[$i]
    if ($c) { $script:Contacts[$i] = $c; Save-Data; Update-ContactList }
}
$btnCEdit.Add_Click($editContact)
$script:lvContacts.Add_DoubleClick($editContact)

$btnCRemove.Add_Click({
    if ($script:lvContacts.SelectedItems.Count -eq 0) { return }
    $i = [int]$script:lvContacts.SelectedItems[0].Tag
    $ans = [System.Windows.Forms.MessageBox]::Show($form, "Remove contact '$($script:Contacts[$i].Name)'?", 'ISPinger', 'YesNo', 'Question')
    if ($ans -eq [System.Windows.Forms.DialogResult]::Yes) {
        $script:Contacts.RemoveAt($i); Save-Data; Update-ContactList
    }
})

# ---- Settings tab
$tabSettings.AutoScroll = $true
Add-Label $tabSettings 'SMTP server:' 15 18
$script:tbHost = Add-TextBox $tabSettings 150 15 260
Add-Label $tabSettings 'Port:' 15 50
$script:numPort = New-Object System.Windows.Forms.NumericUpDown
$script:numPort.Location = New-Object System.Drawing.Point(150, 47)
$script:numPort.Width = 80
$script:numPort.Minimum = 1
$script:numPort.Maximum = 65535
$tabSettings.Controls.Add($script:numPort)
$script:chkSsl = New-Object System.Windows.Forms.CheckBox
$script:chkSsl.Text = 'Use SSL/TLS'
$script:chkSsl.AutoSize = $true
$script:chkSsl.Location = New-Object System.Drawing.Point(245, 49)
$tabSettings.Controls.Add($script:chkSsl)
Add-Label $tabSettings 'Username:' 15 82
$script:tbUser = Add-TextBox $tabSettings 150 79 260
Add-Label $tabSettings 'Password:' 15 114
$script:tbPass = Add-TextBox $tabSettings 150 111 260
$script:tbPass.UseSystemPasswordChar = $true
$script:lblPassState = New-Object System.Windows.Forms.Label
$script:lblPassState.AutoSize = $true
$script:lblPassState.ForeColor = [System.Drawing.Color]::Gray
$script:lblPassState.Location = New-Object System.Drawing.Point(418, 114)
$tabSettings.Controls.Add($script:lblPassState)
Add-Label $tabSettings 'From address:' 15 146
$script:tbFrom = Add-TextBox $tabSettings 150 143 260
$script:chkMin = New-Object System.Windows.Forms.CheckBox
$script:chkMin.Text = 'Start minimized to the system tray'
$script:chkMin.AutoSize = $true
$script:chkMin.Location = New-Object System.Drawing.Point(150, 176)
$tabSettings.Controls.Add($script:chkMin)

$btnSave = New-Button 'Save' 95
$btnSave.Location = New-Object System.Drawing.Point(150, 208)
$btnTest = New-Button 'Send Test Email' 130
$btnTest.Location = New-Object System.Drawing.Point(252, 208)
$tabSettings.Controls.AddRange(@($btnSave, $btnTest))

$help = New-Object System.Windows.Forms.Label
$help.AutoSize = $false
$help.Size = New-Object System.Drawing.Size(640, 110)
$help.Location = New-Object System.Drawing.Point(15, 252)
$help.ForeColor = [System.Drawing.Color]::DimGray
$help.Text = "Free Gmail setup:`n" +
             "  1. Turn on 2-Step Verification on the Google account.`n" +
             "  2. Create an App Password at myaccount.google.com/apppasswords.`n" +
             "  3. Server smtp.gmail.com, port 587, SSL/TLS on. Username and From = your Gmail address,`n" +
             "      Password = the 16-character app password.`n`n" +
             "Data file: $($script:DataFile)"
$tabSettings.Controls.Add($help)

$btnSave.Add_Click({
    Save-SettingsFromUi
    [void][System.Windows.Forms.MessageBox]::Show($form, 'Settings saved.', 'ISPinger')
})

$btnTest.Add_Click({
    Save-SettingsFromUi
    $to = $script:Settings.FromAddress
    if (-not (Test-Email $to)) {
        [void][System.Windows.Forms.MessageBox]::Show($form, 'Enter a valid From address first - the test email is sent to it.', 'ISPinger', 'OK', 'Warning')
        return
    }
    $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    try {
        $m = New-SmtpMessage $to 'ISPinger test email' "If you're reading this, ISPinger can send email from $env:COMPUTERNAME."
        try { $m.Client.Send($m.Message) } finally { $m.Message.Dispose(); $m.Client.Dispose() }
        [void][System.Windows.Forms.MessageBox]::Show($form, "Test email sent to $to.", 'ISPinger', 'OK', 'Information')
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show($form, "Test failed:`n`n$(Get-ErrorText $_.Exception)", 'ISPinger', 'OK', 'Error')
    } finally {
        $form.Cursor = [System.Windows.Forms.Cursors]::Default
    }
})

# ============================================================== TRAY ICON
$script:Tray = New-Object System.Windows.Forms.NotifyIcon
$script:Tray.Icon = $script:AppIcon
$script:Tray.Text = 'ISPinger'
$trayMenu = New-Object System.Windows.Forms.ContextMenuStrip
$miOpen = New-Object System.Windows.Forms.ToolStripMenuItem 'Open ISPinger'
$miOpen.Font = New-Object System.Drawing.Font($miOpen.Font, [System.Drawing.FontStyle]::Bold)
$miExit = New-Object System.Windows.Forms.ToolStripMenuItem 'Exit'
[void]$trayMenu.Items.Add($miOpen)
[void]$trayMenu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void]$trayMenu.Items.Add($miExit)
$script:Tray.ContextMenuStrip = $trayMenu

$script:ReallyExit = $false
$script:TrayHintShown = $false

function Show-MainWindow {
    $script:MainForm.Show()
    $script:MainForm.WindowState = 'Normal'
    $script:MainForm.Activate()
}

function Hide-ToTray {
    $script:MainForm.Hide()
    if (-not $script:TrayHintShown) {
        Show-Balloon 'ISPinger' 'Still running here. Double-click to open, right-click to exit.'
        $script:TrayHintShown = $true
    }
}

$miOpen.Add_Click({ Show-MainWindow })
$script:Tray.Add_DoubleClick({ Show-MainWindow })
$miExit.Add_Click({
    $script:ReallyExit = $true
    $script:Timer.Stop()
    Save-Data
    $script:Tray.Visible = $false
    $script:Tray.Dispose()
    $script:MainForm.Close()
    [System.Windows.Forms.Application]::Exit()
})

$form.Add_Resize({ if ($form.WindowState -eq 'Minimized') { Hide-ToTray } })
$form.Add_FormClosing({
    param($s, $ev)
    if (-not $script:ReallyExit -and $ev.CloseReason -eq [System.Windows.Forms.CloseReason]::UserClosing) {
        $ev.Cancel = $true      # the X button hides to tray; use tray menu > Exit to quit
        Hide-ToTray
    }
})
$form.Add_FormClosed({ [System.Windows.Forms.Application]::Exit() })

# ============================================================== START
Import-Data

$script:tbHost.Text   = $script:Settings.SmtpHost
$script:numPort.Value = [math]::Min(65535, [math]::Max(1, [int]$script:Settings.SmtpPort))
$script:chkSsl.Checked = [bool]$script:Settings.UseSsl
$script:tbUser.Text   = $script:Settings.Username
$script:tbFrom.Text   = $script:Settings.FromAddress
$script:chkMin.Checked = [bool]$script:Settings.StartMinimized
Update-PasswordLabel

Update-EntryList
Update-ContactList

$script:Timer = New-Object System.Windows.Forms.Timer
$script:Timer.Interval = 1000
$script:Timer.Add_Tick({
    try { Invoke-Tick } catch { Show-Balloon 'ISPinger error' $_.Exception.Message 'Error' }
})
$script:Timer.Start()

$script:Tray.Visible = $true
Update-TrayText

if ($script:Settings.StartMinimized) {
    Show-Balloon 'ISPinger' 'Running in the system tray.'
    $script:TrayHintShown = $true
} else {
    $form.Show()
}

[System.Windows.Forms.Application]::Run()
