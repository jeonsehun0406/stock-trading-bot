@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 자동매매 대시보드를 여는 중입니다...
echo (이 창을 닫으면 대시보드 접속이 끊깁니다)
start "" http://127.0.0.1:8000/dashboard.html
python -m http.server 8000
