$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'msvc_compile_arguments.ps1')

$objectDirectory = 'C:\test output\harness objects'
$executablePath = 'C:\test output\harness.exe'
$actual = @(New-MsvcHarnessCompileArguments `
  -IncludeDirectory 'C:\project with spaces\include' `
  -SourceFiles @('C:\project with spaces\test.cpp', 'C:\project with spaces\support.cpp') `
  -ObjectDirectory $objectDirectory `
  -ExecutablePath $executablePath)

if ($actual.Count -ne 8) {
  throw "MSVC compile argument regression: expected 8 distinct arguments, got $($actual.Count). Last argument was '$($actual[-1])'."
}

$expectedObject = '/Fo' + $objectDirectory.TrimEnd('\') + '\'
$expectedExecutable = '/Fe' + $executablePath
if ($actual[-2] -cne $expectedObject) {
  throw "MSVC compile argument regression: expected separate object output '$expectedObject', got '$($actual[-2])'."
}
if ($actual[-1] -cne $expectedExecutable) {
  throw "MSVC compile argument regression: expected separate executable output '$expectedExecutable', got '$($actual[-1])'."
}

Write-Output 'PASS: /Fo and /Fe remain separate MSVC arguments with paths containing spaces.'
