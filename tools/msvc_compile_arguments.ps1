function New-MsvcHarnessCompileArguments {
  param(
    [Parameter(Mandatory = $true)][string]$IncludeDirectory,
    [Parameter(Mandatory = $true)][string[]]$SourceFiles,
    [Parameter(Mandatory = $true)][string]$ObjectDirectory,
    [Parameter(Mandatory = $true)][string]$ExecutablePath
  )

  $arguments = @('/nologo', '/std:c++14', '/W4', ('/I' + $IncludeDirectory))
  foreach ($source in $SourceFiles) { $arguments += $source }
  $arguments += ('/Fo' + ($ObjectDirectory.TrimEnd('\') + '\'))
  $arguments += ('/Fe' + $ExecutablePath)
  return $arguments
}
