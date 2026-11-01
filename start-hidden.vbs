' Start the Service Panel without a console window (double-click friendly).
' If the panel is already running, panel.py just opens the browser and exits.
Option Explicit

Dim sh, fso, dir, py, script

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)

py = dir & "\.venv\Scripts\python.exe"
If Not fso.FileExists(py) Then py = dir & "\venv\Scripts\python.exe"
If Not fso.FileExists(py) Then py = "python.exe"

script = dir & "\panel.py"

If Not fso.FileExists(script) Then
  MsgBox "panel.py not found:" & vbCrLf & script, 16, "Service Panel"
  WScript.Quit 1
End If

sh.CurrentDirectory = dir
sh.Run """" & py & """ -X utf8 """ & script & """", 0, False
