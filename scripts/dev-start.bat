@echo off
rem ============================================================
rem  XiaoZhu (DEV build) - runs backend(18771) + Electron frontend
rem  Development data lives in D:\xiaob\planner\data (isolated
rem  from the release build at planner-release).
rem ============================================================
title XiaoZhu - DEV (18771)
cd /d "D:\xiaob\planner\frontend"
npm start
