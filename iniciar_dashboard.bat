@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo Actualizando el modelo de datos...
python -m sisap.cli construir-dwh

echo.
echo Iniciando el dashboard... se abrira solo en tu navegador.
streamlit run src\sisap\dashboard.py

pause
