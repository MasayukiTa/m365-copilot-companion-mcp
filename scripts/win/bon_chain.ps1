# Wait for the best-of-N samples, grade them, and run the selector -- without a person in the
# loop. The measurement is only finished when the selector has been given candidates that
# differ in CORRECTNESS, and none of the three attempts so far produced that: the effort arms
# agreed outright, and the easy population had every sample correct.
[CmdletBinding()]
param(
    [string]$Dir = ".fleet/swe/bon_hard",
    [int]$Samples = 3,
    [string]$Log = ".fleet/swe/bon_chain.log",
    [int]$CheckSec = 60,
    [int]$MaxHours = 6
)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
$py = ".\.venv\Scripts\python.exe"

function Say([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m
    Write-Output $line
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Say ("waiting for {0} samples in {1}" -f $Samples, $Dir)
$deadline = (Get-Date).AddHours($MaxHours)
while ((Get-Date) -lt $deadline) {
    $done = @(Get-ChildItem "$Dir/preds_*.json" -EA SilentlyContinue |
              Where-Object { $_.Length -gt 100 }).Count
    if ($done -ge $Samples) { Say ("{0} samples present" -f $done); break }
    Start-Sleep -Seconds $CheckSec
}
$done = @(Get-ChildItem "$Dir/preds_*.json" -EA SilentlyContinue | Where-Object { $_.Length -gt 100 }).Count
if ($done -lt 1) { Say "no samples were produced; nothing to grade"; exit 1 }
Say ("grading {0} sample(s)" -f $done)

# Shape the captures for the grader and ship them with the raw rows for these instances.
& $py -c @"
import io, json, os, glob
d = r'$Dir'
ids = [l.strip() for l in io.open(os.path.join(d, 'bon_ids.txt'), encoding='utf-8-sig') if l.strip()]
s50 = json.load(io.open('.fleet/swe/pro_slice50_full.json', encoding='utf-8'))
by = {r['instance_id']: r for r in s50}
with io.open(os.path.join(d, 'bon_raw.jsonl'), 'w', encoding='utf-8', newline='\n') as f:
    for i in ids:
        if i in by:
            f.write(json.dumps(by[i], ensure_ascii=False) + '\n')
n = 0
for p in sorted(glob.glob(os.path.join(d, 'preds_*.json'))):
    k = os.path.basename(p).replace('preds_', '').replace('.json', '')
    o = json.load(io.open(p, encoding='utf-8-sig'))
    rows = o.get('predictions') if isinstance(o, dict) else o
    if not rows:
        continue
    out = [{'instance_id': r['instance_id'], 'patch': r['patch'], 'prefix': 'companion'} for r in rows]
    io.open(os.path.join(d, 'grade_preds_%s.json' % k), 'w', encoding='utf-8', newline='\n').write(
        json.dumps(out, ensure_ascii=False))
    n += 1
print('prepared %d sample(s), %d instances' % (n, len(ids)))
"@ 2>&1 | ForEach-Object { Say $_ }

& ssh -o BatchMode=yes EVAL_HOST "powershell -NoProfile -Command `"New-Item -ItemType Directory -Force C:\swe-grade\bonhard | Out-Null`"" 2>&1 | Out-Null
& scp -o BatchMode=yes "$Dir/bon_raw.jsonl" "EVAL_HOST:C:/swe-grade/bonhard/" 2>&1 | Out-Null
Get-ChildItem "$Dir/grade_preds_*.json" | ForEach-Object {
    & scp -o BatchMode=yes $_.FullName "EVAL_HOST:C:/swe-grade/bonhard/" 2>&1 | Out-Null
}
& $py -c "import io; s=io.open('bench/remote/bon_grade.sh',encoding='utf-8').read().replace('/mnt/c/swe-grade/bon','/mnt/c/swe-grade/bonhard'); io.open('.fleet/swe/bonhard_grade.sh','w',encoding='utf-8',newline='\n').write(s)"
& scp -o BatchMode=yes ".fleet/swe/bonhard_grade.sh" "EVAL_HOST:C:/swe-grade/bonhard/bon_grade.sh" 2>&1 | Out-Null
Say "launching the grader on the eval host"
& ssh -o BatchMode=yes EVAL_HOST "powershell -NoProfile -Command `"schtasks /Delete /TN BonHardGrade /F 2>`$null | Out-Null; schtasks /Create /TN BonHardGrade /TR ('\`"C:\Windows\System32\wsl.exe\`" -d Ubuntu -e /bin/bash /mnt/c/swe-grade/bonhard/bon_grade.sh') /SC ONCE /ST 23:56 /RL HIGHEST /F | Out-Null; schtasks /Run /TN BonHardGrade`"" 2>&1 | ForEach-Object { Say ("remote: " + $_) }

Say "waiting for the verdicts"
$deadline2 = (Get-Date).AddHours(2)
while ((Get-Date) -lt $deadline2) {
    $probe = & ssh -o BatchMode=yes EVAL_HOST "if exist `"C:\swe-grade\bonhard\bon_grade.out`" (findstr /C:`"DONE_BON_GRADE`" `"C:\swe-grade\bonhard\bon_grade.out`") else (echo waiting)" 2>&1
    if ($probe -match "DONE_BON_GRADE") { Say "grading finished"; break }
    Start-Sleep -Seconds 60
}
Get-ChildItem "$Dir/grade_preds_*.json" | ForEach-Object {
    $k = $_.BaseName -replace 'grade_preds_', ''
    & scp -o BatchMode=yes ("EVAL_HOST:C:/swe-grade/bonhard/out_$k/eval_results.json") "$Dir/verdict_$k.json" 2>&1 | Out-Null
}
Say "verdicts fetched; running the selector"
& $py bench/bestofn_on_samples.py --dir $Dir 2>&1 | ForEach-Object { Say $_ }
Say "bon chain complete"
