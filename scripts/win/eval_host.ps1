# Where the eval host is, WITHOUT WRITING ITS NAME IN A TRACKED FILE.
#
# This repository is public, and the SSH alias for the eval host is one of the identifiers that
# must never appear in its content. Five scripts had it hardcoded -- ssh and scp lines written
# while chaining runs to grading -- and the shaped checker in scripts/check_no_identifying_names.py
# only catches it when IDENTITY_NAMES is configured, so it passed unconfigured every time.
#
# The name now lives in the environment, or in .env, both of which are outside the repository.
# There is no default: a default IS the name, written down.
#
# Dot-source this and use $EvalHost:
#     . "$PSScriptRoot\eval_host.ps1"
#     & ssh -o BatchMode=yes $EvalHost "echo up"

function Get-EvalHost {
    if ($env:SWE_EVAL_HOST) { return $env:SWE_EVAL_HOST }

    # .env is gitignored, so it is a legitimate place for it. Read without the shell: a value
    # with an '=' in it must survive, and Split() on every '=' would truncate it.
    $repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $envFile = Join-Path $repo ".env"
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
            $t = $line.Trim()
            if ($t.StartsWith("#") -or -not $t.Contains("=")) { continue }
            $i = $t.IndexOf("=")
            if ($t.Substring(0, $i).Trim() -eq "SWE_EVAL_HOST") {
                return $t.Substring($i + 1).Trim().Trim('"')
            }
        }
    }

    throw ("SWE_EVAL_HOST is not set. This repository is public and the eval host's name is " +
           "not written in it, so the name has to come from the environment or from .env " +
           "(which is gitignored). Set SWE_EVAL_HOST to the ssh alias for the eval host.")
}

$EvalHost = Get-EvalHost
