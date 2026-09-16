' Launch start_server.bat with no visible console window.
'
' A copy of this file lives in the Startup folder so the app comes back after a
' reboot. Without it, "tailscale serve" keeps proxying to port 8000 after a
' restart while nothing is listening there, and the app answers 502.
'
' Run 0 = hidden, False = do not wait. Remove the shortcut from
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
' to stop it starting automatically.

Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\start_server.bat""", 0, False
