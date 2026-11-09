#!/bin/bash
# Get system information

echo "=== System Information ==="
uname -a

echo ""
echo "=== Uptime ==="
uptime

echo ""
echo "=== CPU Info ==="
nproc
echo "CPUs available: $(nproc)"

echo ""
echo "=== Current Load ==="
cat /proc/loadavg

echo ""
echo "=== System Time ==="
date
