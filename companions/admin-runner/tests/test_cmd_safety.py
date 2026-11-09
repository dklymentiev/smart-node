from cmd_safety import is_safe_command


def test_safe_reads():
    for c in ["ps aux --sort=-%cpu | head -n 15", "df -h", "free -m",
              "journalctl -u smart-node-gateway -n 50", "systemctl status nginx",
              "docker ps", "ss -ltnp", "cat /proc/loadavg", "uptime"]:
        assert is_safe_command(c) is True, c


def test_destructive_blocked():
    for c in ["rm -rf /tmp/x", "systemctl restart nginx", "echo x > /etc/hosts",
              "dd if=/dev/zero of=/dev/sda", "kill -9 1", "docker rm -f web",
              "journalctl --vacuum-time=1d", "ip link set eth0 down",
              "ps aux; rm -rf /", "cat /etc/passwd && reboot"]:
        assert is_safe_command(c) is False, c
