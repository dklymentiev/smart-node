#!/bin/bash
# Comprehensive system diagnostics for server issues
# Run with: /opt/smart-node/brin-client-tg/tools/diagnose-system.sh [target]

TARGET="${1:-general}"

case "$TARGET" in
    fail2ban|fail2ban-cpu)
        echo "=== FAIL2BAN DIAGNOSTICS ==="
        echo ""
        echo "[1] PROCESS STATUS"
        ps aux | grep fail2ban-server | grep -v grep | awk '{print "PID: " $2 " | CPU: " $3 "% | Memory: " $6 "K | Command: " $11 " " $12}'
        echo ""
        echo "[2] ACTIVE JAILS"
        sudo /usr/bin/fail2ban-client status 2>/dev/null | grep -E "Number of jail|Jail list"
        echo ""
        echo "[3] SSHD JAIL"
        sudo /usr/bin/fail2ban-client status sshd 2>/dev/null | tail -8
        echo ""
        echo "[4] POSTFIX-DOCKER JAIL"
        sudo /usr/bin/fail2ban-client status postfix-docker 2>/dev/null | head -6
        echo ""
        echo "[5] SYSTEMD SERVICE"
        systemctl status fail2ban --no-pager 2>/dev/null | head -8
        echo ""
        echo "[6] RECENT SERVICE LOGS"
        sudo journalctl -u fail2ban -n 10 --no-pager 2>/dev/null | tail -5
        ;;

    docker)
        echo "=== DOCKER DIAGNOSTICS ==="
        echo ""
        echo "[1] DOCKER DAEMON"
        ps aux | grep dockerd | grep -v grep | awk '{print "PID: " $2 " | CPU: " $3 "% | Memory: " $6 "K"}'
        echo ""
        echo "[2] CONTAINER COUNT"
        sudo docker ps -a 2>/dev/null | wc -l
        echo ""
        echo "[3] TOP CONTAINERS BY MEMORY"
        sudo docker stats --no-stream 2>/dev/null | head -6
        echo ""
        echo "[4] DISK USAGE"
        sudo docker system df 2>/dev/null
        ;;

    cpu)
        echo "=== CPU DIAGNOSTICS ==="
        echo ""
        echo "[1] SYSTEM LOAD"
        uptime
        echo ""
        echo "[2] TOP 5 PROCESSES BY CPU"
        ps aux --sort=-%cpu | head -6
        echo ""
        echo "[3] CONTEXT SWITCHES"
        vmstat 1 2 | tail -1
        ;;

    memory)
        echo "=== MEMORY DIAGNOSTICS ==="
        echo ""
        echo "[1] MEMORY STATUS"
        free -h
        echo ""
        echo "[2] TOP 5 PROCESSES BY MEMORY"
        ps aux --sort=-%mem | head -6
        echo ""
        echo "[3] SWAP USAGE"
        swapon -s 2>/dev/null || echo "No swap configured"
        ;;

    disk)
        echo "=== DISK DIAGNOSTICS ==="
        echo ""
        echo "[1] DISK SPACE"
        df -h | grep -E "^/dev|^Filesystem"
        echo ""
        echo "[2] INODE USAGE"
        df -i | grep -E "^/dev|^Filesystem"
        echo ""
        echo "[3] LARGEST DIRECTORIES"
        du -sh /* 2>/dev/null | sort -rh | head -10
        ;;

    *)
        echo "=== GENERAL SYSTEM DIAGNOSTICS ==="
        echo ""
        echo "[1] SYSTEM UPTIME & LOAD"
        uptime
        echo ""
        echo "[2] TOP 3 PROCESSES BY CPU"
        ps aux --sort=-%cpu | head -4 | tail -3
        echo ""
        echo "[3] MEMORY USAGE"
        free -h | head -2
        echo ""
        echo "[4] DISK USAGE"
        df -h / | tail -1
        echo ""
        echo "[5] ACTIVE DOCKER CONTAINERS"
        sudo docker ps 2>/dev/null | wc -l
        echo ""
        echo "[6] FAIL2BAN STATUS"
        sudo /usr/bin/fail2ban-client status 2>/dev/null | grep "Jail list"
        ;;
esac
