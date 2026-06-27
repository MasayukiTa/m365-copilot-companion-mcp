' Windowless launcher for the GAIA pipeline (scheduled-task action).
' Runs the pipeline with NO console window (WshShell.Run style 0 = hidden).
Dim sh : Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\USER\companion-mcp"
sh.Run "cmd /c set PYTHONIOENCODING=utf-8 && .venv\Scripts\python.exe -u bench\gaia\run_pipeline.py >> .fleet\gaia\pipeline_task.log 2>&1", 0, False
