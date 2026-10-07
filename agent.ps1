$forwardedInput = @($input)
if ($forwardedInput.Count -gt 0) {
    $forwardedInput | & (Join-Path $PSScriptRoot 'bin\neoxider.ps1') @args
} else {
    & (Join-Path $PSScriptRoot 'bin\neoxider.ps1') @args
}
exit $LASTEXITCODE
