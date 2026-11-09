#!/bin/bash
# Get process information

echo "=== Top CPU Consuming Processes ==="
ps aux --sort=-%cpu | head -11

echo ""
echo "=== Top Memory Consuming Processes ==="
ps aux --sort=-%mem | head -11
