@echo off
setlocal EnableDelayedExpansion

:: Forca o diretorio de trabalho como a pasta do projeto
:: (funciona mesmo se o atalho for executado de outro lugar)
cd /d "%~dp0"

echo.
echo ========================================
echo   CTF Orchestrator - Servidor Local
echo ========================================
echo.

:: Localiza o Python
set "PYTHON_CMD=python"
where python >nul 2>&1 || set "PYTHON_CMD=python3"
%PYTHON_CMD% --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERRO: Python nao encontrado no PATH.
    echo Instale o Python em https://www.python.org/ e tente novamente.
    echo.
    pause
    exit /b 1
)

:: Verifica se as dependencias estao instaladas
echo [1/2] Verificando dependencias...
%PYTHON_CMD% -c "import uvicorn, fastapi" >nul 2>&1
if %errorlevel% neq 0 (
    echo    AVISO: uvicorn/fastapi nao estao instalados.
    echo    Rode: %PYTHON_CMD% -m pip install -e .
    echo    ou:   %PYTHON_CMD% -m pip install fastapi uvicorn[standard] sqlmodel jinja2 python-multipart pydantic pyyaml
    echo.
    pause
    exit /b 1
)

:: Inicia o servidor local
if "%KOTH_PORT%"=="" set "KOTH_PORT=8777"
echo [2/2] Iniciando uvicorn em http://127.0.0.1:%KOTH_PORT%
echo    Abra seu navegador nesse endereco.
echo    Pressione CTRL+C para parar o servidor.
echo.

%PYTHON_CMD% -m uvicorn app.main:app --host 127.0.0.1 --port %KOTH_PORT%

endlocal
