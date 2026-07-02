' Windowless launcher for the GAIA pipeline (scheduled-task action).
' Runs the pipeline with NO console window (WshShell.Run style 0 = hidden).
Dim sh : Set sh = CreateObject("WScript.Shell")
Dim fso : Set fso = CreateObject("Scripting.FileSystemObject")
' repo root = two levels up from this script (bench\gaia\run_pipeline_hidden.vbs)
Dim scriptDir : scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
sh.Run "cmd /c set PYTHONIOENCODING=utf-8 && .venv\Scripts\python.exe -u bench\gaia\run_pipeline.py >> .fleet\gaia\pipeline_task.log 2>&1", 0, False
