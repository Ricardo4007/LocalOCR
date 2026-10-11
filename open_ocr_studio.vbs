Option Explicit

Dim fileSystem, shell, projectRoot, localPython, gpuPython, basePython, commandLine
Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
gpuPython = fileSystem.BuildPath(projectRoot, ".venv-train\Scripts\python.exe")
basePython = fileSystem.BuildPath(projectRoot, ".venv\Scripts\python.exe")
shell.CurrentDirectory = projectRoot

If fileSystem.FileExists(gpuPython) Then
    localPython = gpuPython
ElseIf fileSystem.FileExists(basePython) Then
    localPython = basePython
End If

If Len(localPython) > 0 Then
    commandLine = Chr(34) & localPython & Chr(34) & " " & Chr(34) & "scripts\portable_start.py" & Chr(34)
    On Error Resume Next
    shell.Run commandLine, 0, False
    If Err.Number <> 0 Then
        MsgBox "Could not start Local OCR: " & Err.Description, 16, "Local OCR"
    End If
    On Error GoTo 0
Else
    commandLine = "cmd.exe /d /c uv run python scripts\portable_start.py"
    On Error Resume Next
    shell.Run commandLine, 0, False
    If Err.Number <> 0 Then
        MsgBox "Could not start Local OCR. Please install uv and add it to PATH." & vbCrLf & Err.Description, 16, "Local OCR"
    End If
    On Error GoTo 0
End If
