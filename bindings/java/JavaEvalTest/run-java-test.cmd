@echo off
setlocal enableextensions enabledelayedexpansion
SET SOL_DIR=%~dpf1
SET BUILD_PATH=%~dpf2

SET PATH=%PATH%;%BUILD_PATH%

pushd "%SOL_DIR%\bindings\java\JavaEvalTest"
"%JAVA_HOME%\bin\javac" -cp "%SOL_DIR%\bindings\java\Swig\cntk.jar" src\Main.java || (
  echo "Java Compilation Failed"
  popd
  EXIT /B 1
)
"%JAVA_HOME%\bin\java" -Djava.library.path=%BUILD_PATH% -classpath "%SOL_DIR%\bindings\java\JavaEvalTest\src;%SOL_DIR%\bindings\java\Swig\cntk.jar" Main || (
  echo "Running Java Failed"
  popd
  EXIT /B 1
)
popd
