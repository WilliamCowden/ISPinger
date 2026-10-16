' Starts ISPinger with no console window.
' Put a shortcut to this file in shell:startup to launch it at login.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptDir & "\ISPinger.ps1""", 0, False
