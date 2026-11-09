@echo off
rem Windows Startup helper for WSL installs: starts WSL so Docker and the court-search
rem user service come up after login. Edit the distro, user and path, then place a
rem shortcut to this file in shell:startup.
wsl.exe -d Ubuntu-24.04 -u YOUR_WSL_USER -- /home/YOUR_WSL_USER/edrsr-search/deployment/start-on-boot.sh
